"""
Daily metrics collector for time-range data (not snapshot, runs once per day).

Collects metrics for the previous full day using explicit since/until boundaries,
and stores in HourlyMetricsCollection. Unlike hourly collectors (24x/day) and
snapshot collectors (no time window), daily collectors run once per day with
a full-day time window.

Current daily collectors:
  - task_executions_service: pipeline observability from the metrics-service DB
  - indirect_managed_nodes: indirect managed node audit from the AWX DB
  - main_host_daily: changed host inventory from the AWX DB
  - main_hostmetric: Renewal Guidance host metrics from the AWX DB
"""

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from ..utils import create_task_result, generic_collect_metrics, get_db_connection, parse_datetime_string

logger = logging.getLogger(__name__)


def _get_daily_collectors():
    """
    Get daily collectors registry with lazy imports.

    These collectors:
    - Use an explicit since/until time window (previous full day)
    - Run once per day (not 24x like hourly collectors)
    - May query a different DB than AWX (e.g. metrics-service own DB)

    Each entry may include a "database" key to specify which Django DB connection
    to use. Defaults to "awx" if not specified.
    """
    from metrics_utility.anonymized_rollups import IndirectManagedNodesAnonymizedRollup, TaskExecutionsAnonymizedRollup
    from metrics_utility.library.collectors.controller import (
        main_host_daily,
        main_hostmetric,
        main_indirectmanagednodeaudit,
    )
    from metrics_utility.library.collectors.service import task_executions_service

    return {
        "task_executions_service": {
            "collector_func": task_executions_service,
            "rollup_processor": TaskExecutionsAnonymizedRollup,
            "description": "Task execution observability metrics (pipeline health)",
            "database": "default",
        },
        "indirect_managed_nodes": {
            "collector_func": main_indirectmanagednodeaudit,
            "rollup_processor": IndirectManagedNodesAnonymizedRollup,
            "description": "Indirect managed node audit daily collection",
            "database": "awx",
        },
        "main_host_daily": {
            "collector_func": main_host_daily,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Changed host inventory daily collection",
            "database": "awx",
        },
        "main_hostmetric": {
            "collector_func": main_hostmetric,
            "rollup_processor": None,
            "persist_to_hourly": False,
            "description": "Renewal Guidance host metrics daily collection",
            "database": "awx",
        },
    }


def collect_daily_metrics(**kwargs) -> dict[str, Any]:
    """
    Collect daily metrics for a specific collector type using a since/until time window.

    Unlike hourly collectors (24x/day, 1-hour windows) and snapshot collectors
    (current state, no time window), daily collectors run once per day and cover
    the previous full calendar day. Analytics-only collectors are persisted in
    AnalyticsPayload instead of HourlyMetricsCollection.

    The collection is stored with a timestamp of yesterday 23:00 UTC so that
    the daily_metrics_rollup task finds it within its query window alongside
    snapshot collections.

    Args:
        **kwargs: Task data containing:
            - collector_type (str): Type of collector to run (required)
            - since (str): ISO timestamp for start of window (defaults to yesterday 00:00 UTC)
            - until (str): ISO timestamp for end of window (defaults to today 00:00 UTC)

    Returns:
        dict: Task result with collection status and record ID
    """
    collector_type = kwargs.pop("collector_type", None)
    if not collector_type:
        return create_task_result("error", error="collector_type parameter is required")

    execution_id = kwargs.get("execution_id")

    # Determine since/until window (default: previous full calendar day, UTC).
    # Use a scheduler-injected hour_timestamp (today's midnight at dispatch time) when
    # available so that retries always operate on the originally intended window even if
    # wall-clock "now" has rolled past midnight into the next day.
    hour_timestamp_str = kwargs.pop("hour_timestamp", None)
    if hour_timestamp_str:
        today_midnight = parse_datetime_string(hour_timestamp_str)
        if today_midnight is None:
            return create_task_result("error", error=f"Invalid hour_timestamp format: {hour_timestamp_str}")
    else:
        now = timezone.now()
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    since_str = kwargs.get("since")
    if since_str:
        since = parse_datetime_string(since_str)
        if since is None:
            return create_task_result("error", error=f"Invalid since format: {since_str}")
    else:
        since = today_midnight - timedelta(days=1)

    until_str = kwargs.get("until")
    if until_str:
        until = parse_datetime_string(until_str)
        if until is None:
            return create_task_result("error", error=f"Invalid until format: {until_str}")
    else:
        until = today_midnight

    # Store at yesterday 23:00 UTC so the daily_metrics_rollup query window
    # (yesterday 00:00 -> today 00:00) picks this up alongside snapshot collections.
    collection_timestamp = since.replace(hour=23, minute=0, second=0, microsecond=0)

    collector_registry = _get_daily_collectors()
    db_name = collector_registry.get(collector_type, {}).get("database", "awx")
    db_connection = get_db_connection(db_name)

    return generic_collect_metrics(
        collector_type=collector_type,
        collector_registry=collector_registry,
        collection_mode="daily",
        timestamp=collection_timestamp,
        db_connection=db_connection,
        collector_kwargs={"since": since, "until": until},
        task_execution_id=execution_id,
    )
