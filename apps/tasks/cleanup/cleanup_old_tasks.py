"""
Clean up old completed and failed tasks from the database.

This task removes tasks that have been completed or failed for more than
the specified number of days. This helps maintain database performance
and prevents unlimited growth of task history.
"""

import logging
from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from ..utils import create_task_result, log_task_execution

logger = logging.getLogger(__name__)


def cleanup_old_tasks(**kwargs) -> dict[str, Any]:
    """
    Clean up old completed and failed tasks from the database.

    This task removes tasks that have been completed or failed for more than
    the specified number of days. This helps maintain database performance
    and prevents unlimited growth of task history.

    IMPORTANT: Recurring tasks are automatically preserved and will NOT be deleted,
    regardless of their age, to ensure scheduled tasks continue to function.

    Args:
        **kwargs: Task data containing cleanup parameters:
            - days_old (int): Number of days old tasks should be to qualify for cleanup (default: 5)
            - dry_run (bool): If True, only count tasks that would be deleted (default: False)
            - include_executions (bool): Also cleanup related TaskExecution records (default: True)
            - preserve_recurring (bool): If True, exclude recurring tasks from cleanup (default: True)

    Returns:
        dict: Task result dictionary with cleanup statistics
    """
    from ..models import Task, TaskExecution

    days_old = kwargs.get("days_old", 5)
    dry_run = kwargs.get("dry_run", False)
    include_executions = kwargs.get("include_executions", True)
    preserve_recurring = kwargs.get("preserve_recurring", True)

    log_task_execution("cleanup_old_tasks", "processing", f"Cleaning up tasks older than {days_old} days")

    # Calculate cutoff date
    cutoff_date = timezone.now() - timedelta(days=days_old)

    # Find tasks that are completed or failed and older than cutoff date
    # Use completed_at if available, otherwise fall back to modified date
    old_tasks_filter = Q(
        status__in=["completed", "failed"],
        completed_at__lt=cutoff_date,
        completed_at__isnull=False,
    )

    # Exclude recurring tasks if preserve_recurring is True (default)
    if preserve_recurring:
        old_tasks_filter &= Task.non_recurring_filter()

    old_tasks = Task.objects.filter(old_tasks_filter)

    # Also include tasks that don't have completed_at but are old based on modified date
    old_tasks_fallback_filter = Q(
        status__in=["completed", "failed"],
        completed_at__isnull=True,
        modified__lt=cutoff_date,
    )

    # Exclude recurring tasks if preserve_recurring is True (default)
    if preserve_recurring:
        old_tasks_fallback_filter &= Task.non_recurring_filter()

    old_tasks_fallback = Task.objects.filter(old_tasks_fallback_filter)

    # Combine querysets
    old_tasks = old_tasks | old_tasks_fallback

    task_count = old_tasks.count()
    execution_count = 0

    if include_executions:
        # Count related executions
        execution_count = TaskExecution.objects.filter(task__in=old_tasks).count()

    deleted_tasks = 0
    deleted_executions = 0

    if not dry_run and task_count > 0:
        log_task_execution("cleanup_old_tasks", "processing", f"Deleting {task_count} old tasks")

        if include_executions:
            # Delete executions first (foreign key constraint)
            _, deletion_info = TaskExecution.objects.filter(task__in=old_tasks).delete()
            deleted_executions = deletion_info.get("tasks.TaskExecution", 0)

        # Delete the tasks
        _, deletion_info = old_tasks.delete()
        deleted_tasks = deletion_info.get("tasks.Task", 0)

        # If we didn't manually delete executions, they were cascade deleted
        if not include_executions:
            deleted_executions = deletion_info.get("tasks.TaskExecution", 0)

        message = f"Deleted {deleted_tasks} tasks and {deleted_executions} executions"
        if preserve_recurring:
            message += " (recurring tasks preserved)"
        log_task_execution("cleanup_old_tasks", "completed", message)
    else:
        message = f"Found {task_count} tasks and {execution_count} executions that would be deleted"
        if preserve_recurring:
            message += " (recurring tasks preserved)"
        log_task_execution("cleanup_old_tasks", "completed", message)

    return create_task_result(
        "success",
        {
            "days_old": days_old,
            "cutoff_date": cutoff_date.isoformat(),
            "dry_run": dry_run,
            "include_executions": include_executions,
            "preserve_recurring": preserve_recurring,
            "tasks_found": task_count,
            "executions_found": execution_count,
            "tasks_deleted": deleted_tasks,
            "executions_deleted": deleted_executions,
        },
    )
