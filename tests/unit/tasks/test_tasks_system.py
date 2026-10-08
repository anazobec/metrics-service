"""
Comprehensive tests for task system functionality.

This module tests the complete task system including task functions, registry,
system task creation, execution, and helper functions.

Organized into clear test classes:
- TestExecuteDbTask: Database task execution and error handling
- TestTaskRegistry: TASK_FUNCTIONS and TASK_METADATA configuration
- TestSystemTaskCreation: System task initialization and management
- TestTaskDispatcher: Task submission and dispatcher integration
- TestEdgeCasesAndErrorHandling: Edge cases, error conditions, and resilience
"""

from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.tasks import tasks, tasks_system
from apps.tasks.models import Task, TaskExecution
from apps.tasks.tasks import (
    TASK_FUNCTIONS,
    submit_task_to_dispatcher,
)
from apps.tasks.tasks_system import execute_db_task
from tests.test_utils import get_test_password

User = get_user_model()


# =============================================================================
# Execute DB Task Tests
# =============================================================================


@pytest.mark.unit
class TestExecuteDbTask(TestCase):
    """Test execute_db_task function and database task execution."""

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(username="taskuser")
        self.task = self._create_task_safely(name="Test Task", function_name="hello_world", task_data={})

    def _create_task_safely(self, **kwargs):
        """Create a task without triggering signals."""
        task = Task(**kwargs)
        task.save()
        return task

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_success(self):
        """Test successful task execution."""
        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "success"

        # Verify task state was updated
        self.task.refresh_from_db()
        assert self.task.status == "completed"
        assert self.task.attempts == 1
        assert self.task.started_at is not None
        assert self.task.completed_at is not None

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_already_claimed(self):
        """Test that a task already claimed by another worker is skipped."""
        self.task.status = "running"
        self.task.save()

        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        assert "already claimed" in result["error"]

        # Task status should remain running (not overwritten)
        self.task.refresh_from_db()
        assert self.task.status == "running"

    @pytest.mark.django_db(transaction=True)
    def test_losing_claim_creates_no_execution(self):
        """Test that a failed claim leaves no orphaned TaskExecution records.

        Before the fix, submit_task_to_dispatcher created a TaskExecution eagerly,
        and a losing claimer would leave it stuck as pending forever. Now _claim_task
        only creates the execution on success, so a loser must produce zero records.
        """
        # Simulate another worker already claimed the task
        self.task.status = "running"
        self.task.save()

        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        # No TaskExecution should have been created
        assert TaskExecution.objects.filter(task=self.task).count() == 0

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_creates_execution_record(self):
        """Test that execute_db_task creates a TaskExecution record via _claim_task."""
        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "success"

        # _claim_task should have created an execution record
        execution = TaskExecution.objects.filter(task=self.task).first()
        assert execution is not None
        assert execution.status == "completed"

    def test_execute_db_task_no_task_id(self):
        """Test execute_db_task without task_id parameter."""
        result = execute_db_task()

        assert result["status"] == "error"
        assert "task_id is required" in result["error"]

    def test_execute_db_task_task_not_found(self):
        """Test execute_db_task with non-existent task ID."""
        result = execute_db_task(task_id=99999)

        assert result["status"] == "error"
        assert "Task matching query does not exist" in result["error"]

    def test_execute_db_task_function_not_found(self):
        """Test execute_db_task with unknown function name."""
        self.task.function_name = "unknown_function"
        self.task.save()

        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        assert "not found in TASK_FUNCTIONS" in result["error"]

        # Task should be marked as failed
        self.task.refresh_from_db()
        assert self.task.status == "failed"

    @pytest.mark.django_db(transaction=True)
    @patch("apps.tasks.tasks.TASK_FUNCTIONS")
    def test_execute_db_task_function_exception(self, mock_task_functions):
        """Test execute_db_task when task function raises exception."""
        mock_function = Mock(side_effect=Exception("Test exception"))
        mock_task_functions.__getitem__.return_value = mock_function
        mock_task_functions.__contains__.return_value = True

        result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        assert "Test exception" in result["error"]

    @pytest.mark.django_db(transaction=True)
    @patch("apps.tasks.tasks.TASK_FUNCTIONS")
    def test_execute_db_task_exception_marks_execution_failed(self, mock_task_functions):
        """Test that an exception after _claim_task marks the TaskExecution as failed.

        Before the fix, the except block passed None for both task and execution,
        leaving the TaskExecution stuck in "running" status forever.
        """
        mock_function = Mock(side_effect=Exception("Boom"))
        mock_task_functions.__getitem__.return_value = mock_function
        mock_task_functions.__contains__.return_value = True

        execute_db_task(task_id=self.task.id)

        # The TaskExecution created by _claim_task must be marked as failed
        execution = TaskExecution.objects.get(task=self.task)
        assert execution.status == "failed"
        assert "Boom" in execution.error_message

        # Task stays failed — the periodic scheduler handles retries
        self.task.refresh_from_db()
        assert self.task.status == "failed"

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_with_advisory_lock(self):
        """Test that tasks in TASK_LOCKS go through run_with_lock."""
        self.task.function_name = "daily_metrics_rollup"
        self.task.save()

        with patch("apps.tasks.tasks_system.run_with_lock") as mock_lock:
            mock_lock.return_value = {"status": "success"}
            result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "success"
        mock_lock.assert_called_once()
        assert mock_lock.call_args[0][0] == "daily_metrics_rollup"

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_contended_lock_marks_failed(self):
        """Test that a task which cannot acquire its advisory lock is marked failed."""
        self.task.function_name = "daily_metrics_rollup"
        self.task.max_attempts = 3
        self.task.save()

        with patch("apps.tasks.tasks_system.run_with_lock") as mock_lock:
            mock_lock.return_value = {"status": "error", "error": "Could not acquire lock"}
            result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        assert "lock" in result["error"].lower()

        self.task.refresh_from_db()
        assert self.task.status == "failed"

    @pytest.mark.django_db(transaction=True)
    def test_execute_db_task_failure_marks_failed_for_scheduler_retry(self):
        """Failed task is marked failed; the periodic scheduler handles retry."""
        self.task.function_name = "hello_world"
        self.task.max_attempts = 3
        self.task.task_data = {"retry_delay_seconds": 120}
        self.task.save()

        with patch("apps.tasks.tasks.TASK_FUNCTIONS") as mock_fns:
            mock_fns.__contains__.return_value = True
            mock_fns.__getitem__.return_value = Mock(return_value={"status": "error", "error": "fail"})
            result = execute_db_task(task_id=self.task.id)

        assert result["status"] == "error"
        self.task.refresh_from_db()
        assert self.task.status == "failed"
        assert self.task.attempts < self.task.max_attempts


# =============================================================================
# Task Registry Tests
# =============================================================================


@pytest.mark.unit
class TestTaskRegistry(TestCase):
    """Test TASK_FUNCTIONS registry and task configuration."""

    def test_task_functions_registry_exists(self):
        """Test that TASK_FUNCTIONS registry exists and is properly configured."""
        assert hasattr(tasks, "TASK_FUNCTIONS")
        assert isinstance(tasks.TASK_FUNCTIONS, dict)

    def test_task_functions_contains_expected_functions(self):
        """Test TASK_FUNCTIONS contains all expected task functions."""
        expected_functions = [
            "hello_world",
            "cleanup_old_tasks",
        ]

        for func_name in expected_functions:
            assert func_name in TASK_FUNCTIONS, f"{func_name} not in TASK_FUNCTIONS"
            assert callable(TASK_FUNCTIONS[func_name]), f"{func_name} is not callable"

    def test_task_function_signatures(self):
        """Test task functions accept keyword arguments."""
        import inspect

        for func_name, func in TASK_FUNCTIONS.items():
            sig = inspect.signature(func)
            # Every task function must accept **kwargs
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            assert has_var_keyword, f"{func_name} does not accept **kwargs"

    def test_every_task_function_has_metadata_with_queue(self):
        """Every TASK_FUNCTIONS entry must have a TASK_METADATA entry with a valid queue."""
        import yaml

        from apps.tasks.tasks import TASK_METADATA

        # Load valid queues from dispatcherd config
        config_path = Path(__file__).resolve().parents[3] / "apps" / "settings" / "dispatcherd.yaml"
        with open(config_path) as f:
            valid_queues = set(yaml.safe_load(f)["brokers"]["pg_notify"]["channels"])

        for func_name in TASK_FUNCTIONS:
            assert func_name in TASK_METADATA, f"{func_name} missing from TASK_METADATA"
            queue = TASK_METADATA[func_name].get("queue")
            assert queue, f"{func_name} has no queue in TASK_METADATA"
            assert queue in valid_queues, f"{func_name} has unknown queue {queue!r} (valid: {valid_queues})"


# =============================================================================
# System Task Creation Tests
# =============================================================================


@pytest.mark.unit
class TestSystemTaskCreation(TestCase):
    """Test system task initialization and management."""

    def setUp(self):
        """Set up test environment."""
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password=get_test_password()
        )

    def test_create_system_tasks_with_disabled_tasks(self):
        """Test handling of disabled system tasks."""
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value={}):
            result = tasks_system.create_system_tasks()

            # All tasks filtered out
            assert result["created"] == 0
            assert result["removed"] == 0

    def test_all_system_tasks_deleted_on_reinit(self):
        """All existing system tasks are unconditionally removed on reinit.

        create_system_tasks() is only called from the init container, before
        the app starts, so no tasks can be running at that point.
        """
        for name in ("task_a", "task_b"):
            Task.objects.create(name=name, function_name="hello_world", is_system_task=True)
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value={}):
            result = tasks_system.create_system_tasks()

        assert result["removed"] == 2
        assert Task.objects.filter(is_system_task=True).count() == 0

    def test_create_task_from_group_respects_max_attempts_override(self):
        """Tasks with a max_attempts field in their config get that value instead of the model default."""
        config = {
            "function": "hello_world",
            "description": "Test task",
            "cron": None,
            "max_attempts": 7,
        }
        results = {"created": 0, "tasks": []}
        tasks_system._create_task_from_group("test_override_task", config, results, Task)

        task = Task.objects.get(name="test_override_task")
        assert task.max_attempts == 7

    def test_create_task_from_group_uses_model_default_when_no_max_attempts(self):
        """Tasks without a max_attempts field in their config use the model default."""
        config = {
            "function": "hello_world",
            "description": "Test task",
            "cron": None,
        }
        results = {"created": 0, "tasks": []}
        tasks_system._create_task_from_group("test_default_task", config, results, Task)

        task = Task.objects.get(name="test_default_task")
        assert task.max_attempts == 3

    @pytest.mark.django_db(transaction=True)
    def test_completed_oneshot_preserved_across_reinit(self):
        """A completed one-shot task retains completed status after create_system_tasks()."""
        Task.objects.create(
            name="initial_dashboard_collection",
            function_name="hello_world",
            is_system_task=True,
            cron_expression=None,
            status="completed",
        )
        one_shot_config = {
            "initial_dashboard_collection": {
                "function": "hello_world",
                "description": "one-shot",
                "cron": None,
                "args": {},
            }
        }
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=one_shot_config):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_dashboard_collection", is_system_task=True)
        assert task.status == "completed"

    @pytest.mark.django_db(transaction=True)
    def test_completed_oneshot_keeps_completed_at_across_reinit(self):
        """A completed one-shot task keeps its original completed_at after create_system_tasks()."""
        completed_at = timezone.now() - timedelta(days=10)
        Task.objects.create(
            name="initial_dashboard_collection",
            function_name="hello_world",
            is_system_task=True,
            cron_expression=None,
            status="completed",
            completed_at=completed_at,
        )
        one_shot_config = {
            "initial_dashboard_collection": {
                "function": "hello_world",
                "description": "one-shot",
                "cron": None,
                "args": {},
            }
        }
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=one_shot_config):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_dashboard_collection", is_system_task=True)
        assert task.status == "completed"
        assert task.completed_at == completed_at

    @pytest.mark.django_db(transaction=True)
    def test_completed_initial_resource_sync_is_preserved_for_same_build(self):
        """A successful Gateway resource sync stays complete across restarts of the same build."""
        Task.objects.create(
            name="initial_resource_sync",
            function_name="sync_resources_from_gateway",
            is_system_task=True,
            cron_expression=None,
            status="completed",
            task_data={"_resource_sync_version": "build-a"},
        )
        sync_config = {
            "initial_resource_sync": {
                "function": "sync_resources_from_gateway",
                "description": "Gateway resource sync",
                "cron": None,
                "args": {},
            }
        }
        with (
            patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=sync_config),
            patch("apps.tasks.tasks_system._get_resource_sync_version", return_value="build-a"),
        ):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_resource_sync", is_system_task=True)
        assert task.status == "completed"
        assert task.task_data["_resource_sync_version"] == "build-a"

    @pytest.mark.django_db(transaction=True)
    def test_completed_initial_resource_sync_is_requeued_for_new_build(self):
        """A successful sync is pending again when the installed build changes."""
        Task.objects.create(
            name="initial_resource_sync",
            function_name="sync_resources_from_gateway",
            is_system_task=True,
            cron_expression=None,
            status="completed",
            task_data={"_resource_sync_version": "build-a"},
        )
        sync_config = {
            "initial_resource_sync": {
                "function": "sync_resources_from_gateway",
                "description": "Gateway resource sync",
                "cron": None,
                "args": {},
            }
        }
        with (
            patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=sync_config),
            patch("apps.tasks.tasks_system._get_resource_sync_version", return_value="build-b"),
        ):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_resource_sync", is_system_task=True)
        assert task.status == "pending"
        assert task.task_data["_resource_sync_version"] == "build-b"

    @pytest.mark.django_db(transaction=True)
    def test_initial_resource_sync_records_build_fingerprint(self):
        """A fresh sync task records the build that it must complete for."""
        sync_config = {
            "initial_resource_sync": {
                "function": "sync_resources_from_gateway",
                "description": "Gateway resource sync",
                "cron": None,
                "args": {},
            }
        }
        with (
            patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=sync_config),
            patch("apps.tasks.tasks_system._get_resource_sync_version", return_value="build-a"),
        ):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_resource_sync", is_system_task=True)
        assert task.status == "pending"
        assert task.task_data["_resource_sync_version"] == "build-a"

    @pytest.mark.django_db(transaction=True)
    def test_failed_oneshot_is_reset_to_pending(self):
        """A failed one-shot task is recreated as pending so it can retry on next init."""
        Task.objects.create(
            name="initial_dashboard_collection",
            function_name="hello_world",
            is_system_task=True,
            cron_expression=None,
            status="failed",
        )
        one_shot_config = {
            "initial_dashboard_collection": {
                "function": "hello_world",
                "description": "one-shot",
                "cron": None,
                "args": {},
            }
        }
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=one_shot_config):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_dashboard_collection", is_system_task=True)
        assert task.status == "pending"

    @pytest.mark.django_db(transaction=True)
    def test_fresh_install_oneshot_starts_pending(self):
        """With no prior tasks (fresh install), one-shot tasks are created as pending."""
        one_shot_config = {
            "initial_dashboard_collection": {
                "function": "hello_world",
                "description": "one-shot",
                "cron": None,
                "args": {},
            }
        }
        with patch("apps.tasks.task_groups.get_all_tasks_for_init", return_value=one_shot_config):
            tasks_system.create_system_tasks()

        task = Task.objects.get(name="initial_dashboard_collection", is_system_task=True)
        assert task.status == "pending"


# =============================================================================
# System Task Helper Functions Tests
# =============================================================================


# =============================================================================
# Task Dispatcher Tests
# =============================================================================


@pytest.mark.unit
class TestTaskDispatcher(TestCase):
    """Test task submission and dispatcher integration."""

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(username="submituser")
        # Create task without triggering signals
        self.task = Task(name="Submit Task", function_name="hello_world", created_by=self.user)
        self.task.save()

    @patch("apps.tasks.dispatcherd_config.ensure_dispatcherd_configured")
    def test_submit_task_to_dispatcher_raises_on_failure(self, mock_ensure_config):
        """Test submit_task_to_dispatcher propagates exceptions to caller."""
        mock_ensure_config.side_effect = Exception("Connection error")

        with pytest.raises(Exception, match="Connection error"):
            submit_task_to_dispatcher(self.task)

        # Task status unchanged — caller decides error policy
        self.task.refresh_from_db()
        assert self.task.status == "pending"

    @patch("dispatcherd.publish.submit_task")
    @patch("apps.tasks.dispatcherd_config.ensure_dispatcherd_configured")
    @patch("apps.tasks.tasks.get_queue_for_function")
    def test_submit_skips_when_active_execution_exists(self, mock_get_queue, mock_ensure_config, mock_submit):
        """Test that submit_task_to_dispatcher is a no-op when a pending/running execution exists."""
        # Create an existing pending execution
        from apps.tasks.models import TaskExecution

        TaskExecution.objects.create(task=self.task, status="pending", worker_id="other-worker")

        submit_task_to_dispatcher(self.task)

        # Should not have submitted anything
        mock_submit.assert_not_called()

        # Should still have only the one execution
        assert TaskExecution.objects.filter(task=self.task).count() == 1

    def test_dispatcherd_decorator_functionality(self):
        """Test dispatcherd decorator is properly configured."""
        assert callable(tasks_system.task)

    def test_fallback_decorator(self):
        """Test fallback decorator when dispatcherd is not available."""

        @tasks_system.task()
        def test_function():
            return "test"

        # Decorator should not interfere with function
        result = test_function()
        assert result == "test"


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================


@pytest.mark.unit
class TestEdgeCasesAndErrorHandling:
    """Test edge cases, error conditions, and system resilience."""


# =============================================================================
# Task Retry and Error Handling Tests
# =============================================================================


@pytest.mark.unit
class TestTaskRetryBehavior(TestCase):
    """
    Test task retry behavior and error handling.

    This test class consolidates all retry-related tests, including:
    - Retry with existing TaskExecution records
    - max_attempts enforcement during retry
    - Error handling and attempts counter incrementation
    - Prevention of infinite retry loops
    """

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password=get_test_password()
        )

    def test_retry_with_existing_executions_sets_pending(self):
        """
        Test that retry() resets status even when TaskExecution records exist.

        IMPORTANT: The attempts counter should NOT be reset to 0 on retry.
        This ensures max_attempts is properly enforced across all retries.
        """
        task = Task(
            name="Failed Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=3,
            error_message="Something went wrong",
        )
        task.save()

        TaskExecution.objects.create(task=task, status="failed", error_message="Something went wrong")

        result = task.retry()

        assert result is True

        task.refresh_from_db()
        assert task.status == "pending"
        assert task.attempts == 1

    def test_retry_updates_error_message(self):
        """Test that retry() clears the error message."""
        task = Task(
            name="Failed Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=3,
            error_message="Something went wrong",
        )
        task.save()

        result = task.retry()
        assert result is True

        task.refresh_from_db()
        assert task.error_message == ""

    def test_retry_does_not_bypass_max_attempts(self):
        """
        Test that repeated retry() calls respect max_attempts limit.

        This verifies that the attempts counter is NOT reset to 0 on retry,
        preventing indefinite retries that would bypass max_attempts.
        """
        # Create a task with max_attempts=3
        task = Task(
            name="Test Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=3,
        )
        task.save()

        # First retry should succeed (attempts=1 < max_attempts=3)
        assert task.can_retry() is True
        result = task.retry()
        assert result is True

        task.refresh_from_db()
        assert task.status == "pending"
        # CRITICAL: attempts should NOT be reset to 0
        # It should remain at 1 to track total execution attempts
        assert task.attempts == 1

        # Simulate task running and failing again (attempts increments to 2)
        task.status = "failed"
        task.attempts = 2
        task.save()

        # Second retry should succeed (attempts=2 < max_attempts=3)
        assert task.can_retry() is True
        result = task.retry()
        assert result is True

        task.refresh_from_db()
        assert task.status == "pending"
        # attempts should still be 2, not reset to 0
        assert task.attempts == 2

        # Simulate task running and failing again (attempts increments to 3)
        task.status = "failed"
        task.attempts = 3
        task.save()

        # Third retry should FAIL (attempts=3 >= max_attempts=3)
        assert task.can_retry() is False
        result = task.retry()
        assert result is False

        task.refresh_from_db()
        # Task should remain failed
        assert task.status == "failed"
        assert task.attempts == 3

    def test_retry_tracks_total_attempts_not_per_retry_attempts(self):
        """
        Test that attempts counter tracks total execution attempts, not per-retry attempts.

        The attempts counter should increment with each execution, regardless of whether
        it was an automatic retry or a manual retry() call.
        """
        # Create a task that has already failed twice
        task = Task(
            name="Test Task",
            function_name="hello_world",
            status="failed",
            attempts=2,  # Already tried twice
            max_attempts=3,
        )
        task.save()

        # Should be able to retry one more time (2 < 3)
        assert task.can_retry() is True
        result = task.retry()
        assert result is True

        task.refresh_from_db()
        assert task.status == "pending"
        # attempts should stay at 2, tracking total attempts
        assert task.attempts == 2

        # After this next execution (which would make it 3), no more retries
        task.status = "failed"
        task.attempts = 3
        task.save()

        # Now can_retry should return False
        assert task.can_retry() is False
        result = task.retry()
        assert result is False

    # Tests from test_handle_task_error_attempts.py
    def test_handle_task_error_increments_attempts_for_pending_task(self):
        """
        Test that handle_task_error increments attempts for a task that fails
        before reaching "running" status (early failure scenario).
        """
        from apps.tasks.utils import handle_task_error

        # Create a task in pending status (never started running)
        task = Task(
            name="Test Task",
            function_name="test_func",
            status="pending",
            attempts=0,
            max_attempts=3,
            created_by=self.user,
        )
        task.save()

        # Create an execution record
        execution = TaskExecution.objects.create(task=task, status="pending", worker_id="test-worker")

        # Simulate an error that occurs before the task reaches "running" status
        # (e.g., function not found, invalid parameters, etc.)
        result = handle_task_error(
            task_instance=task,
            execution_instance=execution,
            error_message="Function not found",
        )

        # Refresh from database
        task.refresh_from_db()

        # Verify attempts was incremented
        assert task.attempts == 1, "Attempts should be incremented for early failure"
        assert task.status == "failed"
        assert task.error_message == "Function not found"
        assert task.completed_at is not None
        assert result["status"] == "error"

    def test_handle_task_error_does_not_double_increment_for_running_task(self):
        """
        Test that handle_task_error does NOT double-increment attempts for a task
        that already reached "running" status (late failure scenario).
        """
        from apps.tasks.tasks_system import _claim_task
        from apps.tasks.utils import handle_task_error

        # Create a task in pending status
        task = Task(
            name="Test Task",
            function_name="test_func",
            status="pending",
            attempts=0,
            max_attempts=3,
            created_by=self.user,
        )
        task.save()

        # Transition to running via _claim_task (the real path, increments attempts atomically)
        task, execution = _claim_task(task.id)
        assert task.attempts == 1, "Attempts should be 1 after _claim_task"
        assert task.status == "running"

        # Now simulate an error that occurs during task execution
        result = handle_task_error(
            task_instance=task,
            execution_instance=execution,
            error_message="Task execution failed",
        )

        # Refresh from database
        task.refresh_from_db()

        # Verify attempts was NOT incremented again (should still be 1)
        assert task.attempts == 1, "Attempts should not be double-incremented for late failure"
        assert task.status == "failed"
        assert task.error_message == "Task execution failed"
        assert task.completed_at is not None
        assert result["status"] == "error"

    def test_handle_task_error_allows_correct_retry_behavior(self):
        """
        Test that the fix prevents infinite retry loops by correctly
        incrementing attempts for early failures.
        """
        from apps.tasks.utils import handle_task_error

        # Create a task that will fail before reaching "running" status
        task = Task(
            name="Test Task",
            function_name="test_func",
            status="pending",
            attempts=0,
            max_attempts=3,
            created_by=self.user,
        )
        task.save()

        execution = TaskExecution.objects.create(task=task, status="pending", worker_id="test-worker")

        # Simulate multiple early failures (e.g., function not found each time)
        for expected_attempts in range(1, 4):
            # Simulate failure before reaching "running" status
            handle_task_error(
                task_instance=task,
                execution_instance=execution,
                error_message="Function not found",
            )

            task.refresh_from_db()
            assert task.attempts == expected_attempts, f"Attempts should be {expected_attempts}"
            assert task.status == "failed"

            # After each failure, check if task can be retried
            # Task should be retryable before max_attempts is reached
            if expected_attempts < 3:
                assert task.can_retry() is True, f"Task should be retryable at attempt {expected_attempts}"
                # Reset task to pending to simulate retry (this is what would happen in practice)
                task.status = "pending"
                task.error_message = ""
                task.save()
            else:
                assert task.can_retry() is False, "Task should not be retryable after max_attempts reached"

        # Final verification: After 3 attempts, task should NOT be retryable
        task.refresh_from_db()
        assert task.attempts == 3
        assert task.can_retry() is False, "Task should not be retryable after max_attempts reached"

    def test_handle_task_error_with_task_id_and_execution_id(self):
        """
        Test that handle_task_error works correctly when called with
        task_id and execution_id instead of instances.
        """
        from apps.tasks.utils import handle_task_error

        task = Task(
            name="Test Task",
            function_name="test_func",
            status="pending",
            attempts=0,
            max_attempts=3,
            created_by=self.user,
        )
        task.save()

        execution = TaskExecution.objects.create(task=task, status="pending", worker_id="test-worker")

        # Call with IDs instead of instances (as done in execute_db_task line 404)
        result = handle_task_error(
            task_id=task.id,
            execution_id=execution.id,
            error_message="Task failed with exception",
        )

        task.refresh_from_db()

        # Verify attempts was incremented
        assert task.attempts == 1, "Attempts should be incremented when called with task_id"
        assert task.status == "failed"
        assert task.error_message == "Task failed with exception"
        assert result["status"] == "error"


# =============================================================================
# Additional Coverage Tests - Merged from Enhanced Test File
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestImportErrorFallback(TestCase):
    """Test ImportError fallback for dispatcherd decorator."""

    def test_fallback_decorator_when_dispatcherd_not_available(self):
        """Test task decorator fallback when dispatcherd.publish is not available."""
        # The fallback decorator is defined in lines 28-34 of tasks_system.py
        # It should just return the function unchanged

        # Import with dispatcherd available would use the real decorator
        # But when ImportError occurs, it uses the fallback
        # The fallback is already imported in tasks_system module

        # Test that the fallback decorator works
        @tasks_system.task()
        def dummy_task():
            return "executed"

        result = dummy_task()
        assert result == "executed"


@pytest.mark.unit
@pytest.mark.django_db
class TestSubmitTaskToDispatcherSuccess(TestCase):
    """Test submit_task_to_dispatcher success path."""

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser_submit", email="test@example.com", password=get_test_password()
        )

    @patch("dispatcherd.publish.submit_task")
    @patch("apps.tasks.dispatcherd_config.ensure_dispatcherd_configured")
    @patch("apps.tasks.tasks.get_queue_for_function")
    def test_submit_task_success_path(self, mock_get_queue, mock_ensure_config, mock_submit):
        """Test successful task submission to dispatcher."""
        # Arrange
        task = Task.objects.create(
            name="Test Task",
            function_name="hello_world",
            task_data={},
            created_by=self.user,
        )
        mock_get_queue.return_value = "maintenance"

        # Act
        submit_task_to_dispatcher(task)

        # Assert
        # Verify dispatcherd was configured
        mock_ensure_config.assert_called_once()

        # Verify queue was determined
        mock_get_queue.assert_called_once_with("hello_world")

        # Verify task was submitted to dispatcherd
        mock_submit.assert_called_once()
        call_kwargs = mock_submit.call_args.kwargs
        assert call_kwargs["kwargs"]["task_id"] == task.id
        assert "execution_id" not in call_kwargs["kwargs"]
        assert call_kwargs["queue"] == "maintenance"

        # TaskExecution is no longer created here — _claim_task creates it
        assert TaskExecution.objects.filter(task=task).count() == 0

        # Verify task status updated
        task.refresh_from_db()
        assert task.status == "pending"

    @patch("dispatcherd.publish.submit_task")
    @patch("apps.tasks.dispatcherd_config.ensure_dispatcherd_configured")
    @patch("apps.tasks.tasks.get_queue_for_function")
    def test_submit_task_raises_on_submission_error(self, mock_get_queue, mock_ensure_config, mock_submit):
        """Test submit_task_to_dispatcher propagates submission errors."""
        task = Task.objects.create(
            name="Test Task",
            function_name="hello_world",
            created_by=self.user,
        )
        mock_get_queue.return_value = "maintenance"
        mock_submit.side_effect = Exception("Submission failed")

        with pytest.raises(Exception, match="Submission failed"):
            submit_task_to_dispatcher(task)

        # Task status unchanged
        task.refresh_from_db()
        assert task.status == "pending"


@pytest.mark.unit
@pytest.mark.django_db
class TestCreateSystemTasksExceptionHandling(TestCase):
    """Test create_system_tasks exception handling per task."""

    def setUp(self):
        """Set up test environment."""
        self.user = User.objects.create_user(
            username="testuser_exception", email="test@example.com", password=get_test_password()
        )

    @patch("apps.tasks.tasks_system._create_task_from_group")
    @patch("apps.tasks.task_groups.get_all_tasks_for_init")
    def test_handles_exception_creating_individual_task(self, mock_get_tasks, mock_create_task):
        """Test create_system_tasks handles exceptions per task."""
        # Arrange
        mock_get_tasks.return_value = {
            "task1": {"function": "hello_world", "cron": "0 * * * *"},
            "task2": {"function": "cleanup_old_tasks", "cron": "0 2 * * *"},
        }

        # First task fails, second succeeds
        mock_create_task.side_effect = [
            Exception("Creation failed for task1"),
            None,  # Second task succeeds
        ]

        # Act
        result = tasks_system.create_system_tasks()

        # Assert
        # Should have attempted both tasks
        assert mock_create_task.call_count == 2

        # Result should contain error for failed task
        assert any("Error with task1" in msg for msg in result["tasks"])
        assert any("Creation failed" in msg for msg in result["tasks"])


# =============================================================================
# Retry Backoff Tests
# =============================================================================


@pytest.mark.unit
class TestComputeRetryDelay(TestCase):
    """
    Unit tests for compute_retry_delay().

    Verifies that delays double with each attempt (exponential backoff) and are
    capped at RETRY_MAX_DELAY_SECONDS.

    Formula: min(base_delay * 2^(attempts - 1), RETRY_MAX_DELAY_SECONDS)
    """

    def setUp(self):
        """Import the helpers under test."""
        from apps.tasks.tasks_system import (
            RETRY_MAX_DELAY_SECONDS,
            compute_retry_delay,
        )

        self.compute_retry_delay = compute_retry_delay
        self.RETRY_MAX_DELAY_SECONDS = RETRY_MAX_DELAY_SECONDS

    def test_first_attempt_returns_base_delay(self):
        """attempts=1: 2^0 = 1, so delay equals the base unchanged."""
        assert self.compute_retry_delay(600, 1) == 600

    def test_second_attempt_doubles_delay(self):
        """attempts=2: 600 * 2^1 = 1200s (20 min)."""
        assert self.compute_retry_delay(600, 2) == 1200

    def test_third_attempt_quadruples_base(self):
        """attempts=3: 600 * 2^2 = 2400s (40 min)."""
        assert self.compute_retry_delay(600, 3) == 2400

    def test_fourth_attempt_scales_correctly(self):
        """attempts=4: 600 * 2^3 = 4800s (1h 20m)."""
        assert self.compute_retry_delay(600, 4) == 4800

    def test_fifth_attempt_scales_correctly(self):
        """attempts=5: 600 * 2^4 = 9600s (2h 40m)."""
        assert self.compute_retry_delay(600, 5) == 9600

    def test_delay_is_capped_at_max(self):
        """Delay must never exceed RETRY_MAX_DELAY_SECONDS regardless of attempt count."""
        # attempts=7: 600 * 2^6 = 38400 > 28800, so it is capped
        result = self.compute_retry_delay(600, 7)
        assert result == self.RETRY_MAX_DELAY_SECONDS

    def test_high_attempt_count_stays_at_cap(self):
        """Very high attempt counts should still be capped."""
        assert self.compute_retry_delay(600, 100) == self.RETRY_MAX_DELAY_SECONDS

    def test_zero_attempts_treated_as_first_attempt(self):
        """attempts=0 is out-of-range; exponent is floored to 0, so result equals base_delay."""
        assert self.compute_retry_delay(600, 0) == 600

    def test_custom_base_delay_doubles(self):
        """Custom base delay should double with each attempt just like the default."""
        assert self.compute_retry_delay(120, 1) == 120
        assert self.compute_retry_delay(120, 2) == 240
        assert self.compute_retry_delay(120, 3) == 480

    def test_delays_are_strictly_increasing_until_cap(self):
        """Each successive attempt should produce a delay >= the previous until the cap is hit."""
        base = 600
        delays = [self.compute_retry_delay(base, a) for a in range(1, 8)]
        for i in range(len(delays) - 1):
            assert delays[i] <= delays[i + 1], f"Delay at attempt {i + 1} was not <= attempt {i + 2}"


@pytest.mark.unit
class TestRetryBackoffProgression(TestCase):
    """
    Integration-style tests confirming that consecutive task failures schedule
    each retry further into the future than the previous one.
    """

    def setUp(self):
        """Set up a failing task."""
        self.user = User.objects.create_user(
            username="backoff_user", email="backoff@example.com", password=get_test_password()
        )

    @pytest.mark.django_db(transaction=True)
    def test_second_retry_scheduled_further_than_first(self):
        """Backoff increases with attempt count when the periodic scheduler retries."""
        from django.utils import timezone

        from apps.tasks.tasks_system import _schedule_retry

        task = Task(
            name="Backoff Test Task",
            function_name="hello_world",
            status="failed",
            attempts=1,
            max_attempts=7,
            task_data={},
        )
        task.save()

        before_first = timezone.now()
        _schedule_retry(task)
        task.refresh_from_db()
        assert task.status == "pending"
        first_scheduled_time = task.scheduled_time

        # Simulate second failure at higher attempt count
        task.status = "failed"
        task.attempts = 2
        task.scheduled_time = None
        task.save()

        before_second = timezone.now()
        _schedule_retry(task)
        task.refresh_from_db()
        assert task.status == "pending"

        first_delay = (first_scheduled_time - before_first).total_seconds()
        second_delay = (task.scheduled_time - before_second).total_seconds()

        assert second_delay > first_delay, (
            f"Second retry delay ({second_delay:.1f}s) should exceed first ({first_delay:.1f}s)"
        )
