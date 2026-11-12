"""
Background tasks for dashboard reports data collection and cleanup.

Provides dispatcherd tasks:
- collect_dashboard_reports_initial_data: full historical backfill (window from the Controller's retention schedule, default 90 days)
- backfill_dashboard_organization_names: one-time fix of JobData rows missing organization_name
- update_dashboard_job_definition: one-time update of the retention window to the unified dashboard job definition
- collect_dashboard_reports_data: incremental sync from last known timestamp (deprecated)
- sync_dashboard_job_records: writes unified_jobs data from the hourly hook to JobData
- sync_dashboard_host_summaries: writes host summary data from the hourly hook to JobHostSummary
- cleanup_dashboard_reports_old_data: removes JobData records beyond retention period
"""

import logging
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, QuerySet, Value, When
from metrics_utility.library.collectors.controller import unified_jobs_dashboard
from metrics_utility.library.collectors.dashboard.queries import get_job_host_summaries_for_ids_query

from apps.dashboard_reports.awx_queries import fetch_job_types_for_ids, fetch_organizations, fetch_retention_settings
from apps.dashboard_reports.job_definition import prepare_dashboard_records, select_dashboard_jobs
from apps.dashboard_reports.models import DashboardTelemetry, JobData, JobHostSummary
from apps.tasks.retention import DEFAULT_RETENTION_DAYS, _parse_dt, resolve_retention_days
from apps.tasks.utils import create_task_result, get_db_connection, ignore_database_override, log_task_execution

ORG_NAME_BACKFILL_BATCH_SIZE = 5_000
JOB_TYPE_BACKFILL_BATCH_SIZE = 2_000

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


def get_retention_days() -> int:
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
        db_connection = get_db_connection("awx")
        retention_data, _ = fetch_retention_settings(db_connection=db_connection)
    except Exception:
        logger.exception(f"Error fetching retention settings; using default {DEFAULT_RETENTION_DAYS} days")
        return DEFAULT_RETENTION_DAYS

    retention_days = resolve_retention_days(retention_data)
    if retention_days is None:
        logger.warning("No valid Controller cleanup_jobs retention schedule found; using 90-day fallback")
        return DEFAULT_RETENTION_DAYS
    return retention_days


def _collect_jobs(db_connection, since: datetime, until: datetime, after_id: int, batch_size: int):
    """
    Return one ID-ordered page of unified job rows finished in ``[since, until)`` as a DataFrame.

    Uses cursor-based pagination (``id > after_id ORDER BY id LIMIT batch_size``) so memory and
    transaction times stay bounded for large windows. This is the same collector the hourly hook
    reads, so the backfill and the hook see identical rows.
    """
    return unified_jobs_dashboard(
        db=db_connection, since=since, until=until, after_id=after_id, batch_size=batch_size
    ).gather()


def _iter_job_pages(db_connection, since: datetime, until: datetime, batch_size: int) -> Iterator[Any]:
    """Yield raw unified job pages until the window is exhausted."""
    after_id = 0
    while True:
        page = _collect_jobs(db_connection, since, until, after_id, batch_size)
        if page is None or page.empty:
            return
        yield page
        # A short page is the last one; the raw size is used because the dashboard job filter
        # is applied after pagination.
        if len(page) < batch_size:
            return
        after_id = int(page["id"].max())


def _fetch_host_summaries(db_connection, job_ids: list[int]) -> dict[int, list[dict]]:
    """Return the Controller host summaries of the given jobs, keyed by job ID."""
    if not job_ids:
        return {}
    query, params = get_job_host_summaries_for_ids_query(job_ids)
    summaries: dict[int, list[dict]] = {}
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        for summary_id, host_name, host_id, job_id in cursor:
            summaries.setdefault(job_id, []).append({"id": summary_id, "host_id": host_id, "host_name": host_name})
    return summaries


@dataclass
class _PageStats:
    """Outcome of storing one page of dashboard jobs."""

    processed: int = 0
    added: int = 0
    updated: int = 0
    failed: list[int] = field(default_factory=list)


def _store_dashboard_page(db_connection, page) -> _PageStats:
    """
    Select the dashboard jobs of a raw page and upsert them into JobData by ``job_id``.

    Shared by the initial backfill and the job definition update task. ``added`` counts rows that did
    not exist yet and ``updated`` counts existing rows whose ``launch_type`` was still unset. When any job
    fails, the whole page is rolled back and its job IDs are returned in ``failed``.
    """
    filtered = select_dashboard_jobs(page)
    if filtered is None:
        return _PageStats()

    jobs = [_assemble_dashboard_job(record) for record in prepare_dashboard_records(filtered)]

    # Only the "job" model has host summaries; other unified job types never do.
    host_summaries = _fetch_host_summaries(db_connection, [job["id"] for job in jobs if job.get("job_type") == "job"])
    for job in jobs:
        if job.get("job_type") == "job":
            job["host_summaries"] = host_summaries.get(job["id"], [])

    existing_launch_types = dict(
        JobData.objects.filter(job_id__in=[job["id"] for job in jobs]).values_list("job_id", "launch_type")
    )
    failed = _sync_jobs_atomically(jobs)
    if failed:
        return _PageStats(failed=failed)

    return _PageStats(
        processed=len(jobs),
        added=sum(job["id"] not in existing_launch_types for job in jobs),
        updated=sum(existing_launch_types.get(job["id"], "") is None for job in jobs),
    )


def _sync_jobs_atomically(job_results: list, default_job_type: str | None = None) -> list:
    """
    Persist all jobs in a single atomic transaction.

    Returns the list of job IDs that failed to sync.  If any job fails every
    save made in this call is rolled back, keeping JobData.last_finished_timestamp()
    unchanged so the next incremental run retries from the same watermark.


    Note: retry behaviour for the outer Task is handled by the Task model's max_attempts
    mechanism (default: 3 attempts). When this function signals failure the calling task
    returns create_task_result("error"), which marks the Task as failed and allows the
    scheduler to retry it automatically up to max_attempts times via Task.can_retry().
    A default job type is used only when creating a row or filling a stored NULL value.
    """
    failed_jobs: list = []
    try:
        with transaction.atomic():
            for job in job_results:
                try:
                    if default_job_type is not None and "job_type" not in job:
                        JobData.create_or_update_from_awx(job, default_job_type=default_job_type)
                    else:
                        JobData.create_or_update_from_awx(job)
                except Exception as e:
                    logger.exception(f"Error creating/updating JobData for job {job['id']}: {str(e)}")
                    failed_jobs.append(job["id"])
            if failed_jobs:
                raise _PartialSyncRollbackError()
    except _PartialSyncRollbackError:
        pass  # transaction rolled back; caller inspects failed_jobs
    return failed_jobs


def _retention_window_start(until: datetime) -> datetime:
    """Return the UTC midnight at which the retention window ending at *until* starts."""
    if _use_controller_retention():
        retention_days = get_retention_days()
        if retention_days <= 0:
            raise ValueError(f"Retention days must be > 0, got {retention_days!r}")
    else:
        retention_days = DEFAULT_RETENTION_DAYS
    return (until - timedelta(days=retention_days)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _configured_batch_size() -> int:
    """Return the validated ``DASHBOARD_COLLECTION['BACKFILL_BATCH_SIZE']`` setting (default 5000)."""
    dashboard_cfg = getattr(settings, "DASHBOARD_COLLECTION", None) or {}
    raw_batch_size = dashboard_cfg.get("BACKFILL_BATCH_SIZE", 5_000)
    try:
        batch_size = int(raw_batch_size)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"DASHBOARD_COLLECTION.BACKFILL_BATCH_SIZE must be a positive integer, got {raw_batch_size!r}"
        ) from e
    if batch_size <= 0:
        raise ValueError("DASHBOARD_COLLECTION.BACKFILL_BATCH_SIZE must be > 0")
    return batch_size


def _resolve_collection_params(task_name: str, kwargs: dict) -> tuple[datetime, datetime, int]:
    """Resolve since, until, and batch_size from task kwargs with defaults applied.

    When ``since`` is not provided and no prior ``JobData`` timestamp exists, the backfill
    window defaults to ``DEFAULT_RETENTION_DAYS`` (90 days). When the
    ``DASHBOARD_COLLECTION['USE_CONTROLLER_RETENTION']`` setting is enabled, the window is
    derived from active AWX cleanup_jobs schedules via ``get_retention_days`` instead.
    """
    ignore_database_override(task_name, kwargs)
    until = _parse_dt(kwargs.get("until")) or datetime.now(tz=UTC)
    # Use MAX(finished) as the watermark because all collection queries use date_field='finished'.
    # This aligns the watermark with the query filter so the Controller DB finished index is
    # used correctly and no jobs are missed in the gap between MAX(finished) and MAX(awx_modified).
    since = _parse_dt(kwargs.get("since")) or JobData.last_finished_timestamp()
    if since is None:
        since = _retention_window_start(until)
    return since, until, _configured_batch_size()


def _process_batches(
    db_connection, since: datetime, until: datetime, batch_size: int, task_name: str
) -> tuple[int, str | None]:
    """Collect and store jobs in cursor-paginated batches. Returns (total_synced, error_message_or_None)."""
    total_synced = 0
    pages = _iter_job_pages(db_connection, since, until, batch_size)
    while True:
        try:
            page = next(pages, None)
        except Exception as e:
            logger.exception("Error collecting jobs batch")
            return total_synced, f"Collecting jobs failed: {str(e)}"
        if page is None:
            break

        try:
            stats = _store_dashboard_page(db_connection, page)
        except Exception as e:
            logger.exception("Error storing jobs batch")
            return total_synced, f"Storing jobs failed: {str(e)}"
        if stats.failed:
            return total_synced, f"Failed to sync {len(stats.failed)} job(s): {stats.failed}"

        total_synced += stats.processed
        log_task_execution(
            task_name=task_name,
            operation="processing",
            details=f"Synced batch of {stats.processed} jobs (total so far: {total_synced})",
        )
    return total_synced, None


def _collect_data(task_name: str, **kwargs) -> dict[str, Any]:
    """
    Core data collection logic for dashboard reports.

    Fetches jobs in cursor-paginated batches (default 5 000 records each) to
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
        since, until, batch_size = _resolve_collection_params(task_name, kwargs)
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
        db_connection = get_db_connection("awx")
    except Exception as e:
        logger.exception("Database error during collection setup")
        result["error"] = True
        result["message"] = f"Database error: {str(e)}"
        return result

    total_synced, error_msg = _process_batches(db_connection, since, until, batch_size, task_name)

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


def _backfill_batch_size(default_batch_size: int, kwargs: dict[str, Any]) -> int:
    """Return a validated ``batch_size`` kwarg for the organization-name backfill.

    Has one argument, ``default_batch_size``, which is used when the kwargs does not
    contain ``batch_size``.
    """
    raw = kwargs.get("batch_size", default_batch_size)
    # bool is an int subclass and int() would truncate floats, so require a real integer
    # (numeric strings are accepted, as task_data may come from the API as text).
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise ValueError(f"batch_size must be a positive integer, got {raw!r}")
    try:
        batch_size = int(raw)
    except ValueError as e:
        raise ValueError(f"batch_size must be a positive integer, got {raw!r}") from e
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size!r}")
    return batch_size


def _update_organization_names(
    missing_qs: QuerySet[JobData], names_by_id: dict[int, str], batch_size: int, task_name: str
) -> int:
    """Set organization_name on rows of ``missing_qs`` whose organization is in ``names_by_id``.

    Walks the rows by primary key in batches of ``batch_size``; each batch is a single UPDATE,
    committed on its own, so the task never holds locks on the whole table. The UPDATE re-checks
    ``organization_name IS NULL`` so rows named concurrently (e.g. by the hourly sync) are left alone.
    Returns the number of updated rows.
    """
    resolved_qs = missing_qs.filter(organization_id__in=names_by_id.keys()).order_by("pk")
    updated = 0
    last_pk = 0
    while True:
        batch = list(resolved_qs.filter(pk__gt=last_pk).values_list("pk", "organization_id")[:batch_size])
        if not batch:
            break
        last_pk = batch[-1][0]
        batch_org_ids = {org_id for _, org_id in batch}
        updated += JobData.objects.filter(pk__in=[pk for pk, _ in batch], organization_name__isnull=True).update(
            organization_name=Case(
                *[When(organization_id=org_id, then=Value(names_by_id[org_id])) for org_id in batch_org_ids],
                output_field=CharField(),
            )
        )
        log_task_execution(
            task_name=task_name,
            operation="processing",
            details=f"Updated batch of {len(batch)} rows (total so far: {updated}, cursor pk: {last_pk})",
        )
    return updated


def fill_missing_organization_names(task_name: str, batch_size: int = ORG_NAME_BACKFILL_BATCH_SIZE) -> dict[str, Any]:
    """
    Set organization_name on JobData rows that have ``organization_id`` set but no name.

    Names are resolved from the AWX ``main_organization`` table and written in batches. Rows whose
    organization no longer exists in AWX are left NULL and counted in ``unresolved_records``. Only
    rows still missing a name are touched, so repeated calls are safe. Raises if AWX cannot be queried.

    Returns ``{"updated_records", "unresolved_records", "unresolved_ids"}``.
    """
    missing_qs = JobData.objects.filter(organization_id__isnull=False, organization_name__isnull=True)
    # order_by() drops JobData's default -started ordering, which would otherwise be added to SELECT DISTINCT.
    org_ids = set(missing_qs.order_by().values_list("organization_id", flat=True).distinct())
    if not org_ids:
        log_task_execution(
            task_name=task_name, operation="completed", details="No JobData rows missing organization_name"
        )
        return {"updated_records": 0, "unresolved_records": 0, "unresolved_ids": []}

    organizations, _ = fetch_organizations(db_connection=get_db_connection("awx"))
    names_by_id = {org["id"]: org["name"] for org in organizations if org["id"] in org_ids}
    updated = _update_organization_names(missing_qs, names_by_id, batch_size, task_name)

    unresolved_ids = sorted(org_ids - names_by_id.keys())
    unresolved_records = missing_qs.filter(organization_id__in=unresolved_ids).count() if unresolved_ids else 0
    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=(
            f"Set organization_name on {updated} JobData records; {unresolved_records} records left unchanged "
            f"(organization not found in AWX, ids: {unresolved_ids})"
        ),
    )
    return {"updated_records": updated, "unresolved_records": unresolved_records, "unresolved_ids": unresolved_ids}


def backfill_dashboard_organization_names(**kwargs: Any) -> dict[str, Any]:
    """
    One-time fix of JobData rows stored without organization_name by earlier collector versions.

    Runs once as a system task (``cron=None``) after install/upgrade. Only needed where
    ``collect_dashboard_reports_initial_data`` already ran with the old collector (it is a one-shot
    task and will not run again); on fresh installs the fixed collector stores names itself, so
    this finds no rows and completes immediately. See ``fill_missing_organization_names`` for the
    update rules.
    """
    task_name = "backfill_dashboard_organization_names"
    try:
        ignore_database_override(task_name, kwargs)
        batch_size = _backfill_batch_size(ORG_NAME_BACKFILL_BATCH_SIZE, kwargs)
        stats = fill_missing_organization_names(task_name, batch_size=batch_size)
    except Exception as e:
        logger.exception(f"{task_name}: failed")
        msg = f"Filling organization names failed: {str(e)}"
        log_task_execution(task_name=task_name, operation="failed", details=msg)
        return create_task_result("error", error=msg)
    return create_task_result("success", data={"task_type": task_name, **stats})


def backfill_dashboard_job_types(**kwargs: Any) -> dict[str, Any]:
    """Populate job_type on existing JobData rows without deleting any records.

    Controller lookups are limited to the current batch of stored job IDs. Each batch
    commits independently, so a failed run can be retried safely; already classified
    rows are skipped. IDs no longer present in Controller remain NULL and are included
    in default job views as legacy/unclassified rows; explicit non-job filters exclude them.
    """
    task_name = "backfill_dashboard_job_types"
    started_at = time.monotonic()
    updated_records = 0
    unresolved_records = 0
    try:
        ignore_database_override(task_name, kwargs)
        batch_size = _backfill_batch_size(JOB_TYPE_BACKFILL_BATCH_SIZE, kwargs)

        if not JobData.objects.filter(job_type__isnull=True).exists():
            log_task_execution(
                task_name=task_name, operation="completed", details="No JobData rows need classification"
            )
            return create_task_result(
                "success",
                data={
                    "task_type": task_name,
                    "updated_records": 0,
                    "unresolved_records": 0,
                },
            )

        db_connection = get_db_connection("awx")
        last_job_id: int | None = None
        while True:
            missing = JobData.objects.filter(job_type__isnull=True)
            if last_job_id is not None:
                missing = missing.filter(job_id__gt=last_job_id)
            job_ids = list(missing.order_by("job_id").values_list("job_id", flat=True)[:batch_size])
            if not job_ids:
                break
            last_job_id = job_ids[-1]

            job_types = fetch_job_types_for_ids(db_connection, job_ids)
            unresolved_records += len(job_ids) - len(job_types)
            if job_types:
                updated = JobData.objects.filter(
                    job_id__in=job_types,
                    job_type__isnull=True,
                ).update(
                    job_type=Case(
                        *[When(job_id=job_id, then=Value(job_type)) for job_id, job_type in job_types.items()],
                        output_field=CharField(),
                    )
                )
                updated_records += updated

            log_task_execution(
                task_name=task_name,
                operation="processing",
                details=(
                    f"Processed {len(job_ids)} stored job IDs "
                    f"(updated: {updated_records}, unresolved: {unresolved_records})"
                ),
            )

        duration_ms = (time.monotonic() - started_at) * 1000
        log_task_execution(
            task_name=task_name,
            operation="completed",
            details=(
                f"Classified {updated_records} JobData rows; {unresolved_records} IDs were not found in Controller "
                "and remain NULL for the default legacy job filter"
            ),
        )
        _save_telemetry_details(
            task_name=task_name,
            success=True,
            collection_duration_ms=duration_ms,
            number_of_records_processed=updated_records,
            database_query_time_ms=None,
            cache_hit_rate=None,
        )
        return create_task_result(
            "success",
            data={
                "task_type": task_name,
                "updated_records": updated_records,
                "unresolved_records": unresolved_records,
            },
        )
    except Exception as e:
        duration_ms = (time.monotonic() - started_at) * 1000
        logger.exception("%s: failed", task_name)
        msg = f"Job type reconciliation failed: {e}"
        log_task_execution(task_name=task_name, operation="failed", details=msg)
        _save_telemetry_details(
            task_name=task_name,
            success=False,
            collection_duration_ms=duration_ms,
            number_of_records_processed=updated_records,
            database_query_time_ms=None,
            cache_hit_rate=None,
        )
        return create_task_result("error", error=msg)


def update_dashboard_job_definition(**kwargs: Any) -> dict[str, Any]:
    """Re-store the Controller jobs of the retention window with the unified dashboard job definition.

    Existing JobData rows get ``launch_type`` (and ``job_type``) filled and jobs that were never stored
    before (sync, workflow and non-job unified jobs the old backfill skipped) are added. Rows go through
    the same collector and upsert by ``job_id`` as the hourly hook and the initial backfill, in
    cursor-paginated batches that each commit independently. The task is idempotent: a second run adds
    and updates nothing.

    The result and the DashboardTelemetry row report ``processed`` (jobs stored), ``updated`` (existing rows
    whose launch_type was unset), ``added`` (new rows) and ``failed`` (jobs in rolled-back batches).
    Telemetry stores ``processed`` as the record count and flags the run unsuccessful when any job failed.
    """
    task_name = "update_dashboard_job_definition"
    started_at = time.monotonic()
    totals = {"processed": 0, "updated": 0, "added": 0, "failed": 0}
    since = until = None
    error: str | None = None
    try:
        ignore_database_override(task_name, kwargs)
        batch_size = _backfill_batch_size(_configured_batch_size(), kwargs)
        until = datetime.now(tz=UTC)
        since = _retention_window_start(until)
        log_task_execution(
            task_name=task_name,
            operation="processing",
            details=f"Updating dashboard job definition for: {since.isoformat()} to {until.isoformat()}",
        )

        db_connection = get_db_connection("awx")
        for page in _iter_job_pages(db_connection, since, until, batch_size):
            stats = _store_dashboard_page(db_connection, page)
            totals["processed"] += stats.processed
            totals["updated"] += stats.updated
            totals["added"] += stats.added
            totals["failed"] += len(stats.failed)
            log_task_execution(
                task_name=task_name,
                operation="processing",
                details=(
                    f"Processed {totals['processed']} jobs (updated: {totals['updated']}, "
                    f"added: {totals['added']}, failed: {totals['failed']})"
                ),
            )
    except Exception as e:
        logger.exception("%s: failed", task_name)
        error = f"Updating dashboard job definition failed: {e}"
    if totals["failed"]:
        error = error or f"Failed to store {totals['failed']} job(s); rerun the task to retry them"

    duration_ms = (time.monotonic() - started_at) * 1000
    _save_telemetry_details(
        task_name=task_name,
        success=error is None,
        collection_duration_ms=duration_ms,
        number_of_records_processed=totals["processed"],
        database_query_time_ms=None,
        cache_hit_rate=None,
    )
    data = {
        "task_type": task_name,
        "date_range": {"start": since.isoformat() if since else None, "end": until.isoformat() if until else None},
        **totals,
    }
    if error:
        log_task_execution(task_name=task_name, operation="failed", details=error)
        return create_task_result("error", error=error, data=data)

    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=(
            f"Processed {totals['processed']} jobs (updated: {totals['updated']}, "
            f"added: {totals['added']}, failed: {totals['failed']})"
        ),
    )
    return create_task_result("success", data=data)


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


def _has_dashboard_job_model(row: dict) -> bool:
    """Return whether a dashboard job row has a usable Controller model name."""
    model = row.get("model")
    return isinstance(model, str) and bool(model.strip())


def _get_dashboard_job_ids_missing_models(rows: list[dict]) -> list[int]:
    """Return unique integer job IDs whose Controller models need to be recovered."""
    return list(
        dict.fromkeys(row["id"] for row in rows if isinstance(row.get("id"), int) and not isinstance(row["id"], bool))
    )


def _resolve_dashboard_job_models(raw_jobs: list[Any], task_name: str) -> str | None:
    """Recover missing Controller model names and return an error message on failure."""
    jobs_missing_model = [row for row in raw_jobs if isinstance(row, dict) and not _has_dashboard_job_model(row)]
    job_ids = _get_dashboard_job_ids_missing_models(jobs_missing_model)
    if not job_ids:
        return None

    try:
        controller_models = fetch_job_types_for_ids(get_db_connection("awx"), job_ids)
    except Exception:
        logger.exception("Unable to recover Controller models for queued dashboard job records")
        error = f"Unable to recover Controller models for {len(job_ids)} queued dashboard job record(s)"
        log_task_execution(task_name=task_name, operation="error", details=error)
        return error

    for row in jobs_missing_model:
        model = controller_models.get(row.get("id"))
        if isinstance(model, str) and model.strip():
            row["model"] = model
    return None


def _parse_dashboard_job_labels(row: dict) -> list[int] | None:
    """Parse the optional label IDs from a raw dashboard job row."""
    if "label_ids" not in row:
        # Older metrics_utility versions omit this column. None tells
        # create_or_update_from_awx to preserve existing labels.
        return None

    label_ids_raw = row["label_ids"]
    if label_ids_raw is None or (isinstance(label_ids_raw, float) and math.isnan(label_ids_raw)):
        return []
    if isinstance(label_ids_raw, str):
        return [int(value.strip()) for value in label_ids_raw.split(",") if value.strip()]
    return [int(label_ids_raw)]


def _assemble_dashboard_job(row: dict) -> dict:
    """Build the JobData payload from a serialized dashboard job row."""
    awx_job = {
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
        "labels": _parse_dashboard_job_labels(row),
        # host_summaries=None signals create_or_update_from_awx to leave
        # existing JobHostSummary records intact (created by initial backfill).
        "host_summaries": None,
        "num_hosts": row.get("num_hosts") or 0,
    }
    if _has_dashboard_job_model(row):
        awx_job["job_type"] = row["model"]
    if "launch_type" in row:
        # Rows queued before launch_type was collected omit it; that keeps the stored value.
        awx_job["launch_type"] = row["launch_type"]
    return awx_job


def sync_dashboard_job_records(**kwargs) -> dict[str, Any]:
    """
    Write unified_jobs raw data collected during the hourly rollup to the dashboard JobData table.

    New hourly tasks carry Controller model names in task_data. Missing model names are resolved
    from Controller in one batch when possible; unresolved rows are stored unclassified so the
    default report filter can retain them and a later backfill can retry classification.
    """
    task_name = "sync_dashboard_job_records"
    hour_timestamp = kwargs.get("hour_timestamp", "unknown")
    raw_jobs = [row.copy() if isinstance(row, dict) else row for row in kwargs.get("raw_jobs", [])]
    start_time = time.monotonic()

    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"Syncing {len(raw_jobs)} dashboard job records for {hour_timestamp}",
    )

    error = _resolve_dashboard_job_models(raw_jobs, task_name)
    if error:
        return create_task_result("error", error=error)

    unclassified_count = sum(not isinstance(row, dict) or not _has_dashboard_job_model(row) for row in raw_jobs)
    if unclassified_count:
        logger.warning(
            "Syncing %s dashboard job record(s) without a Controller model for %s; they remain unclassified",
            unclassified_count,
            hour_timestamp,
        )

    assembled = [_assemble_dashboard_job(row) for row in raw_jobs]

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
    ignore_database_override(task_name, kwargs)
    retention_days = _get_renamed_kwarg(kwargs, "retention_days", "retention_period_days", task_name)
    if retention_days is _MISSING:
        retention_days = get_retention_days() if _use_controller_retention() else DEFAULT_RETENTION_DAYS
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
