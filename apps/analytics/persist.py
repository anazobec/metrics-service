"""Persist raw collector output into the analytics payload table.

Called from ``apps.tasks.utils.generic_collect_metrics`` right after ``collector.gather()``,
this writes the raw pre-``prepare()`` output for *enabled* collectors into
:class:`~apps.analytics.models.AnalyticsPayload`. It is intentionally best-effort: any failure
here is logged and swallowed so it can never break the existing rollup collection path.

Import this lazily (inside the caller) to avoid an import-time cycle between the tasks and
analytics apps.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from apps.analytics.models import LOCAL_SOURCE, AnalyticsPayload
from apps.analytics.registry import get_entry_by_type

logger = logging.getLogger(__name__)


def _to_jsonable(raw_data: Any) -> Any:
    """Convert a collector's ``gather()`` output to a JSON-serialisable structure.

    Collectors return either a pandas DataFrame (most) or a plain dict (``config``). pandas
    ``to_json`` handles numpy dtypes, NaN and timestamps natively — avoiding the numpy-int64 /
    NaN pitfalls that ``DjangoJSONEncoder`` chokes on — so we round-trip DataFrames through it.

    This is the minimal generic conversion; richer per-collector serialization can be added at
    the API boundary later.
    """
    # Duck-type a DataFrame without importing pandas at module load.
    to_json = getattr(raw_data, "to_json", None)
    if callable(to_json) and hasattr(raw_data, "columns"):
        import json

        return json.loads(raw_data.to_json(orient="records", date_format="iso"))

    if raw_data is None:
        return {}

    # Already dict/list-shaped (e.g. the config collector).
    return raw_data


def persist_analytics_payload(
    collector_type: str,
    raw_data: Any,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    source: str = LOCAL_SOURCE,
) -> None:
    """Persist raw ``gather()`` output for an enabled collector, keyed by public name.

    No-op for collectors that aren't enabled in the analytics registry. Best-effort: logs and
    swallows all errors so the caller's rollup path is unaffected. Stores the public ``name``
    (``group.function``) in ``collector``. Every successful collection is appended.

    Args:
        collector_type: metrics-service collector key (mapped to the public name via the registry).
        raw_data: the collector's ``gather()`` output (DataFrame or dict).
        since: ``since`` passed to the collector (or None for snapshot collectors).
        until: ``until`` passed to the collector (or None for snapshot collectors).
        started_at: when the ``gather()`` call started.
        finished_at: when the ``gather()`` call finished.
        source: origin of the data; defaults to the local install.
    """
    entry = get_entry_by_type(collector_type)
    if entry is None or not entry.enabled:
        return

    try:
        payload = _to_jsonable(raw_data)
        # collector_type is the internal task key (for example, ``main_host``)
        # entry.name is the full whitelist/API/storage name (for example, ``controller.main_host``)
        AnalyticsPayload.objects.create(
            collector=entry.name,
            source=source,
            since=since,
            until=until,
            payload=payload,
            started_at=started_at,
            finished_at=finished_at,
        )
    except Exception:  # noqa: BLE001 - never let analytics persistence break collection
        logger.exception("Failed to persist analytics payload for collector %s", collector_type)
