"""
Anonymize daily rollup and prepare payload for Segment.

This task fetches the daily rollup summary (containing all 6 collectors),
combines the rollup JSONs using anonymize_rollups(), and creates an anonymized
payload ready for transmission.

The output is a flattened structure with statistics and arrays ready for Segment.
"""

import logging
import random
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from ..task_groups import SEGMENT_MAX_ATTEMPTS, get_feature_enabled_from_db
from ..utils import create_task_result, generate_salt, log_task_execution

logger = logging.getLogger(__name__)


def random_offset():
    """Return a random jitter offset in minutes for scheduling."""
    return random.randint(1, 240)  # noqa: S311


def _collect_leaderboard_telemetry() -> dict[str, Any]:
    """Observe the current effective setting, independently of the summary date."""
    observed_at = timezone.now().isoformat()
    try:
        enabled = get_feature_enabled_from_db("SHOW_LEADERBOARD", default=True, raise_errors=True)
    except Exception:
        logger.exception("Failed to read Leaderboard enablement for telemetry")
        enabled = None

    return {"enabled": enabled, "observed_at": observed_at}


def daily_anonymize_and_prepare(**kwargs) -> dict[str, Any]:
    """
    Anonymize daily metrics summary and prepare payload for Segment

    Acquires an advisory lock to prevent concurrent execution, then:
    - Fetches DailyMetricsSummary with status=aggregated (upstream dependency)
    - Anonymizes using anonymize_rollups() from metrics-utility
    - Creates AnonymizedMetricsPayload record
    - Schedules a one-time send_anonymized_to_segment task at a randomized,
      installation-stable time offset (jitter derived from ServiceID UUID)

    If the upstream dependency (aggregated summary) is not met or the lock
    cannot be acquired, the task fails and will be retried automatically.

    Args:
        **kwargs: Task data containing:
            - summary_date (str): Date to anonymize (YYYY-MM-DD, defaults to yesterday)

    Returns:
        dict: Task result with payload ID and scheduled send time
    """
    # Import from metrics-utility (will fail if not available)
    from django.db import transaction
    from metrics_utility.anonymized_rollups import anonymize_rollups

    from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary, Task

    # Determine summary date
    summary_date_str = kwargs.get("summary_date")
    if summary_date_str:
        summary_date = date.fromisoformat(summary_date_str)
    else:
        summary_date = timezone.now().date() - timedelta(days=1)

    log_task_execution("daily_anonymize_and_prepare", "processing", f"Anonymizing daily rollup for: {summary_date}")

    try:
        # Get daily summary (merged rollup but not anonymized)
        daily_summary = DailyMetricsSummary.objects.get(summary_date=summary_date, status="aggregated")
        metrics = daily_summary.aggregated_metrics

        anonymized_data = anonymize_rollups(
            events_modules_rollup=metrics.get("main_jobevent_service", {}),
            execution_environments_rollup=metrics.get("execution_environments", {}),
            jobs_rollup=metrics.get("unified_jobs", {}),
            job_host_summary_rollup=metrics.get("job_host_summary_service", {}),
            credentials_rollup=metrics.get("credentials_service", {}),
            table_metadata_rollup=metrics.get("table_metadata", {}),
            controller_version_rollup=metrics.get("controller_version_service", {}),
            feature_flags_rollup=metrics.get("feature_flags_service", {}),
            task_executions_rollup=metrics.get("task_executions_service", []),
            indirect_managed_nodes_rollup=metrics.get("indirect_managed_nodes", {}),
        )

        # Add metadata
        aggregation_timestamp = (
            daily_summary.aggregation_completed_at.isoformat() if daily_summary.aggregation_completed_at else None
        )

        anonymized_data["summary_metadata"] = {
            "summary_date": str(summary_date),
            "hourly_collections_count": daily_summary.hourly_collections_count,
            "missing_hours": daily_summary.missing_hours,
            "aggregation_timestamp": aggregation_timestamp,
            "install_type": settings.INSTALL_TYPE,
        }

        # Get dashboard telemetry
        anonymized_data["dashboard_telemetry"] = metrics.get("dashboard_telemetry", [])
        anonymized_data["leaderboard_telemetry"] = _collect_leaderboard_telemetry()

        offset_minutes = random_offset()
        send_scheduled_time = timezone.now() + timedelta(minutes=offset_minutes)

        # Use atomic transaction to prevent duplicate payloads and ensure the
        # send task is only created when the payload is successfully persisted.
        with transaction.atomic():
            # Create AnonymizedMetricsPayload
            event_name = "Controller Metrics Daily Rollup"
            payload = AnonymizedMetricsPayload.objects.create(
                summary_date=summary_date,
                anonymized_data=anonymized_data,
                status="pending",
                daily_summary=daily_summary,
                anonymization_task_execution_id=kwargs.get("execution_id"),
                segment_event_name=kwargs.get("event_name", event_name),
                segment_user_id=kwargs.get("user_id", generate_salt()),
            )

            # Update daily summary status
            daily_summary.status = "anonymized"
            daily_summary.save()

            Task.objects.create(
                name=f"send_to_segment_{summary_date}",
                description=f"Send anonymized metrics payload {payload.id} to Segment (scheduled with jitter)",
                function_name="send_anonymized_to_segment",
                task_data={
                    "payload_id": payload.id,
                    "_feature_flag": "ANONYMIZED_DATA_COLLECTION",
                },
                scheduled_time=send_scheduled_time,
                is_system_task=False,
                max_attempts=SEGMENT_MAX_ATTEMPTS,
            )

        logger.info(
            "Anonymization complete. Scheduling Segment upload for %s (Offset: %d minutes)",
            send_scheduled_time.isoformat(),
            offset_minutes,
        )
        log_task_execution("daily_anonymize_and_prepare", "completed", f"Created anonymized payload ID: {payload.id}")

        return create_task_result(
            "success",
            {
                "task_type": "daily_anonymize_and_prepare",
                "payload_id": payload.id,
                "summary_date": str(summary_date),
                "payload_size_bytes": payload.payload_size_bytes,
                "segment_send_scheduled_time": send_scheduled_time.isoformat(),
                "segment_send_offset_minutes": offset_minutes,
            },
        )

    except DailyMetricsSummary.DoesNotExist:
        error_msg = f"No daily summary found for {summary_date} with status=aggregated"
        logger.error(error_msg)
        return create_task_result("error", error=error_msg)

    except Exception as e:
        logger.exception("Error in daily_anonymize_and_prepare: %s", e)
        return create_task_result("error", error=f"Anonymization failed: {str(e)}")
