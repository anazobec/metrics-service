"""
Enhanced unit tests for apps/tasks/models.py to improve coverage.

Tests cover:
- Task.retry() with submission failures (lines 158-186)
- Task.get_next_run_time() with croniter import and exceptions (lines 218-229)
- Task classmethod queries (lines 233, 237, 241)
- HourlyMetricsCollection.__str__() and save() (lines 378, 391-394)
- DailyMetricsSummary.__str__() and get_hourly_collections() (lines 468, 477-487)
- AnonymizedMetricsPayload.__str__(), can_retry(), save() (lines 575, 584, 597-600)
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest
from django.utils import timezone

from apps.tasks.models import (
    AnonymizedMetricsPayload,
    DailyMetricsSummary,
    HourlyMetricsCollection,
    Task,
)


@pytest.mark.unit
@pytest.mark.django_db
class TestTaskRetry:
    """Test Task.retry() method including error handling."""

    def test_retry_success_immediate_task(self, user):
        """Test retry sets task to pending and clears error state."""
        task = Task.objects.create(
            name="Failed Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=3,
            created_by=user,
            error_message="Previous error",
        )

        result = task.retry()

        assert result is True
        task.refresh_from_db()
        assert task.status == "pending"
        assert task.error_message == ""
        assert task.started_at is None
        assert task.completed_at is None
        assert task.attempts == 1

    def test_retry_clears_scheduled_time(self, user):
        """Test retry without delay clears scheduled_time."""
        future_time = timezone.now() + timedelta(hours=1)
        task = Task.objects.create(
            name="Scheduled Failed Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=3,
            scheduled_time=future_time,
            created_by=user,
        )

        result = task.retry()

        assert result is True
        task.refresh_from_db()
        assert task.status == "pending"
        assert task.scheduled_time is None

    def test_retry_returns_false_when_cannot_retry(self, user):
        """Test retry returns False when can_retry() is False."""
        task = Task.objects.create(
            name="Failed Task",
            function_name="hello_world",
            status="failed",
            attempts=3,
            max_attempts=3,
            created_by=user,
        )

        result = task.retry()

        assert result is False
        task.refresh_from_db()
        assert task.status == "failed"

    def test_retry_force_bypasses_max_attempts(self, user):
        """Test retry(force=True) resets an exhausted task."""
        task = Task.objects.create(
            name="Exhausted Task",
            function_name="hello_world",
            status="failed",
            attempts=3,
            max_attempts=3,
            created_by=user,
        )

        result = task.retry(force=True)

        assert result is True
        task.refresh_from_db()
        assert task.status == "pending"

    def test_retry_force_rejects_non_failed(self, user):
        """Test retry(force=True) still requires status == 'failed'."""
        task = Task.objects.create(
            name="Running Task",
            function_name="hello_world",
            status="running",
            attempts=1,
            max_attempts=3,
            created_by=user,
        )

        result = task.retry(force=True)

        assert result is False
        task.refresh_from_db()
        assert task.status == "running"


@pytest.mark.unit
@pytest.mark.django_db
class TestTaskGetNextRunTime:
    """Test Task.get_next_run_time() method."""

    def test_returns_none_for_non_recurring_task(self, user):
        """Test get_next_run_time returns None for non-recurring task (lines 218-219)."""
        # Arrange
        task = Task.objects.create(
            name="One-time Task",
            function_name="hello_world",
            created_by=user,
        )

        # Act
        result = task.get_next_run_time()

        # Assert
        assert result is None

    def test_calculates_next_run_time_for_recurring_task(self, user):
        """Test get_next_run_time calculates next run (lines 221-225)."""
        # Arrange
        task = Task.objects.create(
            name="Recurring Task",
            function_name="hello_world",
            cron_expression="0 * * * *",  # Every hour
            created_by=user,
        )

        # Act
        result = task.get_next_run_time()

        # Assert
        assert result is not None
        assert isinstance(result, str)  # ISO format datetime string
        # Verify it's a valid ISO datetime string
        from datetime import datetime

        datetime.fromisoformat(result)  # Should not raise

    # Note: test_handles_croniter_import_error removed
    # Lines 226-227 handle ImportError when croniter is not installed, but this is not easily
    # testable since croniter is imported locally inside a try block. Mocking local imports
    # requires module reloading which breaks other tests in Django's app registry.
    # This code path is defensive programming for environments without croniter installed.

    def test_handles_invalid_cron_expression(self, user):
        """Test get_next_run_time handles invalid cron expression (lines 228-229)."""
        # Arrange
        task = Task.objects.create(
            name="Bad Cron Task",
            function_name="hello_world",
            cron_expression="invalid cron",
            created_by=user,
        )

        # Act
        result = task.get_next_run_time()

        # Assert
        assert result == "Invalid cron_expression"


@pytest.mark.unit
@pytest.mark.django_db
class TestTaskClassmethods:
    """Test Task classmethod queries."""

    def test_immediate_tasks_returns_pending_immediate_tasks(self, user):
        """Test immediate_tasks classmethod (line 233)."""
        # Arrange
        Task.objects.create(
            name="Immediate 1",
            function_name="hello_world",
            status="pending",
            created_by=user,
        )
        Task.objects.create(
            name="Immediate 2",
            function_name="hello_world",
            status="pending",
            created_by=user,
        )
        # Create tasks that should NOT be in results
        Task.objects.create(
            name="Scheduled",
            function_name="hello_world",
            status="pending",
            scheduled_time=timezone.now() + timedelta(hours=1),
            created_by=user,
        )
        Task.objects.create(
            name="Recurring",
            function_name="hello_world",
            status="pending",
            cron_expression="0 * * * *",
            created_by=user,
        )
        Task.objects.create(
            name="Completed",
            function_name="hello_world",
            status="completed",
            created_by=user,
        )

        # Act
        immediate = Task.immediate_tasks()

        # Assert
        assert immediate.count() == 2
        assert all(t.status == "pending" for t in immediate)
        assert all(t.scheduled_time is None for t in immediate)
        assert all(t.cron_expression is None for t in immediate)

    def test_scheduled_tasks_returns_pending_scheduled_tasks(self, user):
        """Test scheduled_tasks classmethod (line 237)."""
        # Arrange
        Task.objects.create(
            name="Scheduled 1",
            function_name="hello_world",
            status="pending",
            scheduled_time=timezone.now() + timedelta(hours=1),
            created_by=user,
        )
        Task.objects.create(
            name="Scheduled 2",
            function_name="hello_world",
            status="pending",
            scheduled_time=timezone.now() + timedelta(hours=2),
            created_by=user,
        )
        # Create tasks that should NOT be in results
        Task.objects.create(
            name="Immediate",
            function_name="hello_world",
            status="pending",
            created_by=user,
        )

        # Act
        scheduled = Task.scheduled_tasks()

        # Assert
        assert scheduled.count() == 2
        assert all(t.status == "pending" for t in scheduled)
        assert all(t.scheduled_time is not None for t in scheduled)

    def test_recurring_tasks_returns_pending_recurring_tasks(self, user):
        """Test recurring_tasks classmethod (line 241)."""
        # Arrange
        Task.objects.create(
            name="Recurring 1",
            function_name="hello_world",
            status="pending",
            cron_expression="0 * * * *",
            created_by=user,
        )
        Task.objects.create(
            name="Recurring 2",
            function_name="hello_world",
            status="pending",
            cron_expression="0 2 * * *",
            created_by=user,
        )
        # Create tasks that should NOT be in results
        Task.objects.create(
            name="Immediate",
            function_name="hello_world",
            status="pending",
            created_by=user,
        )

        # Act
        recurring = Task.recurring_tasks()

        # Assert
        assert recurring.count() == 2
        assert all(t.status == "pending" for t in recurring)
        assert all(t.cron_expression is not None for t in recurring)


@pytest.mark.unit
@pytest.mark.django_db
class TestHourlyMetricsCollection:
    """Test HourlyMetricsCollection model methods."""

    def test_str_representation(self):
        """Test __str__ method (line 378)."""
        # Arrange
        collection = HourlyMetricsCollection.objects.create(
            collector_type="job_host_summary_service",
            collection_timestamp=timezone.now(),
            raw_data={"test": "data"},
        )

        # Act
        str_repr = str(collection)

        # Assert
        assert "Job Host Summary Service" in str_repr  # Display name
        assert str(collection.collection_timestamp) in str_repr

    def test_save_calculates_data_size(self):
        """Test save() calculates data_size_bytes (lines 391-394)."""
        # Arrange
        raw_data = {"test": "data", "count": 42, "items": [1, 2, 3]}
        collection = HourlyMetricsCollection(
            collector_type="job_host_summary_service",
            collection_timestamp=timezone.now(),
            raw_data=raw_data,
        )

        # Act
        collection.save()

        # Assert
        collection.refresh_from_db()
        assert collection.data_size_bytes > 0
        # Verify it's approximately correct
        import json

        expected_size = len(json.dumps(raw_data).encode("utf-8"))
        assert collection.data_size_bytes == expected_size

    def test_save_normalizes_json_values(self):
        collection = HourlyMetricsCollection(
            collector_type="job_host_summary_service",
            collection_timestamp=timezone.now(),
            raw_data={"cpu": Decimal("1.0"), "count": np.int64(2), "missing": float("nan")},
        )

        collection.save()
        collection.refresh_from_db()

        assert collection.raw_data == {"cpu": "1.0", "count": 2, "missing": None}


@pytest.mark.unit
@pytest.mark.django_db
class TestDailyMetricsSummary:
    """Test DailyMetricsSummary model methods."""

    def test_str_representation(self):
        """Test __str__ method (line 468)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            status="aggregated",
            aggregated_metrics={"total": 100},
        )

        # Act
        str_repr = str(summary)

        # Assert
        assert "Daily Summary" in str_repr
        assert "2024-01-15" in str_repr
        assert "Aggregated" in str_repr  # Display name

    def test_get_hourly_collections_returns_empty_when_none(self):
        """Test get_hourly_collections with no collections (lines 480-481)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
            hourly_collection_ids={},
        )

        # Act
        collections = summary.get_hourly_collections()

        # Assert
        assert collections.count() == 0

    def test_get_hourly_collections_returns_associated_collections(self):
        """Test get_hourly_collections returns related collections (lines 477-487)."""
        # Arrange
        collection_time = timezone.make_aware(timezone.datetime(2024, 1, 15, 10, 0))

        # Create hourly collections
        coll1 = HourlyMetricsCollection.objects.create(
            collector_type="job_host_summary_service",
            collection_timestamp=collection_time,
            raw_data={"data": 1},
        )
        coll2 = HourlyMetricsCollection.objects.create(
            collector_type="credentials_service",
            collection_timestamp=collection_time,
            raw_data={"data": 2},
        )
        # Create unrelated collection
        HourlyMetricsCollection.objects.create(
            collector_type="main_jobevent_service",
            collection_timestamp=collection_time,
            raw_data={"data": 3},
        )

        # Create summary with references
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
            hourly_collection_ids={
                "job_host_summary_service": [coll1.id],
                "credentials_service": [coll2.id],
            },
        )

        # Act
        collections = summary.get_hourly_collections()

        # Assert
        assert collections.count() == 2
        collection_ids = {c.id for c in collections}
        assert coll1.id in collection_ids
        assert coll2.id in collection_ids


@pytest.mark.unit
@pytest.mark.django_db
class TestAnonymizedMetricsPayload:
    """Test AnonymizedMetricsPayload model methods."""

    def test_str_representation(self):
        """Test __str__ method (line 575)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload.objects.create(
            summary_date=date(2024, 1, 15),
            anonymized_data={"test": "data"},
            status="pending",
            daily_summary=summary,
        )

        # Act
        str_repr = str(payload)

        # Assert
        assert "Anonymized Payload" in str_repr
        assert "2024-01-15" in str_repr
        assert "Pending" in str_repr  # Display name

    def test_can_retry_returns_true_for_failed_payload(self):
        """Test can_retry returns True for failed payload (line 584)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload.objects.create(
            summary_date=date(2024, 1, 15),
            anonymized_data={"test": "data"},
            status="failed",
            retry_count=1,
            max_retries=3,
            daily_summary=summary,
        )

        # Act
        result = payload.can_retry()

        # Assert
        assert result is True

    def test_can_retry_returns_true_for_retry_status(self):
        """Test can_retry returns True for retry status (line 584)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload.objects.create(
            summary_date=date(2024, 1, 15),
            anonymized_data={"test": "data"},
            status="retry",
            retry_count=1,
            max_retries=3,
            daily_summary=summary,
        )

        # Act
        result = payload.can_retry()

        # Assert
        assert result is True

    def test_can_retry_returns_false_when_max_retries_reached(self):
        """Test can_retry returns False when max retries reached (line 584)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload.objects.create(
            summary_date=date(2024, 1, 15),
            anonymized_data={"test": "data"},
            status="failed",
            retry_count=3,
            max_retries=3,
            daily_summary=summary,
        )

        # Act
        result = payload.can_retry()

        # Assert
        assert result is False

    def test_can_retry_returns_false_for_sent_status(self):
        """Test can_retry returns False for sent status (line 584)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload.objects.create(
            summary_date=date(2024, 1, 15),
            anonymized_data={"test": "data"},
            status="sent",
            retry_count=1,
            max_retries=3,
            daily_summary=summary,
        )

        # Act
        result = payload.can_retry()

        # Assert
        assert result is False

    def test_save_calculates_payload_size(self):
        """Test save() calculates payload_size_bytes (lines 597-600)."""
        # Arrange
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        anonymized_data = {"metrics": {"count": 42}, "config": {"version": "1.0"}}
        payload = AnonymizedMetricsPayload(
            summary_date=date(2024, 1, 15),
            anonymized_data=anonymized_data,
            daily_summary=summary,
        )

        # Act
        payload.save()

        # Assert
        payload.refresh_from_db()
        assert payload.payload_size_bytes > 0
        # Verify it's approximately correct
        import json

        expected_size = len(json.dumps(anonymized_data).encode("utf-8"))
        assert payload.payload_size_bytes == expected_size

    def test_save_normalizes_json_values(self):
        summary = DailyMetricsSummary.objects.create(
            summary_date=date(2024, 1, 15),
            aggregated_metrics={},
        )
        payload = AnonymizedMetricsPayload(
            summary_date=date(2024, 1, 15),
            anonymized_data={"cpu": Decimal("1.0"), "count": np.int64(2), "missing": float("nan")},
            daily_summary=summary,
        )

        payload.save()
        payload.refresh_from_db()

        assert payload.anonymized_data == {"cpu": "1.0", "count": 2, "missing": None}
