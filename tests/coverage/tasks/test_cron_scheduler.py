"""
Unit tests for apps/tasks/cron_scheduler.py.
Targets 13.78% → ~90% coverage.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone


# ---------------------------------------------------------------------------
# _inject_dispatch_timestamps
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_inject_timestamps_hourly_pins_previous_hour():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    result = _inject_dispatch_timestamps("collect_hourly_metrics", {})
    assert "hour_timestamp" in result
    # The pinned timestamp should be on the hour boundary
    from apps.tasks.utils import parse_datetime_string

    dt = parse_datetime_string(result["hour_timestamp"])
    assert dt is not None
    assert dt.minute == 0
    assert dt.second == 0


@pytest.mark.unit
def test_inject_timestamps_snapshot_sets_collection_timestamp():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    result = _inject_dispatch_timestamps("collect_snapshot_metrics", {})
    assert "collection_timestamp" in result


@pytest.mark.unit
def test_inject_timestamps_daily_sets_hour_timestamp():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    result = _inject_dispatch_timestamps("collect_daily_metrics", {})
    assert "hour_timestamp" in result
    from apps.tasks.utils import parse_datetime_string

    dt = parse_datetime_string(result["hour_timestamp"])
    assert dt.hour == 0


@pytest.mark.unit
def test_inject_timestamps_does_not_overwrite_existing():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    existing_ts = "2024-01-01T10:00:00+00:00"
    task_data = {"hour_timestamp": existing_ts}
    result = _inject_dispatch_timestamps("collect_hourly_metrics", task_data)
    assert result["hour_timestamp"] == existing_ts


@pytest.mark.unit
def test_inject_timestamps_unknown_function_unchanged():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    task_data = {"collector_type": "foo"}
    result = _inject_dispatch_timestamps("some_other_function", task_data)
    assert result == {"collector_type": "foo"}


@pytest.mark.unit
def test_inject_timestamps_returns_copy():
    from apps.tasks.cron_scheduler import _inject_dispatch_timestamps

    original = {}
    result = _inject_dispatch_timestamps("collect_hourly_metrics", original)
    assert result is not original


# ---------------------------------------------------------------------------
# UnifiedTaskScheduler — start / stop
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_scheduler_start_and_stop(mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    # Reset the global singleton
    cs._scheduler_instance = None

    with patch.object(cs.UnifiedTaskScheduler, "_sync_database_tasks"):
        scheduler = cs.UnifiedTaskScheduler()
        scheduler.start()

    assert scheduler.running is True
    scheduler.scheduler = mock_apscheduler
    scheduler.stop()
    assert scheduler.running is False


@pytest.mark.unit
def test_scheduler_start_idempotent(mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    with patch.object(cs.UnifiedTaskScheduler, "_sync_database_tasks"):
        scheduler = cs.UnifiedTaskScheduler()
        scheduler.start()
        # Second call should be a no-op
        scheduler.start()

    # scheduler.start() on the mock should have been called only once
    mock_apscheduler.start.assert_called_once()
    scheduler.scheduler = mock_apscheduler
    scheduler.stop()


@pytest.mark.unit
def test_scheduler_stop_when_not_running(mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    # Stop without starting should be a no-op
    scheduler.stop()
    mock_apscheduler.shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# _task_feature_flag_enabled
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_feature_flag_enabled_no_flag(user):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(name="t", function_name="hello_world", task_data={}, created_by=user)
    scheduler = cs.UnifiedTaskScheduler()
    assert scheduler._task_feature_flag_enabled(task) is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_feature_flag_enabled_flag_on(user):
    import apps.tasks.cron_scheduler as cs
    from apps.dynamic_settings.models import Setting
    from apps.tasks.models import Task

    Setting.objects.get_or_create(setting_key="SCHED_FLAG_ON", defaults={"current_value": "true"})
    task = Task.objects.create(
        name="t", function_name="hello_world", task_data={"_feature_flag": "SCHED_FLAG_ON"}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    assert scheduler._task_feature_flag_enabled(task) is True


@pytest.mark.unit
@pytest.mark.django_db
def test_task_feature_flag_enabled_flag_off(user):
    import apps.tasks.cron_scheduler as cs
    from apps.dynamic_settings.models import Setting
    from apps.tasks.models import Task

    Setting.objects.get_or_create(setting_key="SCHED_FLAG_OFF", defaults={"current_value": "false"})
    task = Task.objects.create(
        name="t2", function_name="hello_world", task_data={"_feature_flag": "SCHED_FLAG_OFF"}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    assert scheduler._task_feature_flag_enabled(task) is False


@pytest.mark.unit
def test_task_feature_flag_enabled_reuses_sync_cache():
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    task_a = MagicMock(task_data={"_feature_flag": "SHARED_FLAG"})
    task_b = MagicMock(task_data={"_feature_flag": "SHARED_FLAG"})
    feature_flags = {}

    with patch("apps.tasks.task_groups.get_feature_enabled_from_db", return_value=True) as mock_get_feature:
        assert scheduler._task_feature_flag_enabled(task_a, feature_flags) is True
        assert scheduler._task_feature_flag_enabled(task_b, feature_flags) is True

    mock_get_feature.assert_called_once_with("SHARED_FLAG")
    assert feature_flags == {"SHARED_FLAG": True}


# ---------------------------------------------------------------------------
# _add_database_recurring_task
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_add_recurring_task_registers_job(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="rec", function_name="hello_world", cron_expression="0 * * * *", task_data={}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._add_database_recurring_task(task)

    mock_apscheduler.add_job.assert_called_once()
    assert task.id in scheduler._db_task_jobs


@pytest.mark.unit
@pytest.mark.django_db
def test_add_recurring_task_skips_if_already_tracked(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="rec2", function_name="hello_world", cron_expression="0 * * * *", task_data={}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._db_task_jobs[task.id] = "already_there"

    scheduler._add_database_recurring_task(task)
    mock_apscheduler.add_job.assert_not_called()


# ---------------------------------------------------------------------------
# _add_database_scheduled_task
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_add_scheduled_task_future_registers_job(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    future_time = timezone.now() + timedelta(hours=1)
    task = Task.objects.create(
        name="sched", function_name="hello_world", scheduled_time=future_time, task_data={}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._add_database_scheduled_task(task)

    mock_apscheduler.add_job.assert_called_once()
    assert task.id in scheduler._db_task_jobs


@pytest.mark.unit
@pytest.mark.django_db
def test_add_scheduled_task_past_due_executes_immediately(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    past_time = timezone.now() - timedelta(hours=2)
    task = Task.objects.create(
        name="past_sched", function_name="hello_world", scheduled_time=past_time, task_data={}, created_by=user
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    with patch.object(scheduler, "_execute_database_task") as mock_execute:
        scheduler._add_database_scheduled_task(task)
        mock_execute.assert_called_once_with(task.id)


# ---------------------------------------------------------------------------
# _remove_database_task
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_remove_database_task_calls_remove_job(mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._db_task_jobs[42] = "job_id_42"

    scheduler._remove_database_task(42)

    mock_apscheduler.remove_job.assert_called_once_with("job_id_42")
    assert 42 not in scheduler._db_task_jobs


@pytest.mark.unit
def test_remove_database_task_not_tracked_is_noop(mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    # Calling remove on a non-tracked ID should not raise
    scheduler._remove_database_task(999)
    mock_apscheduler.remove_job.assert_not_called()


# ---------------------------------------------------------------------------
# _execute_database_task — recurring creates child
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_execute_database_task_recurring_creates_child_task(user, mock_apscheduler, mock_dispatcherd_config):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="template",
        function_name="hello_world",
        cron_expression="0 * * * *",
        task_data={"_feature_flag": "METRICS_COLLECTION"},
        created_by=user,
        is_system_task=True,
    )

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.tasks_system.submit_task_to_dispatcher") as mock_submit,
        patch("apps.tasks.task_groups.get_feature_enabled_from_db", return_value=True),
    ):
        scheduler._execute_database_task(task.id)

    # A new (non-recurring) child task should be created
    child = Task.objects.exclude(id=task.id).filter(function_name="hello_world").first()
    assert child is not None
    assert child.cron_expression is None
    mock_submit.assert_called_once_with(child)


@pytest.mark.unit
@pytest.mark.django_db
def test_execute_database_task_not_found_removes_tracking(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._db_task_jobs[9999] = "some_job"

    with patch("apps.tasks.cron_scheduler.close_old_connections"):
        scheduler._execute_database_task(9999)

    # Job should be removed from tracking
    assert 9999 not in scheduler._db_task_jobs


@pytest.mark.unit
@pytest.mark.django_db
def test_execute_database_task_cancelled_task_removed(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="cancelled", function_name="hello_world", task_data={}, created_by=user, status="cancelled"
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._db_task_jobs[task.id] = "job_id"

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch.object(scheduler, "_remove_database_task") as mock_remove,
    ):
        scheduler._execute_database_task(task.id)
        mock_remove.assert_called_once_with(task.id)


# ---------------------------------------------------------------------------
# _execute_database_task — submit errors
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_execute_database_task_recurring_submit_error_logs_warning(user, mock_apscheduler, mock_dispatcherd_config):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="template",
        function_name="hello_world",
        cron_expression="0 * * * *",
        task_data={},
        created_by=user,
        is_system_task=True,
    )

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.tasks_system.submit_task_to_dispatcher", side_effect=RuntimeError("broker down")),
    ):
        scheduler._execute_database_task(task.id)

    # Child task was still created despite submit failure
    assert Task.objects.exclude(id=task.id).filter(function_name="hello_world").exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_execute_database_task_nonrecurring_submit_error_removes_tracking(
    user, mock_apscheduler, mock_dispatcherd_config
):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="one_shot", function_name="hello_world", task_data={}, created_by=user, status="pending"
    )
    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    scheduler._db_task_jobs[task.id] = "job_id"

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.tasks_system.submit_task_to_dispatcher", side_effect=RuntimeError("broker down")),
    ):
        scheduler._execute_database_task(task.id)

    # Task removed from tracking even on submit failure (finally block)
    assert task.id not in scheduler._db_task_jobs


# ---------------------------------------------------------------------------
# _periodic_database_sync — retry error handling
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_continues_when_retry_failed_tasks_raises(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=True),
        patch.object(scheduler, "_retry_failed_tasks", side_effect=RuntimeError("db error")),
        patch("apps.tasks.models.Task.immediate_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.scheduled_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.recurring_tasks", return_value=Task.objects.none()),
    ):
        scheduler._periodic_database_sync()


# ---------------------------------------------------------------------------
# _periodic_database_sync — stuck tasks
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_fails_stuck_tasks(user, mock_apscheduler):
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task, TaskExecution

    task = Task.objects.create(
        name="stuck",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="running",
        max_attempts=1,
        attempts=1,
    )
    # Backdate started_at to trigger stuck detection
    started = timezone.now() - timedelta(seconds=cs.STUCK_TASK_TIMEOUT_SECONDS + 60)
    Task.objects.filter(pk=task.pk).update(started_at=started)
    execution = TaskExecution.objects.create(task=task, status="running")

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    # _periodic_database_sync also calls Task.immediate_tasks() etc. — mock them to return empty
    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=True),
        patch("apps.tasks.models.Task.immediate_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.scheduled_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.recurring_tasks", return_value=Task.objects.none()),
    ):
        scheduler._periodic_database_sync()

    task.refresh_from_db()
    execution.refresh_from_db()
    assert task.status == "failed"
    assert execution.status == "failed"


# ---------------------------------------------------------------------------
# get_scheduler singleton
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_get_scheduler_returns_same_instance():
    import apps.tasks.cron_scheduler as cs

    cs._scheduler_instance = None
    s1 = cs.get_scheduler()
    s2 = cs.get_scheduler()
    assert s1 is s2
    cs._scheduler_instance = None  # cleanup


# ---------------------------------------------------------------------------
# awx_db_ready (moved to utils.py)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_awx_db_ready_returns_true_when_tables_exist():
    """Test awx_db_ready returns True when probe tables exist."""
    from unittest.mock import MagicMock

    from apps.tasks.utils import awx_db_ready

    # Mock get_db_connection to return a connection
    mock_conn = MagicMock()

    # Mock _check_db_ready to return True (tables found)
    with (
        patch("apps.tasks.utils.get_db_connection", return_value=mock_conn),
        patch("apps.tasks.utils._check_db_ready", return_value=True),
    ):
        result = awx_db_ready()

    assert result is True


@pytest.mark.unit
def test_awx_db_ready_returns_false_when_tables_missing():
    """Test awx_db_ready returns False when probe tables don't exist."""
    from unittest.mock import MagicMock

    from apps.tasks.utils import awx_db_ready

    # Mock get_db_connection to return a connection
    mock_conn = MagicMock()

    # Mock _check_db_ready to return False (tables not found)
    with (
        patch("apps.tasks.utils.get_db_connection", return_value=mock_conn),
        patch("apps.tasks.utils._check_db_ready", return_value=False),
    ):
        result = awx_db_ready()

    assert result is False


@pytest.mark.unit
def test_awx_db_ready_returns_false_on_exception():
    """Test awx_db_ready returns False when an exception occurs."""
    from apps.tasks.utils import awx_db_ready

    # Mock get_db_connection to raise an exception
    with patch("apps.tasks.utils.get_db_connection", side_effect=Exception("DB connection failed")):
        result = awx_db_ready()

    assert result is False


# ---------------------------------------------------------------------------
# DB readiness error escalation
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_tracks_db_not_ready_during_grace_period(user, mock_apscheduler):
    """Test that timestamp is set when DB is not ready within grace period."""
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler

    # Initially should be None
    assert scheduler._db_not_ready_since is None

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=False),
        patch.object(cs.UnifiedTaskScheduler, "_fail_stuck_tasks"),
        patch("apps.tasks.cron_scheduler.logger") as mock_logger,
    ):
        scheduler._periodic_database_sync()

    # Timestamp should be set when DB is not ready
    assert scheduler._db_not_ready_since is not None
    # Should call warning, not error (during grace period)
    mock_logger.warning.assert_called_once()
    mock_logger.error.assert_not_called()


@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_escalates_to_error_after_grace_period(user, mock_apscheduler):
    """Test that ERROR is logged when DB is not ready after grace period."""
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    # Simulate that DB has been unready for longer than grace period
    scheduler._db_not_ready_since = timezone.now() - timedelta(minutes=15)

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=False),
        patch.object(cs.UnifiedTaskScheduler, "_fail_stuck_tasks"),
        patch("apps.tasks.cron_scheduler.logger") as mock_logger,
    ):
        scheduler._periodic_database_sync()

    # Should log error after grace period
    mock_logger.error.assert_called_once()
    mock_logger.warning.assert_not_called()
    # Verify error message mentions migrations failed
    error_call_args = mock_logger.error.call_args[0][0]
    assert "migrations failed" in error_call_args


@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_clears_timestamp_when_db_ready(user, mock_apscheduler):
    """Test that timestamp is cleared and info logged when DB becomes ready."""
    import apps.tasks.cron_scheduler as cs
    from apps.tasks.models import Task

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    # Simulate that DB was unready for some time
    scheduler._db_not_ready_since = timezone.now() - timedelta(seconds=120)

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=True),
        patch.object(cs.UnifiedTaskScheduler, "_fail_stuck_tasks"),
        patch("apps.tasks.models.Task.immediate_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.scheduled_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.models.Task.recurring_tasks", return_value=Task.objects.none()),
        patch("apps.tasks.cron_scheduler.logger") as mock_logger,
    ):
        scheduler._periodic_database_sync()

    # Should log info that DB is ready
    mock_logger.info.assert_called()
    info_call_args = mock_logger.info.call_args[0][0]
    assert "AWX database is now ready" in info_call_args
    # Timestamp should be cleared
    assert scheduler._db_not_ready_since is None


@pytest.mark.unit
@pytest.mark.django_db
def test_periodic_sync_shows_elapsed_time_in_logs(user, mock_apscheduler):
    """Test that elapsed time is shown in warning messages."""
    import apps.tasks.cron_scheduler as cs

    scheduler = cs.UnifiedTaskScheduler()
    scheduler.scheduler = mock_apscheduler
    # Simulate that DB has been unready for 3 minutes
    scheduler._db_not_ready_since = timezone.now() - timedelta(minutes=3)

    with (
        patch("apps.tasks.cron_scheduler.close_old_connections"),
        patch("apps.tasks.utils.awx_db_ready", return_value=False),
        patch.object(cs.UnifiedTaskScheduler, "_fail_stuck_tasks"),
        patch("apps.tasks.cron_scheduler.logger") as mock_logger,
    ):
        scheduler._periodic_database_sync()

    # Should show elapsed time (around 180 seconds)
    mock_logger.warning.assert_called_once()
    warning_call_args = mock_logger.warning.call_args[0][0]
    assert "s elapsed" in warning_call_args
