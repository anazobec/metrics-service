"""Tests for analytics storage models."""

from datetime import UTC, datetime

import pytest

from apps.analytics.models import AnalyticsPayload

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _times():
    started = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 17, 10, 1, tzinfo=UTC)
    return started, finished


def test_analytics_payload_string_for_window():
    started, finished = _times()
    row = AnalyticsPayload.objects.create(
        collector="controller.unified_jobs_dashboard",
        since=started,
        until=finished,
        started_at=started,
        finished_at=finished,
        payload=[{"id": 1}],
    )

    assert str(row) == f"controller.unified_jobs_dashboard [local] ({started} \u2192 {finished})"


def test_analytics_payload_string_for_snapshot():
    started, finished = _times()
    row = AnalyticsPayload.objects.create(
        collector="controller.config",
        started_at=started,
        finished_at=finished,
        payload={"version": "1.2.3"},
    )

    assert str(row) == "controller.config [local] (snapshot)"


def test_analytics_payload_retains_multiple_snapshots():
    started, finished = _times()
    for offset in range(2):
        AnalyticsPayload.objects.create(
            collector="controller.config",
            started_at=started.replace(minute=started.minute + offset),
            finished_at=finished.replace(minute=finished.minute + offset),
            payload={"version": str(offset)},
        )

    assert AnalyticsPayload.objects.filter(collector="controller.config").count() == 2


def test_analytics_payload_retains_duplicate_window():
    started, finished = _times()
    window = {"collector": "controller.unified_jobs_dashboard", "since": started, "until": finished}
    for value in (1, 2):
        AnalyticsPayload.objects.create(started_at=started, finished_at=finished, payload={"v": value}, **window)

    assert AnalyticsPayload.objects.filter(**window).count() == 2
