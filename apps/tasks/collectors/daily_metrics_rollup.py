"""
Create daily summary from hourly rollups

This task merges hourly rollup statistics into a daily summary,
collects daily data (unified_jobs, execution_environments, config),
and creates a comprehensive daily rollup record.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.dashboard_reports.models import DashboardTelemetry

from ..utils import (
    create_task_result,
    log_task_execution,
)

logger = logging.getLogger(__name__)


def _merge_collects(collections: list, rollup_processor) -> dict:
    """
    Aggregate rollup statistics from hourly collections

    This function merges hourly rollup JSON using the rollup processor's
    merge logic to produce a daily rollup.

    Args:
        collections: List of HourlyMetricsCollection objects with rollup data (JSON)
        rollup_processor: Rollup processor instance

    Returns:
        dict: Daily rollup JSON with merged statistics
    """
    # Merge all hourly rollup JSON
    merged = None

    for collection in collections:
        rollup_json = collection.raw_data

        # Skip empty collections
        if not rollup_json:
            continue

        # Merge JSON directly - rollup.merge(json, json) -> json
        merged = rollup_processor.merge(merged, rollup_json)

    # postprocess and return
    return rollup_processor.base(merged).get("json", {})


def _collect_and_group_hourly_collections(summary_date: date) -> tuple[dict[str, list], datetime, datetime]:
    """
    Query and group hourly collections by collector type.

    Args:
        summary_date: Date to query collections for

    Returns:
        tuple: (collections_by_type dict, start_datetime, end_datetime)
    """
    from apps.tasks.models import HourlyMetricsCollection

    start_datetime = timezone.make_aware(datetime.combine(summary_date, datetime.min.time()))
    end_datetime = start_datetime + timedelta(days=1)

    hourly_collections = HourlyMetricsCollection.objects.filter(
        collection_timestamp__gte=start_datetime, collection_timestamp__lt=end_datetime, status="collected"
    ).order_by("collector_type", "collection_timestamp")

    # Group by collector type
    collections_by_type: dict[str, list] = {}
    for collection in hourly_collections:
        if collection.collector_type not in collections_by_type:
            collections_by_type[collection.collector_type] = []
        collections_by_type[collection.collector_type].append(collection)

    return collections_by_type, start_datetime, end_datetime


def _merge_hourly_rollups(collections_by_type: dict[str, list]) -> tuple[dict, list]:
    """
    Merge hourly and daily rollups using rollup processors

    Handles both:
    - Hourly collectors: Merge 24 hourly collections into daily rollup, check for missing hours
    - Daily snapshots: Process single daily collection, no hourly checks

    Args:
        collections_by_type: Dict mapping collector type to list of HourlyMetricsCollection objects

    Returns:
        tuple: (daily_rollup dict, missing_hours list)
    """
    from metrics_utility.anonymized_rollups import (  # EventModulesAnonymizedRollup,
        ControllerVersionAnonymizedRollup,
        CredentialsAnonymizedRollup,
        EventModulesAnonymizedRollup,
        ExecutionEnvironmentsAnonymizedRollup,
        FeatureFlagsAnonymizedRollup,
        IndirectManagedNodesAnonymizedRollup,
        JobHostSummaryAnonymizedRollup,
        JobsAnonymizedRollup,
        TableMetadataAnonymizedRollup,
        TaskExecutionsAnonymizedRollup,
    )

    # Rollup processors for each collector type
    # Hourly collectors expect 24 collections (one per hour)
    hourly_rollup_processors = {
        "credentials_service": CredentialsAnonymizedRollup(),
        "job_host_summary_service": JobHostSummaryAnonymizedRollup(),
        "main_jobevent_service": EventModulesAnonymizedRollup(),
        "unified_jobs": JobsAnonymizedRollup(),
    }
    # events_table, workflow_job_node_table, and query_info are raw analytics collectors.
    # They intentionally have no entry here because they do not produce anonymized rollup data.

    # Daily collectors expect 1 collection per day.
    # Includes both snapshot collectors and daily time-range collectors.
    daily_rollup_processors = {
        "execution_environments": ExecutionEnvironmentsAnonymizedRollup(),
        "controller_version_service": ControllerVersionAnonymizedRollup(),
        "feature_flags_service": FeatureFlagsAnonymizedRollup(),
        "table_metadata": TableMetadataAnonymizedRollup(),
        "task_executions_service": TaskExecutionsAnonymizedRollup(),
        "indirect_managed_nodes": IndirectManagedNodesAnonymizedRollup(),
    }
    # config is handled separately, while the remaining raw analytics collectors are intentionally
    # absent because they do not produce anonymized rollup data:
    # main_host, counts, cred_type_counts, host_metric_summary_monthly_table, instance_info,
    # inventory_counts, org_counts, projects_by_scm_type, unified_job_template_table,
    # workflow_job_template_node_table, main_host_daily, and main_hostmetric.

    # Merge hourly rollups into daily rollups
    daily_rollup = {}
    missing_hours = []

    # Process hourly collectors (expect 24 collections)
    for collector_type, processor in hourly_rollup_processors.items():
        collections = collections_by_type.get(collector_type, [])

        # Check for missing hours (should have 24 collections for hourly collectors)
        if len(collections) < 24:
            collected_hours = {c.collection_timestamp.hour for c in collections}
            missing_hours.extend([f"{collector_type}:{hour}" for hour in range(24) if hour not in collected_hours])

        # Merge hourly rollups using rollup processor
        daily_rollup[collector_type] = _merge_collects(collections, processor)

    # Process daily snapshot collectors (expect 1 collection)
    for collector_type, processor in daily_rollup_processors.items():
        collections = collections_by_type.get(collector_type, [])

        # For daily snapshots, we just need the rollup data (no merging across hours)
        if collections:
            daily_rollup[collector_type] = _merge_collects(collections, processor)
        else:
            logger.warning(f"No {collector_type} collection found for summary date")
            missing_hours.append(f"{collector_type}:daily")
            daily_rollup[collector_type] = _merge_collects([], processor)

    return daily_rollup, missing_hours


def _save_daily_summary(
    summary_date: date,
    daily_rollup: dict,
    collections_by_type: dict[str, list],
    config_data: dict,
    missing_hours: list,
    execution_id: str | None,
) -> tuple:
    """
    Create or update DailyMetricsSummary record.

    Args:
        summary_date: Date being summarized
        daily_rollup: Aggregated daily rollup data
        collections_by_type: Collections grouped by type
        config_data: Config data
        missing_hours: List of missing hourly collections
        execution_id: Task execution ID

    Returns:
        tuple: (daily_summary object, created boolean, hourly_collections_count)
    """
    from apps.tasks.models import DailyMetricsSummary, HourlyMetricsCollection

    # Keep raw analytics collections out of the daily summary. They may share the collection
    # table, but only collector types present in the anonymized rollup belong in this summary.
    hourly_collection_ids = {
        collector_type: [c.id for c in collections]
        for collector_type, collections in collections_by_type.items()
        if collector_type in daily_rollup
    }

    # Mark every collected record as processed, including raw analytics records that were not
    # included in the summary, so they are not reconsidered by a later rollup.
    all_processed_ids = []
    for ids_list in hourly_collection_ids.values():
        all_processed_ids.extend(ids_list)
    raw_only_ids = [
        c.id
        for collector_type, collections in collections_by_type.items()
        if collector_type not in daily_rollup
        for c in collections
    ]
    all_processed_ids.extend(raw_only_ids)
    hourly_collections_count = sum(len(ids_list) for ids_list in hourly_collection_ids.values())

    # Create or update DailyMetricsSummary
    daily_summary, created = DailyMetricsSummary.objects.update_or_create(
        summary_date=summary_date,
        defaults={
            "aggregated_metrics": daily_rollup,
            "hourly_collection_ids": hourly_collection_ids,
            "config_data": config_data,
            "status": "aggregated",
            "hourly_collections_count": hourly_collections_count,
            "missing_hours": missing_hours,
            "aggregation_completed_at": timezone.now(),
            "rollup_task_execution_id": execution_id,
            "error_message": "",  # Clear any previous error
        },
    )

    # Mark only the hourly collections we actually processed as "processed"
    HourlyMetricsCollection.objects.filter(id__in=all_processed_ids).update(status="processed")

    return daily_summary, created, hourly_collections_count


def _aggregate_dashboard_telemetry(summary_date: date) -> list[dict]:
    """
    Query DashboardTelemetry rows for summary_date and return
    an anonymized, aggregate-only list (no org/user/job details).
    """
    try:
        rows = DashboardTelemetry.objects.filter(collection_run_date=summary_date)
        return [
            {
                "task_name": row.task_name,
                "success": row.success,
                "collection_duration_ms": float(row.collection_duration_ms),
                "number_of_records_processed": row.number_of_records_processed,
                "database_query_time_ms": float(row.database_query_time_ms)
                if row.database_query_time_ms is not None
                else None,
                "cache_hit_rate": float(row.cache_hit_rate) if row.cache_hit_rate is not None else None,
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Failed to aggregate dashboard telemetry for rollup - skipping")
        return []


def daily_metrics_rollup(**kwargs) -> dict[str, Any]:
    """
    Create daily summary from hourly rollups

    This task:
    - pulls hourly, daily, and snapshot collections for the target data
    - fails if no hourlies exist
    - merges them into a daily rollup
    - saves to DailyMetricsSummary

    Args:
        **kwargs: Task data containing:
            - summary_date (str): Date to summarize (YYYY-MM-DD, defaults to yesterday)
            - database (str): Database name (default: 'awx')
            - execution_id

    Returns:
        dict: Task result with summary ID and statistics
    """
    # Determine summary date (default to yesterday)
    summary_date_str = kwargs.get("summary_date")
    if summary_date_str:
        summary_date = date.fromisoformat(summary_date_str)
    else:
        summary_date = timezone.now().date() - timedelta(days=1)

    log_task_execution("daily_metrics_rollup", "processing", f"Creating daily rollup for: {summary_date}")

    # Check upstream dependency: at least some hourly collections must exist
    from ..models import HourlyMetricsCollection

    start_dt = timezone.make_aware(datetime.combine(summary_date, datetime.min.time()))
    end_dt = start_dt + timedelta(days=1)
    has_collections = HourlyMetricsCollection.objects.filter(
        collection_timestamp__gte=start_dt, collection_timestamp__lt=end_dt, status="collected"
    ).exists()

    if not has_collections:
        msg = f"No collected hourly metrics found for {summary_date} — upstream dependency not met"
        log_task_execution("daily_metrics_rollup", "skipped", msg)
        return create_task_result("error", error=msg)

    try:
        # Query and group hourly collections by type
        collections_by_type, start_datetime, end_datetime = _collect_and_group_hourly_collections(summary_date)

        # Extract config snapshot from daily collections
        config_collections = collections_by_type.pop("config", [])
        config = config_collections[0].raw_data if config_collections else {}
        if not config:
            logger.warning("No config collection found for summary date")

        # Merge hourly rollups into daily rollups
        daily_rollup, missing_hours = _merge_hourly_rollups(collections_by_type)

        # Append dashboard telemetry
        daily_rollup["dashboard_telemetry"] = _aggregate_dashboard_telemetry(summary_date)

        # Save daily summary and update hourly collection status
        daily_summary, created, hourly_collections_count = _save_daily_summary(
            summary_date,
            daily_rollup,
            collections_by_type,
            config,
            missing_hours,
            kwargs.get("execution_id"),
        )

        action = "Created" if created else "Updated"
        log_task_execution(
            "daily_metrics_rollup",
            "completed",
            f"{action} daily rollup ID: {daily_summary.id} with {hourly_collections_count} hourly collections",
        )

        return create_task_result(
            "success",
            {
                "task_type": "daily_metrics_rollup",
                "summary_id": daily_summary.id,
                "summary_date": str(summary_date),
                "hourly_collections_count": hourly_collections_count,
                "missing_hours": missing_hours,
                "aggregated_collectors": list(daily_rollup.keys()),
                "created": created,  # True if new record, False if updated existing
            },
        )

    except Exception as e:
        logger.exception(f"Error in daily_metrics_rollup: {str(e)}")
        return create_task_result("error", error=f"Rollup failed: {str(e)}")
