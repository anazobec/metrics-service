"""
Unit tests for apps/tasks/cleanup/cleanup_old_tasks.py

Tests cover:
- Actual deletion when dry_run=False
- Task and execution deletion
- Cascade deletion tracking
- Recurring task preservation/deletion
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks


@pytest.mark.unit
@pytest.mark.django_db
class TestCleanupOldTasks:
    """Test cleanup_old_tasks function."""

    def test_dry_run_counts_without_deleting(self, old_completed_task, old_failed_task):
        """Test dry_run=True counts tasks without deleting."""
        # Arrange
        task1_id = old_completed_task.id
        task2_id = old_failed_task.id

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=True)

        # Assert
        assert result["status"] == "success"
        assert result["dry_run"] is True
        assert result["tasks_found"] == 2
        assert result["tasks_deleted"] == 0

        # Verify tasks still exist
        from apps.tasks.models import Task

        assert Task.objects.filter(id=task1_id).exists()
        assert Task.objects.filter(id=task2_id).exists()

    def test_actual_deletion_when_dry_run_false(self, old_completed_task, old_failed_task):
        """Test actual deletion when dry_run=False."""
        # Arrange
        task1_id = old_completed_task.id
        task2_id = old_failed_task.id

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False)

        # Assert
        assert result["status"] == "success"
        assert result["dry_run"] is False
        assert result["tasks_deleted"] == 2

        # Verify tasks were deleted
        from apps.tasks.models import Task

        assert not Task.objects.filter(id=task1_id).exists()
        assert not Task.objects.filter(id=task2_id).exists()

    def test_deletes_tasks_and_executions_when_include_executions_true(self, old_completed_task):
        """Test deletes both tasks and executions when include_executions=True."""
        # Arrange
        from apps.tasks.models import TaskExecution

        # Create executions for the task
        exec1 = TaskExecution.objects.create(
            task=old_completed_task,
            status="completed",
            started_at=timezone.now() - timedelta(days=100),
        )
        exec2 = TaskExecution.objects.create(
            task=old_completed_task,
            status="failed",
            started_at=timezone.now() - timedelta(days=100),
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False, include_executions=True)

        # Assert
        assert result["tasks_deleted"] == 1
        assert result["executions_deleted"] == 2

        # Verify executions were deleted
        assert not TaskExecution.objects.filter(id=exec1.id).exists()
        assert not TaskExecution.objects.filter(id=exec2.id).exists()

    def test_cascade_deletes_executions_when_include_executions_false(self, old_completed_task):
        """Test cascade deletion tracks executions when include_executions=False."""
        # Arrange
        from apps.tasks.models import TaskExecution

        # Create executions for the task
        TaskExecution.objects.create(
            task=old_completed_task,
            status="completed",
            started_at=timezone.now() - timedelta(days=100),
        )
        TaskExecution.objects.create(
            task=old_completed_task,
            status="failed",
            started_at=timezone.now() - timedelta(days=100),
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False, include_executions=False)

        # Assert
        assert result["tasks_deleted"] == 1
        # Cascade deletion should still track deleted executions
        assert result["executions_deleted"] == 2

    def test_preserves_recurring_tasks_by_default(self, user):
        """Test preserves recurring tasks by default."""
        # Arrange
        from apps.tasks.models import Task

        # Create old recurring task
        old_time = timezone.now() - timedelta(days=100)
        recurring_task = Task.objects.create(
            name="Old Recurring Task",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression="0 * * * *",
            completed_at=old_time,
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False, preserve_recurring=True)

        # Assert
        assert result["preserve_recurring"] is True
        assert result["tasks_deleted"] == 0

        # Verify recurring task still exists
        assert Task.objects.filter(id=recurring_task.id).exists()

    def test_deletes_recurring_tasks_when_preserve_recurring_false(self, user):
        """Test deletes recurring tasks when preserve_recurring=False."""
        # Arrange
        from apps.tasks.models import Task

        # Create old recurring task
        old_time = timezone.now() - timedelta(days=100)
        recurring_task = Task.objects.create(
            name="Old Recurring Task",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression="0 * * * *",
            completed_at=old_time,
        )
        recurring_task_id = recurring_task.id

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False, preserve_recurring=False)

        # Assert
        assert result["preserve_recurring"] is False
        assert result["tasks_deleted"] == 1

        # Verify recurring task was deleted
        assert not Task.objects.filter(id=recurring_task_id).exists()

    def test_uses_completed_at_when_available(self, user):
        """Test uses completed_at field when available."""
        # Arrange
        from apps.tasks.models import Task

        # Create task with completed_at older than cutoff
        old_time = timezone.now() - timedelta(days=100)
        Task.objects.create(
            name="Old Task",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            completed_at=old_time,
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False)

        # Assert
        assert result["tasks_deleted"] == 1

    def test_falls_back_to_modified_when_completed_at_null(self, user):
        """Test falls back to modified field when completed_at is null."""
        # Arrange
        from apps.tasks.models import Task

        # Create task without completed_at
        task = Task.objects.create(
            name="Old Task",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
        )
        # Update modified to old date
        old_time = timezone.now() - timedelta(days=100)
        Task.objects.filter(id=task.id).update(modified=old_time, completed_at=None)

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False)

        # Assert
        assert result["tasks_deleted"] == 1

    def test_only_deletes_completed_and_failed_tasks(self, user):
        """Test only deletes tasks with completed or failed status."""
        # Arrange
        from apps.tasks.models import Task

        old_time = timezone.now() - timedelta(days=100)

        # Create tasks with various statuses
        pending_task = Task.objects.create(
            name="Pending Task",
            function_name="hello_world",
            created_by=user,
            status="pending",
        )
        Task.objects.filter(id=pending_task.id).update(modified=old_time)

        running_task = Task.objects.create(
            name="Running Task",
            function_name="hello_world",
            created_by=user,
            status="running",
        )
        Task.objects.filter(id=running_task.id).update(modified=old_time)

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False)

        # Assert
        assert result["tasks_deleted"] == 0

        # Verify tasks still exist
        assert Task.objects.filter(id=pending_task.id).exists()
        assert Task.objects.filter(id=running_task.id).exists()

    def test_does_not_delete_recent_tasks(self, user):
        """Test does not delete recent completed/failed tasks."""
        # Arrange
        from apps.tasks.models import Task

        # Create recent completed task
        recent_task = Task.objects.create(
            name="Recent Task",
            function_name="hello_world",
            created_by=user,
            status="completed",
            completed_at=timezone.now() - timedelta(days=1),
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False)

        # Assert
        assert result["tasks_deleted"] == 0

        # Verify task still exists
        assert Task.objects.filter(id=recent_task.id).exists()

    def test_uses_custom_days_old_parameter(self, user):
        """Test uses custom days_old parameter."""
        # Arrange
        from apps.tasks.models import Task

        # Create task 15 days old
        old_time = timezone.now() - timedelta(days=15)
        Task.objects.create(
            name="15 Day Old Task",
            function_name="hello_world",
            created_by=user,
            status="completed",
            completed_at=old_time,
        )

        # Act - should not delete with 30 day cutoff
        result_30 = cleanup_old_tasks(days_old=30, dry_run=False)
        assert result_30["tasks_deleted"] == 0

        # Act - should delete with 10 day cutoff
        result_10 = cleanup_old_tasks(days_old=10, dry_run=False)
        assert result_10["tasks_deleted"] == 1

    def test_returns_correct_cutoff_date(self):
        """Test returns correct cutoff_date in result."""
        # Arrange
        before_call = timezone.now()

        # Act
        result = cleanup_old_tasks(days_old=5, dry_run=True)

        # Assert
        after_call = timezone.now()
        cutoff = timezone.datetime.fromisoformat(result["cutoff_date"])

        expected_min = before_call - timedelta(days=5)
        expected_max = after_call - timedelta(days=5)

        assert expected_min <= cutoff <= expected_max

    def test_cleans_up_tasks_with_empty_cron_expression(self, user):
        """Test tasks with empty cron_expression are treated as non-recurring and cleaned up."""
        # Arrange
        from apps.tasks.models import Task

        old_time = timezone.now() - timedelta(days=100)

        # Create task with empty string cron_expression (simulating old API bug)
        task_with_empty_string = Task.objects.create(
            name="Task with empty cron",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression="",  # Empty string should be cleaned up
            completed_at=old_time,
        )
        task_with_empty_string_fallback = Task.objects.create(
            name="Task with empty cron fallback",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression="",
            completed_at=None,
        )
        Task.objects.filter(id=task_with_empty_string_fallback.id).update(modified=old_time)

        # Create task with NULL cron_expression
        task_with_null = Task.objects.create(
            name="Task with null cron",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression=None,  # NULL should be cleaned up
            completed_at=old_time,
        )

        # Create task with valid cron_expression
        task_with_valid_cron = Task.objects.create(
            name="Task with valid cron",
            function_name="hello_world",
            task_data={},
            created_by=user,
            status="completed",
            cron_expression="0 * * * *",  # Valid cron should be preserved
            completed_at=old_time,
        )

        # Act
        result = cleanup_old_tasks(days_old=30, dry_run=False, preserve_recurring=True)

        # Assert
        # Empty string and NULL should be deleted, but valid cron should be preserved
        assert result["tasks_deleted"] == 3
        assert not Task.objects.filter(id=task_with_null.id).exists()
        assert not Task.objects.filter(id=task_with_empty_string.id).exists()
        assert not Task.objects.filter(id=task_with_empty_string_fallback.id).exists()

        # Valid cron task should still exist
        assert Task.objects.filter(id=task_with_valid_cron.id).exists()
