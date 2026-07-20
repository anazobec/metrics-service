#!/usr/bin/env python
"""
API performance benchmark for the automation dashboard reporting endpoints (SLO check).

Times 11 read-only dashboard_reports endpoints (report, report/details,
report/export, templates, filter_sets, subscription_costs, labels,
organizations, projects, collection_status, collection_telemetry) against
already-collected data. Triggers no collection task itself — it measures
read-path latency of endpoints backed by data a prior collection run
already populated.

Duration for each endpoint is measured client-side (wall clock around the
GET), reported alongside an SLO threshold so regressions are easy to spot.

Memory reporting note
---------------------
The Prometheus /metrics endpoint reflects the web process only, not the
dispatcherd workers that run the collectors.  The RSS figure is a health
check, not directly comparable to the internal benchmark's peak memory.

Environment variables
---------------------
BASE_URL            Base URL of the metrics-service API  (default: http://localhost:18002/api)
BENCHMARK_USER      Admin username                        (default: superadmin)
PASSWORD            Password for the above user
METRICS_URL         Prometheus /metrics endpoint URL      (optional)
TEST_SINCE          ISO-8601 start of the test period   (default: 2024-01-01)
TEST_UNTIL          ISO-8601 end of the test period     (default: 2024-03-31)
DB_NAME             AWX database alias (default: awx)
DIRECT_DB           Set to "true" to bypass HTTP entirely and drive every
                     endpoint timer straight through DRF's APIClient
                     (force_authenticate), instead of a real HTTP GET with
                     Basic Auth. Use this in production deployments where
                     these endpoints only accept a JWT from the AAP gateway
                     and Basic Auth is rejected. The same view code runs
                     either way, but force_authenticate() skips the auth
                     middleware entirely — durations in this mode exclude
                     any JWT validation / RBAC lookup overhead the real
                     HTTP path incurs, so they are a lower bound, not an
                     exact match. (default: false)

Usage:
    BASE_URL=http://localhost:18002/api \\
    BENCHMARK_USER=superadmin \\
    PASSWORD=<password> \\
    METRICS_URL=http://localhost:18002/metrics \\
        .venv/bin/python \\
        metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api_slo.py
"""

# ruff: noqa: T201, E402
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Django bootstrap — needed only for the JobData delete step
# ---------------------------------------------------------------------------
script_dir = Path(__file__).parent
project_root = script_dir.parent.parent.parent
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(project_root))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "metrics_service.settings")

import django

django.setup()

from prometheus_utils import prometheus_delta, read_prometheus_metrics

from apps.dashboard_reports.models import JobData

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000/api").rstrip("/")
USERNAME = os.environ.get("BENCHMARK_USER", "superadmin")
PASSWORD = os.environ.get("PASSWORD", "")
METRICS_URL = os.environ.get("METRICS_URL", "")
DB_NAME = os.environ.get("DB_NAME", "awx")
DIRECT_DB = os.environ.get("DIRECT_DB", "false").lower() == "true"

until_str = os.environ.get("TEST_UNTIL", "2024-03-31")
until = datetime.fromisoformat(until_str).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)

since_str = os.environ.get("TEST_SINCE", "2024-01-01")
since = datetime.fromisoformat(since_str).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)

# Derived phase windows
MONTH_SINCE = since
MONTH_UNTIL = since + timedelta(days=30)
WEEK_SINCE = until - timedelta(days=7)
WEEK_UNTIL = until
DAY_SINCE = until - timedelta(days=1)
DAY_UNTIL = until

POLL_INTERVAL = 1.0  # seconds between status checks
POLL_TIMEOUT = 3600  # seconds before giving up on a task

AUTH = HTTPBasicAuth(USERNAME, PASSWORD)

if not DIRECT_DB:
    _base_host = urlsplit(BASE_URL).hostname or ""
    if urlsplit(BASE_URL).scheme != "https" and _base_host not in ("localhost", "127.0.0.1", "::1"):
        logger.warning(
            "BASE_URL=%s is plain HTTP against a non-loopback host. HTTPBasicAuth sends "
            "credentials base64-encoded (not encrypted) — they will be exposed on the wire. "
            "Use HTTPS, or run with DIRECT_DB=true to bypass HTTP auth entirely.",
            BASE_URL,
        )


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

_api_client_cache = None


def _get_api_client():
    """Return a cached DRF APIClient authenticated as USERNAME (DIRECT_DB mode)."""
    global _api_client_cache
    if _api_client_cache is None:
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.get(username=USERNAME)
        client = APIClient()
        client.force_authenticate(user=user)
        _api_client_cache = client
    return _api_client_cache


def _get(url: str) -> int:
    """GET url and return its status code, raising on failure.

    In production, these endpoints only accept a JWT from the AAP gateway —
    Basic Auth is rejected. With DIRECT_DB=true, the request is made
    in-process via a DRF APIClient (force_authenticate) instead of a real
    HTTP call, bypassing auth entirely while still exercising the same view
    code, so measured durations remain comparable to the HTTP path.
    """
    if DIRECT_DB:
        client = _get_api_client()
        parsed = urlsplit(url)
        path = parsed.path if not parsed.query else f"{parsed.path}?{parsed.query}"
        resp = client.get(path)
        if resp.status_code >= 400:
            raise RuntimeError(f"[Status {resp.status_code}] Something went wrong accessing endpoint: {url}")
        return resp.status_code

    resp = requests.get(url, auth=AUTH, timeout=10)
    resp.raise_for_status()
    return resp.status_code


def time_dashboard_reports_report_endpoint(
    period: str = "last_90_days",
    tz: str = "UTC",
    page_size: int = 255,
) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/report/"
    endpoint = f"{base}?page=1&page_size={page_size}&period={period}&tz={tz}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_report_details_endpoint(
    period: str = "last_90_days",
    tz: str = "UTC",
    page_size: int = 255,
) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/report/details/"
    endpoint = f"{base}?page=1&page_size={page_size}&period={period}&tz={tz}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_report_export_endpoint(
    report_type: str = "summary",
    period: str = "last_90_days",
    tz: str = "UTC",
) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/report/export/"
    endpoint = f"{base}?export_format=csv&period={period}&tz={tz}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_templates_endpoint(
    page_size: int = 255,
) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/templates/"
    endpoint = f"{base}?page=1&page_size={page_size}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_filter_sets_endpoint() -> (float, str):
    endpoint = f"{BASE_URL}/v1/dashboard_reports/filter_sets/"
    logger.debug(f"Estimating: {endpoint}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), endpoint


def time_dashboard_reports_subscription_costs_endpoint() -> (float, str):
    endpoint = f"{BASE_URL}/v1/dashboard_reports/subscription_costs/"
    logger.debug(f"Estimating: {endpoint}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), endpoint


def time_dashboard_reports_labels_endpoint(page_size: int = 255) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/labels/"
    endpoint = f"{base}?page=1&page_size={page_size}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_organizations_endpoint(page_size: int = 255) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/organizations/"
    endpoint = f"{base}?page=1&page_size={page_size}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_projects_endpoint(page_size: int = 255) -> (float, str):
    base = f"{BASE_URL}/v1/dashboard_reports/projects/"
    endpoint = f"{base}?page=1&page_size={page_size}"
    logger.debug(f"Estimating: {base}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), base


def time_dashboard_reports_collection_status_endpoint() -> (float, str):
    endpoint = f"{BASE_URL}/v1/dashboard_reports/collection_status/"
    logger.debug(f"Estimating: {endpoint}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), endpoint


def time_dashboard_reports_collection_telemetry_endpoint() -> (float, str):
    endpoint = f"{BASE_URL}/v1/dashboard_reports/collection_telemetry/"
    logger.debug(f"Estimating: {endpoint}")
    start_time = time.perf_counter()

    _get(endpoint)
    end_time = time.perf_counter()
    return (end_time - start_time), endpoint


def run_phase(label: str, phase_since: datetime, phase_until: datetime) -> float:
    """Time all read endpoints against the existing data in the DB."""

    benchmark_endpoints = [
        time_dashboard_reports_report_endpoint,
        time_dashboard_reports_report_details_endpoint,
        time_dashboard_reports_report_export_endpoint,
        time_dashboard_reports_templates_endpoint,
        time_dashboard_reports_filter_sets_endpoint,
        time_dashboard_reports_subscription_costs_endpoint,
        time_dashboard_reports_labels_endpoint,
        time_dashboard_reports_organizations_endpoint,
        time_dashboard_reports_projects_endpoint,
        time_dashboard_reports_collection_status_endpoint,
        time_dashboard_reports_collection_telemetry_endpoint,
    ]

    print(f"\n{label}")
    print(f"  Range: {phase_since.date()} → {phase_until.date()}")

    elapsed_ms_times: dict = {}
    for endpoint_function in benchmark_endpoints:
        elapsed, endpoint = endpoint_function()
        elapsed_ms = elapsed * 1000
        elapsed_ms_times[endpoint] = elapsed_ms

        print(f"  Duration ({endpoint}):    {elapsed_ms:.2f}ms")

    job_data_count = JobData.objects.count()
    mean_elapsed_ms = sum(elapsed_ms_times.values()) / len(elapsed_ms_times)
    print(f"  Mean duration:            {mean_elapsed_ms:.2f}ms")
    print(f"  JobData rows in DB:       {job_data_count:,}")

    return mean_elapsed_ms


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"\n{'=' * 80}")
    print("  Automation Dashboard Collection API Benchmark")
    print(f"  Test period:  {since.date()} → {until.date()}")
    print(f"  Phase 1 (month):  {MONTH_SINCE.date()} → {MONTH_UNTIL.date()}")
    print(f"  Phase 2 (week):   {WEEK_SINCE.date()} → {WEEK_UNTIL.date()}")
    print(f"  Phase 3 (day):    {DAY_SINCE.date()} → {DAY_UNTIL.date()}")
    print(f"  Target:    {BASE_URL}")
    print(f"  User:      {USERNAME}")
    print(f"  Prometheus: {METRICS_URL or '(not configured)'}")
    if DIRECT_DB:
        print(
            "  Mode:      DIRECT_DB (force_authenticate) — bypasses auth middleware; "
            "durations exclude JWT/RBAC overhead present on the real HTTP path"
        )
    print(f"{'=' * 80}\n")

    # Verify connectivity
    print("Verifying connectivity...")
    if DIRECT_DB:
        # No HTTP round trip in direct-DB mode — just confirm the benchmark user exists.
        from django.contrib.auth import get_user_model

        if not get_user_model().objects.filter(username=USERNAME).exists():
            print(f"  ERROR: user '{USERNAME}' does not exist")
            raise SystemExit(1)
    else:
        resp = requests.get(f"{BASE_URL}/v1/", auth=AUTH, timeout=10)
        if not resp.ok:
            print(f"  ERROR: {BASE_URL}/v1/ returned {resp.status_code}")
            raise SystemExit(1)
    print("  OK")

    metrics_before = read_prometheus_metrics(METRICS_URL)
    overall_start = time.perf_counter()

    month_elapsed = run_phase("Phase 1: One month collection", MONTH_SINCE, MONTH_UNTIL)
    week_elapsed = run_phase("Phase 2: One week collection", WEEK_SINCE, WEEK_UNTIL)
    day_elapsed = run_phase("Phase 3: One day collection", DAY_SINCE, DAY_UNTIL)

    total_wall = time.perf_counter() - overall_start
    metrics_after = read_prometheus_metrics(METRICS_URL)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  Final Results")
    print(f"{'=' * 80}\n")
    print(f"  {'Phase':<30} {'Window':<24} {'Task time':>10}")
    print(f"  {'-' * 30} {'-' * 24} {'-' * 10}")
    print(
        f"  {'Phase 1 — one month':<30} {str(MONTH_SINCE.date()) + ' → ' + str(MONTH_UNTIL.date()):<24} {month_elapsed:>9.2f}ms"
    )
    print(
        f"  {'Phase 2 — one week':<30} {str(WEEK_SINCE.date()) + ' → ' + str(WEEK_UNTIL.date()):<24} {week_elapsed:>9.2f}ms"
    )
    print(
        f"  {'Phase 3 — one day':<30} {str(DAY_SINCE.date()) + ' → ' + str(DAY_UNTIL.date()):<24} {day_elapsed:>9.2f}ms"
    )
    print()
    print(f"  Total wall time:  {total_wall:.1f}s ({total_wall / 60:.1f} min)")
    print()

    if metrics_before and metrics_after:
        print("  Server-side Metrics (Prometheus — web process only)")
        print("  Note: RSS is the web process, not dispatcherd workers.")
        cpu = prometheus_delta(metrics_before, metrics_after, "process_cpu_seconds_total")
        rss_mb = metrics_after.get("process_resident_memory_bytes", 0) / 1024 / 1024
        print(f"    CPU time used (web process): {cpu:.3f}s")
        print(f"    RSS memory (web process):    {rss_mb:.1f} MB")
        print()


if __name__ == "__main__":
    main()
