"""Tests for the read-only analytics API."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.analytics.models import AnalyticsPayload
from apps.tasks.models import Task

pytestmark = [pytest.mark.unit, pytest.mark.django_db]

ROOT = "/api/v1/analytics/"
UNIFIED = "controller.unified_jobs_dashboard"
CONFIG = "controller.config"


def _row(collector, since, until, payload=None):
    started = datetime(2026, 8, 17, 10, tzinfo=UTC)
    return AnalyticsPayload.objects.create(
        collector=collector,
        since=since,
        until=until,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        payload=payload or {"collector": collector},
    )


def test_root_lists_enabled_collectors(authenticated_client):
    response = authenticated_client.get(ROOT)

    assert response.status_code == 200
    names = {collector["name"] for collector in response.json()["collectors"]}
    assert UNIFIED in names
    assert CONFIG in names
    unified = next(collector for collector in response.json()["collectors"] if collector["name"] == UNIFIED)
    assert unified["description"] == (
        "Job executions, including status and timing, organization, inventory, project, template, "
        "execution environment, launcher, labels, and host count."
    )
    assert "service.task_executions_service" not in names
    assert "dashboard.dashboard_jobs" not in names
    assert all(
        collector["rows_url"].startswith("http://testserver/api/v1/analytics/")
        for collector in response.json()["collectors"]
    )
    assert all(
        collector["collect_url"].endswith(f"/api/v1/analytics/{collector['name']}/collect/")
        for collector in response.json()["collectors"]
    )
    assert all("last_collect" in collector for collector in response.json()["collectors"])
    assert all(collector["last_collect"] is None for collector in response.json()["collectors"])


def test_root_reports_latest_finished_collection_per_collector(authenticated_client):
    older = _row(CONFIG, None, None)
    newer = _row(CONFIG, None, None)
    _row(UNIFIED, datetime(2026, 8, 17, 10, tzinfo=UTC), datetime(2026, 8, 17, 11, tzinfo=UTC))

    older.finished_at = datetime(2026, 8, 17, 10, 1, tzinfo=UTC)
    older.save(update_fields=["finished_at"])
    newer.finished_at = datetime(2026, 8, 17, 10, 3, tzinfo=UTC)
    newer.save(update_fields=["finished_at"])

    response = authenticated_client.get(ROOT)

    assert response.status_code == 200
    collectors = {collector["name"]: collector for collector in response.json()["collectors"]}
    assert collectors[CONFIG]["last_collect"] == "2026-08-17T10:03:00Z"
    assert collectors[UNIFIED]["last_collect"] == "2026-08-17T10:00:01Z"


def test_root_browsable_response_links_collector_urls(authenticated_client):
    response = authenticated_client.get(ROOT, HTTP_ACCEPT="text/html")

    assert response.status_code == 200
    assert 'href="http://testserver/api/v1/analytics/controller.config/"' in response.text
    assert "accepts_since_until" in response.text
    assert "collect_url" in response.text


def test_rows_browsable_response_shows_field_description(authenticated_client):
    response = authenticated_client.get(f"{ROOT}{CONFIG}/", HTTP_ACCEPT="text/html")

    assert response.status_code == 200
    assert "The linked collector row endpoints return paginated collection envelopes." in response.text
    assert "payload" in response.text


def test_rows_pagination_links_are_absolute(authenticated_client):
    for index in range(2):
        _row(
            CONFIG,
            datetime(2026, 8, 17, 10 + index, tzinfo=UTC),
            datetime(2026, 8, 17, 11 + index, tzinfo=UTC),
            {"index": index},
        )

    response = authenticated_client.get(f"{ROOT}{CONFIG}/?page_size=1")

    assert response.status_code == 200
    body = response.json()
    assert body["next"].startswith("http://testserver/api/v1/analytics/")
    assert body["previous"] is None


def test_root_requires_auth(api_client):
    assert api_client.get(ROOT).status_code in (401, 403)


def test_rows_unknown_collector_404(authenticated_client):
    assert authenticated_client.get(f"{ROOT}nope.nope/").status_code == 404


def test_rows_return_collection_envelope(authenticated_client):
    _row(UNIFIED, datetime(2026, 8, 17, 10, tzinfo=UTC), datetime(2026, 8, 17, 11, tzinfo=UTC), {"v": 1})

    response = authenticated_client.get(f"{ROOT}{UNIFIED}/")

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["collector"] == UNIFIED
    assert result["source"] == "local"
    assert result["payload"] == {"v": 1}
    assert "started_at" in result
    assert "finished_at" in result
    assert "state" not in result


def test_rows_retain_duplicate_collections(authenticated_client):
    since = datetime(2026, 8, 17, 10, tzinfo=UTC)
    until = datetime(2026, 8, 17, 11, tzinfo=UTC)
    _row(UNIFIED, since, until, {"v": 1})
    _row(UNIFIED, since, until, {"v": 2})

    response = authenticated_client.get(f"{ROOT}{UNIFIED}/")

    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_rows_window_overlap_until_exclusive(authenticated_client):
    _row(UNIFIED, datetime(2026, 8, 17, 10, tzinfo=UTC), datetime(2026, 8, 17, 11, tzinfo=UTC))
    _row(UNIFIED, datetime(2026, 8, 17, 11, tzinfo=UTC), datetime(2026, 8, 17, 12, tzinfo=UTC))

    response = authenticated_client.get(f"{ROOT}{UNIFIED}/?since=2026-08-17T11:00:00Z&until=2026-08-17T12:00:00Z")

    assert response.status_code == 200
    windows = {(row["since"], row["until"]) for row in response.json()["results"]}
    assert len(windows) == 1


def test_rows_snapshot_null_window_always_matches(authenticated_client):
    _row(CONFIG, None, None, {"v": 1})
    response = authenticated_client.get(f"{ROOT}{CONFIG}/?since=2026-08-17T11:00:00Z&until=2026-08-17T12:00:00Z")

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_rows_one_sided_window_is_open_ended(authenticated_client):
    _row(UNIFIED, datetime(2026, 8, 17, 10, tzinfo=UTC), None)
    _row(UNIFIED, None, datetime(2026, 8, 17, 11, tzinfo=UTC))

    response = authenticated_client.get(f"{ROOT}{UNIFIED}/?until=2026-08-17T12:00:00Z")

    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_rows_invalid_since_400(authenticated_client):
    assert authenticated_client.get(f"{ROOT}{UNIFIED}/?since=notadate").status_code == 400


def test_rows_invalid_until_400(authenticated_client):
    assert authenticated_client.get(f"{ROOT}{UNIFIED}/?until=notadate").status_code == 400


def test_rows_reversed_window_400(authenticated_client):
    response = authenticated_client.get(f"{ROOT}{UNIFIED}/?since=2026-08-17T11:00:00Z&until=2026-08-17T10:00:00Z")

    assert response.status_code == 400


def test_collect_snapshot_creates_pending_task_without_collecting(authenticated_client):
    response = authenticated_client.post(f"{ROOT}{CONFIG}/collect/", {}, format="json")

    assert response.status_code == 202
    body = response.json()
    task = Task.objects.get(pk=body["task_id"])
    assert body["task_url"].endswith(f"/api/v1/tasks/{task.pk}/")
    assert body["task_data"] == {"collector": CONFIG}
    assert task.status == "pending"
    assert not AnalyticsPayload.objects.exists()


def test_collect_windowed_missing_until_uses_default(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{UNIFIED}/collect/",
        {"since": "2026-08-17T10:00:00Z"},
        format="json",
    )

    assert response.status_code == 202
    task_data = response.json()["task_data"]
    assert task_data["collector"] == UNIFIED
    assert task_data["since"] == "2026-08-17T10:00:00+00:00"
    assert task_data["until"] is not None


def test_collect_windowed_fills_default_window(authenticated_client):
    response = authenticated_client.post(f"{ROOT}{UNIFIED}/collect/", {}, format="json")

    assert response.status_code == 202
    task_data = response.json()["task_data"]
    assert task_data["collector"] == UNIFIED
    assert task_data["since"] is not None
    assert task_data["until"] is not None
    assert task_data["since"] < task_data["until"]


def test_collect_windowed_explicit_null_bounds_remain_unbounded(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{UNIFIED}/collect/",
        {"since": None, "until": None},
        format="json",
    )

    assert response.status_code == 202
    assert response.json()["task_data"] == {"collector": UNIFIED}


def test_collect_windowed_explicit_null_leaves_since_open(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{UNIFIED}/collect/",
        {"since": None},
        format="json",
    )

    assert response.status_code == 202
    task_data = response.json()["task_data"]
    assert task_data["collector"] == UNIFIED
    assert "since" not in task_data
    assert task_data["until"] is not None


def test_collect_windowed_explicit_null_leaves_until_open(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{UNIFIED}/collect/",
        {"until": None},
        format="json",
    )

    assert response.status_code == 202
    task_data = response.json()["task_data"]
    assert task_data["collector"] == UNIFIED
    assert task_data["since"] is not None
    assert "until" not in task_data


def test_collect_rejects_invalid_since(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{UNIFIED}/collect/",
        {"since": "not-a-date"},
        format="json",
    )

    assert response.status_code == 400


def test_collect_rejects_configured_source(authenticated_client):
    response = authenticated_client.post(
        f"{ROOT}{CONFIG}/collect/",
        {"source": "controller"},
        format="json",
    )

    assert response.status_code == 400
