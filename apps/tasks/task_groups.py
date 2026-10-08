"""
Task groups configuration for organized task management.

This module defines task groups with feature enabled controls and provides
a centralized way to manage different categories of tasks.

Feature enabled settings are stored in the database using the Setting model, allowing
runtime configuration without code changes. Values resolve in order: Setting row,
then settings.FEATURE when the key is present (including env overrides via METRICS_SERVICE_FEATURE__*),
then the direct top-level settings attribute FEATURE_<name>_ENABLED (set by the installer
via settings.yaml), then DAB AAPFlag FEATURE_<name>_ENABLED, then the function default.

run `manage.py metrics_service init-system-tasks` to update the DB from `TASK_GROUPS`
"""

import json
import logging
from typing import Any

from django.conf import settings

SEGMENT_MAX_ATTEMPTS = 7  # Extended window for Segment transmission (~10.5h with exponential backoff)

logger = logging.getLogger(__name__)


def get_feature_enabled_from_db(setting_name: str, default: bool = False) -> bool:
    """
    Get a feature enabled value from database settings.

    Order: ``Setting`` row → ``FEATURE[setting_name]`` if that key exists
    (Dynaconf merges ``METRICS_SERVICE_FEATURE__*``) → top-level
    ``FEATURE_<setting_name>_ENABLED`` settings attribute (set directly in settings.yaml
    by the installer) → boolean ``AAPFlag`` ``FEATURE_<setting_name>_ENABLED`` → ``default``.

    Feature keys omitted from ``FEATURE`` in defaults use the direct-attribute /
    AAPFlag / default path so platform toggles work without a duplicate static default.

    Args:
        setting_name: Name of the feature enabled setting
        default: Default value if not found in database

    Returns:
        bool: Feature enabled value from database or default
    """
    try:
        # Avoid circular import by importing here
        from apps.dynamic_settings.models import Setting

        setting = Setting.objects.filter(setting_key=setting_name).first()
        if setting and setting.current_value:
            try:
                value = json.loads(setting.current_value)
            except (json.JSONDecodeError, ValueError):
                # If not valid JSON, treat as string boolean
                value = setting.current_value.lower() in ("true", "1", "yes", "on")
            return bool(value)

        feature_enabled = getattr(settings, "FEATURE", {})
        if setting_name in feature_enabled:
            return bool(feature_enabled[setting_name])

        # Direct top-level attribute set by the installer (e.g. FEATURE_DASHBOARD_COLLECTION_ENABLED: True
        # in settings.yaml). Checked before AAPFlag so installer opt-in overrides the seeded default.
        direct_attr = f"FEATURE_{setting_name}_ENABLED"
        direct_value = getattr(settings, direct_attr, None)
        if direct_value is not None:
            return bool(direct_value)

        # Platform default from DAB (YAML-seeded), when not overridden above
        try:
            from ansible_base.feature_flags.models import AAPFlag

            flag = AAPFlag.objects.filter(name=direct_attr, condition="boolean").first()
            if flag is not None:
                return flag.value.lower() in ("true", "1", "yes", "on")
        except Exception as e:
            logger.warning(f"Error reading feature enabled setting {setting_name} from AAPFlag: {e}")

        return default

    except Exception as e:
        logger.warning(f"Error reading feature enabled setting {setting_name} from database: {e}")
        feature_enabled = getattr(settings, "FEATURE", {})
        return bool(feature_enabled[setting_name]) if setting_name in feature_enabled else default


class TaskGroup:
    """
    Represents a group of related tasks with optional feature flag control.

    Task groups can have a feature_flag that controls all tasks in the group.
    Individual tasks can be disabled via the 'enabled' field regardless of the group's feature flag.
    """

    def __init__(
        self,
        name: str,
        description: str,
        tasks: list[dict[str, Any]] = None,
        feature_flag: str | None = None,
    ):
        """
        Initialize a task group.

        Args:
            name: Unique name for the task group
            description: Human-readable description
            tasks: List of task configurations in this group
            feature_flag: Optional feature flag name that controls this entire group
        """
        self.name = name
        self.description = description
        self.tasks = tasks or []
        self.feature_flag = feature_flag

    def get_enabled_tasks(self) -> list[dict[str, Any]]:
        """
        Get all tasks in this group that should be active.

        First checks the group-level feature flag (if present).
        Then filters out tasks with enabled=False.

        Feature flag defaults are defined in Django settings (FEATURE dict).

        Returns:
            List of task configurations that are enabled
        """
        # Check group-level feature flag first
        if self.feature_flag and not get_feature_enabled_from_db(self.feature_flag):
            return []

        # Filter tasks by their individual enabled field
        enabled_tasks = [task for task in self.tasks if task.get("enabled", True)]

        return enabled_tasks


# System Tasks Group - Always enabled, core system maintenance
SYSTEM_TASKS_GROUP = TaskGroup(
    name="system_tasks",
    description="Core system maintenance tasks that are always enabled",
    tasks=[
        {
            "task_id": "daily_task_cleanup",
            "function": "cleanup_old_tasks",
            "cron": "0 5 * * *",  # Daily at 5 AM
            "args": {
                "days_old": 5,
                "dry_run": False,
                "include_executions": True,
                "preserve_recurring": True,
            },
            "enabled": True,
            "description": "Daily cleanup of old completed/failed tasks (preserves recurring tasks)",
        },
        {
            "task_id": "hourly_health_check",
            "function": "hello_world",
            "cron": "0 * * * *",  # Every hour
            "args": {},
            "enabled": True,
            "description": "Hourly system health check",
        },
        {
            "task_id": "initial_resource_sync",
            "function": "sync_resources_from_gateway",
            "cron": None,  # Run once for each installed service/DAB build
            "args": {
                # Short base delay so retries align with the gateway's 60s service-id
                # populate cooldown.  Backoff: 60s → 120s → 240s → 480s.
                "retry_delay_seconds": 60,
            },
            "max_attempts": 5,
            "enabled": True,
            "description": "Reconcile users, orgs, teams and RBAC role assignments from Gateway after install or upgrade",
        },
    ],
)

# Metrics Collection Group - Feature flag: METRICS_COLLECTION (default: True).
# Hourly/daily collectors, rollup, and metrics DB cleanup are gated independently of
# ANONYMIZED_DATA_COLLECTION (upstream anonymization/Segment). Disabling this stops
# local scheduled collection; re-enable and run collectors to backfill if needed.
METRICS_COLLECTION_GROUP = TaskGroup(
    name="metrics_collection",
    description="Metrics collection, rollup, and cleanup (METRICS_COLLECTION feature flag)",
    feature_flag="METRICS_COLLECTION",
    tasks=[
        # Hourly Collection Tasks
        {
            # ORDERING: hourly_unified_jobs must fire before hourly_job_host_summary.
            # The unified_jobs hook dispatches sync_dashboard_job_records which writes JobData rows.
            # The job_host_summary hook dispatches sync_dashboard_host_summaries which looks up those
            # rows by job_id. If host summaries run first, no matching JobData rows exist yet and all
            # host summary records are silently skipped with no retry. The 5-minute gap (XX:05 vs XX:10)
            # is sufficient because sync_dashboard_job_records completes in milliseconds to seconds.
            "task_id": "hourly_unified_jobs",
            "function": "collect_hourly_metrics",
            "cron": "5 * * * *",  # Every hour at XX:05
            "args": {"collector_type": "unified_jobs"},
            "enabled": True,
            "description": "Collect unified jobs metrics every hour",
        },
        {
            "task_id": "hourly_job_host_summary",
            "function": "collect_hourly_metrics",
            "cron": "10 * * * *",  # Every hour at XX:10 — after hourly_unified_jobs so JobData rows exist
            "args": {"collector_type": "job_host_summary_service"},
            "enabled": True,
            "description": "Collect job host summary metrics every hour (service variant)",
        },
        {
            "task_id": "hourly_credentials",
            "function": "collect_hourly_metrics",
            "cron": "15 * * * *",  # Every hour at XX:15
            "args": {"collector_type": "credentials_service"},
            "enabled": True,
            "description": "Collect credentials metrics every hour",
        },
        {
            "task_id": "hourly_job_events",
            "function": "collect_hourly_metrics",
            "cron": "20 * * * *",  # Every hour at XX:20
            "args": {"collector_type": "main_jobevent_service"},
            "enabled": True,
            "description": "Collect job events (event modules) metrics every hour",
        },
        # Daily Snapshot Collection
        {
            "task_id": "daily_execution_environments",
            "function": "collect_snapshot_metrics",
            "cron": "0 1 * * *",  # Daily at 1:00 AM
            "args": {"collector_type": "execution_environments"},
            "enabled": True,
            "description": "Collect execution environments snapshot daily",
        },
        {
            "task_id": "daily_config",
            "function": "collect_snapshot_metrics",
            "cron": "30 1 * * *",  # Daily at 1:30 AM
            "args": {"collector_type": "config"},
            "enabled": True,
            "description": "Collect system configuration snapshot daily",
        },
        {
            "task_id": "daily_controller_version",
            "function": "collect_snapshot_metrics",
            "cron": "35 1 * * *",  # Daily at 1:35 AM
            "args": {"collector_type": "controller_version_service"},
            "enabled": True,
            "description": "Collect controller version snapshot daily",
        },
        {
            "task_id": "daily_table_metadata",
            "function": "collect_snapshot_metrics",
            "cron": "40 1 * * *",  # Daily at 1:40 AM
            "args": {"collector_type": "table_metadata"},
            "enabled": True,
            "description": "Collect table metadata snapshot daily",
        },
        {
            "task_id": "daily_feature_flags",
            "function": "collect_snapshot_metrics",
            "cron": "45 1 * * *",  # Daily at 1:45 AM
            "args": {"collector_type": "feature_flags_service"},
            "enabled": True,
            "description": "Collect feature flags snapshot daily",
        },
        {
            "task_id": "daily_task_executions",
            "function": "collect_daily_metrics",
            "cron": "50 1 * * *",  # Daily at 1:50 AM — after snapshots (1:45 AM), before rollup (2:00 AM)
            "args": {"collector_type": "task_executions_service"},
            "enabled": True,
            "description": "Collect task execution observability metrics for the previous day (pipeline health)",
        },
        # Daily Rollup
        {
            "task_id": "daily_metrics_rollup",
            "function": "daily_metrics_rollup",
            "cron": "0 2 * * *",  # Daily at 2:00 AM
            "args": {},
            "enabled": True,
            "description": "Create daily rollup from hourly collections",
        },
        # Cleanup Task
        {
            "task_id": "cleanup_metrics_data",
            "function": "cleanup_metrics_data",
            "cron": "0 4 * * *",  # Daily at 4:00 AM
            "args": {
                "hourly_retention_days": 7,
                "daily_retention_days": 30,
                "payload_retention_days": 7,
                # Analytics retention is resolved from the Controller cleanup_jobs schedule at run time.
            },
            "enabled": True,
            "description": "Clean up old metrics data based on retention policies",
        },
    ],
)

# Anonymization Group - Controlled by ANONYMIZED_DATA_COLLECTION.
# Disabling the flag stops anonymization / upstream transmission while METRICS_COLLECTION
# can remain on for local collectors (no data gap when re-enabling anonymization).
ANONYMIZATION_GROUP = TaskGroup(
    name="anonymization",
    description="Anonymization and transmission of metrics to Red Hat (opt-out via ANONYMIZED_DATA_COLLECTION)",
    feature_flag="ANONYMIZED_DATA_COLLECTION",
    tasks=[
        {
            "task_id": "daily_anonymize",
            "function": "daily_anonymize_and_prepare",
            "cron": "0 3 * * *",  # Daily at 3:00 AM
            "args": {},
            "max_attempts": SEGMENT_MAX_ATTEMPTS,
            "enabled": True,
            "description": "Anonymize daily summary for Segment transmission",
        },
    ],
)

# Dashboard Collection Group - automation-reports integration
# Feature flag: DASHBOARD_COLLECTION (default: True — enabled by default)
# Disable via METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=false or dynamic_settings.Setting.
DASHBOARD_COLLECTION_GROUP = TaskGroup(
    name="dashboard_collection",
    description="Automation-reports dashboard data collection (SQL-based, separate from anonymization)",
    feature_flag="DASHBOARD_COLLECTION",
    tasks=[
        {
            "task_id": "initial_dashboard_collection",
            "function": "collect_dashboard_reports_initial_data",
            "cron": None,  # No schedule, run once on enable
            "args": {},
            "enabled": True,
            "description": "Initial dashboard report collection (backfill; window controlled by the Controller's cleanup_jobs retention schedule, default 90 days)",
        },
        {
            "task_id": "backfill_dashboard_organization_names",
            "function": "backfill_dashboard_organization_names",
            "cron": None,  # No schedule, run once after install/upgrade
            "args": {},
            "enabled": True,
            "description": "One-time fix of JobData organization_name left NULL by earlier initial collections",
        },
        {
            "task_id": "cleanup_dashboard_reports_old_data",
            "function": "cleanup_dashboard_reports_old_data",
            "cron": "30 5 * * *",  # Daily at 5:30 AM
            "args": {},  # retention_days defaults to the Controller's cleanup_jobs retention schedule
            "enabled": True,
            "description": "Clean up old dashboard report data based on retention policy",
        },
        {
            "task_id": "cleanup_dashboard_telemetry",
            "function": "cleanup_dashboard_telemetry",
            "cron": "45 5 * * *",  # Daily at 5:45 AM
            "args": {},  # retention_days defaults to 60
            "enabled": True,
            "description": "Delete DashboardTelemetry rows older than 60 days to prevent unbounded table growth",
        },
    ],
)

# Indirect Node Collection Group - automation-reports integration
# Feature flag: INDIRECT_NODE_COLLECTION (default: True; enabled by default)
# Disable with METRICS_SERVICE_FEATURE__INDIRECT_NODE_COLLECTION=false or dynamic_settings.Setting.
INDIRECT_NODE_COLLECTION_GROUP = TaskGroup(
    name="indirect_node_collection",
    description="Indirect managed node daily collection (INDIRECT_NODE_COLLECTION feature flag)",
    feature_flag="INDIRECT_NODE_COLLECTION",
    tasks=[
        {
            "task_id": "daily_collect_indirect_nodes",
            "function": "collect_daily_metrics",
            "cron": "55 1 * * *",  # Daily at 1:55 AM
            "args": {"collector_type": "indirect_managed_nodes"},
            "enabled": True,
            "description": "Collect indirect managed node audit data daily",
        },
    ],
)

# Registry of all task groups
TASK_GROUPS = [
    SYSTEM_TASKS_GROUP,
    METRICS_COLLECTION_GROUP,
    ANONYMIZATION_GROUP,
    DASHBOARD_COLLECTION_GROUP,
    INDIRECT_NODE_COLLECTION_GROUP,
]


def get_all_enabled_tasks() -> dict[str, dict[str, Any]]:
    """
    Get all enabled tasks from all groups.

    Group-level feature flags are evaluated at call, so tasks whose group flag
    is disabled are excluded. Use get_all_tasks_for_init() when you need
    all tasks unconditionally.

    Returns:
        Dictionary mapping task_id to task configuration for all enabled tasks.
        Each task config includes the group's feature_flag (if any) for runtime checking.
    """
    all_tasks = {}

    for group in TASK_GROUPS:
        enabled_tasks = group.get_enabled_tasks()
        for task in enabled_tasks:
            task_id = task["task_id"]
            task_config = task.copy()
            task_config["group"] = group.name
            task_config["group_description"] = group.description
            # Add group's feature flag for runtime checking
            if group.feature_flag:
                task_config["feature_flag"] = group.feature_flag
            all_tasks[task_id] = task_config

    return all_tasks


def get_all_tasks_for_init() -> dict[str, dict[str, Any]]:
    """
    Get all tasks from all groups for use by init-system-tasks.

    This function does NOT evaluate group-level feature flags.
    Every task that has enabled=True (or no enabled field) is included.
    The group's feature_flag is embedded in each task config.

    Returns:
        Dictionary mapping task_id to task configuration for all individually-enabled
        tasks across all groups, with feature_flag included where applicable.
    """
    all_tasks = {}

    for group in TASK_GROUPS:
        # Respect individual task enabled fields but ignore the group-level flag.
        individually_enabled = [task for task in group.tasks if task.get("enabled", True)]
        for task in individually_enabled:
            task_id = task["task_id"]
            task_config = task.copy()
            task_config["group"] = group.name
            task_config["group_description"] = group.description
            if group.feature_flag:
                task_config["feature_flag"] = group.feature_flag
            all_tasks[task_id] = task_config

    return all_tasks


def get_oneshot_task_ids() -> set[str]:
    """
    Return the task ids of one-shot (cron=None) tasks defined in the task groups.

    These are the system tasks whose completed state init-system-tasks preserves. Per-run rows that
    the scheduler creates for recurring system tasks (e.g. "hourly_health_check (Execution ...)")
    are also non-recurring system tasks, but their names never match a task id.
    """
    return {task_id for task_id, config in get_all_tasks_for_init().items() if not config.get("cron")}
