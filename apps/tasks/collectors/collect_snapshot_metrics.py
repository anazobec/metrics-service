"""
Snapshot metrics collector for point-in-time data.

Collects current state snapshots (not time-series), computes rollup
statistics, and stores in HourlyMetricsCollection.
"""

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from ..utils import create_task_result, generic_collect_metrics, get_db_connection, parse_datetime_string

logger = logging.getLogger(__name__)


def _get_snapshot_collectors():
    """
    Get snapshot collectors registry with lazy imports.

    Lazy imports prevent metrics_utility dependency from breaking
    unrelated task registration (e.g., hello_world, cleanup_old_tasks).
    """
    from metrics_utility.anonymized_rollups import (
        ControllerVersionAnonymizedRollup,
        ExecutionEnvironmentsAnonymizedRollup,
        FeatureFlagsAnonymizedRollup,
        TableMetadataAnonymizedRollup,
    )
    from metrics_utility.library.collectors.controller import (
        config,
        controller_version_service,
        counts,
        cred_type_counts,
        execution_environments,
        feature_flags_service,
        host_metric_summary_monthly_table,
        instance_info,
        inventory_counts,
        main_host,
        org_counts,
        projects_by_scm_type,
        table_metadata,
        unified_job_template_table,
        workflow_job_template_node_table,
    )

    # Registry mapping collector_type to (collector_func, rollup_processor_class)
    # rollup_processor can be None for collectors that don't need processing (e.g., config)
    return {
        "execution_environments": {
            "collector_func": execution_environments,
            "rollup_processor": ExecutionEnvironmentsAnonymizedRollup,
            "description": "Execution environments snapshot",
        },
        "config": {
            "collector_func": config,
            "rollup_processor": None,  # Config is raw data, no rollup processing needed
            "description": "System configuration snapshot",
        },
        "controller_version_service": {
            "collector_func": controller_version_service,
            "rollup_processor": ControllerVersionAnonymizedRollup,
            "description": "Controller version snapshot",
        },
        "feature_flags_service": {
            "collector_func": feature_flags_service,
            "rollup_processor": FeatureFlagsAnonymizedRollup,
            "description": "Feature flags snapshot",
        },
        "table_metadata": {
            "collector_func": table_metadata,
            "rollup_processor": TableMetadataAnonymizedRollup,
            "description": "Table metadata snapshot",
        },
        "main_host": {
            "collector_func": main_host,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Current host inventory snapshot",
        },
        "counts": {
            "collector_func": counts,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Controller object counts snapshot",
        },
        "cred_type_counts": {
            "collector_func": cred_type_counts,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Credential type counts snapshot",
        },
        "host_metric_summary_monthly_table": {
            "collector_func": host_metric_summary_monthly_table,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Monthly host metric summary snapshot",
        },
        "instance_info": {
            "collector_func": instance_info,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Controller instance information snapshot",
        },
        "inventory_counts": {
            "collector_func": inventory_counts,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Inventory counts snapshot",
        },
        "org_counts": {
            "collector_func": org_counts,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Organization counts snapshot",
        },
        "projects_by_scm_type": {
            "collector_func": projects_by_scm_type,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Project counts by SCM type snapshot",
        },
        "unified_job_template_table": {
            "collector_func": unified_job_template_table,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Unified job template snapshot",
        },
        "workflow_job_template_node_table": {
            "collector_func": workflow_job_template_node_table,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Workflow job template node snapshot",
        },
    }


def collect_snapshot_metrics(**kwargs) -> dict[str, Any]:
    """
    Collect snapshot metrics for a specific collector type.

    This function handles all snapshot collectors that gather current
    state data (not time-series). It collects raw data, computes rollup
    statistics, and stores the rollup in HourlyMetricsCollection. Raw analytics-only
    collectors are persisted in AnalyticsPayload instead and do not create an hourly row.

    Args:
        **kwargs: Task data containing:
            - collector_type (str): Type of collector (required)
            - database (str): Database name (default: 'awx')
            - collection_timestamp (str): default yesterday 23:00

    Returns:
        dict: Task result with collection status and record ID
    """
    collector_type = kwargs.pop("collector_type", None)
    if not collector_type:
        return create_task_result("error", error="collector_type parameter is required")

    # Extract optional execution_id for linking to TaskExecution
    execution_id = kwargs.get("execution_id")  # Available when called via execute_db_task

    # Determine hour to collect (default to previous full hour)
    collection_timestamp_str = kwargs.get("collection_timestamp")
    if collection_timestamp_str:
        collection_timestamp = parse_datetime_string(collection_timestamp_str)
        if collection_timestamp is None:
            return create_task_result("error", error=f"Invalid collection_timestamp format: {collection_timestamp_str}")
    else:
        # Snapshot collections belong to the previous day (the day being summarized)
        # Use 23:00 of the previous day to ensure they fall within the daily rollup's query window
        collection_timestamp = timezone.now().replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)

    # Get database connection
    db_connection = get_db_connection()

    # For auditing, store collection date at start of day (not the 23:00 trick used for rollup query)
    # This makes auditing clearer - snapshots represent "state at end of this day"
    collection_date = collection_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)

    # Use generic collector without time window (snapshot = current state)
    # Pass collection_time in collector_kwargs for audit - it gets filtered out before calling collector
    return generic_collect_metrics(
        collector_type=collector_type,
        collector_registry=_get_snapshot_collectors(),
        collection_mode="snapshot",
        timestamp=collection_timestamp,
        db_connection=db_connection,
        collector_kwargs={"collection_time": collection_date},
        task_execution_id=execution_id,
    )
