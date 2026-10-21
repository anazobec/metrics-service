"""Tests for generic API request usage middleware."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import resolve
from django.utils import timezone

from apps.analytics.middleware import APIUsageMiddleware, record_api_usage
from apps.analytics.models import APIUsageCounter

pytestmark = [pytest.mark.unit, pytest.mark.django_db]

ROOT = "/api/v1/analytics/"
CONFIG = "controller.config"


@pytest.mark.parametrize(
    ("method", "path", "status_code", "endpoint_name"),
    [
        ("GET", ROOT, 200, "analytics:v1:root"),
        ("GET", ROOT, 401, "analytics:v1:root"),
        ("GET", f"{ROOT}{CONFIG}/", 302, "analytics:v1:rows"),
        ("GET", f"{ROOT}unknown.collector/", 404, "analytics:v1:rows"),
        ("POST", f"{ROOT}{CONFIG}/collect/", 202, "analytics:v1:collect"),
        ("DELETE", f"{ROOT}{CONFIG}/collect/", 500, "analytics:v1:collect"),
    ],
)
def test_middleware_counts_resolved_api_requests_by_status_class(method, path, status_code, endpoint_name):
    request = RequestFactory().generic(method, path)
    request.resolver_match = resolve(path)
    expected_date = timezone.now().date()
    middleware = APIUsageMiddleware(lambda _request: HttpResponse(status=status_code))

    response = middleware(request)

    assert response.status_code == status_code
    counter = APIUsageCounter.objects.get(
        usage_date=expected_date,
        endpoint_name=endpoint_name,
        status_class=status_code // 100,
    )
    assert counter.request_count == 1


@pytest.mark.parametrize("path", ["/health/", "/api/v1/tasks/", "/api/v1/users/"])
def test_middleware_ignores_non_analytics_api_routes(path):
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    middleware = APIUsageMiddleware(lambda _request: HttpResponse(status=200))

    middleware(request)

    assert not APIUsageCounter.objects.exists()


@pytest.mark.parametrize("path", ["/api/v1/metrics", "/api/v1/metrics/", "/api/v1/docs/schema/"])
def test_middleware_ignores_infrastructure_and_documentation_routes(path):
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    middleware = APIUsageMiddleware(lambda _request: HttpResponse(status=200))

    middleware(request)

    assert not APIUsageCounter.objects.exists()


def test_counter_write_failure_does_not_change_api_response():
    request = RequestFactory().get(ROOT)
    request.resolver_match = resolve(ROOT)
    middleware = APIUsageMiddleware(lambda _request: HttpResponse(status=503))

    with patch.object(APIUsageCounter, "record_request", side_effect=RuntimeError("db unavailable")):
        response = middleware(request)

    assert response.status_code == 503


@pytest.mark.parametrize("path", ["/api/metrics/v1/analytics/", "/metrics/api/v1/analytics/"])
@override_settings(URL_PREFIX="/api/metrics")
def test_preflight_request_resolves_endpoint_with_service_url_prefix(path):
    request = RequestFactory().options(path)
    usage_date = timezone.now().date()

    record_api_usage(request, 200)

    counter = APIUsageCounter.objects.get(
        usage_date=usage_date,
        endpoint_name="analytics:v1:root",
        status_class=2,
    )
    assert counter.request_count == 1


def test_counter_increment_uses_atomic_addition_for_repeated_calls():
    usage_date = datetime(2026, 8, 17, tzinfo=UTC).date()

    for _ in range(10):
        APIUsageCounter.record_request(usage_date, "analytics:v1:rows", 2)

    counter = APIUsageCounter.objects.get(
        usage_date=usage_date,
        endpoint_name="analytics:v1:rows",
        status_class=2,
    )
    assert counter.request_count == 10
