"""
Background tasks for dashboard reports data collection and cleanup.

Provides dispatcherd tasks:
- collect_dashboard_reports_initial_data: full historical backfill (window from the Controller's retention schedule, default 90 days)
- backfill_dashboard_organization_names: one-time fix of JobData rows missing organization_name
- collect_dashboard_reports_data: incremental sync from last known timestamp (deprecated)
- sync_dashboard_job_records: writes unified_jobs data from the hourly hook to JobData
- sync_dashboard_host_summaries: writes host summary data from the hourly hook to JobHostSummary
- sync_dashboard_on_demand: manually runs both sync paths for a time window
- cleanup_dashboard_reports_old_data: removes JobData records beyond retention period
"""

import logging
import math
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, QuerySet, Value, When
from metrics_utility.library.collectors.dashboard import (
    DashboardJobsResultType,
    dashboard_jobs,
)

from apps.dashboard_reports.awx_queries import fetch_job_types_for_ids, fetch_organizations, fetch_retention_settings
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


def _resolve_collection_params(task_name: str, kwargs: dict) -> tuple[datetime, datetime, int]:
    """Resolve since, until, and batch_size from task kwargs with defaults applied.

    When ``since`` is not provided and no prior ``JobData`` timestamp exists, the backfill
    window defaults to ``DEFAULT_RETENTION_DAYS`` (90 days). When the
    ``DASHBOARD_COLLECTION['USE_CONTROLLER_RETENTION']`` setting is enabled, the window is
    derived from active AWX cleanup_jobs schedules via ``get_retention_days`` instead.
    """
    dashboard_cfg = getattr(settings, "DASHBOARD_COLLECTION", None) or {}
    ignore_database_override(task_name, kwargs)
    until = _parse_dt(kwargs.get("until")) or datetime.now(tz=UTC)
    # Use MAX(finished) as the watermark because all collection queries use date_field='finished'.
    # This aligns the watermark with the query filter so the Controller DB finished index is
    # used correctly and no jobs are missed in the gap between MAX(finished) and MAX(awx_modified).
    since = _parse_dt(kwargs.get("since")) or JobData.last_finished_timestamp()
    if since is None:
        if _use_controller_retention():
            retention_days = get_retention_days()
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
    return since, until, batch_size


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

        # The dashboard_jobs collector joins main_job, so its rows are Controller model "job".
        failed_jobs = _sync_jobs_atomically(batch["results"], default_job_type="job")
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
    save_telemetry = kwargs.get("save_telemetry", True)
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
    if save_telemetry:
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
    save_telemetry = kwargs.get("save_telemetry", True)
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
    if save_telemetry:
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


_ON_DEMAND_WINDOW = timedelta(days=1)  # sub-window size, bounds memory for large backfills
_ON_DEMAND_MAX_RANGE = timedelta(days=7)  # longer ranges belong to collect_dashboard_reports_initial_data


def _save_on_demand_telemetry(task_name: str, start_time: float, success: bool, records: int = 0) -> None:
    """Record telemetry for an on-demand sync run, including failed ones."""
    _save_telemetry_details(
        task_name=task_name,
        success=success,
        collection_duration_ms=(time.monotonic() - start_time) * 1000,
        number_of_records_processed=records,
        database_query_time_ms=None,
        cache_hit_rate=None,
    )


def _on_demand_windows(since: datetime, until: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Yield consecutive [start, end) sub-windows of at most ``_ON_DEMAND_WINDOW`` covering since..until."""
    start = since
    while start < until:
        end = min(start + _ON_DEMAND_WINDOW, until)
        yield start, end
        start = end


def _sync_on_demand_window(db_connection, start: datetime, end: datetime) -> tuple[int, int, str | None]:
    """Run both dashboard sync paths for one window.

    Returns ``(job_count, host_summary_job_count, error_or_None)``. Jobs are synced first because
    host summaries are only written for jobs already present in JobData.
    """
    # Lazy imports: keep metrics_utility optional at module level and avoid the
    # apps.tasks <-> apps.dashboard_reports import cycle.
    from metrics_utility.library.collectors.controller import job_host_summary_service, unified_jobs_dashboard

    from apps.tasks.collectors.collect_hourly_metrics import _build_dashboard_job_records, _build_host_summary_by_job

    window = f"{start.isoformat()} to {end.isoformat()}"
    hour_timestamp = start.isoformat()
    try:
        raw_jobs = unified_jobs_dashboard(db=db_connection, since=start, until=end).gather()
        job_records = _build_dashboard_job_records(raw_jobs)
        raw_summaries = job_host_summary_service(db=db_connection, since=start, until=end).gather()
        summary_records = [r for records in _build_host_summary_by_job(raw_summaries).values() for r in records]
    except Exception:
        logger.exception(f"On-demand dashboard sync collection failed for {window}")
        return 0, 0, f"Collection failed for {window}. See server logs for details."

    job_count = 0
    if job_records:
        job_result = sync_dashboard_job_records(
            raw_jobs=job_records, hour_timestamp=hour_timestamp, save_telemetry=False
        )
        if job_result["status"] != "success":
            return 0, 0, f"sync_dashboard_job_records failed for {window}: {job_result.get('error', 'unknown error')}"
        job_count = job_result.get("job_count", 0)

    summary_job_count = 0
    if summary_records:
        summary_result = sync_dashboard_host_summaries(
            raw_host_summaries=summary_records, hour_timestamp=hour_timestamp, save_telemetry=False
        )
        if summary_result["status"] != "success":
            return (
                job_count,
                0,
                f"sync_dashboard_host_summaries failed for {window}: {summary_result.get('failed', 0)} job(s) failed",
            )
        summary_job_count = summary_result.get("job_count", 0)
    return job_count, summary_job_count, None


def sync_dashboard_on_demand(**kwargs) -> dict[str, Any]:
    """Manually sync dashboard JobData and JobHostSummary records for a time window.

    Runs the same collection and upsert paths as the hourly hooks (``sync_dashboard_job_records``
    then ``sync_dashboard_host_summaries``), inline, so failures are reported in the task result.
    Safe to rerun: both writers upsert. Inner writer telemetry is suppressed so each run records
    a single ``sync_dashboard_on_demand`` row. Respects the ``DASHBOARD_COLLECTION`` enablement
    setting: when disabled, nothing is collected or written and an error is returned.

    Args:
        **kwargs: task_data containing:
            - since (str|None): ISO start of window, inclusive. Defaults to the latest JobData
              ``finished`` timestamp (or the retention window if no data exists).
            - until (str|None): ISO end of window, exclusive. Defaults to now.
            The range may not exceed ``_ON_DEMAND_MAX_RANGE``; use
            ``collect_dashboard_reports_initial_data`` for larger backfills.

    Returns:
        dict: task result with ``date_range``, ``job_count``, ``host_summary_job_count`` and
        ``synced_until`` (end of the last fully synced window, usable as the next ``since``);
        status is ``error`` if any window failed.
    """
    task_name = "sync_dashboard_on_demand"
    start_time = time.monotonic()
    # Lazy import: circular dependency (task_groups -> tasks -> dashboard_reports.tasks).
    from apps.tasks.task_groups import get_feature_enabled_from_db

    # Tasks created via schedule_immediate carry no _feature_flag, so the gate is enforced here.
    if not get_feature_enabled_from_db("DASHBOARD_COLLECTION"):
        _save_on_demand_telemetry(task_name=task_name, start_time=start_time, success=False)
        return create_task_result("error", error="DASHBOARD_COLLECTION is disabled; on-demand sync skipped")
    try:
        since, until, _ = _resolve_collection_params(task_name, kwargs)
    except (TypeError, ValueError) as e:
        logger.warning("On-demand dashboard sync rejected: %s", e)
        _save_on_demand_telemetry(task_name=task_name, start_time=start_time, success=False)
        return create_task_result(
            "error",
            error="Invalid on-demand sync parameters: since/until must be ISO 8601 timestamps. See server logs for details.",
        )
    if since >= until:
        _save_on_demand_telemetry(task_name=task_name, start_time=start_time, success=False)
        return create_task_result("error", error=f"Invalid date range: '{since}' to '{until}'")
    if until - since > _ON_DEMAND_MAX_RANGE:
        _save_on_demand_telemetry(task_name=task_name, start_time=start_time, success=False)
        return create_task_result(
            "error",
            error=(
                f"Range {since.isoformat()} to {until.isoformat()} exceeds {_ON_DEMAND_MAX_RANGE.days} days; "
                "pass a narrower since/until, or use collect_dashboard_reports_initial_data for first-time backfills"
            ),
        )

    date_range = {"start": since.isoformat(), "end": until.isoformat()}
    log_task_execution(
        task_name=task_name,
        operation="processing",
        details=f"On-demand dashboard sync for {date_range['start']} to {date_range['end']}",
    )

    total_jobs = 0
    total_summary_jobs = 0
    synced_until: datetime | None = None
    error: str | None = None
    try:
        db_connection = get_db_connection("awx")
        for start, end in _on_demand_windows(since, until):
            job_count, summary_job_count, error = _sync_on_demand_window(db_connection, start, end)
            total_jobs += job_count
            total_summary_jobs += summary_job_count
            if error:
                break
            synced_until = end
            log_task_execution(
                task_name=task_name,
                operation="processing",
                details=f"Synced up to {end.isoformat()} ({total_jobs} jobs so far)",
            )
    except Exception:
        logger.exception("On-demand dashboard sync failed")
        error = "Database error during on-demand sync. See server logs for details."

    data = {
        "task_type": task_name,
        "date_range": date_range,
        "job_count": total_jobs,
        "host_summary_job_count": total_summary_jobs,
        "synced_until": synced_until.isoformat() if synced_until else None,
    }

    _save_on_demand_telemetry(
        task_name=task_name,
        start_time=start_time,
        success=error is None,
        records=total_jobs + total_summary_jobs,
    )

    if error:
        return create_task_result("error", data=data, error=error)
    log_task_execution(
        task_name=task_name,
        operation="completed",
        details=f"Synced {total_jobs} jobs and host summaries for {total_summary_jobs} jobs",
    )
    return create_task_result("success", data=data)


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
