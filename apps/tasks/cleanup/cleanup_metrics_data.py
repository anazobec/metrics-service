"""
Clean up old metrics data based on retention policies.

Retention policies:
- Hourly collections: 7 days
- Daily summaries: 30 days
- Anonymized payloads: 30 days (or 7 days after sent)
- Analytics payloads: Controller cleanup_jobs retention, falling back to 90 days
- API usage counters: same analytics retention, keyed on usage_date
"""

import logging
from datetime import timedelta
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from ..retention import DEFAULT_AWX_DB_NAME, get_controller_retention_days
from ..utils import create_task_result, log_task_execution

logger = logging.getLogger(__name__)


def _delete_old_rows(queryset: QuerySet, dry_run: bool, label: str | None = None) -> dict[str, int]:
    """
    Count rows in a cleanup queryset and delete them unless dry_run.

    Returns a ``{"found", "deleted"}`` stats dict. When ``label`` names a model
    (``app_label.ModelName``), ``deleted`` counts only that model's rows from
    Django's deletion info, excluding rows removed by cascade.
    """
    found = queryset.count()
    if dry_run or found == 0:
        return {"found": found, "deleted": 0}
    deleted_total, deletion_info = queryset.delete()
    deleted = deletion_info.get(label, 0) if label else deleted_total
    return {"found": found, "deleted": deleted}


def cleanup_metrics_data(**kwargs) -> dict[str, Any]:
    """
    Clean up old metrics data based on retention policies.

    Retention policies:
    - Hourly collections: 7 days
    - Daily summaries: 30 days
    - Anonymized payloads: 30 days (or 7 days after sent)
    - Analytics payloads: Controller cleanup_jobs retention, falling back to 90 days
    - API usage counters: same analytics retention, keyed on usage_date

    Args:
        **kwargs: Task data containing:
            - hourly_retention_days (int): Days to keep hourly data (default: 7)
            - daily_retention_days (int): Days to keep daily summaries (default: 30)
            - payload_retention_days (int): Days to keep sent payloads (default: 7)
            - analytics_retention_days (int): Explicit analytics retention override for manual runs.
              When omitted, use the Controller cleanup_jobs schedule or 90 days if unavailable.
              Applies to analytics payloads and API usage counters.
            - awx_database (str): Controller database alias (default: awx)
            - dry_run (bool): If true, only count without deleting (default: False)

    Returns:
        dict: Task result with cleanup statistics
    """
    from apps.analytics.models import AnalyticsPayload, APIUsageCounter
    from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary, HourlyMetricsCollection

    hourly_retention_days = kwargs.get("hourly_retention_days", 7)
    daily_retention_days = kwargs.get("daily_retention_days", 30)
    payload_retention_days = kwargs.get("payload_retention_days", 7)
    analytics_retention_days = kwargs.get("analytics_retention_days")
    if analytics_retention_days is None:
        analytics_retention_days = get_controller_retention_days(kwargs.get("awx_database", DEFAULT_AWX_DB_NAME))
    else:
        try:
            analytics_retention_days = int(analytics_retention_days)
        except (TypeError, ValueError):
            return create_task_result(
                "error", error=f"Invalid analytics_retention_days value: {analytics_retention_days!r}"
            )
        if analytics_retention_days < 1:
            return create_task_result(
                "error", error=f"analytics_retention_days must be > 0, got {analytics_retention_days!r}"
            )
    dry_run = kwargs.get("dry_run", False)

    log_task_execution("cleanup_metrics_data", "processing", f"Cleaning up metrics data (dry_run={dry_run})")

    results = {
        "hourly_collections": {"found": 0, "deleted": 0},
        "daily_summaries": {"found": 0, "deleted": 0},
        "anonymized_payloads": {"found": 0, "deleted": 0},
        "analytics_payloads": {"found": 0, "deleted": 0},
        "api_usage_counters": {"found": 0, "deleted": 0},
    }

    try:
        now = timezone.now()

        # Cleanup hourly collections older than retention period
        hourly_cutoff = now - timedelta(days=hourly_retention_days)
        results["hourly_collections"] = _delete_old_rows(
            HourlyMetricsCollection.objects.filter(collection_timestamp__lt=hourly_cutoff), dry_run
        )

        # Cleanup daily summaries older than retention period
        daily_cutoff_date = now.date() - timedelta(days=daily_retention_days)
        results["daily_summaries"] = _delete_old_rows(
            DailyMetricsSummary.objects.filter(summary_date__lt=daily_cutoff_date),
            dry_run,
            label="tasks.DailyMetricsSummary",
        )

        # Cleanup sent payloads older than retention period
        # Keep unsent/failed/pending payloads longer (30 days) for retry/debugging
        sent_payload_cutoff = now - timedelta(days=payload_retention_days)
        unsent_payload_cutoff = now - timedelta(days=30)

        sent_stats = _delete_old_rows(
            AnonymizedMetricsPayload.objects.filter(status="sent", sent_at__lt=sent_payload_cutoff), dry_run
        )
        unsent_stats = _delete_old_rows(
            AnonymizedMetricsPayload.objects.filter(
                status__in=["failed", "pending", "sending", "retry", "unavailable"], created__lt=unsent_payload_cutoff
            ),
            dry_run,
        )
        results["anonymized_payloads"] = {
            "found": sent_stats["found"] + unsent_stats["found"],
            "deleted": sent_stats["deleted"] + unsent_stats["deleted"],
        }

        # Cleanup analytics payloads (BYO-BI raw collector output) older than retention period.
        # Keyed on `created` (row age), not `finished_at` (collection completion time).
        analytics_cutoff = now - timedelta(days=analytics_retention_days)
        results["analytics_payloads"] = _delete_old_rows(
            AnalyticsPayload.objects.filter(created__lt=analytics_cutoff), dry_run
        )

        # Cleanup API usage counters (daily endpoint aggregates) under the same analytics
        # retention. Keyed on `usage_date`; the daily rollup reads only yesterday's counters,
        # which survive even the shortest valid retention because the cutoff is exclusive.
        api_usage_cutoff_date = now.date() - timedelta(days=analytics_retention_days)
        results["api_usage_counters"] = _delete_old_rows(
            APIUsageCounter.objects.filter(usage_date__lt=api_usage_cutoff_date), dry_run
        )

        log_task_execution("cleanup_metrics_data", "completed", f"Cleanup complete: {results}")

        return create_task_result(
            "success",
            {
                "task_type": "cleanup_metrics_data",
                "dry_run": dry_run,
                "retention_policies": {
                    "hourly_days": hourly_retention_days,
                    "daily_days": daily_retention_days,
                    "payload_days": payload_retention_days,
                    "analytics_days": analytics_retention_days,
                },
                "results": results,
            },
        )

    except Exception as e:
        logger.exception(f"Error in cleanup_metrics_data: {str(e)}")
        return create_task_result("error", error=f"Cleanup failed: {str(e)}")
