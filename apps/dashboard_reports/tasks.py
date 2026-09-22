"""
Background tasks for dashboard reports data collection and cleanup.

Provides five dispatcherd tasks:
- collect_dashboard_reports_initial_data: full historical backfill (window from the Controller's retention schedule, default 90 days)
- collect_dashboard_reports_data: incremental sync from last known timestamp (deprecated)
- sync_dashboard_job_records: writes unified_jobs data from the hourly hook to JobData
- sync_dashboard_host_summaries: writes host summary data from the hourly hook to JobHostSummary
- cleanup_dashboard_reports_old_data: removes JobData records beyond retention period
"""

import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from metrics_utility.library.collectors.dashboard import (
    DashboardJobsResultType,
    dashboard_jobs,
)

from apps.dashboard_reports.awx_queries import fetch_retention_settings
from apps.dashboard_reports.models import DashboardTelemetry, JobData, JobHostSummary
from apps.tasks.utils import create_task_result, get_db_connection, log_task_execution

DEFAULT_AWX_DB_NAME = "awx"
DEFAULT_RETENTION_DAYS = 90

logger = logging.getLogger(__name__)


class _PartialSyncRollbackError(Exception):
    """Sentinel raised inside a transaction.atomic() block to roll back all saves when any job fails to sync."""


_MISSING = object()


def _get_renamed_kwarg(kwargs: dict, new_key: str, old_key: str, task_name: str) -> Any:
    """Look up ``new_key`` in kwargs, falling back to the deprecated ``old_key`` with a warning.

    Operators dispatch these tasks with free-form ``task_data`` via the task API, so a
    stale kwarg name (from an old runbook or script) would otherwise be silently dropped
    and replaced by whatever default the caller applies. Returns ``_MISSING`` if neither
    key is present.
    """
    if new_key in kwargs:
        return kwargs[new_key]
    if old_key in kwargs:
        logger.warning(f"{task_name}: '{old_key}' is deprecated; use '{new_key}' instead")
        return kwargs[old_key]
    return _MISSING


def _use_controller_retention() -> bool:
    """Return True when DASHBOARD_COLLECTION['USE_CONTROLLER_RETENTION'] is set in settings."""
    dashboard_cfg = getattr(settings, "DASHBOARD_COLLECTION", None) or {}
    return bool(dashboard_cfg.get("USE_CONTROLLER_RETENTION", True))


def get_retention_days(db_name: str = DEFAULT_AWX_DB_NAME) -> int:
    """
    Return the effective retention period in days derived from active AWX cleanup schedules.

    Connects to the AWX database and queries
    ``cleanup_jobs`` system job schedules (which govern job-run retention),
    then applies the most aggressive (lowest) retention policy:

    - Only enabled schedules (``schedule_enabled = True``) are considered.
    - Schedules whose ``retention_days`` is missing or not a valid integer are skipped
      individually, so one malformed schedule doesn't affect the others.
    - When multiple schedules exist, the one with the lowest ``retention_days`` wins;
      ties are broken by the soonest ``next_run``.
    - Falls back to ``DEFAULT_RETENTION_DAYS`` if the query fails or no valid active rows remain.

    Always returns a valid int, so callers don't need to re-validate the return value.

    Note: ``cleanup_activitystream`` schedules are intentionally excluded — they govern
    a different resource (activity stream entries) and must not drive ``JobData`` retention.
    """
    try:
        db_connection = get_db_connection(db_name)
        retention_data, _ = fetch_retention_settings(db_connection=db_connection)
    except Exception:
        logger.exception(f"Error fetching retention settings; using default {DEFAULT_RETENTION_DAYS} days")
        return DEFAULT_RETENTION_DAYS

    active = _active_retention_schedules(retention_data)
    if not active:
        return DEFAULT_RETENTION_DAYS

    return _select_retention_schedule(active)["retention_days"]


def _parse_dt(value: Any) -> datetime | None:
    """Coerce value to a timezone-aware datetime.

    - None      → None
    - str       → parsed via fromisoformat; naive result is assumed UTC
    - datetime  → returned unchanged if tz-aware; naive datetime is localised to UTC
    - other     → raises TypeError so callers receive a structured error
    """
    if value is None:
        return None
    if isinstance(value, str):
        dt = None if value == "NaT" else datetime.fromisoformat(value)  # NaT: pandas null serialised as string
        return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    raise TypeError(f"_parse_dt: expected str, datetime, or None; got {type(value).__name__!r}")


def _active_retention_schedules(retention_data: list[dict]) -> list[dict]:
    """Return enabled cleanup-job schedules with valid integer retention periods."""
    active = []
    for row in retention_data:
        if row.get("job_type") != "cleanup_jobs" or not row.get("schedule_enabled"):
            continue
        raw_days = row.get("retention_days")
        if raw_days is None:
            continue
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            logger.warning(f"get_retention_days: skipping schedule with invalid retention_days={raw_days!r}")
            continue
        active.append({**row, "retention_days": days})
    return active


def _select_retention_schedule(active: list[dict]) -> dict:
    """Select the most aggressive active schedule, breaking ties by the soonest next run."""
    best: dict = active[0]
    for row in active[1:]:
        if row["retention_days"] < best["retention_days"] or (
            row["retention_days"] == best["retention_days"] and _has_earlier_next_run(row, best)
        ):
            best = row
    return best


def _has_earlier_next_run(row: dict, best: dict) -> bool:
    """Return whether ``row`` wins a retention tie by its next-run timestamp."""
    try:
        row_next = _parse_dt(row.get("next_run"))
        best_next = _parse_dt(best.get("next_run"))
    except TypeError:
        return False
    return row_next is not None and (best_next is None or row_next < best_next)


def _collect_jobs(
    db_connection, since: datetime, until: datetime, after_id: int | None = None, batch_size: int | None = None
) -> DashboardJobsResultType:
    """
    Collect dashboard jobs data from the database for the specified date range.

    When *after_id* and *batch_size* are both supplied the underlying query uses
    cursor-based pagination (``id > after_id ORDER BY id LIMIT batch_size``) and
    labels/host-summaries are fetched by the returned job IDs rather than the
    full date range, which is far more efficient for large backfills.
    """
    return dashboard_jobs(
        db=db_connection, since=since, until=until, after_id=after_id, batch_size=batch_size, date_field="finished"
    ).gather()


def _get_job_id_range(db_connection, since: datetime, until: datetime) -> tuple:
    """Return (min_id, max_id) for jobs matching the backfill filter, or (None, None) if empty."""
    # Lazy import: keeps metrics_utility optional at module level so unrelated tasks
    # (hello_world, cleanup_old_tasks) can be registered without the dependency installed.
    from metrics_utility.library.collectors.dashboard import get_min_max_job_id_query

    query, params = get_min_max_job_id_query(since, until, date_field="finished")
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _sync_jobs_atomically(job_results: list) -> list:
    """
    Persist all jobs in a single atomic transaction.

    Returns the list of job IDs that failed to sync.  If any job fails every
    save made in this call is rolled back, keeping JobData.last_timestamp()
    unchanged so the next incremental run retries from the same watermark.


    Note: retry behaviour for the outer Task is handled by the Task model's max_attempts
    mechanism (default: 3 attempts). When this function signals failure the calling task
    returns create_task_result("error"), which marks the Task as failed and allows the
    scheduler to retry it automatically up to max_attempts times via Task.can_retry().
    """
    failed_jobs: list = []
    try:
        with transaction.atomic():
            for job in job_results:
                try:
                    JobData.create_or_update_from_awx(job)
                except Exception as e:
                    logger.exception(f"Error creating/updating JobData for job {job['id']}: {str(e)}")
                    failed_jobs.append(job["id"])
            if failed_jobs:
                raise _PartialSyncRollbackError()
    except _PartialSyncRollbackError:
        pass  # transaction rolled back; caller inspects failed_jobs
    return failed_jobs


def _resolve_collection_params(task_name: str, kwargs: dict) -> tuple[str, datetime, datetime, int]:
    """Resolve db_name, since, until, and batch_size from task kwargs with defaults applied.

    When ``since`` is not provided and no prior ``JobData`` timestamp exists, the backfill
    window defaults to ``DEFAULT_RETENTION_DAYS`` (90 days). When the
    ``DASHBOARD_COLLECTION['USE_CONTROLLER_RETENTION']`` setting is enabled, the window is
    derived from active AWX cleanup_jobs schedules via ``get_retention_days`` instead.
    """
    dashboard_cfg = getattr(settings, "DASHBOARD_COLLECTION", None) or {}
    db_name = _get_renamed_kwarg(kwargs, "awx_database", "database", task_name)
    db_name = DEFAULT_AWX_DB_NAME if db_name is _MISSING else db_name
    until = _parse_dt(kwargs.get("until")) or datetime.now(tz=UTC)
    # Use MAX(finished) as the watermark because all collection queries use date_field='finished'.
    # This aligns the watermark with the query filter so the Controller DB finished index is
    # used correctly and no jobs are missed in the gap between MAX(finished) and MAX(awx_modified).
    since = _parse_dt(kwargs.get("since")) or JobData.last_finished_timestamp()
    if since is None:
        if _use_controller_retention():
            retention_days = get_retention_days(db_name)
            if retention_days <= 0:
                raise ValueError(f"Retention days must be > 0, got {retention_days!r}")
        else:
            retention_days = DEFAULT_RETENTION_DAYS
        since = (
            (until - timedelta(days=retention_days)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        )
    raw_batch_size = dashboard_cfg.get("BACKFILL_BATCH_SIZE", 5_000)
    try:
        batch_size = int(raw_batch_size)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"DASHBOARD_COLLECTION.BACKFILL_BATCH_SIZE must be a positive integer, got {raw_batch_size!r}"
        ) from e
    if batch_size <= 0:
        raise ValueError("DASHBOARD_COLLECTION.BACKFILL_BATCH_SIZE must be > 0")
    return db_name, since, until, batch_size


def _process_batches(
    db_connection, since: datetime, until: datetime, max_id: int, after_id: int, batch_size: int, task_name: str
) -> tuple[int, str | None]:
    """Collect and sync jobs in cursor-paginated batches. Returns (total_synced, error_message_or_None)."""
    total_synced = 0
    while after_id < max_id:
        try:
            batch = _collect_jobs(db_connection, since=since, until=until, after_id=after_id, batch_size=batch_size)
        except Exception as e:
            logger.exception(f"Error collecting jobs batch after id {after_id}")
            return total_synced, f"Collecting jobs failed: {str(e)}"

        if not batch["results"]:
            break

        failed_jobs = _sync_jobs_atomically(batch["results"])
        if failed_jobs:
            return total_synced, f"Failed to sync {len(failed_jobs)} job(s): {failed_jobs}"

        batch_count = batch["count"]
        total_synced += batch_count
        after_id = max(j["id"] for j in batch["results"])
        log_task_execution(
            task_name=task_name,
            operation="processing",
            details=f"Synced batch of {batch_count} jobs (total so far: {total_synced}, cursor id: {after_id})",
        )
    return total_synced, None


def _collect_data(task_name: str, **kwargs) -> dict[str, Any]:
    """
    Core data collection logic for dashboard reports.

    Fetches jobs in cursor-paginated batches (default 10 000 records each) to
    keep memory usage and DB transaction times bounded regardless of window size.
    Each batch is committed independently so progress is preserved if the task
    is interrupted and retried.
    """
    result = {
        "error": False,
        "message": "",
        "data": {"task_type": task_name, "date_range": {"start": None, "end": None}, "job_count": 0},
    }
    try:
        db_name, since, until, batch_size = _resolve_collection_params(task_name, kwargs)
    except ValueError as e:
        result["error"] = True
        result["message"] = str(e)
        return result

    if since >= until:
        msg = f"Invalid date range: since ({since.isoformat()}) must be before until ({until.isoformat()})"
        logger.error(msg)
        result["error"] = True
        result["message"] = msg
        return result

    start_str = since.isoformat()
    end_str = until.isoformat()

    log_task_execution(
        task_name=task_name, operation="processing", details=f"Collecting dashboard data for: {start_str} to {end_str}"
    )

    try:
        db_connection = get_db_connection(db_name)
        min_id, max_id = _get_job_id_range(db_connection, since, until)
    except Exception as e:
        logger.exception("Database error during collection setup")
        result["error"] = True
        result["message"] = f"Database error: {str(e)}"
        return result

    if min_id is None:
        log_task_execution(task_name=task_name, operation="completed", details="No jobs found in date range")
        result["data"] = {"task_type": task_name, "date_range": {"start": start_str, "end": end_str}, "job_count": 0}
        return result

    after_id = min_id - 1
    total_synced, error_msg = _process_batches(db_connection, since, until, max_id, after_id, batch_size, task_name)

    if error_msg:
        result["error"] = True
        result["message"] = error_msg
        return result

    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=f"Collected and stored {total_synced} jobs from {start_str} to {end_str}",
    )
    result["data"] = {
        "task_type": task_name,
        "date_range": {"start": start_str, "end": end_str},
        "job_count": total_synced,
    }
    return result


def _save_telemetry_details(
    task_name: str,
    success: bool,
    collection_duration_ms: float,
    number_of_records_processed: int,
    database_query_time_ms: float | None,
    cache_hit_rate: float | None,
) -> None:
    try:
        DashboardTelemetry.objects.create(
            task_name=task_name,
            collection_run_date=datetime.now(UTC).date(),
            success=success,
            collection_duration_ms=collection_duration_ms,
            number_of_records_processed=number_of_records_processed,
            database_query_time_ms=database_query_time_ms,
            cache_hit_rate=cache_hit_rate,
        )
    except Exception:
        logger.exception("Failed to record dashboard telemetry")


def collect_dashboard_reports_initial_data(**kwargs) -> dict[str, Any]:
    """
    Collect historical AWX job data as a one-time backfill.

    The retention window is determined by ``get_retention_days`` (queried from active AWX
    cleanup schedules), falling back to ``DEFAULT_RETENTION_DAYS`` when the AWX DB is
    unreachable or no active schedules exist. The window can be overridden by passing
    ``since`` or ``until`` in kwargs.

    Records telemetry for each run via ``DashboardTelemetry``.
    Returns a task result dict with status, date range, job count, and any error details.
    """
    task_name = "collect_dashboard_reports_initial_data"
    start_time = time.monotonic()
    result = _collect_data(task_name=task_name, **kwargs)

    error = result.get("error", False)

    duration_ms = (time.monotonic() - start_time) * 1000
    records_processed = result.get("data", {}).get("job_count", 0)

    _save_telemetry_details(
        task_name=task_name,
        success=not error,
        collection_duration_ms=duration_ms,
        number_of_records_processed=records_processed,
        database_query_time_ms=None,  # DB time not tracked for batched backfill
        cache_hit_rate=None,
    )

    if error:
        return create_task_result(
            "error", error=result.get("message", "An unknown error occurred during initial data collection")
        )

    return create_task_result("success", data=result.get("data", {}))


def collect_dashboard_reports_data(**kwargs) -> dict[str, Any]:
    """
    Incrementally collect AWX job data since the last known JobData timestamp.

    .. deprecated::
        No longer registered as a scheduled system task. Ongoing incremental sync is driven
        automatically by the hourly_unified_jobs hook (see _build_dashboard_sync_hook).
        This function remains available for manual operator-triggered collection via the task API.

    Falls back to 90 days ago if no previous records exist.
    Returns a task result dict with status, date range, job count, and any error details.
    """
    task_name = "collect_dashboard_reports_data"
    result = _collect_data(task_name=task_name, **kwargs)
    error = result.get("error", False)

    if error:
        return create_task_result(
            "error", error=result.get("message", "An unknown error occurred during incremental data collection")
        )
    return create_task_result("success", data=result.get("data", {}))


def sync_dashboard_job_records(**kwargs) -> dict[str, Any]:
    """
    Write unified_jobs raw data collected during the hourly rollup to the dashboard JobData table.

    Scheduled automatically by the hourly_unified_jobs collection hook so no extra Controller
    DB queries are needed — the raw data is passed in task_data.
    """
    task_name = "sync_dashboard_job_records"
    hour_timestamp = kwargs.get("hour_timestamp", "unknown")
    raw_jobs = kwargs.get("raw_jobs", [])
    start_time = time.monotonic()

    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"Syncing {len(raw_jobs)} dashboard job records for {hour_timestamp}",
    )

    assembled = []
    for row in raw_jobs:
        if "label_ids" not in row:
            # Column was absent in collected data (older metrics_utility without label support).
            # Use None to signal that create_or_update_from_awx should preserve existing labels
            # rather than clearing them.
            labels: list[int] | None = None
        else:
            label_ids_raw = row["label_ids"]
            if label_ids_raw is None or (isinstance(label_ids_raw, float) and math.isnan(label_ids_raw)):
                labels = []
            elif isinstance(label_ids_raw, str):
                labels = [int(x.strip()) for x in label_ids_raw.split(",") if x.strip()]
            else:
                labels = [int(label_ids_raw)]
        assembled.append(
            {
                "id": row["id"],
                "name": row["name"],
                "unified_job_template_id": row.get("unified_job_template_id"),
                "organization_id": row.get("organization_id"),
                "organization_name": row.get("organization_name"),
                "started": _parse_dt(row.get("started")),
                "finished": _parse_dt(row.get("finished")),
                "status": row["status"],
                "elapsed": row["elapsed"],
                "launched_by_id": row.get("launched_by_id"),
                "launched_by_username": row.get("launched_by_username"),
                "project_id": row.get("project_id"),
                "project_name": row.get("project_name"),
                "created": _parse_dt(row.get("created")),
                "modified": _parse_dt(row.get("modified")),
                "labels": labels,
                # host_summaries=None signals create_or_update_from_awx to leave
                # existing JobHostSummary records intact (created by initial backfill).
                "host_summaries": None,
                "num_hosts": row.get("num_hosts") or 0,
            }
        )

    failed_jobs = _sync_jobs_atomically(assembled)

    duration_ms = (time.monotonic() - start_time) * 1000
    success = not failed_jobs
    _save_telemetry_details(
        task_name=task_name,
        success=success,
        collection_duration_ms=duration_ms,
        number_of_records_processed=len(assembled) - len(failed_jobs),
        database_query_time_ms=None,
        cache_hit_rate=None,
    )

    if failed_jobs:
        return create_task_result("error", error=f"Failed to sync {len(failed_jobs)} job(s): {failed_jobs}")

    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=f"Synced {len(assembled)} job records for {hour_timestamp}",
    )
    return create_task_result(
        "success", data={"task_type": task_name, "job_count": len(assembled), "hour_timestamp": hour_timestamp}
    )


def sync_dashboard_host_summaries(**kwargs) -> dict[str, Any]:
    """Write job_host_summary_service data to JobHostSummary records in the dashboard tables.

    Scheduled automatically by the hourly job_host_summary_service hook so no extra Controller
    DB queries are needed — the raw data is passed in task_data. Jobs not present in JobData
    (e.g. sync-type or non-terminal jobs filtered out by the dashboard collector) are silently
    skipped.
    """
    task_name = "sync_dashboard_host_summaries"
    hour_timestamp = kwargs.get("hour_timestamp", "unknown")
    raw_host_summaries = kwargs.get("raw_host_summaries", [])
    start_time = time.monotonic()

    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"Syncing host summaries for {len(raw_host_summaries)} records ({hour_timestamp})",
    )

    # Group by job_remote_id; the hook's serializer already maps host_remote_id → host_id.
    by_job: dict[int, list] = {}
    for row in raw_host_summaries:
        job_id = row.get("job_remote_id")
        if job_id is None:
            continue
        summary_id = row.get("id")
        if summary_id is None:
            continue
        by_job.setdefault(job_id, []).append(
            {
                "id": summary_id,
                "host_id": row.get("host_id"),
                "host_name": row.get("host_name"),
            }
        )

    # Batch-fetch all matching JobData and their existing JobHostSummary records in two queries
    # rather than 2N queries (one get + one filter per job).
    job_data_by_id: dict[int, Any] = {}
    existing_by_job_data_pk: dict[int, dict] = {}
    if by_job:
        job_data_by_id = {jd.job_id: jd for jd in JobData.objects.filter(job_id__in=by_job.keys())}
        for hs in JobHostSummary.objects.filter(job_data__in=job_data_by_id.values()):
            existing_by_job_data_pk.setdefault(hs.job_data_id, {})[hs.host_summary_id] = hs

    synced = 0
    failed = 0
    for job_remote_id, host_summaries in by_job.items():
        job_data = job_data_by_id.get(job_remote_id)
        if job_data is None:
            continue  # sync/non-terminal job — no matching JobData record
        try:
            with transaction.atomic():
                existing = existing_by_job_data_pk.get(job_data.pk, {})
                JobData._sync_host_summaries(job_data, host_summaries, existing)
            synced += 1
        except Exception:
            logger.exception(f"Error syncing host summaries for job {job_remote_id}")
            failed += 1

    duration_ms = (time.monotonic() - start_time) * 1000
    success = not failed
    _save_telemetry_details(
        task_name=task_name,
        success=success,
        collection_duration_ms=duration_ms,
        number_of_records_processed=synced,
        database_query_time_ms=None,
        cache_hit_rate=None,
    )

    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=f"Synced host summaries for {synced} jobs, {failed} failed ({hour_timestamp})",
    )
    if failed:
        return create_task_result(
            "error",
            data={"task_type": task_name, "job_count": synced, "failed": failed, "hour_timestamp": hour_timestamp},
        )
    return create_task_result(
        "success", data={"task_type": task_name, "job_count": synced, "hour_timestamp": hour_timestamp}
    )


def _normalize_retention_days(task_name: str, retention_days: Any) -> tuple[int, dict[str, Any] | None]:
    """Validate a retention_days value shared by the cleanup tasks below.

    Returns ``(retention_days, None)`` on success, or ``(0, error_result)`` when the value
    can't be coerced to an int, or is <= 0 — a cutoff of "now" would silently delete every
    row, so this is treated as an error rather than clamped. Callers must check
    ``error_result`` before using the returned int.
    """
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        logger.error(f"{task_name}: retention_days={retention_days!r} is not a valid integer; aborting cleanup")
        return 0, create_task_result("error", error=f"Invalid retention_days value: {retention_days!r}")
    if retention_days <= 0:
        logger.error(f"{task_name}: retention_days={retention_days} must be > 0; aborting cleanup")
        return 0, create_task_result("error", error=f"retention_days must be > 0, got {retention_days!r}")
    return retention_days, None


def cleanup_dashboard_reports_old_data(**kwargs) -> dict[str, Any]:
    """
    Delete JobData records with a finished date older than retention_days.

    The default retention period is resolved from active AWX ``cleanup_jobs`` schedules via
    ``get_retention_days`` (lowest ``retention_days`` across enabled schedules), falling back
    to ``DEFAULT_RETENTION_DAYS`` when the AWX DB is unreachable or no active schedules exist.
    The caller may override this default by passing ``retention_days`` in kwargs.

    Returns a task result dict with the number of deleted records, cutoff date, and any error details.
    """
    task_name = "cleanup_dashboard_reports_old_data"
    db_name = _get_renamed_kwarg(kwargs, "awx_database", "database", task_name)
    db_name = DEFAULT_AWX_DB_NAME if db_name is _MISSING else db_name
    retention_days = _get_renamed_kwarg(kwargs, "retention_days", "retention_period_days", task_name)
    if retention_days is _MISSING:
        retention_days = get_retention_days(db_name) if _use_controller_retention() else DEFAULT_RETENTION_DAYS
    retention_days, error_result = _normalize_retention_days(task_name, retention_days)
    if error_result is not None:
        return error_result
    cutoff_date = datetime.now(tz=UTC) - timedelta(days=retention_days)
    cutoff_date = cutoff_date.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_date_str = cutoff_date.isoformat()

    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"Cleaning up JobData records older than {cutoff_date_str} (retention period: {retention_days} days)",
    )

    start_time = time.monotonic()
    duration_db_ms = 0
    jobdata_count = 0
    try:
        start_db_time = time.monotonic()
        queryset = JobData.objects.filter(finished__lt=cutoff_date)
        # Count JobData rows before deletion — delete() returns the total across all
        # cascaded models (JobLabel, JobHostSummary, etc.) which inflates the count.
        jobdata_count = queryset.count()
        queryset.delete()
        duration_db_ms = (time.monotonic() - start_db_time) * 1000
        log_task_execution(
            task_name=task_name,
            operation="completed",
            details=f"Deleted {jobdata_count} JobData records finished before {cutoff_date_str}",
        )
        duration_ms = (time.monotonic() - start_time) * 1000

        _save_telemetry_details(
            task_name=task_name,
            success=True,
            collection_duration_ms=duration_ms,
            number_of_records_processed=jobdata_count,
            database_query_time_ms=duration_db_ms,
            cache_hit_rate=None,
        )
        return create_task_result(
            "success",
            data={
                "deleted_records": jobdata_count,
                "cutoff_date": cutoff_date_str,
                "retention_days": retention_days,
            },
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        logger.exception(f"Error during cleanup of old JobData records: {str(e)}")
        _save_telemetry_details(
            task_name=task_name,
            success=False,
            collection_duration_ms=duration_ms,
            number_of_records_processed=jobdata_count,
            database_query_time_ms=duration_db_ms,
            cache_hit_rate=None,
        )
        return create_task_result("error", error=f"Cleanup failed: {str(e)}")


def cleanup_dashboard_telemetry(**kwargs) -> dict[str, Any]:
    """
    Delete DashboardTelemetry rows older than retention_days (default: 60).

    Keeps the telemetry table bounded; the API window is 30 days so rows beyond
    60 days are no longer surfaced and can safely be purged.
    Returns a task result dict with the number of deleted rows and cutoff date.
    """
    task_name = "cleanup_dashboard_telemetry"
    retention_days = _get_renamed_kwarg(kwargs, "retention_days", "retention_period_days", task_name)
    retention_days = 60 if retention_days is _MISSING else retention_days
    retention_days, error_result = _normalize_retention_days(task_name, retention_days)
    if error_result is not None:
        return error_result

    cutoff_date = (datetime.now(tz=UTC) - timedelta(days=retention_days)).date()
    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"Deleting DashboardTelemetry rows with collection_run_date < {cutoff_date} (retention: {retention_days} days)",
    )

    try:
        deleted_count, _ = DashboardTelemetry.objects.filter(collection_run_date__lt=cutoff_date).delete()
        log_task_execution(
            task_name=task_name,
            operation="completed",
            details=f"Deleted {deleted_count} DashboardTelemetry rows before {cutoff_date}",
        )
        return create_task_result(
            "success",
            data={
                "deleted_records": deleted_count,
                "cutoff_date": str(cutoff_date),
                "retention_days": retention_days,
            },
        )
    except Exception as e:
        logger.exception("Error during cleanup of DashboardTelemetry rows")
        return create_task_result("error", error=f"Cleanup failed: {str(e)}")
