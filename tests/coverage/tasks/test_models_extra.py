"""
Unit tests for uncovered paths in apps/tasks/models.py.
Targets 65.61% → ~92% coverage.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone


# ---------------------------------------------------------------------------
# Task.is_ready_to_run
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_is_ready_to_run_immediate(user):
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user, status="pending")
    assert task.is_ready_to_run() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_is_ready_to_run_future_schedule_not_ready(user):
    from apps.tasks.models import Task

    future = timezone.now() + timedelta(hours=1)
    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, status="pending", scheduled_time=future
    )
    assert task.is_ready_to_run() is False


@pytest.mark.unit
@pytest.mark.django_db
def test_task_is_ready_to_run_past_schedule_ready(user):
    from apps.tasks.models import Task

    past = timezone.now() - timedelta(hours=1)
    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, status="pending", scheduled_time=past
    )
    assert task.is_ready_to_run() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_is_ready_to_run_not_pending(user):
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user, status="running")
    assert task.is_ready_to_run() is False


# ---------------------------------------------------------------------------
# Task.can_retry
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_retry_failed_under_max(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="failed",
        attempts=1,
        max_attempts=3,
    )
    assert task.can_retry() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_retry_exceeded_max(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="failed",
        attempts=3,
        max_attempts=3,
    )
    assert task.can_retry() is False


@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_retry_not_failed(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="completed",
        attempts=1,
        max_attempts=3,
    )
    assert task.can_retry() is False


# ---------------------------------------------------------------------------
# Task.retry
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_retry_resets_to_pending(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="failed",
        attempts=1,
        max_attempts=3,
    )
    result = task.retry()

    assert result is True
    task.refresh_from_db()
    assert task.status == "pending"
    assert task.error_message == ""
    assert task.started_at is None


@pytest.mark.unit
@pytest.mark.django_db
def test_task_retry_with_delay_sets_scheduled_time(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="failed",
        attempts=1,
        max_attempts=3,
    )
    task.retry(delay_seconds=300)

    task.refresh_from_db()
    assert task.scheduled_time is not None


@pytest.mark.unit
@pytest.mark.django_db
def test_task_retry_returns_false_when_cannot(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="completed",
        attempts=1,
        max_attempts=3,
    )
    result = task.retry()
    assert result is False


# ---------------------------------------------------------------------------
# Task.can_delete / can_modify
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_delete_non_system(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, is_system_task=False
    )
    assert task.can_delete() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_delete_system_task(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, is_system_task=True
    )
    assert task.can_delete() is False


@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_modify_non_system(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, is_system_task=False
    )
    assert task.can_modify() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_can_modify_system_task(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, is_system_task=True
    )
    assert task.can_modify() is False


# ---------------------------------------------------------------------------
# Task.get_next_run_time
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_get_next_run_time_with_cron(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, cron_expression="0 * * * *"
    )
    next_run = task.get_next_run_time()
    assert next_run is not None
    assert "T" in next_run  # ISO format


@pytest.mark.unit
@pytest.mark.django_db
def test_task_get_next_run_time_no_cron(user):
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    assert task.get_next_run_time() is None


@pytest.mark.unit
@pytest.mark.django_db
def test_task_get_next_run_time_invalid_cron(user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={}, created_by=user, cron_expression="invalid"
    )
    result = task.get_next_run_time()
    # Should return an error string, not raise
    assert result is not None
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Task managers
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_immediate_tasks_queryset(user):
    from apps.tasks.models import Task

    t1 = Task.objects.create(name="imm", function_name="hello_world", task_data={}, created_by=user, status="pending")
    Task.objects.create(
        name="sched",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="pending",
        scheduled_time=timezone.now() + timedelta(hours=1),
    )
    Task.objects.create(
        name="rec",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="pending",
        cron_expression="0 * * * *",
    )

    immediate = Task.immediate_tasks()
    ids = [t.id for t in immediate]
    assert t1.id in ids


@pytest.mark.unit
@pytest.mark.django_db
def test_task_scheduled_tasks_queryset(user):
    from apps.tasks.models import Task

    future = timezone.now() + timedelta(hours=1)
    t_sched = Task.objects.create(
        name="sched2",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="pending",
        scheduled_time=future,
    )
    Task.objects.create(name="imm2", function_name="hello_world", task_data={}, created_by=user, status="pending")

    scheduled = Task.scheduled_tasks()
    ids = [t.id for t in scheduled]
    assert t_sched.id in ids


@pytest.mark.unit
@pytest.mark.django_db
def test_task_recurring_tasks_queryset(user):
    from apps.tasks.models import Task

    t_rec = Task.objects.create(
        name="rec3",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="pending",
        cron_expression="0 * * * *",
    )
    Task.objects.create(name="plain", function_name="hello_world", task_data={}, created_by=user, status="pending")

    recurring = Task.recurring_tasks()
    ids = [t.id for t in recurring]
    assert t_rec.id in ids


# ---------------------------------------------------------------------------
# TaskExecution.save — auto execution time
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_execution_auto_calculates_execution_time(user):
    from apps.tasks.models import Task, TaskExecution

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    now = timezone.now()
    started = now - timedelta(seconds=30)

    execution = TaskExecution(task=task, status="completed", started_at=started, completed_at=now)
    execution.save()

    assert execution.execution_time_seconds is not None
    assert abs(execution.execution_time_seconds - 30) < 1


@pytest.mark.unit
@pytest.mark.django_db
def test_task_execution_no_completed_at_no_execution_time(user):
    from apps.tasks.models import Task, TaskExecution

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    execution = TaskExecution.objects.create(task=task, status="running")
    assert execution.execution_time_seconds is None


# ---------------------------------------------------------------------------
# HourlyMetricsCollection.save — auto data_size_bytes
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_hourly_collection_auto_calculates_data_size():
    from apps.tasks.models import HourlyMetricsCollection

    coll = HourlyMetricsCollection.objects.create(
        collector_type="unified_jobs",
        collection_timestamp=timezone.now().replace(minute=0, second=0, microsecond=0),
        raw_data={"key": "value", "count": 42},
        status="collected",
    )
    assert coll.data_size_bytes > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_hourly_collection_empty_data_has_small_size():
    from apps.tasks.models import HourlyMetricsCollection

    coll = HourlyMetricsCollection.objects.create(
        collector_type="unified_jobs",
        collection_timestamp=timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2),
        raw_data={},
        status="collected",
    )
    assert coll.data_size_bytes == 2  # len(b"{}") == 2


# ---------------------------------------------------------------------------
# AnonymizedMetricsPayload.can_retry
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_anonymized_payload_can_retry_when_failed():
    from apps.tasks.models import AnonymizedMetricsPayload

    payload = AnonymizedMetricsPayload.objects.create(
        summary_date=timezone.now().date(),
        anonymized_data={"data": "x"},
        status="failed",
        retry_count=0,
        max_retries=3,
    )
    assert payload.can_retry() is True


@pytest.mark.unit
@pytest.mark.django_db
def test_anonymized_payload_cannot_retry_when_sent():
    from apps.tasks.models import AnonymizedMetricsPayload

    payload = AnonymizedMetricsPayload.objects.create(
        summary_date=timezone.now().date() - timedelta(days=1),
        anonymized_data={"data": "x"},
        status="sent",
        retry_count=0,
        max_retries=3,
    )
    assert payload.can_retry() is False


@pytest.mark.unit
@pytest.mark.django_db
def test_anonymized_payload_cannot_retry_when_max_exceeded():
    from apps.tasks.models import AnonymizedMetricsPayload

    payload = AnonymizedMetricsPayload.objects.create(
        summary_date=timezone.now().date() - timedelta(days=2),
        anonymized_data={"data": "x"},
        status="failed",
        retry_count=3,
        max_retries=3,
    )
    assert payload.can_retry() is False


@pytest.mark.unit
@pytest.mark.django_db
def test_anonymized_payload_auto_calculates_size():
    from apps.tasks.models import AnonymizedMetricsPayload

    payload = AnonymizedMetricsPayload.objects.create(
        summary_date=timezone.now().date() - timedelta(days=3),
        anonymized_data={"key": "value"},
        status="pending",
    )
    assert payload.payload_size_bytes > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_metrics_summary_str_representation():
    from apps.tasks.models import DailyMetricsSummary

    summary = DailyMetricsSummary.objects.create(
        summary_date=timezone.now().date() - timedelta(days=1),
    )
    assert str(summary.summary_date) in str(summary)


# ---------------------------------------------------------------------------
# StatusTrackingMixin.get_duration
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_get_duration_returns_seconds(user):
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    now = timezone.now()
    Task.objects.filter(pk=task.pk).update(
        started_at=now - timedelta(seconds=60),
        completed_at=now,
    )
    task.refresh_from_db()
    duration = task.get_duration()
    assert duration is not None
    assert abs(duration - 60) < 1


@pytest.mark.unit
@pytest.mark.django_db
def test_get_duration_returns_none_when_no_started_at(user):
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    assert task.get_duration() is None
