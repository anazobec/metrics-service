"""Tests for the read-only analytics API."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.analytics.models import AnalyticsPayload, APIUsageCounter
from apps.tasks.models import Task

pytestmark = [pytest.mark.unit, pytest.mark.django_db]

ROOT = "/api/v1/analytics/"
UNIFIED = "controller.unified_jobs_dashboard"
CONFIG = "controller.config"
INSTANCE_INFO = "controller.instance_info"


def _row(collector, since, until, payload=None, cluster_id=None):
    started = datetime(2026, 8, 17, 10, tzinfo=UTC)
    return AnalyticsPayload.objects.create(
        collector=collector,
        cluster_id=cluster_id,
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
        "execution environment, launcher, labels, and host count. The nested installed_collections "
        "object is keyed by collection FQCN."
    )
    assert unified["payload_shape"] == "array"
    assert unified["key_field"] is None
    config = next(collector for collector in response.json()["collectors"] if collector["name"] == CONFIG)
    assert config["payload_shape"] == "object"
    assert config["key_field"] is None
    # A collector keyed by data reports what its top-level keys mean; the two object shapes
    # are only distinguishable by that value.
    instance_info = next(collector for collector in response.json()["collectors"] if collector["name"] == INSTANCE_INFO)
    assert instance_info["payload_shape"] == "object"
    assert instance_info["key_field"] == "uuid"
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
    assert "payload_shape" in response.text
    assert "key_field" in response.text
    assert "collect_url" in response.text


def test_openapi_associates_every_enabled_collector_with_a_payload_schema():
    from drf_spectacular.generators import SchemaGenerator

    from apps.analytics.registry import enabled_collectors

    schema = SchemaGenerator().get_schema(request=None, public=True)
    operation = schema["paths"]["/api/v1/analytics/{collector}/"]["get"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    components = schema["components"]["schemas"]
    result_schema = components["AnalyticsPayloadByCollector"]

    assert response_schema["$ref"] == "#/components/schemas/PaginatedAnalyticsPayloadList"
    assert result_schema["discriminator"]["propertyName"] == "collector"
    assert set(result_schema["discriminator"]["mapping"]) == set(enabled_collectors())
    assert all(ref["$ref"].split("/")[-1] in components for ref in result_schema["oneOf"])
    assert all(
        entry.payload_schema.name in components
        for entry in enabled_collectors().values()
        if entry.payload_schema.kind == "array"
    )
    assert {parameter["name"] for parameter in operation["parameters"]} >= {"page", "page_size", "since", "until"}


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


def test_api_requests_are_counted_for_successful_and_failed_responses(authenticated_client):
    usage_date = timezone.now().date()

    unauthorized_response = APIClient().get(ROOT)
    discovery_response = authenticated_client.get(ROOT)
    rows_response = authenticated_client.get(f"{ROOT}nope.nope/")
    collect_response = authenticated_client.post(f"{ROOT}{CONFIG}/collect/", {}, format="json")

    assert unauthorized_response.status_code in (401, 403)
    assert discovery_response.status_code == 200
    assert rows_response.status_code == 404
    assert collect_response.status_code == 202

    summary = APIUsageCounter.daily_summary(usage_date)
    assert summary["total_requests"] == 4
    assert summary["by_endpoint"]["analytics:v1:root"]["4xx"] == 1
    assert summary["by_endpoint"]["analytics:v1:root"]["2xx"] == 1
    assert summary["by_endpoint"]["analytics:v1:rows"]["4xx"] == 1
    assert summary["by_endpoint"]["analytics:v1:collect"]["2xx"] == 1


def test_api_preflight_requests_are_counted():
    usage_date = timezone.now().date()

    response = APIClient().options(ROOT)

    assert response.status_code in (200, 403)
    status_class = response.status_code // 100
    summary = APIUsageCounter.daily_summary(usage_date)
    assert summary["total_requests"] == 1
    assert summary["by_endpoint"]["analytics:v1:root"][f"{status_class}xx"] == 1


def test_rows_return_collection_envelope(authenticated_client):
    _row(
        UNIFIED,
        datetime(2026, 8, 17, 10, tzinfo=UTC),
        datetime(2026, 8, 17, 11, tzinfo=UTC),
        {"v": 1},
        cluster_id="2aebf27a-42ee-4e15-93d0-8bd5f9b52219",
    )

    response = authenticated_client.get(f"{ROOT}{UNIFIED}/")

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["collector"] == UNIFIED
    assert result["source"] == "local"
    assert result["cluster_id"] == "2aebf27a-42ee-4e15-93d0-8bd5f9b52219"
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
