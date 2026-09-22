"""
Hourly metrics collector for time-series data.

Collects metrics for a specific hour, computes rollup statistics,
and stores in HourlyMetricsCollection.
"""

import logging
import math
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from ..utils import create_task_result, generic_collect_metrics, get_db_connection, parse_datetime_string

logger = logging.getLogger(__name__)


def _get_hourly_collectors():
    """
    Get hourly collectors registry with lazy imports.

    Lazy imports prevent metrics_utility dependency from breaking
    unrelated task registration (e.g., hello_world, cleanup_old_tasks).
    """
    from metrics_utility.anonymized_rollups import (
        CredentialsAnonymizedRollup,
        EventModulesAnonymizedRollup,
        JobHostSummaryAnonymizedRollup,
        JobsAnonymizedRollup,
    )
    from metrics_utility.library.collectors.controller import (
        credentials_service,
        job_host_summary_service,
        main_jobevent_service,
        unified_jobs_dashboard,
    )

    # Registry mapping collector_type to (collector_func, rollup_processor_class)
    return {
        "job_host_summary_service": {
            "collector_func": job_host_summary_service,
            "rollup_processor": JobHostSummaryAnonymizedRollup,
            "description": "Job host summary metrics (partition-optimized)",
            "post_collect_hook_factory": _build_dashboard_host_summary_sync_hook,
        },
        "unified_jobs": {
            # unified_jobs_dashboard extends the base unified_jobs query with dashboard-specific
            # fields (project_id/name, launched_by, label_ids, num_hosts); the registry key is
            # kept as "unified_jobs" for backwards-compat with task scheduling and API references.
            "collector_func": unified_jobs_dashboard,
            "rollup_processor": JobsAnonymizedRollup,
            "description": "Unified jobs metrics",
            "post_collect_hook_factory": _build_dashboard_sync_hook,
        },
        "credentials_service": {
            "collector_func": credentials_service,
            "rollup_processor": CredentialsAnonymizedRollup,
            "description": "Credentials usage metrics",
        },
        "main_jobevent_service": {
            "collector_func": main_jobevent_service,
            "rollup_processor": EventModulesAnonymizedRollup,
            "description": "Job events (event modules) metrics",
        },
    }


def collect_hourly_metrics(**kwargs) -> dict[str, Any]:
    """
    Collect hourly metrics for a specific collector type.

    This function handles all time-series collectors that gather data
    for a specific hour window. It collects raw data, computes rollup
    statistics, and stores only the rollup in HourlyMetricsCollection.

    Args:
        **kwargs: Task data containing:
            - collector_type (str): Type of collector (required)
            - hour_timestamp (str): ISO timestamp for the hour to collect (optional, defaults to previous hour)
            - database (str): Database name (default: 'awx')

    Returns:
        dict: Task result with collection status and record ID
    """
    collector_type = kwargs.pop("collector_type", None)
    if not collector_type:
        return create_task_result("error", error="collector_type parameter is required")

    # Extract optional execution_id for linking to TaskExecution
    execution_id = kwargs.get("execution_id")  # Available when called via execute_db_task

    # Determine hour to collect (default to previous full hour)
    hour_timestamp_str = kwargs.get("hour_timestamp")
    if hour_timestamp_str:
        hour_timestamp = parse_datetime_string(hour_timestamp_str)
        if hour_timestamp is None:
            return create_task_result("error", error=f"Invalid hour_timestamp format: {hour_timestamp_str}")
    else:
        # Fallback only — the cron scheduler pins hour_timestamp into task_data
        # at dispatch time via _inject_dispatch_timestamps(), so retries reuse
        # the original window instead of recomputing from now.
        now = timezone.now()
        hour_timestamp = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    start_datetime = hour_timestamp
    end_datetime = start_datetime + timedelta(hours=1)

    # Get database connection
    db_connection = get_db_connection()

    collector_registry = _get_hourly_collectors()
    hook_factory = collector_registry.get(collector_type, {}).get("post_collect_hook_factory")
    post_collect_hook = hook_factory(start_datetime) if hook_factory else None

    collector_kwargs: dict[str, Any] = {"since": start_datetime, "until": end_datetime}
    if collector_type == "main_jobevent_service":
        collector_kwargs["row_limit"] = settings.JOBEVENT_ROW_LIMIT
        collector_kwargs["job_limit"] = settings.JOBEVENT_JOB_LIMIT

    # Use generic collector with hourly-specific time window
    return generic_collect_metrics(
        collector_type=collector_type,
        collector_registry=collector_registry,
        collection_mode="hourly",
        timestamp=start_datetime,
        db_connection=db_connection,
        collector_kwargs=collector_kwargs,
        task_execution_id=execution_id,
        post_collect_hook=post_collect_hook,
    )


_SYNC_TASK_CHUNK_SIZE = 500  # max jobs per sync_dashboard_job_records task
_HOST_SUMMARY_RECORD_CHUNK_SIZE = 2000  # max host summary records per sync_dashboard_host_summaries task

_HOST_SUMMARY_COLUMNS = ("id", "host_name", "host_remote_id", "job_remote_id")

_DASHBOARD_COLUMNS = [
    "id",
    "name",
    "unified_job_template_id",
    "organization_id",
    "organization_name",
    "started",
    "finished",
    "status",
    "elapsed",
    "launched_by_id",
    "launched_by_username",
    "project_id",
    "project_name",
    "created",
    "modified",
    "label_ids",
    "num_hosts",
]


_INT_FIELDS = ("id", "organization_id", "unified_job_template_id", "launched_by_id", "project_id", "num_hosts")
_STRING_FIELDS = ("name", "organization_name", "status", "launched_by_username", "project_name", "label_ids")


def _is_nan(value: Any) -> bool:
    """Return whether a value is a native float NaN requiring JSON normalization."""
    return isinstance(value, float) and math.isnan(value)


def _serialize_dashboard_datetime_fields(row: dict) -> None:
    for field in ("started", "finished", "created", "modified"):
        val = row.get(field)
        if val is not None and hasattr(val, "isoformat"):
            row[field] = val.isoformat()


def _serialize_dashboard_integer_fields(row: dict) -> None:
    # Pandas upcasts nullable integer columns to float64 when NaN is present.
    # .where(notna(), other=None) can leave float NaN, so guard before int(nan).
    for field in _INT_FIELDS:
        val = row.get(field)
        if val is None or _is_nan(val):
            row[field] = None
        else:
            row[field] = int(val)


def _serialize_dashboard_string_fields(row: dict) -> None:
    # Entirely missing string columns can also arrive as float NaN from pandas.
    for field in _STRING_FIELDS:
        val = row.get(field)
        if _is_nan(val):
            row[field] = None


def _serialize_dashboard_elapsed(row: dict) -> None:
    val = row.get("elapsed")
    if val is None or _is_nan(val):
        row["elapsed"] = None
    else:
        row["elapsed"] = float(val)


def _serialize_dashboard_record(row: dict) -> None:
    """Coerce all non-serializable numpy/pandas types to Python natives in-place.

    pandas .to_dict("records") preserves numpy dtypes (numpy.int64, numpy.float64) for
    columns with no NaN values. DjangoJSONEncoder does not handle these, so storing raw
    records in Task.task_data would raise TypeError. Nullable numeric and string fields
    are normalized to None when pandas represents missing values as float NaN.
    """
    _serialize_dashboard_datetime_fields(row)
    _serialize_dashboard_integer_fields(row)
    _serialize_dashboard_string_fields(row)
    _serialize_dashboard_elapsed(row)


def _serialize_host_summary_record(row: dict) -> dict:
    """Coerce numpy/pandas types to Python natives for a single host summary row.

    id and job_remote_id are always non-null integers from main_jobhostsummary.
    host_remote_id (the AWX DB column name) is read from the input row and written
    as host_id in the result — the canonical wire/model field name used by
    _sync_host_summaries. host_id is nullable (None when the AWX host record has
    been deleted).
    """
    result: dict = {}
    for field in ("id", "job_remote_id"):
        val = row.get(field)
        result[field] = None if (val is None or _is_nan(val)) else int(val)
    # host_remote_id (AWX column) → host_id (canonical wire/model field name).
    val = row.get("host_remote_id")
    result["host_id"] = None if (val is None or _is_nan(val)) else int(val)
    result["host_name"] = str(row["host_name"]) if row.get("host_name") is not None else None
    return result


def _group_host_summary_rows(rows: list[dict]) -> dict[int, list]:
    """Group serialised host-summary rows by job_remote_id, dropping rows with a null job id."""
    by_job: dict[int, list] = {}
    for row in rows:
        record = _serialize_host_summary_record(row)
        job_id = record.get("job_remote_id")
        if job_id is None:
            continue
        by_job.setdefault(job_id, []).append(record)
    return by_job


def _build_host_summary_task_chunks(by_job: dict[int, list], hour_ts_str: str) -> dict[str, list]:
    """Partition host-summary records into named chunks for Task scheduling.

    All records for a single job stay in the same chunk because _sync_host_summaries
    deletes stale records — splitting a job across chunks would cause data loss.
    """
    chunks: dict[str, list] = {}
    chunk_index = 0
    current_chunk: list = []

    for job_records in by_job.values():
        if current_chunk and len(current_chunk) + len(job_records) > _HOST_SUMMARY_RECORD_CHUNK_SIZE:
            name = f"sync_dashboard_host_summaries_{hour_ts_str}_{chunk_index}"
            chunks[name] = current_chunk
            chunk_index += 1
            current_chunk = []
        current_chunk.extend(job_records)

    # Flush the final (or only) chunk — always non-empty because by_job is non-empty.
    name = f"sync_dashboard_host_summaries_{hour_ts_str}_{chunk_index}"
    chunks[name] = current_chunk
    return chunks


def _build_dashboard_host_summary_sync_hook(hour_timestamp):
    """Return a post_collect_hook that schedules sync_dashboard_host_summaries tasks.

    Reuses the raw DataFrame from job_host_summary_service to incrementally write
    JobHostSummary records for post-backfill jobs without adding extra Controller
    DB queries. Only schedules tasks when the DASHBOARD_COLLECTION feature flag is enabled.
    """
    # Lazy import: circular dependency (collect_hourly_metrics → task_groups → tasks → collect_hourly_metrics).
    from apps.tasks.task_groups import get_feature_enabled_from_db

    # The flag is intentionally evaluated at hook-build time (when the hourly task starts),
    # not when the hook executes after gather(). A flag change mid-task takes effect on the
    # next collection cycle. This is acceptable — the window is the duration of one gather() call.
    if not get_feature_enabled_from_db("DASHBOARD_COLLECTION"):
        return None

    def hook(raw_data):
        if raw_data is None or raw_data.empty:
            return

        available = [c for c in _HOST_SUMMARY_COLUMNS if c in raw_data.columns]
        missing = set(_HOST_SUMMARY_COLUMNS) - set(available)
        if missing:
            logger.warning(
                "job_host_summary_service is missing expected columns for dashboard sync: %s",
                sorted(missing),
            )
            return

        subset = raw_data[list(available)]
        rows = subset.where(subset.notna(), other=None).to_dict("records")
        by_job = _group_host_summary_rows(rows)
        if not by_job:
            return

        # Lazy import inside closure: Task must be resolved at call time so tests can
        # patch apps.tasks.models.Task after the hook is built (same circular-import reason).
        from apps.tasks.models import Task

        hour_ts_str = hour_timestamp.isoformat()
        chunks = _build_host_summary_task_chunks(by_job, hour_ts_str)
        new_names: set[str] = set()
        for chunk_index, (chunk_name, chunk) in enumerate(chunks.items()):
            new_names.add(chunk_name)
            Task.objects.update_or_create(
                name=chunk_name,
                defaults={
                    "description": (
                        f"Sync dashboard host summary records from job_host_summary_service"
                        f" for {hour_ts_str} (chunk {chunk_index})"
                    ),
                    "function_name": "sync_dashboard_host_summaries",
                    "task_data": {
                        "raw_host_summaries": chunk,
                        "hour_timestamp": hour_ts_str,
                        "_feature_flag": "DASHBOARD_COLLECTION",
                    },
                    "is_system_task": False,
                },
            )
        # Remove pending chunks from a prior run that exceed the current chunk count.
        if not new_names:
            return
        Task.objects.filter(
            name__startswith=f"sync_dashboard_host_summaries_{hour_ts_str}_",
            status="pending",
        ).exclude(name__in=new_names).delete()

    return hook


def _build_dashboard_sync_hook(hour_timestamp):
    """
    Return a post_collect_hook that schedules a sync_dashboard_job_records task.

    The hook serialises the filtered raw unified_jobs DataFrame into task_data so the
    follow-up task can write to JobData without re-querying the Controller DB.
    Only schedules the task when the DASHBOARD_COLLECTION feature flag is enabled.
    """
    # Lazy import: circular dependency (collect_hourly_metrics → task_groups → tasks → collect_hourly_metrics).
    from apps.tasks.task_groups import get_feature_enabled_from_db

    # The flag is intentionally evaluated at hook-build time (when the hourly task starts),
    # not when the hook executes after gather(). A flag change mid-task takes effect on the
    # next collection cycle. This is acceptable — the window is the duration of one gather() call.
    if not get_feature_enabled_from_db("DASHBOARD_COLLECTION"):
        return None

    def hook(raw_data):
        if raw_data is None or raw_data.empty:
            return

        mask = raw_data["status"].isin(["failed", "successful"]) & (~raw_data["launch_type"].isin(["sync", "workflow"]))
        filtered = raw_data[mask]
        if filtered.empty:
            return

        # unified_jobs_dashboard guarantees these columns; guard with available
        # in case a future schema change temporarily drops one.
        available = [c for c in _DASHBOARD_COLUMNS if c in filtered.columns]
        missing = set(_DASHBOARD_COLUMNS) - set(available)
        if missing:
            logger.warning(
                "unified_jobs_dashboard is missing expected columns: %s",
                sorted(missing),
            )
        subset = filtered[available]
        records = subset.where(subset.notna(), other=None).to_dict("records")
        for row in records:
            _serialize_dashboard_record(row)

        # Lazy import inside closure: Task must be resolved at call time so tests can
        # patch apps.tasks.models.Task after the hook is built (same circular-import reason).
        from apps.tasks.models import Task

        hour_ts_str = hour_timestamp.isoformat()
        new_names: set[str] = set()
        for chunk_index, start in enumerate(range(0, len(records), _SYNC_TASK_CHUNK_SIZE)):
            chunk = records[start : start + _SYNC_TASK_CHUNK_SIZE]
            chunk_name = f"sync_dashboard_jobs_{hour_ts_str}_{chunk_index}"
            new_names.add(chunk_name)
            Task.objects.update_or_create(
                name=chunk_name,
                defaults={
                    "description": f"Sync dashboard job records from unified_jobs for {hour_ts_str} (chunk {chunk_index})",
                    "function_name": "sync_dashboard_job_records",
                    "task_data": {
                        "raw_jobs": chunk,
                        "hour_timestamp": hour_ts_str,
                        "_feature_flag": "DASHBOARD_COLLECTION",
                    },
                    "is_system_task": False,
                },
            )
        # Remove pending chunks from a prior run that exceed the current chunk count.
        if not new_names:
            return
        Task.objects.filter(
            name__startswith=f"sync_dashboard_jobs_{hour_ts_str}_",
            status="pending",
        ).exclude(name__in=new_names).delete()

    return hook
