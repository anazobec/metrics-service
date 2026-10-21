"""Tests for analytics storage models."""

from datetime import UTC, datetime

import pytest

from apps.analytics.models import AnalyticsPayload, APIUsageCounter

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


def test_api_usage_counter_increments_and_summarizes_by_endpoint_and_status():
    usage_date = datetime(2026, 8, 17, tzinfo=UTC).date()

    APIUsageCounter.record_request(usage_date, "analytics:v1:root", 2)
    APIUsageCounter.record_request(usage_date, "analytics:v1:root", 2)
    APIUsageCounter.record_request(usage_date, "analytics:v1:rows", 4)
    APIUsageCounter.record_request(usage_date.replace(day=18), "analytics:v1:collect", 5)

    summary = APIUsageCounter.daily_summary(usage_date)

    assert summary["available"] is True
    assert summary["total_requests"] == 3
    assert summary["by_endpoint"]["analytics:v1:root"]["2xx"] == 2
    assert summary["by_endpoint"]["analytics:v1:rows"]["4xx"] == 1
    assert APIUsageCounter.objects.filter(usage_date=usage_date).count() == 2


def test_api_usage_counter_returns_empty_endpoint_map_when_empty():
    summary = APIUsageCounter.daily_summary(datetime(2026, 8, 17, tzinfo=UTC).date())

    assert summary["available"] is True
    assert summary["total_requests"] == 0
    assert summary["by_endpoint"] == {}


def test_api_usage_counter_stores_only_aggregate_fields():
    field_names = {field.name for field in APIUsageCounter._meta.get_fields()}

    assert field_names == {"id", "usage_date", "endpoint_name", "status_class", "request_count"}
