"""
Clean up old metrics data based on retention policies.

Retention policies:
- Hourly collections: 7 days
- Daily summaries: 30 days
- Anonymized payloads: 30 days (or 7 days after sent)
- Analytics payloads: 90 days
"""

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from ..utils import create_task_result, log_task_execution

logger = logging.getLogger(__name__)


def cleanup_metrics_data(**kwargs) -> dict[str, Any]:
    """
    Clean up old metrics data based on retention policies.

    Retention policies:
    - Hourly collections: 7 days
    - Daily summaries: 30 days
    - Anonymized payloads: 30 days (or 7 days after sent)
    - Analytics payloads: 90 days

    Args:
        **kwargs: Task data containing:
            - hourly_retention_days (int): Days to keep hourly data (default: 7)
            - daily_retention_days (int): Days to keep daily summaries (default: 30)
            - payload_retention_days (int): Days to keep sent payloads (default: 7)
            - analytics_retention_days (int): Days to keep analytics payloads (default: 90)
            - dry_run (bool): If true, only count without deleting (default: False)

    Returns:
        dict: Task result with cleanup statistics
    """
    from apps.analytics.models import AnalyticsPayload
    from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary, HourlyMetricsCollection

    hourly_retention_days = kwargs.get("hourly_retention_days", 7)
    daily_retention_days = kwargs.get("daily_retention_days", 30)
    payload_retention_days = kwargs.get("payload_retention_days", 7)
    analytics_retention_days = kwargs.get("analytics_retention_days", 90)
    dry_run = kwargs.get("dry_run", False)

    log_task_execution("cleanup_metrics_data", "processing", f"Cleaning up metrics data (dry_run={dry_run})")

    results = {
        "hourly_collections": {"found": 0, "deleted": 0},
        "daily_summaries": {"found": 0, "deleted": 0},
        "anonymized_payloads": {"found": 0, "deleted": 0},
        "analytics_payloads": {"found": 0, "deleted": 0},
    }

    try:
        now = timezone.now()

        # Cleanup hourly collections older than retention period
        hourly_cutoff = now - timedelta(days=hourly_retention_days)
        old_hourly = HourlyMetricsCollection.objects.filter(collection_timestamp__lt=hourly_cutoff)
        results["hourly_collections"]["found"] = old_hourly.count()

        if not dry_run and results["hourly_collections"]["found"] > 0:
            deleted_count, _ = old_hourly.delete()
            results["hourly_collections"]["deleted"] = deleted_count

        # Cleanup daily summaries older than retention period
        daily_cutoff_date = now.date() - timedelta(days=daily_retention_days)
        old_daily = DailyMetricsSummary.objects.filter(summary_date__lt=daily_cutoff_date)
        results["daily_summaries"]["found"] = old_daily.count()

        if not dry_run and results["daily_summaries"]["found"] > 0:
            _, deletion_info = old_daily.delete()
            results["daily_summaries"]["deleted"] = deletion_info.get("tasks.DailyMetricsSummary", 0)

        # Cleanup sent payloads older than retention period
        # Keep unsent/failed/pending payloads longer (30 days) for retry/debugging
        sent_payload_cutoff = now - timedelta(days=payload_retention_days)
        unsent_payload_cutoff = now - timedelta(days=30)

        old_sent_payloads = AnonymizedMetricsPayload.objects.filter(status="sent", sent_at__lt=sent_payload_cutoff)
        old_unsent_payloads = AnonymizedMetricsPayload.objects.filter(
            status__in=["failed", "pending", "sending", "retry", "unavailable"], created__lt=unsent_payload_cutoff
        )

        total_old_payloads = old_sent_payloads.count() + old_unsent_payloads.count()
        results["anonymized_payloads"]["found"] = total_old_payloads

        if not dry_run and total_old_payloads > 0:
            sent_deleted, _ = old_sent_payloads.delete()
            unsent_deleted, _ = old_unsent_payloads.delete()
            results["anonymized_payloads"]["deleted"] = sent_deleted + unsent_deleted

        # Cleanup analytics payloads (BYO-BI raw collector output) older than retention period.
        # Keyed on `created` (row age), not `finished_at`, which may be null for incomplete rows.
        analytics_cutoff = now - timedelta(days=analytics_retention_days)
        old_analytics = AnalyticsPayload.objects.filter(created__lt=analytics_cutoff)
        results["analytics_payloads"]["found"] = old_analytics.count()

        if not dry_run and results["analytics_payloads"]["found"] > 0:
            deleted_count, _ = old_analytics.delete()
            results["analytics_payloads"]["deleted"] = deleted_count

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
