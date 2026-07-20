#!/usr/bin/env python
"""
API performance benchmark for the automation dashboard collection pipeline.

Mirrors benchmark_dashboard_collection.py but triggers each task via the
HTTP API (POST /api/v1/tasks/schedule_immediate/) instead of calling Python
directly.

Three phases, each clears JobData and collects a different window size:
  Phase 1 — One month:  collect TEST_SINCE → TEST_SINCE + 1 month
  Phase 2 — One week:   collect TEST_UNTIL - 7 days → TEST_UNTIL
  Phase 3 — One day:    collect TEST_UNTIL - 1 day  → TEST_UNTIL

Duration is measured from started_at to completed_at on the TaskExecution
record — actual execution time, directly comparable to the internal benchmark.

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
DIRECT_DB           Set to "true" to bypass HTTP entirely and drive the
                     benchmark straight through the ORM/DRF test client.
                     Use this in production deployments where the
                     /api/v1/tasks/ HTTP API only accepts a JWT from the
                     AAP gateway and Basic Auth is rejected. Task-trigger
                     and collection durations are measured from the
                     TaskExecution record, so they're unaffected by this
                     flag. The CSV export duration, however, is measured
                     as wall-clock time through force_authenticate(), which
                     skips auth middleware entirely — that figure excludes
                     any JWT/RBAC overhead the real HTTP path incurs.
                     (default: false)

Usage:
    BASE_URL=http://localhost:18002/api \\
    BENCHMARK_USER=superadmin \\
    PASSWORD=<password> \\
    METRICS_URL=http://localhost:18002/metrics \\
        .venv/bin/python \\
        metrics_service/tools/dashboard_collection_performance_tests/benchmark_dashboard_api.py
"""

# ruff: noqa: T201, E402
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPBasicAuth

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

from apps.dashboard_reports.models import JobData, TemplateMetadata

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL", "http://localhost:18002/api").rstrip("/")
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
        print(
            f"WARNING: BASE_URL={BASE_URL} is plain HTTP against a non-loopback host. "
            "HTTPBasicAuth sends credentials base64-encoded (not encrypted) — they will be "
            "exposed on the wire. Use HTTPS, or run with DIRECT_DB=true to bypass HTTP auth "
            "entirely.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _trigger_task_http(function_name: str, task_data: dict | None = None) -> str:
    """POST to schedule_immediate and return the task ID."""
    payload = {
        "function_name": function_name,
        "task_data": task_data or {},
        "name": f"benchmark: {function_name}",
    }
    resp = requests.post(
        f"{BASE_URL}/v1/tasks/schedule_immediate/",
        json=payload,
        auth=AUTH,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["task_id"]


def _trigger_task_direct_db(function_name: str, task_data: dict | None = None) -> str:
    """Create the Task row directly, bypassing the HTTP API entirely."""
    from apps.tasks.models import Task

    task = Task.objects.create(
        name=f"benchmark: {function_name}",
        function_name=function_name,
        task_data=task_data or {},
    )
    return str(task.id)


def _wait_for_task_http(task_id: str) -> float:
    """Poll until task reaches a terminal state. Returns execution seconds (started_at → completed_at)."""
    deadline = time.perf_counter() + POLL_TIMEOUT
    while time.perf_counter() < deadline:
        resp = requests.get(f"{BASE_URL}/v1/tasks/{task_id}/", auth=AUTH, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "completed":
            started_at = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
            completed_at = datetime.fromisoformat(data["completed_at"].replace("Z", "+00:00"))
            return (completed_at - started_at).total_seconds()
        if status in ("failed", "cancelled"):
            raise RuntimeError(f"Task {task_id} {status}: {data.get('error', '')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task {task_id} did not complete within {POLL_TIMEOUT}s")


def _wait_for_task_direct_db(task_id: str) -> float:
    """Poll the Task row directly, bypassing the HTTP API entirely."""
    from apps.tasks.models import Task

    deadline = time.perf_counter() + POLL_TIMEOUT
    while time.perf_counter() < deadline:
        task = Task.objects.get(id=task_id)
        if task.status == "completed":
            if task.started_at and task.completed_at:
                return (task.completed_at - task.started_at).total_seconds()
            return 0.0
        if task.status in ("failed", "cancelled"):
            raise RuntimeError(f"Task {task_id} {task.status}: {task.result_data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Task {task_id} did not complete within {POLL_TIMEOUT}s")


def _export_csv_http() -> float | None:
    try:
        start_time = time.perf_counter()
        resp = requests.get(
            f"{BASE_URL}/v1/dashboard_reports/report/export/?period=last_90_days", auth=AUTH, timeout=10
        )
        resp.raise_for_status()
        end_time = time.perf_counter()

        return end_time - start_time
    except Exception as e:
        print(f"Failed to download CSV: {e}")
        return None


def _export_csv_direct_db() -> float | None:
    """Hit the export view in-process via the DRF test client, bypassing HTTP auth entirely."""
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    try:
        user = get_user_model().objects.get(username=USERNAME)
        client = APIClient()
        client.force_authenticate(user=user)

        start_time = time.perf_counter()
        resp = client.get("/api/v1/dashboard_reports/report/export/?period=last_90_days")
        if resp.status_code != 200:
            raise RuntimeError(f"export failed: {resp.status_code} {resp.content[:300]}")
        end_time = time.perf_counter()

        return end_time - start_time
    except Exception as e:
        print(f"Failed to download CSV: {e}")
        return None


trigger_task = _trigger_task_direct_db if DIRECT_DB else _trigger_task_http
wait_for_task = _wait_for_task_direct_db if DIRECT_DB else _wait_for_task_http
export_csv = _export_csv_direct_db if DIRECT_DB else _export_csv_http


def run_phase(label: str, phase_since: datetime, phase_until: datetime) -> float:
    """Clear JobData, trigger a collection for the given window, return task duration."""
    print(f"\n{label}")
    print(f"  Range: {phase_since.date()} → {phase_until.date()}")

    JobData.objects.all().delete()
    TemplateMetadata.objects.all().delete()

    wall_start = time.perf_counter()
    task_id = trigger_task(
        "collect_dashboard_reports_data",
        {
            "since": phase_since.isoformat(),
            "until": phase_until.isoformat(),
            "database": DB_NAME,
        },
    )
    task_elapsed = wait_for_task(task_id)
    wall_elapsed = time.perf_counter() - wall_start
    csv_export_elapsed = export_csv()

    job_data_count = JobData.objects.count()
    csv_export_str = f"{csv_export_elapsed * 1000:.2f}ms" if csv_export_elapsed is not None else "FAILED"
    print(f"  Duration (task):                      {task_elapsed:.2f}s")
    print(f"  Duration (wall):                      {wall_elapsed:.2f}s  (includes queue wait)")
    print(f"  Duration (csv export - last 90 days): {csv_export_str}")
    print(f"  JobData rows in DB:                   {job_data_count:,}")
    return task_elapsed, csv_export_elapsed


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
            "  Mode:      DIRECT_DB — CSV export duration measured via force_authenticate(), "
            "excludes JWT/RBAC auth overhead; task/collection durations unaffected"
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

    month_elapsed, month_csv_export_elapsed = run_phase("Phase 1: One month collection", MONTH_SINCE, MONTH_UNTIL)
    week_elapsed, week_csv_export_elapsed = run_phase("Phase 2: One week collection", WEEK_SINCE, WEEK_UNTIL)
    day_elapsed, day_csv_export_elapsed = run_phase("Phase 3: One day collection", DAY_SINCE, DAY_UNTIL)

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
        f"  {'Phase 1 — one month':<30} {str(MONTH_SINCE.date()) + ' → ' + str(MONTH_UNTIL.date()):<24} {month_elapsed:>9.2f}s"
    )
    print(
        f"  {'Phase 2 — one week':<30} {str(WEEK_SINCE.date()) + ' → ' + str(WEEK_UNTIL.date()):<24} {week_elapsed:>9.2f}s"
    )
    print(
        f"  {'Phase 3 — one day':<30} {str(DAY_SINCE.date()) + ' → ' + str(DAY_UNTIL.date()):<24} {day_elapsed:>9.2f}s"
    )
    print()
    print(f"  Total wall time:  {total_wall:.1f}s ({total_wall / 60:.1f} min)  (includes queue waits)")
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
