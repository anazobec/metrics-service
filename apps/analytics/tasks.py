"""Analytics background tasks.

``collect_analytics_on_demand`` runs a single enabled collector for an arbitrary window and
writes the result to :class:`~apps.analytics.models.AnalyticsPayload` only — it does NOT touch
the rollup pipeline (``HourlyMetricsCollection``). It is picked up by the scheduler like other
database tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from apps.analytics.persist import persist_analytics_payload
from apps.analytics.registry import get_entry
from apps.tasks.utils import create_task_result, get_db_connection, parse_datetime_string

logger = logging.getLogger(__name__)


def _collector_func(collector_type: str, mode: str):
    """Return the metrics-utility collector callable for a collector_type, by mode.

    Reuses the metrics-service scheduling registries so there's a single source of truth for the
    collector_type -> collector_func mapping. Lazy imports keep metrics_utility out of module load.
    """
    from apps.tasks.collectors.collect_daily_metrics import _get_daily_collectors
    from apps.tasks.collectors.collect_hourly_metrics import _get_hourly_collectors
    from apps.tasks.collectors.collect_snapshot_metrics import _get_snapshot_collectors

    registries = {
        "hourly": _get_hourly_collectors,
        "snapshot": _get_snapshot_collectors,
        "daily": _get_daily_collectors,
    }
    registry_fn = registries.get(mode)
    if registry_fn is None:
        return None
    return registry_fn().get(collector_type, {}).get("collector_func")


def collect_analytics_on_demand(**kwargs) -> dict[str, Any]:
    """Run one enabled collector for a given window and store its raw payload.

    Args:
        **kwargs: task_data containing:
            - collector (str): public collector name (``group.function``), required
            - since (str|None): ISO start of window (windowed collectors only)
            - until (str|None): ISO end of window (windowed collectors only)

    Returns:
        dict: standard task result.
    """
    collector = kwargs.get("collector")
    if not collector:
        return create_task_result("error", error="collector parameter is required")

    entry = get_entry(collector)
    if entry is None or not entry.enabled:
        return create_task_result("error", error=f"Unknown or disabled collector: {collector}")

    raw_since = kwargs.get("since")
    raw_until = kwargs.get("until")
    since = parse_datetime_string(raw_since) if raw_since else None
    until = parse_datetime_string(raw_until) if raw_until else None
    window_error = next(
        (
            f"Invalid {name}: {raw}"
            for name, raw, parsed in (("since", raw_since, since), ("until", raw_until, until))
            if raw and parsed is None
        ),
        None,
    )
    if window_error is None and since and until and until <= since:
        window_error = "until must be after since"
    if window_error:
        return create_task_result("error", error=window_error)

    collector_func = _collector_func(entry.collector_type, entry.mode)
    if collector_func is None:
        return create_task_result("error", error=f"No collector function for {collector}")

    # Windowed collectors take since/until; snapshots take neither.
    collector_kwargs: dict[str, Any] = {}
    if entry.accepts_since_until:
        collector_kwargs["since"] = since
        collector_kwargs["until"] = until
    if entry.collector_type == "main_jobevent_service":
        collector_kwargs["row_limit"] = settings.JOBEVENT_ROW_LIMIT
        collector_kwargs["job_limit"] = settings.JOBEVENT_JOB_LIMIT

    from django.utils import timezone

    db_connection = get_db_connection(entry.database)
    try:
        started = timezone.now()
        raw_data = collector_func(db=db_connection, **collector_kwargs).gather()
        finished = timezone.now()
    except Exception as e:
        logger.exception("On-demand analytics collection failed for %s", collector)
        return create_task_result("error", {"collector": collector}, error=f"Collection failed: {e}")

    # Persistence appends the completed collection and maps collector_type to the public name.
    persist_analytics_payload(
        entry.collector_type,
        raw_data,
        since=since,
        until=until,
        started_at=started,
        finished_at=finished,
    )
    return create_task_result("success", {"collector": collector, "message": f"Collected {collector}"})
