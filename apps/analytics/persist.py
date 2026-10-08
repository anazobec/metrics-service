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
import json
import logging
from typing import Any

from apps.analytics.models import LOCAL_SOURCE, AnalyticsPayload
from apps.analytics.registry import get_entry_by_type
from apps.core.json_utils import to_jsonable

logger = logging.getLogger(__name__)


def _get_cluster_id(db_connection: Any) -> str | None:
    """Read Controller's stable installation UUID from ``conf_setting``.

    AWX stores setting values as JSON-encoded text. Missing settings and database
    lookup errors are non-fatal: payloads are still persisted with a null cluster ID.
    """
    if db_connection is None:
        return None

    try:
        with db_connection.cursor() as cursor:
            cursor.execute("SELECT value FROM conf_setting WHERE key = %s", ["INSTALL_UUID"])
            row = cursor.fetchone()
        if not row or not row[0]:
            return None

        value = row[0]
        if not isinstance(value, str):
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = value
        return decoded if isinstance(decoded, str) and decoded else None
    except Exception:  # noqa: BLE001 - metadata lookup must not block collection persistence
        logger.warning("Failed to read Controller INSTALL_UUID from AWX database", exc_info=True)
        return None


def persist_analytics_payload(
    collector_type: str,
    raw_data: Any,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    source: str = LOCAL_SOURCE,
    raise_on_error: bool = False,
    db_connection: Any = None,
) -> None:
    """Persist raw ``gather()`` output for an enabled collector, keyed by public name.

    No-op for collectors that aren't enabled in the analytics registry. By default, persistence is
    best-effort: errors are logged and swallowed so the caller's rollup path is unaffected. Callers
    that require storage can set ``raise_on_error``. Stores the public ``name`` (``group.function``)
    in ``collector``. Every successful collection is appended.

    Args:
        collector_type: metrics-service collector key (mapped to the public name via the registry).
        raw_data: the collector's ``gather()`` output (DataFrame or dict).
        since: ``since`` passed to the collector (or None for snapshot collectors).
        until: ``until`` passed to the collector (or None for snapshot collectors).
        started_at: when the ``gather()`` call started.
        finished_at: when the ``gather()`` call finished.
        source: origin of the data; defaults to the local install.
        raise_on_error: re-raise persistence errors for callers where storing the payload is required.
    """
    entry = get_entry_by_type(collector_type)
    if entry is None or not entry.enabled:
        return

    try:
        payload = {} if raw_data is None else to_jsonable(raw_data)
        cluster_id = _get_cluster_id(db_connection)
        # collector_type is the internal task key (for example, ``main_host``)
        # entry.name is the full whitelist/API/storage name (for example, ``controller.main_host``)
        AnalyticsPayload.objects.create(
            collector=entry.name,
            source=source,
            cluster_id=cluster_id,
            since=since,
            until=until,
            payload=payload,
            started_at=started_at,
            finished_at=finished_at,
        )
    except Exception:  # noqa: BLE001 - persistence is best-effort unless explicitly required
        logger.exception("Failed to persist analytics payload for collector %s", collector_type)
        if raise_on_error:
            raise
