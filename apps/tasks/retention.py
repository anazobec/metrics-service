"""Controller retention resolution shared by background tasks."""

import logging
from datetime import UTC, datetime
from typing import Any

from .utils import get_db_connection

DEFAULT_AWX_DB_NAME = "awx"
DEFAULT_RETENTION_DAYS = 90

RETENTION_SETTINGS_QUERY = """
SELECT
    sjt.job_type,
    ujt.name AS template_name,
    s.name AS schedule_name,
    s.enabled AS schedule_enabled,
    s.rrule,
    s.next_run,
    s.extra_data::jsonb ->> 'days' AS retention_days
FROM main_systemjobtemplate sjt
JOIN main_unifiedjobtemplate ujt
    ON ujt.id = sjt.unifiedjobtemplate_ptr_id
LEFT JOIN main_schedule s
    ON s.unified_job_template_id = ujt.id
WHERE sjt.job_type = 'cleanup_jobs'
"""

logger = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    """Coerce a schedule timestamp to a timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        dt = None if value == "NaT" else datetime.fromisoformat(value)
        return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    raise TypeError(f"_parse_dt: expected str, datetime, or None; got {type(value).__name__!r}")


def _active_retention_schedules(retention_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return enabled schedules with safe, positive integer retention periods."""
    active = []
    for row in retention_data:
        if row.get("job_type") != "cleanup_jobs" or not row.get("schedule_enabled"):
            continue
        raw_days = row.get("retention_days")
        if raw_days is None or isinstance(raw_days, bool):
            continue
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            continue
        if days < 1:
            continue
        active.append({**row, "retention_days": days})
    return active


def _has_earlier_next_run(row: dict[str, Any], best: dict[str, Any]) -> bool:
    """Return whether ``row`` wins a retention tie by its next-run timestamp."""
    try:
        row_next = _parse_dt(row.get("next_run"))
        best_next = _parse_dt(best.get("next_run"))
    except (TypeError, ValueError):
        return False
    return row_next is not None and (best_next is None or row_next < best_next)


def _select_retention_schedule(active: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the most aggressive schedule, breaking ties by the soonest next run."""
    best = active[0]
    for row in active[1:]:
        if row["retention_days"] < best["retention_days"] or (
            row["retention_days"] == best["retention_days"] and _has_earlier_next_run(row, best)
        ):
            best = row
    return best


def resolve_retention_days(retention_data: list[dict[str, Any]]) -> int | None:
    """Resolve retention from schedule rows, returning None when none are valid."""
    active = _active_retention_schedules(retention_data)
    return _select_retention_schedule(active)["retention_days"] if active else None


def _fetch_retention_settings(db_connection) -> list[dict[str, Any]]:
    """Fetch raw Controller cleanup schedule rows without casting ``days`` in SQL."""
    with db_connection.cursor() as cursor:
        cursor.execute(RETENTION_SETTINGS_QUERY)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def get_controller_retention_days(db_name: str = DEFAULT_AWX_DB_NAME) -> int:
    """Return Controller retention, logging and falling back to 90 days when necessary."""
    try:
        retention_data = _fetch_retention_settings(get_db_connection(db_name))
    except Exception:
        logger.exception("Unable to read Controller retention; using 90-day fallback")
        return DEFAULT_RETENTION_DAYS

    retention_days = resolve_retention_days(retention_data)
    if retention_days is None:
        logger.warning("No valid Controller cleanup_jobs retention schedule found; using 90-day fallback")
        return DEFAULT_RETENTION_DAYS
    return retention_days
