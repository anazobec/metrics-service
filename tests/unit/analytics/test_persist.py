"""Tests for persisting raw collector output into AnalyticsPayload."""

from datetime import UTC, datetime
from unittest.mock import ANY, MagicMock, patch

import pandas as pd
import pytest

from apps.analytics.models import AnalyticsPayload
from apps.analytics.persist import persist_analytics_payload

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _times():
    started = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 17, 10, 1, tzinfo=UTC)
    return started, finished


def test_persist_dataframe_stores_records():
    started, finished = _times()
    df = pd.DataFrame([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    persist_analytics_payload("unified_jobs", df, since=None, until=None, started_at=started, finished_at=finished)

    row = AnalyticsPayload.objects.get(collector="controller.unified_jobs_dashboard")
    assert row.payload == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_persist_dict_payload():
    started, finished = _times()
    persist_analytics_payload(
        "config", {"version": "1.2.3"}, since=None, until=None, started_at=started, finished_at=finished
    )
    row = AnalyticsPayload.objects.get(collector="controller.config")
    assert row.payload == {"version": "1.2.3"}


def test_persist_none_becomes_empty_dict():
    started, finished = _times()
    persist_analytics_payload("config", None, since=None, until=None, started_at=started, finished_at=finished)
    assert AnalyticsPayload.objects.get(collector="controller.config").payload == {}


def test_persist_disabled_collector_is_noop():
    started, finished = _times()
    persist_analytics_payload(
        "task_executions_service", {"x": 1}, since=None, until=None, started_at=started, finished_at=finished
    )
    assert not AnalyticsPayload.objects.exists()


def test_persist_unknown_collector_is_noop():
    started, finished = _times()
    persist_analytics_payload(
        "does_not_exist", {"x": 1}, since=None, until=None, started_at=started, finished_at=finished
    )
    assert not AnalyticsPayload.objects.exists()


def test_persist_retains_duplicate_window():
    started, finished = _times()
    since = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    until = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    for payload in ({"v": 1}, {"v": 2}):
        persist_analytics_payload("config", payload, since=since, until=until, started_at=started, finished_at=finished)
    rows = AnalyticsPayload.objects.filter(collector="controller.config", since=since, until=until).order_by("id")
    assert rows.count() == 2
    assert list(rows.values_list("payload", flat=True)) == [{"v": 1}, {"v": 2}]


def test_persist_retains_multiple_snapshots():
    started, finished = _times()
    for value in (1, 2):
        persist_analytics_payload(
            "config", {"v": value}, since=None, until=None, started_at=started, finished_at=finished
        )

    rows = AnalyticsPayload.objects.filter(collector="controller.config").order_by("id")
    assert rows.count() == 2
    assert list(rows.values_list("payload", flat=True)) == [{"v": 1}, {"v": 2}]


def test_persist_retains_unbounded_window_collections():
    started, finished = _times()
    for value in (1, 2):
        persist_analytics_payload(
            "unified_jobs", {"v": value}, since=None, until=None, started_at=started, finished_at=finished
        )

    rows = AnalyticsPayload.objects.filter(collector="controller.unified_jobs_dashboard").order_by("id")
    assert rows.count() == 2
    assert list(rows.values_list("payload", flat=True)) == [{"v": 1}, {"v": 2}]


def test_persist_retains_one_sided_window():
    started, finished = _times()
    since = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    for value in (1, 2):
        persist_analytics_payload(
            "main_hostmetric", {"v": value}, since=since, until=None, started_at=started, finished_at=finished
        )

    rows = AnalyticsPayload.objects.filter(collector="controller.main_hostmetric", since=since, until=None).order_by(
        "id"
    )
    assert rows.count() == 2
    assert list(rows.values_list("payload", flat=True)) == [{"v": 1}, {"v": 2}]


def test_persist_storage_failure_is_swallowed():
    started, finished = _times()
    with patch("apps.analytics.persist.AnalyticsPayload.objects.create", side_effect=RuntimeError("database down")):
        persist_analytics_payload("config", {}, since=None, until=None, started_at=started, finished_at=finished)


def test_generic_collection_passes_raw_data_to_persistence():
    from apps.tasks.utils import generic_collect_metrics

    started, finished = _times()
    raw_data = {"rows": [1, 2]}
    collector = MagicMock()
    collector.gather.return_value = raw_data
    collector_func = MagicMock(return_value=collector)
    since = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

    with patch("apps.analytics.persist.persist_analytics_payload") as persist:
        result = generic_collect_metrics(
            collector_type="test_type",
            collector_registry={"test_type": {"collector_func": collector_func, "rollup_processor": None}},
            collection_mode="hourly",
            timestamp=started,
            db_connection=MagicMock(),
            collector_kwargs={"since": since, "until": None},
        )

    assert result["status"] == "success"
    persist.assert_called_once_with(
        "test_type",
        raw_data,
        since=since,
        until=None,
        started_at=ANY,
        finished_at=ANY,
    )


def test_generic_collection_does_not_persist_analytics_only_data_to_hourly_table():
    from apps.tasks.utils import generic_collect_metrics

    collector = MagicMock()
    collector.gather.return_value = {"rows": [1, 2]}
    collector_func = MagicMock(return_value=collector)

    with (
        patch("apps.analytics.persist.persist_analytics_payload"),
        patch("apps.tasks.utils._persist_collection") as persist_collection,
    ):
        result = generic_collect_metrics(
            collector_type="analytics_only",
            collector_registry={
                "analytics_only": {
                    "collector_func": collector_func,
                    "rollup_processor": None,
                    "persist_to_hourly": False,
                }
            },
            collection_mode="hourly",
            timestamp=_times()[0],
            db_connection=MagicMock(),
        )

    assert result["status"] == "success"
    persist_collection.assert_not_called()
