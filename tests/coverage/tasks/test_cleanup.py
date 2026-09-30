"""
Unit tests for apps/tasks/cleanup/ modules.
Covers cleanup_old_tasks, cleanup_metrics_data, cleanup_activitystream.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone


# ---------------------------------------------------------------------------
# cleanup_old_tasks
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_old_tasks_dry_run_does_not_delete(user):
    from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
    from apps.tasks.models import Task

    now = timezone.now()
    for i in range(3):
        t = Task.objects.create(
            name=f"old_{i}", function_name="hello_world", task_data={}, created_by=user, status="completed"
        )
        Task.objects.filter(pk=t.pk).update(completed_at=now - timedelta(days=10))

    initial_count = Task.objects.filter(status="completed").count()
    result = cleanup_old_tasks(days_old=5, dry_run=True)

    assert result["status"] == "success"
    assert result["tasks_deleted"] == 0
    assert result["tasks_found"] >= 3
    assert Task.objects.filter(status="completed").count() == initial_count


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_old_tasks_deletes_qualifying(user):
    from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
    from apps.tasks.models import Task

    now = timezone.now()
    for i in range(2):
        t = Task.objects.create(
            name=f"old_del_{i}", function_name="hello_world", task_data={}, created_by=user, status="completed"
        )
        Task.objects.filter(pk=t.pk).update(completed_at=now - timedelta(days=10))

    # A recent task that should NOT be deleted
    recent = Task.objects.create(
        name="recent", function_name="hello_world", task_data={}, created_by=user, status="completed"
    )
    Task.objects.filter(pk=recent.pk).update(completed_at=now - timedelta(days=1))

    result = cleanup_old_tasks(days_old=5)

    assert result["status"] == "success"
    assert result["tasks_deleted"] >= 2
    assert Task.objects.filter(pk=recent.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_old_tasks_preserves_recurring(user):
    from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
    from apps.tasks.models import Task

    now = timezone.now()
    rec = Task.objects.create(
        name="recurring_old",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="completed",
        cron_expression="0 * * * *",
    )
    Task.objects.filter(pk=rec.pk).update(completed_at=now - timedelta(days=10))

    result = cleanup_old_tasks(days_old=5, preserve_recurring=True)

    assert result["status"] == "success"
    assert Task.objects.filter(pk=rec.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_old_tasks_keeps_recent(user):
    from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
    from apps.tasks.models import Task

    now = timezone.now()
    recent = Task.objects.create(
        name="fresh_task", function_name="hello_world", task_data={}, created_by=user, status="completed"
    )
    Task.objects.filter(pk=recent.pk).update(completed_at=now - timedelta(days=1))

    result = cleanup_old_tasks(days_old=5)
    assert Task.objects.filter(pk=recent.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_old_tasks_include_executions_deletes_them(user):
    from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
    from apps.tasks.models import Task, TaskExecution

    now = timezone.now()
    t = Task.objects.create(
        name="exec_cleanup", function_name="hello_world", task_data={}, created_by=user, status="completed"
    )
    Task.objects.filter(pk=t.pk).update(completed_at=now - timedelta(days=10))
    exec_ = TaskExecution.objects.create(task=t, status="completed")

    result = cleanup_old_tasks(days_old=5, include_executions=True)
    assert result["status"] == "success"
    assert not TaskExecution.objects.filter(pk=exec_.pk).exists()


# ---------------------------------------------------------------------------
# cleanup_metrics_data
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_dry_run():
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data
    from apps.tasks.models import HourlyMetricsCollection

    old_ts = timezone.now() - timedelta(days=10)
    coll = HourlyMetricsCollection.objects.create(
        collector_type="unified_jobs",
        collection_timestamp=old_ts.replace(minute=0, second=0, microsecond=0),
        raw_data={},
        status="collected",
    )

    result = cleanup_metrics_data(hourly_retention_days=7, dry_run=True)
    assert result["status"] == "success"
    assert result["results"]["hourly_collections"]["deleted"] == 0
    assert HourlyMetricsCollection.objects.filter(pk=coll.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_deletes_old_hourly():
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data
    from apps.tasks.models import HourlyMetricsCollection

    old_ts = timezone.now() - timedelta(days=10)
    HourlyMetricsCollection.objects.create(
        collector_type="unified_jobs",
        collection_timestamp=old_ts.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
        raw_data={},
        status="collected",
    )

    result = cleanup_metrics_data(hourly_retention_days=7)
    assert result["status"] == "success"
    assert result["results"]["hourly_collections"]["deleted"] >= 1


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_keeps_recent_hourly():
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data
    from apps.tasks.models import HourlyMetricsCollection

    recent_ts = timezone.now() - timedelta(hours=1)
    coll = HourlyMetricsCollection.objects.create(
        collector_type="unified_jobs",
        collection_timestamp=recent_ts.replace(minute=0, second=0, microsecond=0),
        raw_data={},
        status="collected",
    )

    result = cleanup_metrics_data(hourly_retention_days=7)
    assert HourlyMetricsCollection.objects.filter(pk=coll.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_deletes_old_daily_summary():
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data
    from apps.tasks.models import DailyMetricsSummary

    old_date = timezone.now().date() - timedelta(days=35)
    summary = DailyMetricsSummary.objects.create(summary_date=old_date)

    result = cleanup_metrics_data(daily_retention_days=30)
    assert result["status"] == "success"
    assert not DailyMetricsSummary.objects.filter(pk=summary.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_deletes_old_sent_payload():
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data
    from apps.tasks.models import AnonymizedMetricsPayload

    old_date = timezone.now().date() - timedelta(days=10)
    payload = AnonymizedMetricsPayload.objects.create(
        summary_date=old_date,
        anonymized_data={"data": "x"},
        status="sent",
    )
    AnonymizedMetricsPayload.objects.filter(pk=payload.pk).update(sent_at=timezone.now() - timedelta(days=10))

    result = cleanup_metrics_data(payload_retention_days=7)
    assert result["status"] == "success"
    assert not AnonymizedMetricsPayload.objects.filter(pk=payload.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_deletes_old_analytics_payload():
    from apps.analytics.models import AnalyticsPayload
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data

    now = timezone.now()
    row = AnalyticsPayload.objects.create(collector="controller.config", started_at=now, finished_at=now, payload={})
    AnalyticsPayload.objects.filter(pk=row.pk).update(created=timezone.now() - timedelta(days=400))

    result = cleanup_metrics_data(analytics_retention_days=90)
    assert result["status"] == "success"
    assert result["results"]["analytics_payloads"]["deleted"] >= 1
    assert not AnalyticsPayload.objects.filter(pk=row.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_keeps_recent_analytics_payload():
    from apps.analytics.models import AnalyticsPayload
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data

    now = timezone.now()
    row = AnalyticsPayload.objects.create(collector="controller.config", started_at=now, finished_at=now, payload={})

    result = cleanup_metrics_data(analytics_retention_days=90)
    assert result["status"] == "success"
    assert result["results"]["analytics_payloads"]["deleted"] == 0
    assert AnalyticsPayload.objects.filter(pk=row.pk).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_metrics_data_dry_run_keeps_old_analytics_payload():
    from apps.analytics.models import AnalyticsPayload
    from apps.tasks.cleanup.cleanup_metrics_data import cleanup_metrics_data

    now = timezone.now()
    row = AnalyticsPayload.objects.create(collector="controller.config", started_at=now, finished_at=now, payload={})
    AnalyticsPayload.objects.filter(pk=row.pk).update(created=timezone.now() - timedelta(days=400))

    result = cleanup_metrics_data(analytics_retention_days=90, dry_run=True)
    assert result["status"] == "success"
    assert result["results"]["analytics_payloads"]["found"] >= 1
    assert result["results"]["analytics_payloads"]["deleted"] == 0
    assert AnalyticsPayload.objects.filter(pk=row.pk).exists()


# ---------------------------------------------------------------------------
# cleanup_activitystream
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_cleanup_activitystream_invalid_days_negative():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    result = cleanup_activitystream(days_old=-1)
    assert result["status"] == "error"
    assert "positive integer" in result["error"]


@pytest.mark.unit
def test_cleanup_activitystream_invalid_days_zero():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    result = cleanup_activitystream(days_old=0)
    assert result["status"] == "error"


@pytest.mark.unit
def test_cleanup_activitystream_invalid_days_string():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    result = cleanup_activitystream(days_old="seven")
    assert result["status"] == "error"


@pytest.mark.unit
def test_cleanup_activitystream_dry_run_no_delete():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    mock_qs = MagicMock()
    mock_qs.count.return_value = 5
    mock_entry = MagicMock()
    mock_entry.objects.filter.return_value = mock_qs

    with patch("ansible_base.activitystream.models.Entry", mock_entry):
        result = cleanup_activitystream(days_old=7, dry_run=True)

    assert result["status"] == "success"
    assert result["found"] == 5
    assert result["deleted"] == 0
    mock_qs.delete.assert_not_called()


@pytest.mark.unit
def test_cleanup_activitystream_deletes_old_entries():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    mock_qs = MagicMock()
    mock_qs.count.return_value = 10
    mock_qs.delete.return_value = (10, {"dab_activitystream.Entry": 10})
    mock_entry = MagicMock()
    mock_entry.objects.filter.return_value = mock_qs

    with patch("ansible_base.activitystream.models.Entry", mock_entry):
        result = cleanup_activitystream(days_old=7)

    assert result["status"] == "success"
    assert result["deleted"] == 10
    mock_qs.delete.assert_called_once()


@pytest.mark.unit
def test_cleanup_activitystream_dry_run_zero_entries():
    from apps.tasks.cleanup.cleanup_activitystream import cleanup_activitystream

    mock_qs = MagicMock()
    mock_qs.count.return_value = 0
    mock_entry = MagicMock()
    mock_entry.objects.filter.return_value = mock_qs

    with patch("ansible_base.activitystream.models.Entry", mock_entry):
        result = cleanup_activitystream(days_old=7, dry_run=True)

    assert result["status"] == "success"
    assert result["found"] == 0
