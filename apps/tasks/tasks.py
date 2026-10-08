"""
Background tasks for metrics_service using dispatcherd.

This module serves as an aggregator, importing and re-exporting tasks from
individual task modules:
- simple/: Simple system tasks
- cleanup/: Cleanup and maintenance tasks
- collectors/: Metrics collection and anonymization tasks
- tasks_system: Core task execution infrastructure and utilities

Queue routing is defined per-function in TASK_METADATA ("queue" field).
"""

import logging

# Note: Hourly and snapshot collectors handle all collector types via collector_type parameter
# Import system tasks
from ..analytics.tasks import collect_analytics_on_demand

# Dashboard reports tasks
from ..dashboard_reports.tasks import (
    ORG_NAME_BACKFILL_BATCH_SIZE,
    backfill_dashboard_organization_names,
    cleanup_dashboard_reports_old_data,
    cleanup_dashboard_telemetry,
    collect_dashboard_reports_data,
    collect_dashboard_reports_initial_data,
    sync_dashboard_host_summaries,
    sync_dashboard_job_records,
)

# Import cleanup tasks
from .cleanup.cleanup_activitystream import cleanup_activitystream
from .cleanup.cleanup_metrics_data import cleanup_metrics_data
from .cleanup.cleanup_old_tasks import cleanup_old_tasks

# Import collector tasks
from .collectors.collect_daily_metrics import collect_daily_metrics
from .collectors.collect_hourly_metrics import collect_hourly_metrics
from .collectors.collect_snapshot_metrics import collect_snapshot_metrics
from .collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare
from .collectors.daily_metrics_rollup import daily_metrics_rollup
from .collectors.send_anonymized_to_segment import send_anonymized_to_segment
from .simple.hello_world import hello_world
from .simple.resource_sync import sync_resources_from_gateway
from .tasks_system import create_system_tasks, submit_task_to_dispatcher

logger = logging.getLogger(__name__)

# Task configuration for dispatcherd
TASK_FUNCTIONS = {
    # System tasks
    "hello_world": hello_world,
    "cleanup_old_tasks": cleanup_old_tasks,
    "sync_resources_from_gateway": sync_resources_from_gateway,
    "cleanup_activitystream": cleanup_activitystream,
    "cleanup_metrics_data": cleanup_metrics_data,
    # Metrics Collection (hourly time-series, daily snapshots, and daily time-range)
    "collect_hourly_metrics": collect_hourly_metrics,
    "collect_snapshot_metrics": collect_snapshot_metrics,
    "collect_daily_metrics": collect_daily_metrics,
    # On-demand analytics collection (ANSTRAT-1587 BYO-BI) — runs one enabled collector for an
    # arbitrary window and stores raw output in AnalyticsPayload (does not touch the rollup path).
    "collect_analytics_on_demand": collect_analytics_on_demand,
    # Daily Rollup and Anonymization Tasks
    "daily_metrics_rollup": daily_metrics_rollup,
    "daily_anonymize_and_prepare": daily_anonymize_and_prepare,
    "send_anonymized_to_segment": send_anonymized_to_segment,
    # Dashboard reports
    "collect_dashboard_reports_data": collect_dashboard_reports_data,
    "collect_dashboard_reports_initial_data": collect_dashboard_reports_initial_data,
    "backfill_dashboard_organization_names": backfill_dashboard_organization_names,
    "cleanup_dashboard_reports_old_data": cleanup_dashboard_reports_old_data,
    "cleanup_dashboard_telemetry": cleanup_dashboard_telemetry,
    "sync_dashboard_job_records": sync_dashboard_job_records,
    "sync_dashboard_host_summaries": sync_dashboard_host_summaries,
}

# Tasks that require a PostgreSQL advisory lock during scheduled execution.
# The lock key is the function name. Locking is applied in execute_db_task,
# so direct invocations (e.g. run_task.py) run without contention.
TASK_LOCKS = {
    "sync_resources_from_gateway",
    "collect_hourly_metrics",
    "collect_snapshot_metrics",
    "daily_metrics_rollup",
    "daily_anonymize_and_prepare",
    "send_anonymized_to_segment",
    "collect_dashboard_reports_initial_data",
    "backfill_dashboard_organization_names",
    "cleanup_dashboard_reports_old_data",
    "cleanup_dashboard_telemetry",
    "sync_dashboard_job_records",
    "sync_dashboard_host_summaries",
}


def get_queue_for_function(function_name: str) -> str:
    """Get the appropriate queue name for a task function; or 'maintenance'"""
    metadata = TASK_METADATA.get(function_name)
    return (metadata and metadata.get("queue", None)) or "maintenance"


_DASHBOARD_REPORTS_CATEGORY = "Dashboard Reports"

# Enhanced task metadata for dashboard display
TASK_METADATA = {
    # System sync
    "sync_resources_from_gateway": {
        "queue": "maintenance",
        "category": "Maintenance",
        "description": "Sync users, organizations, teams and RBAC role assignments from the gateway resource server",
        "parameters": {},
        "examples": [{"name": "Full resource sync", "data": {}}],
    },
    # Testing
    "hello_world": {
        "queue": "maintenance",
        "category": "Testing",
        "description": "Simple hello world task for testing the dispatcherd integration",
        "parameters": {},
        "examples": [{"name": "Basic Hello World", "data": {}}],
    },
    # Maintenance
    "cleanup_old_tasks": {
        "queue": "maintenance",
        "category": "Maintenance",  # task system records
        "description": "Clean up old completed and failed tasks (preserves recurring tasks by default)",
        "parameters": {
            "days_old": {
                "type": "integer",
                "default": 5,
                "description": "Number of days old tasks should be to qualify for cleanup",
                "min": 1,
                "max": 365,
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "If true, only count tasks that would be deleted without actually deleting",
            },
            "include_executions": {
                "type": "boolean",
                "default": True,
                "description": "Also cleanup related TaskExecution records",
            },
            "preserve_recurring": {
                "type": "boolean",
                "default": True,
                "description": "If true, exclude recurring tasks from cleanup (recommended)",
            },
        },
        "examples": [
            {"name": "Standard cleanup (5 days)", "data": {"days_old": 5}},
            {"name": "Test cleanup (dry run)", "data": {"days_old": 7, "dry_run": True}},
            {"name": "Conservative cleanup", "data": {"days_old": 10, "include_executions": False}},
            {"name": "Include recurring tasks", "data": {"days_old": 30, "preserve_recurring": False}},
        ],
    },
    "cleanup_activitystream": {
        "queue": "maintenance",
        "category": "Maintenance",  # DAB activity stream audit log
        "description": "Clean up old ActivityStream (django-ansible-base) audit log entries",
        "parameters": {
            "days_old": {
                "type": "integer",
                "default": 7,
                "description": "Number of days old ActivityStream entries must be before they are removed",
                "min": 1,
                "max": 365,
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "If true, only count entries that would be deleted without actually deleting",
            },
        },
        "examples": [
            {"name": "Default (7 days)", "data": {}},
            {"name": "Dry run", "data": {"dry_run": True}},
            {"name": "Extended retention (30 days)", "data": {"days_old": 30}},
        ],
    },
    "cleanup_metrics_data": {
        "queue": "metrics",
        "category": "Maintenance",  # metrics collection data
        "description": "Clean up old metrics data based on retention policies",
        "parameters": {
            "hourly_retention_days": {
                "type": "integer",
                "default": 7,
                "description": "Number of days to retain hourly collection data",
                "min": 1,
                "max": 365,
            },
            "daily_retention_days": {
                "type": "integer",
                "default": 30,
                "description": "Number of days to retain daily rollup data",
                "min": 1,
                "max": 730,
            },
            "payload_retention_days": {
                "type": "integer",
                "default": 7,
                "description": "Number of days to retain anonymized payloads",
                "min": 1,
                "max": 90,
            },
            "analytics_retention_days": {
                "type": "integer",
                "default": None,
                "description": "Explicit override; otherwise use the Controller cleanup_jobs schedule or 90 days",
                "min": 1,
                "max": 3650,
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "If true, only count records that would be deleted without actually deleting",
            },
        },
        "examples": [
            {"name": "Default retention", "data": {}},
            {
                "name": "Custom retention",
                "data": {
                    "hourly_retention_days": 14,
                    "daily_retention_days": 60,
                    "payload_retention_days": 14,
                    "analytics_retention_days": 180,
                },
            },
            {"name": "Dry run", "data": {"dry_run": True}},
        ],
    },
    # Metrics Collection (Hourly and Snapshot)
    "collect_hourly_metrics": {
        "queue": "metrics",
        "category": "Metrics Collection",
        "description": "Collect hourly time-series metrics for a specific collector type",
        "parameters": {
            "collector_type": {
                "type": "string",
                "required": True,
                "description": "Type of collector to run (e.g., unified_jobs)",
            },
            "hour_timestamp": {
                "type": "string",
                "description": "ISO timestamp for the hour to collect (defaults to current hour)",
            },
        },
        "examples": [
            {"name": "Job host summary", "data": {"collector_type": "job_host_summary_service"}},
            {"name": "Unified jobs", "data": {"collector_type": "unified_jobs"}},
            {"name": "Credentials", "data": {"collector_type": "credentials_service"}},
            {"name": "Job events", "data": {"collector_type": "main_jobevent_service"}},
            {
                "name": "Specific hour",
                "data": {"collector_type": "job_host_summary_service", "hour_timestamp": "2024-01-01T00:00:00Z"},
            },
            {"name": "Raw automation events", "data": {"collector_type": "events_table"}},
            {"name": "Workflow job nodes", "data": {"collector_type": "workflow_job_node_table"}},
            {"name": "Query metadata", "data": {"collector_type": "query_info"}},
        ],
    },
    "collect_daily_metrics": {
        "queue": "metrics",
        "category": "Metrics Collection",
        "description": "Collect daily time-range metrics (previous full day) for a specific collector type",
        "parameters": {
            "collector_type": {
                "type": "string",
                "required": True,
                "description": "Type of daily collector to run (e.g., task_executions_service)",
            },
            "since": {
                "type": "string",
                "description": "ISO timestamp for start of collection window (defaults to yesterday 00:00 UTC)",
            },
            "until": {
                "type": "string",
                "description": "ISO timestamp for end of collection window (defaults to today 00:00 UTC)",
            },
        },
        "examples": [
            {"name": "Task executions (default: yesterday)", "data": {"collector_type": "task_executions_service"}},
            {"name": "Indirect managed nodes", "data": {"collector_type": "indirect_managed_nodes"}},
            {
                "name": "Task executions (specific day)",
                "data": {
                    "collector_type": "task_executions_service",
                    "since": "2024-01-01T00:00:00Z",
                    "until": "2024-01-02T00:00:00Z",
                },
            },
            {"name": "Changed host inventory", "data": {"collector_type": "main_host_daily"}},
            {"name": "Host metrics", "data": {"collector_type": "main_hostmetric"}},
        ],
    },
    "collect_snapshot_metrics": {
        "queue": "metrics",
        "category": "Metrics Collection",
        "description": "Collect daily snapshot metrics for a specific collector type",
        "parameters": {
            "collector_type": {
                "type": "string",
                "required": True,
                "description": "Type of collector to run (e.g., execution_environments)",
            },
        },
        "examples": [
            {"name": "Execution environments", "data": {"collector_type": "execution_environments"}},
            {"name": "System config", "data": {"collector_type": "config"}},
            {"name": "Controller version", "data": {"collector_type": "controller_version_service"}},
            {"name": "Table metadata", "data": {"collector_type": "table_metadata"}},
            {"name": "Feature flags", "data": {"collector_type": "feature_flags_service"}},
            {"name": "Host inventory", "data": {"collector_type": "main_host"}},
            {"name": "Controller counts", "data": {"collector_type": "counts"}},
            {"name": "Credential type counts", "data": {"collector_type": "cred_type_counts"}},
            {
                "name": "Monthly host metric summary",
                "data": {"collector_type": "host_metric_summary_monthly_table"},
            },
            {"name": "Controller instance information", "data": {"collector_type": "instance_info"}},
            {"name": "Inventory counts", "data": {"collector_type": "inventory_counts"}},
            {"name": "Organization counts", "data": {"collector_type": "org_counts"}},
            {"name": "Projects by SCM type", "data": {"collector_type": "projects_by_scm_type"}},
            {"name": "Unified job templates", "data": {"collector_type": "unified_job_template_table"}},
            {
                "name": "Workflow job template nodes",
                "data": {"collector_type": "workflow_job_template_node_table"},
            },
        ],
    },
    "collect_analytics_on_demand": {
        "queue": "metrics",
        "category": "Metrics Collection",
        "description": "Run one enabled analytics collector on demand for an arbitrary window (BYO-BI)",
        "parameters": {
            "collector": {
                "type": "string",
                "required": True,
                "description": "Public collector name (group.function, e.g. controller.unified_jobs_dashboard)",
            },
            "since": {
                "type": "string",
                "description": "ISO start of window, inclusive (windowed collectors only)",
            },
            "until": {
                "type": "string",
                "description": "ISO end of window, exclusive (windowed collectors only)",
            },
        },
        "examples": [
            {
                "name": "Unified jobs (last hour)",
                "data": {
                    "collector": "controller.unified_jobs_dashboard",
                    "since": "2026-08-17T10:00:00Z",
                    "until": "2026-08-17T11:00:00Z",
                },
            },
            {"name": "Config snapshot", "data": {"collector": "controller.config"}},
        ],
    },
    # Daily Rollup and Anonymization
    "daily_metrics_rollup": {
        "queue": "metrics",
        "category": "Metrics Rollup",
        "description": "Merge hourly collections and create daily rollup summary",
        "parameters": {
            "summary_date": {
                "type": "string",
                "description": "ISO date for the rollup (defaults to yesterday)",
            },
        },
        "examples": [
            {"name": "Default (yesterday)", "data": {}},
            {"name": "Specific date", "data": {"summary_date": "2024-01-01"}},
        ],
    },
    "daily_anonymize_and_prepare": {
        "queue": "metrics",
        "category": "Metrics Anonymization",
        "description": "Anonymize daily rollup and prepare payload for transmission",
        "parameters": {
            "summary_date": {
                "type": "string",
                "description": "ISO date for the summary to anonymize (defaults to yesterday)",
            },
        },
        "examples": [
            {"name": "Default (yesterday)", "data": {}},
            {"name": "Specific date", "data": {"summary_date": "2024-01-01"}},
        ],
    },
    "send_anonymized_to_segment": {
        "queue": "metrics",
        "category": "Metrics Transmission",
        "description": "Send anonymized metrics payloads to Segment.com",
        "parameters": {
            "payload_id": {
                "type": "integer",
                "description": "Specific payload ID to send (optional, sends pending/retry payloads if not specified)",
            },
            "max_payloads": {
                "type": "integer",
                "default": 5,
                "description": "Maximum number of payloads to send in one task execution",
                "min": 1,
                "max": 100,
            },
            "stale_minutes": {
                "type": "integer",
                "default": 10,
                "description": "Minutes before 'sending' status is considered stale and recovered",
                "min": 1,
                "max": 60,
            },
        },
        "examples": [
            {"name": "Default (send pending)", "data": {}},
            {"name": "Specific payload", "data": {"payload_id": 123}},
            {"name": "Send batch of 10", "data": {"max_payloads": 10}},
            {"name": "Recover stale payloads (30 min)", "data": {"stale_minutes": 30}},
        ],
    },
    "collect_dashboard_reports_data": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "Collect data for automation-reports dashboard (job templates, top projects/users) with configurable date range",
        "parameters": {
            "since": {
                "type": "string",
                "description": "Start datetime for collection (ISO format, defaults to last collected timestamp or the Controller's retention period)",
                "pattern": "datetime",
            },
            "until": {
                "type": "string",
                "description": "End datetime for collection (ISO format, defaults to now)",
                "pattern": "datetime",
            },
        },
        "examples": [
            {"name": "Default collection (last collected timestamp or Controller retention period)", "data": {}},
            {
                "name": "Custom date range",
                "data": {"since": "2024-01-30T00:00:00Z", "until": "2024-01-31T23:59:59Z"},
            },
            {"name": "Last 7 days", "data": {"since": "2024-01-24T00:00:00Z", "until": "2024-01-31T00:00:00Z"}},
        ],
    },
    "collect_dashboard_reports_initial_data": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "One-time backfill of historical AWX job data (window controlled by the Controller's cleanup_jobs retention schedule, default 90 days). Ongoing incremental sync is driven automatically by the hourly_unified_jobs hook after this completes.",
        "parameters": {
            "since": {
                "type": "string",
                "description": "Start datetime for collection (ISO format, defaults to the Controller's retention period, or 90 days ago if unavailable)",
                "pattern": "datetime",
            },
            "until": {
                "type": "string",
                "description": "End datetime for collection (ISO format, defaults to now)",
                "pattern": "datetime",
            },
        },
        "examples": [
            {"name": "Default (Controller retention period)", "data": {}},
            {
                "name": "Custom date range",
                "data": {"since": "2024-01-01T00:00:00Z", "until": "2024-03-31T23:59:59Z"},
            },
        ],
    },
    "backfill_dashboard_organization_names": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "One-time fix: set organization_name on JobData rows where it is NULL but organization_id is set (organizations no longer in AWX are left unchanged)",
        "parameters": {
            "batch_size": {
                "type": "integer",
                "default": ORG_NAME_BACKFILL_BATCH_SIZE,
                "description": "Number of JobData rows updated per batch (each batch is a separate UPDATE)",
                "min": 1,
            },
        },
        "examples": [{"name": "Default", "data": {}}, {"name": "Smaller batches", "data": {"batch_size": 1000}}],
    },
    "sync_dashboard_job_records": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "Write unified_jobs data collected during the hourly rollup to the dashboard JobData table",
        "parameters": {
            "hour_timestamp": {
                "type": "string",
                "description": "ISO timestamp of the hour being synced",
            },
            "raw_jobs": {
                "type": "array",
                "description": "Serialised unified_jobs rows from the hourly collector hook",
            },
        },
        "examples": [
            {
                "name": "Sync one hour of job records",
                "data": {
                    "hour_timestamp": "2024-01-01T00:00:00+00:00",
                    "raw_jobs": [
                        {
                            "id": 1,
                            "name": "Demo Job Template",
                            "unified_job_template_id": 10,
                            "organization_id": 1,
                            "organization_name": "Default",
                            "started": "2024-01-01T00:01:00+00:00",
                            "finished": "2024-01-01T00:02:30+00:00",
                            "status": "successful",
                            "elapsed": 90.0,
                            "launched_by_id": 1,
                            "launched_by_username": "admin",
                            "project_id": 5,
                            "project_name": "Demo Project",
                            "created": "2024-01-01T00:00:50+00:00",
                            "modified": "2024-01-01T00:02:30+00:00",
                            "label_ids": None,
                            "num_hosts": 3,
                        }
                    ],
                },
            },
        ],
    },
    "sync_dashboard_host_summaries": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "Write job_host_summary_service data collected during the hourly rollup to the dashboard JobHostSummary table",
        "parameters": {
            "hour_timestamp": {
                "type": "string",
                "description": "ISO timestamp of the hour being synced",
            },
            "raw_host_summaries": {
                "type": "array",
                "description": "Serialised job_host_summary_service rows from the hourly collector hook",
            },
        },
        "examples": [
            {
                "name": "Sync one hour of host summary records",
                "data": {
                    "hour_timestamp": "2024-01-01T00:00:00+00:00",
                    "raw_host_summaries": [
                        {
                            "id": 1,
                            "host_name": "web01",
                            "host_remote_id": 10,
                            "job_remote_id": 42,
                        }
                    ],
                },
            },
        ],
    },
    "cleanup_dashboard_reports_old_data": {
        "queue": "dashboard",
        "category": "Maintenance",  # dashboard report JobData
        "description": "Delete dashboard report JobData records older than the retention period (defaults to the Controller's cleanup_jobs retention schedule)",
        "parameters": {
            "retention_days": {
                "type": "integer",
                "default": None,
                "description": "Number of days to retain dashboard report data. Defaults to the Controller's cleanup_jobs retention schedule (or 90 if unavailable).",
                "min": 0,
                "max": 365,
            },
        },
        "examples": [
            {"name": "Default (matches Controller retention schedule)", "data": {}},
            {"name": "Extended retention", "data": {"retention_days": 180}},
        ],
    },
    "cleanup_dashboard_telemetry": {
        "queue": "dashboard",
        "category": _DASHBOARD_REPORTS_CATEGORY,
        "description": "Delete DashboardTelemetry rows older than retention_days (default: 60) to prevent unbounded table growth.",
        "parameters": {
            "retention_days": {
                "type": "integer",
                "default": 60,
                "description": "Number of days to retain telemetry rows. Rows with collection_run_date older than this are deleted.",
                "min": 0,
                "max": 365,
            },
        },
        "examples": [
            {"name": "Default (60 days)", "data": {}},
            {"name": "30-day retention", "data": {"retention_days": 30}},
        ],
    },
}

# Explicit exports for better IDE support
__all__ = [
    # System tasks
    "hello_world",
    "cleanup_old_tasks",
    "cleanup_activitystream",
    "cleanup_metrics_data",
    "submit_task_to_dispatcher",
    "create_system_tasks",
    # Metrics collection (hourly, snapshot, and daily time-range)
    "collect_hourly_metrics",
    "collect_snapshot_metrics",
    "collect_daily_metrics",
    "collect_analytics_on_demand",
    # Daily rollup and anonymization tasks
    "daily_metrics_rollup",
    "daily_anonymize_and_prepare",
    "send_anonymized_to_segment",
    # Configuration
    "TASK_FUNCTIONS",
    "TASK_LOCKS",
    "TASK_METADATA",
    "get_queue_for_function",
    # Dashboard reports
    "collect_dashboard_reports_data",
    "collect_dashboard_reports_initial_data",
    "backfill_dashboard_organization_names",
    "cleanup_dashboard_reports_old_data",
    "cleanup_dashboard_telemetry",
    "sync_dashboard_job_records",
    "sync_dashboard_host_summaries",
]
