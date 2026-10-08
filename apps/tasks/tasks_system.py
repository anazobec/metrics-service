"""
System and maintenance background tasks for metrics_service.

This module provides system-level tasks including cleanup, maintenance,
communication, and testing tasks with proper error handling and status tracking.
"""

import hashlib
import logging
from importlib import util as importlib_util
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from django.db import models, transaction

from .utils import (
    create_task_result,
    ensure_django_setup,
    handle_task_error,
    log_task_execution,
    run_with_lock,
    update_task_status,
)

logger = logging.getLogger(__name__)

RETRY_BASE_DELAY_SECONDS = 480  # 8 minutes - must not be a multiple of 5 (task cron spacing) to avoid retry collisions
RETRY_MAX_DELAY_SECONDS = 28800  # 8 hours - upper cap on any single retry delay

RESOURCE_SYNC_TASK_NAME = "initial_resource_sync"
RESOURCE_SYNC_VERSION_KEY = "_resource_sync_version"


def _get_resource_sync_version() -> str:
    """Return a stable fingerprint for the installed service and DAB build."""
    source_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()

    source_roots = [("apps", source_root / "apps"), ("metrics_service", source_root / "metrics_service")]
    dab_spec = importlib_util.find_spec("ansible_base")
    if dab_spec:
        dab_roots = dab_spec.submodule_search_locations or (
            [str(Path(dab_spec.origin).parent)] if dab_spec.origin else []
        )
        source_roots.extend(("ansible_base", Path(root)) for root in dab_roots)

    source_files = []
    for package_name, package_root in source_roots:
        if package_root.exists():
            source_files.extend(
                (f"{package_name}/{path.relative_to(package_root).as_posix()}", path)
                for path in package_root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
            )
    for relative_path, path in sorted(source_files):
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())

    versions = []
    for distribution in ("metrics-service", "django-ansible-base"):
        try:
            distribution_version = version(distribution)
        except PackageNotFoundError:
            distribution_version = "unknown"
        versions.append(f"{distribution}={distribution_version}")

    return f"{';'.join(versions)};source={digest.hexdigest()}"


def compute_retry_delay(base_delay: int, attempts: int) -> int:
    """Seconds before next retry: min(base_delay * 2**max(0, attempts - 1), RETRY_MAX_DELAY_SECONDS)."""
    exponent = max(0, attempts - 1)
    return min(base_delay * (2**exponent), RETRY_MAX_DELAY_SECONDS)


try:
    from dispatcherd.publish import task
except ImportError:

    def task():
        def decorator(func):
            return func

        return decorator


def _claim_task(task_id):
    """Atomically claim a task for execution, returning (task, execution) or None if already claimed."""
    import os

    from django.utils import timezone

    from .models import Task, TaskExecution

    with transaction.atomic():
        claimed = (
            Task.ready_to_run()
            .filter(id=task_id)
            .update(
                status="running",
                started_at=timezone.now(),
                attempts=models.F("attempts") + 1,
            )
        )
        if not claimed:
            return None, None

        task = Task.objects.get(id=task_id)

        execution = TaskExecution.objects.create(
            task=task,
            status="running",
            worker_id=f"dispatcher-{os.getpid()}",
        )

    return task, execution


def execute_function(task, execution, task_function, locked):
    """Call task_function with execution_id and task_data, under an advisory lock if required."""
    try:
        if locked:
            return run_with_lock(
                task.function_name,  # lock_key
                task.name,
                task_function,
                execution_id=execution.id,
                **task.task_data,
            )
        else:
            return task_function(execution_id=execution.id, **task.task_data)

    except Exception as e:
        logger.exception(f"Task {task.function_name} raised: {e}")
        return create_task_result("error", error=f"Task execution failed: {e}")


def _get_base_delay(task) -> int:
    """Return a validated base retry delay for task, falling back to RETRY_BASE_DELAY_SECONDS."""
    raw = task.task_data.get("retry_delay_seconds", RETRY_BASE_DELAY_SECONDS)
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError(f"retry_delay_seconds must be positive, got {value}")
        return value
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid retry_delay_seconds {raw!r} for task {task.name}, using default {RETRY_BASE_DELAY_SECONDS}s"
        )
        return RETRY_BASE_DELAY_SECONDS


def _schedule_retry(task) -> None:
    """Schedule a retry for a failed task if attempts remain."""
    if not task.can_retry():
        return
    base_delay = _get_base_delay(task)
    retry_delay = compute_retry_delay(base_delay, task.attempts)
    if task.retry(delay_seconds=retry_delay):
        logger.info(
            f"Auto-retrying task {task.name} (attempt {task.attempts}/{task.max_attempts}) (delay {retry_delay}s)"
        )


def execute_claimed(task, execution):
    """Execute a task that has already been atomically claimed, handling retries and status updates."""
    # Import TASK_FUNCTIONS here to avoid circular import
    # This import happens at runtime, not module load time
    from .tasks import TASK_FUNCTIONS, TASK_LOCKS

    # Validate task function exists
    if task.function_name not in TASK_FUNCTIONS:
        error_msg = f"Task function '{task.function_name}' not found in TASK_FUNCTIONS"
        return handle_task_error(task, execution, error_msg)

    log_task_execution(task.function_name, "start", f"Starting {task.function_name} task")
    log_task_execution(task.name, "running", f"Executing function: {task.function_name}")

    # Execute the actual task function, with advisory lock if required
    # Forwarding execution_id the task can link collections back to TaskExecution
    task_function = TASK_FUNCTIONS[task.function_name]
    locked = task.function_name in TASK_LOCKS
    result = execute_function(task, execution, task_function, locked)

    # Complete task execution
    status = "completed" if result.get("status") == "success" else "failed"
    error_message = result.get("error", "") if status == "failed" else ""

    update_task_status(task, execution, status=status, result_data=result, error_message=error_message)

    if status == "completed":
        log_task_execution(task.function_name, "complete", f"Task {task.function_name} completed successfully")
    else:
        error_msg = f"{task.function_name} task failed: {error_message}"
        log_task_execution(task.function_name, "error", error_msg, level="error")
    log_task_execution(task.name, "completed", f"Task execution finished with status: {status}")

    return result


# This is the sole dispatcherd entry point — all DB tasks are routed through it.
@task(decorate=False)
def execute_db_task(**kwargs) -> dict[str, Any]:
    """
    Execute a database-defined task with comprehensive error handling and tracking.

    This function is the main entry point for executing tasks that are defined
    in the database. It handles the complete lifecycle of task execution including
    validation, execution, status tracking, and post-execution processing.

    Args:
        **kwargs: Task data containing:
            - task_id (int): ID of the task to execute (required)

    Returns:
        dict: Task result dictionary with execution status and results
    """
    ensure_django_setup()

    task_id = kwargs.get("task_id")
    if not task_id:
        return create_task_result("error", error="task_id is required")

    task = None
    execution = None

    try:
        from .models import Task

        task, execution = _claim_task(task_id)
        if task is None:
            if not Task.objects.filter(id=task_id).exists():
                return create_task_result("error", error=f"Task matching query does not exist: {task_id}")

            logger.warning(f"Task {task_id} already claimed by another worker, skipping")
            return create_task_result("error", error="Task already claimed by another worker")

        return execute_claimed(task, execution)

    except Exception as e:
        return handle_task_error(
            task, execution, task_id=task_id, execution_id=(execution.id if execution else None), exception=e
        )


def submit_task_to_dispatcher(task: Any) -> None:
    """
    Submit a task to the dispatcher for execution.

    Raises on failure so callers can decide error policy.
    The task's status is never modified here.

    Args:
        task: The task to submit
    """
    from .models import TaskExecution

    # Guard against duplicate submissions
    if TaskExecution.objects.filter(task=task, status__in=["pending", "running"]).exists():
        logger.warning(f"Task {task.name} (ID: {task.id}) already has a pending or running execution, skipping")
        return

    from .dispatcherd_config import ensure_dispatcherd_configured

    ensure_dispatcherd_configured()

    from dispatcherd.publish import submit_task

    from .tasks import get_queue_for_function

    queue = get_queue_for_function(task.function_name)

    submit_task(execute_db_task, kwargs={"task_id": task.id}, queue=queue)

    logger.info(f"Submitted task {task.name} (ID: {task.id}) to dispatcher queue {queue}")


# runs during `manage.py metrics_service init-system-tasks`
def create_system_tasks() -> dict[str, Any]:
    """
    Create system-defined tasks from task groups in the database.

    This function is intended to be called from the init container
    (entrypoint-init.sh), before the application and scheduler start. At that
    point no tasks can be running, so unconditional deletion is safe. A completed
    Gateway resource sync is preserved across restarts while the installed build
    fingerprint is unchanged.

    Removes all existing system tasks and recreates them from task group
    definitions, ensuring the database always matches the code.

    Returns:
        dict: Summary of tasks created and removed.
    """
    try:
        from .models import Task
        from .task_groups import get_all_tasks_for_init, get_oneshot_task_ids
    except ImportError:
        # Handle case where Django isn't fully set up yet
        return {"error": "ERROR_DJANGO_NOT_READY", "created": 0, "removed": 0}

    results = {"created": 0, "removed": 0, "tasks": []}

    resource_sync_version = _get_resource_sync_version()

    # Snapshot completed one-shot tasks before deletion so restarts don't re-trigger
    # one-time work. Resource sync is preserved only for the same installed service/DAB
    # build; an upgrade or an earlier failed sync leaves it pending. Recurring tasks are
    # always recreated as pending so their schedules stay in sync with updated cron expressions.
    # completed_at is kept alongside the name: it is the only record of when the one-shot work
    # actually finished (e.g. the dashboard collection status reports it as last_sync). Only task-group
    # one-shots are snapshotted, not the per-run rows the scheduler creates for recurring system tasks.
    completed_oneshots = dict(
        Task.objects.filter(
            is_system_task=True,
            cron_expression__isnull=True,
            status="completed",
            name__in=get_oneshot_task_ids(),
        ).values_list("name", "completed_at")
    )
    previous_sync = Task.objects.filter(
        name=RESOURCE_SYNC_TASK_NAME,
        is_system_task=True,
        cron_expression__isnull=True,
        status="completed",
    ).first()
    if previous_sync is None or previous_sync.task_data.get(RESOURCE_SYNC_VERSION_KEY) != resource_sync_version:
        completed_oneshots.pop(RESOURCE_SYNC_TASK_NAME, None)

    # Remove all existing system tasks
    _, deletion_info = Task.objects.filter(is_system_task=True).delete()
    removed_count = deletion_info.get("tasks.Task", 0)
    results["removed"] = removed_count
    if removed_count > 0:
        results["tasks"].append(f"Removed {removed_count} existing system tasks")
        logger.info(f"Removed {removed_count} existing system tasks")

    # Get all task group definitions. Use get_all_tasks_for_init() (not get_all_enabled_tasks())
    # so that feature-flagged tasks (e.g. daily_anonymize, metrics collectors) are always
    # written to the DB with _feature_flag stored in task_data. The runtime check in
    # cron_scheduler._execute_database_task() then gates execution without requiring re-init
    # when the flag is toggled.
    task_groups = get_all_tasks_for_init()

    # Create fresh tasks from task groups
    for task_id, config in task_groups.items():
        try:
            if task_id == RESOURCE_SYNC_TASK_NAME:
                config = config.copy()
                config["args"] = config.get("args", {}).copy()
                config["args"][RESOURCE_SYNC_VERSION_KEY] = resource_sync_version
            _create_task_from_group(task_id, config, results, Task)
        except Exception as e:
            results["tasks"].append(f"Error with {task_id}: {str(e)}")
            logger.exception(f"Failed to create task {task_id}: {e}")

    # Restore completed status and completion time for one-shot tasks that already ran
    # successfully, preventing them from re-running on the next upgrade.
    if completed_oneshots:
        restored = 0
        for name, completed_at in completed_oneshots.items():
            restored += Task.objects.filter(
                is_system_task=True,
                cron_expression__isnull=True,
                name=name,
            ).update(status="completed", completed_at=completed_at)
        if restored:
            logger.info(f"Preserved completed status for {restored} one-shot task(s): {sorted(completed_oneshots)}")

    return results


def _create_task_from_group(task_id: str, config: dict[str, Any], results: dict[str, Any], task_model) -> None:
    """
    Create a system task from task group definition.

    Args:
        task_id: Unique identifier for the task
        config: Task configuration from task groups
        results: Results dict to update
        task_model: Task model class
    """
    task_data = config.get("args", {}).copy()
    if config.get("feature_flag"):
        task_data["_feature_flag"] = config["feature_flag"]

    kwargs = {
        "name": task_id,
        "description": config.get("description", ""),
        "function_name": config["function"],
        "task_data": task_data,
        "cron_expression": config.get("cron"),
        "is_system_task": True,
        "status": "pending",
    }
    if config.get("max_attempts") is not None:
        kwargs["max_attempts"] = config["max_attempts"]

    new_task = task_model.objects.create(**kwargs)
    results["created"] += 1
    results["tasks"].append(f"Created: {new_task.name}")
