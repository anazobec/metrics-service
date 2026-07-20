"""Shared Prometheus /metrics helpers for the dashboard performance benchmarks.

Used by benchmark_dashboard_api.py and benchmark_dashboard_api_slo.py to read
the web process's Prometheus /metrics endpoint and diff counters across a
benchmark run.
"""

import requests


def read_prometheus_metrics(url: str) -> dict:
    """Fetch /metrics and parse into {metric_name: float}."""
    if not url:
        return {}
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
    except Exception:
        return {}
    result = {}
    for line in resp.text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0]] = float(parts[-1])
    return result


def prometheus_delta(before: dict, after: dict, key: str) -> float:
    """Return how much a Prometheus counter increased between two snapshots."""
    return after.get(key, 0) - before.get(key, 0)
