"""Best-effort database telemetry for metrics-service API requests."""

import logging

from django.conf import settings
from django.urls import Resolver404, resolve
from django.utils import timezone

logger = logging.getLogger(__name__)


_EXCLUDED_API_ROUTE_PREFIXES = ("api/v1/metrics", "api/v1/docs")
# Keep this PR scoped to BYO-BI analytics routes; add other API namespaces here to expand coverage.
_TRACKED_API_VIEW_NAME_PREFIXES = ("analytics:",)


def _path_matches_prefix(path, prefix):
    """Return whether path is exactly a prefix or one of its child paths."""
    return path == prefix or path.startswith(f"{prefix}/")


def _strip_path_prefix(path, prefix):
    """Remove a service prefix from a path, preserving the root path."""
    if _path_matches_prefix(path, prefix):
        return path[len(prefix) :] or "/"
    return path


def _normalize_prefixed_path(path, api_prefix, service_prefix):
    """Translate a configured service URL prefix to the canonical API path."""
    if _path_matches_prefix(path, api_prefix):
        return "/api" + path[len(api_prefix) :]
    return _strip_path_prefix(path, service_prefix)


def _normalize_request_path(path):
    """Remove the configured service prefix before resolving a request path."""
    url_prefix = getattr(settings, "URL_PREFIX", None)
    if url_prefix:
        api_prefix = url_prefix.rstrip("/")
        service_prefix = api_prefix[4:] if api_prefix.startswith("/api/") else api_prefix
        return _normalize_prefixed_path(path, api_prefix, service_prefix)

    service_name = settings.ROOT_URLCONF.split(".")[0].replace("_", "-")
    return _strip_path_prefix(path, f"/{service_name}")


def _resolve_request_match(request):
    """Return Django's route match, resolving it from the request path if needed."""
    match = getattr(request, "resolver_match", None)
    if match is not None:
        return match
    try:
        return resolve(_normalize_request_path(request.path_info))
    except Resolver404:
        return None


def _is_api_route(route):
    """Return whether a normalized route belongs to the API URL tree."""
    return route == "api" or route.startswith("api/")


def _is_excluded_api_route(route):
    """Return whether a route belongs to API infrastructure or documentation."""
    return any(_path_matches_prefix(route, prefix) for prefix in _EXCLUDED_API_ROUTE_PREFIXES)


def _tracked_api_endpoint_name(match):
    """Return the endpoint name when a resolved match is a tracked analytics API route."""
    route = str(getattr(match, "route", "")).lstrip("^").rstrip("$").strip("/")
    if not _is_api_route(route) or _is_excluded_api_route(route):
        return None

    endpoint_name = getattr(match, "view_name", None)
    if endpoint_name and endpoint_name.startswith(_TRACKED_API_VIEW_NAME_PREFIXES):
        return endpoint_name
    return None


def _endpoint_for_request(request):
    """Return a stable endpoint label for an API route, excluding infrastructure endpoints."""
    match = _resolve_request_match(request)
    if match is None:
        return None
    return _tracked_api_endpoint_name(match)


def record_api_usage(request, status_code: int) -> None:
    """Persist one API endpoint/status counter without affecting the response."""
    try:
        endpoint_name = _endpoint_for_request(request)
        if endpoint_name is None:
            return

        from apps.analytics.models import APIUsageCounter

        APIUsageCounter.record_request(
            usage_date=timezone.now().date(),
            endpoint_name=endpoint_name,
            status_class=status_code // 100,
        )
    except Exception:
        # Telemetry must not change the response returned to the API caller.
        logger.exception("Failed to record API usage counter")


class APIUsageMiddleware:
    """Count requests handled by metrics-service API endpoints without recording request data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        record_api_usage(request, response.status_code)
        return response
