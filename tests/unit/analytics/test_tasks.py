"""Tests for the on-demand analytics collection task."""

from unittest.mock import ANY, MagicMock, patch

import pandas as pd
import pytest

from apps.analytics.models import AnalyticsPayload
from apps.analytics.tasks import collect_analytics_on_demand

pytestmark = [pytest.mark.unit, pytest.mark.django_db]

UNIFIED = "controller.unified_jobs_dashboard"


def _fake_collector(df):
    """Return a collector_func stand-in whose instance .gather() yields df."""
    instance = MagicMock()
    instance.gather.return_value = df
    return MagicMock(return_value=instance)


def test_on_demand_unknown_collector_errors():
    result = collect_analytics_on_demand(collector="nope.nope")
    assert result["status"] == "error"
    assert not AnalyticsPayload.objects.exists()


@pytest.mark.parametrize("collector", ["service.task_executions_service", "dashboard.dashboard_jobs"])
def test_on_demand_disabled_collector_errors(collector):
    result = collect_analytics_on_demand(collector=collector)

    assert result["status"] == "error"
    assert "Unknown or disabled collector" in result["error"]
    assert not AnalyticsPayload.objects.exists()


def test_on_demand_missing_collector_errors():
    result = collect_analytics_on_demand()
    assert result["status"] == "error"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"since": "not-a-date"}, "Invalid since"),
        ({"until": "not-a-date"}, "Invalid until"),
        (
            {"since": "2026-08-17T11:00:00Z", "until": "2026-08-17T10:00:00Z"},
            "until must be after since",
        ),
    ],
)
def test_on_demand_invalid_window_errors(kwargs, message):
    result = collect_analytics_on_demand(collector=UNIFIED, **kwargs)

    assert result["status"] == "error"
    assert message in result["error"]
    assert not AnalyticsPayload.objects.exists()


@patch("apps.analytics.tasks.get_db_connection")
def test_on_demand_happy_path_persists(_mock_db):
    df = pd.DataFrame([{"id": 1}])
    func = _fake_collector(df)
    db_connection = _mock_db.return_value
    db_connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        '"2aebf27a-42ee-4e15-93d0-8bd5f9b52219"',
    )
    with patch("apps.analytics.tasks._collector_func", return_value=func):
        result = collect_analytics_on_demand(
            collector=UNIFIED, since="2026-08-17T10:00:00Z", until="2026-08-17T11:00:00Z"
        )
    assert result["status"] == "success"
    row = AnalyticsPayload.objects.get(collector=UNIFIED)
    assert row.payload == [{"id": 1}]
    assert row.cluster_id == "2aebf27a-42ee-4e15-93d0-8bd5f9b52219"
    # since/until are threaded through to the collector.
    _, kwargs = func.call_args
    assert kwargs["since"] is not None and kwargs["until"] is not None


@patch("apps.analytics.tasks.get_db_connection")
def test_on_demand_failure_does_not_persist_partial_payload(_mock_db):
    since, until = "2026-08-17T10:00:00Z", "2026-08-17T11:00:00Z"
    boom = MagicMock()
    boom.return_value.gather.side_effect = RuntimeError("kaboom")
    with patch("apps.analytics.tasks._collector_func", return_value=boom):
        result = collect_analytics_on_demand(collector=UNIFIED, since=since, until=until)
    assert result["status"] == "error"
    assert not AnalyticsPayload.objects.exists()


@patch("apps.analytics.tasks.get_db_connection")
def test_on_demand_persistence_failure_fails_task(_mock_db):
    func = _fake_collector({"version": "9"})
    with (
        patch("apps.analytics.tasks._collector_func", return_value=func),
        patch(
            "apps.analytics.tasks.persist_analytics_payload",
            side_effect=RuntimeError("database down"),
        ) as persist,
    ):
        result = collect_analytics_on_demand(collector="controller.config")

    assert result["status"] == "error"
    assert result["error"] == "Persistence failed: database down"
    persist.assert_called_once_with(
        "config",
        {"version": "9"},
        since=None,
        until=None,
        started_at=ANY,
        finished_at=ANY,
        raise_on_error=True,
        db_connection=ANY,
    )


@patch("apps.analytics.tasks.get_db_connection")
def test_on_demand_snapshot_no_window(_mock_db):
    func = _fake_collector({"version": "9"})
    with patch("apps.analytics.tasks._collector_func", return_value=func):
        result = collect_analytics_on_demand(collector="controller.config")
    assert result["status"] == "success"
    # Snapshot collectors are called without since/until.
    _, kwargs = func.call_args
    assert "since" not in kwargs and "until" not in kwargs
    assert AnalyticsPayload.objects.get(collector="controller.config").payload == {"version": "9"}
