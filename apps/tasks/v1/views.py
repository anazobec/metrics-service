"""
Task Management API ViewSets for background task operations.

This module provides comprehensive REST API endpoints for managing background tasks
and task executions. It converts functionality previously available only through
the manage_tasks.py management command into a full-featured REST API with proper
authentication, validation, and monitoring capabilities.

ViewSets:
    TaskViewSet: Complete task lifecycle management with CRUD operations
    TaskExecutionViewSet: Task execution monitoring and history tracking

Features:
    - Task creation with scheduling and recurring task support
    - Task status monitoring and real-time updates
    - Task retry and cancellation operations
    - Task cleanup and maintenance operations
    - Comprehensive filtering and search capabilities
    - Available task function discovery
    - Task execution history and metrics

Operations:
    Create Tasks: POST /api/v1/tasks/ with function_name and parameters
    List Tasks: GET /api/v1/tasks/ with filtering by status, function, etc.
    Retry Tasks: POST /api/v1/tasks/{id}/retry/ for failed tasks
    Cancel Tasks: POST /api/v1/tasks/{id}/cancel/ for pending/running tasks
    Cleanup Tasks: POST /api/v1/tasks/cleanup/ to remove old completed tasks
"""

from datetime import timedelta
from typing import Any

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from django.http import HttpRequest
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.tasks.api_utils import ErrorResponseSerializer, build_error_response
from apps.tasks.models import Task, TaskExecution
from apps.tasks.task_groups import get_oneshot_task_ids

from .base_views import BaseViewSet
from .serializers import (
    TaskCleanupSerializer,
    TaskCreateSerializer,
    TaskExecutionSerializer,
    TaskListSerializer,
    TaskSerializer,
)


@extend_schema_view(
    list=extend_schema(
        summary="List all tasks.",
        description="Returns a list of all task.",
    ),
    retrieve=extend_schema(
        summary="Get specific task by ID.",
        description="Returns a specific task by ID.",
    ),
    create=extend_schema(
        summary="Create a task.",
        description="Create a new task object.",
        request=TaskSerializer,
    ),
    update=extend_schema(
        summary="Update a specific task by ID.",
        description="Update a specific task by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific task by ID.",
        description="Partially update a specific task by ID.",
    ),
    destroy=extend_schema(
        summary="Delete a specific task by ID.",
        description="Delete a specific task record by ID.",
    ),
)
class TaskViewSet(BaseViewSet):
    """
    ViewSet for Task model with comprehensive task management functionality.

    This ViewSet provides all the functionality from the manage_tasks.py command
    converted to REST API endpoints including creation, listing, monitoring,
    and control.
    """

    queryset = Task.objects.select_related("created_by").all()
    serializer_class = TaskSerializer
    permission_classes = [IsSystemAdminOrAuditor]
    ordering_fields = ["id", "name", "status", "scheduled_time", "created", "started_at", "completed_at"]
    ordering = ["-id"]

    @property
    def search_fields(self) -> list[str]:
        return self.get_named_model_search_fields(["function_name", "status", "created_by__username"])

    @property
    def filterset_fields(self) -> dict[str, list[str]]:
        extra_fields = {
            "function_name": self.get_text_field_filters(),
            "status": ["exact", "in"],
            "created_by": self.get_foreign_key_filters(),
            "created_by__username": self.get_text_field_filters(),
            "scheduled_time": self.get_datetime_field_filters(),
            "started_at": self.get_datetime_field_filters(),
            "completed_at": self.get_datetime_field_filters(),
        }
        return self.build_filterset_fields(self.get_named_model_filterset_fields(extra_fields))

    def get_serializer_class(self):
        """Use appropriate serializer based on action."""
        if self.action == "create":
            return TaskCreateSerializer
        elif self.action == "list_filtered":
            return TaskListSerializer
        return self.serializer_class

    @extend_schema(
        summary="List tasks with filtering, similar to manage_tasks list command.",
        description="This endpoint replicates the functionality of: 'python manage.py manage-tasks list --status=pending --limit=20'",
        parameters=[
            OpenApiParameter(
                name="status",
                description="Filter by task status (e.g. pending, running, completed)",
                required=False,
                type=OpenApiTypes.STR,
            ),
            OpenApiParameter(
                name="limit",
                description="Limit the number of tasks returned (default 20)",
                required=False,
                type=OpenApiTypes.INT,
            ),
        ],
        responses={200: TaskListSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="list")
    def list_filtered(self, request: HttpRequest) -> Response:
        """
        List tasks with filtering, similar to manage_tasks list command.

        This endpoint replicates the functionality of:
        python manage.py manage_tasks list --status=pending --limit=20

        Returns:
            Response: Filtered and limited list of tasks
        """
        queryset = self.get_queryset()

        # Filter by status if provided
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        # Apply limit (default 20)
        limit = int(request.query_params.get("limit", 20))
        queryset = queryset.order_by("-id")[:limit]

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Get list of currently running tasks.",
        description="This endpoint returns a list of currently running tasks.",
        responses={200: TaskListSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def running(self, request: HttpRequest) -> Response:
        """
        Get list of currently running tasks.

        Returns:
            Response: List of tasks with status 'running'
        """
        running_tasks = self.get_queryset().filter(status="running")
        serializer = self.get_serializer(running_tasks, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Get list of pending tasks waiting to run.",
        description="This endpoint returns a list of pending tasks waiting to run.",
        responses={200: TaskListSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def pending(self, request: HttpRequest) -> Response:
        """
        Get list of pending tasks waiting to run.

        Returns:
            Response: List of tasks with status 'pending'
        """
        pending_tasks = self.get_queryset().filter(status="pending")
        serializer = self.get_serializer(pending_tasks, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Retry a failed task.",
        description=(
            "This endpoint replicates the functionality of: 'python manage.py manage_tasks retry <task_id>'. "
            'Pass {"force": true} to bypass the max_attempts check (task must still be failed).'
        ),
        request=inline_serializer(
            name="TaskRetryRequest",
            fields={"force": serializers.BooleanField(default=False, required=False)},
        ),
        responses={
            200: inline_serializer(name="TaskRetryResponse", fields={"message": serializers.CharField()}),
            400: inline_serializer(name="TaskRetryErrorResponse", fields={"error": serializers.CharField()}),
        },
    )
    @action(detail=True, methods=["post"])
    def retry(self, request: HttpRequest, pk: Any = None) -> Response:
        """
        Retry a failed task.

        This endpoint replicates the functionality of:
        python manage.py manage_tasks retry <task_id>

        Args:
            request: HTTP request
            pk: Primary key of the task

        Returns:
            Response: Success or error response
        """
        task = self.get_object()
        force = bool(request.data.get("force", False))

        if not task.retry(force=force):
            error_response = build_error_response(
                f"Cannot retry task: status={task.status}, attempts={task.attempts}/{task.max_attempts}",
                status_code=400,
            )
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": f"Task '{task.name}' queued for retry"})

    @extend_schema(
        summary="Cancel a pending or running task.",
        description="This endpoint replicates the functionality of: 'python manage.py manage_tasks cancel <task_id>'",
        responses={
            200: inline_serializer(name="TaskCancelResponse", fields={"message": serializers.CharField()}),
            400: inline_serializer(name="TaskCancelErrorResponse", fields={"error": serializers.CharField()}),
        },
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request: HttpRequest, pk: Any = None) -> Response:
        """
        Cancel a pending or running task.

        This endpoint replicates the functionality of:
        python manage.py manage_tasks cancel <task_id>

        Args:
            request: HTTP request
            pk: Primary key of the task

        Returns:
            Response: Success or error response
        """
        task = self.get_object()

        if task.status in ["completed", "cancelled", "failed"]:
            error_response = build_error_response(f"Cannot cancel task with status: {task.status}", status_code=400)
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        task.status = "cancelled"
        task.save(update_fields=["status", "modified"])

        return Response({"message": f"Task '{task.name}' cancelled"})

    @extend_schema(
        summary="Clean up old completed tasks",
        description="This endpoint replicates the functionality of: 'python manage.py manage_tasks cleanup --days=30 --dry-run'",
        request=TaskCleanupSerializer,
        responses={
            200: inline_serializer(
                name="TaskCleanupResponse",
                fields={
                    "message": serializers.CharField(),
                    "count": serializers.IntegerField(),
                    "preview": serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True),
                    "showing_preview_of": serializers.IntegerField(required=False, allow_null=True, default=None),
                },
            ),
        },
    )
    @action(detail=False, methods=["post"])
    def cleanup(self, request: HttpRequest) -> Response:
        """
        Clean up old completed tasks.

        This endpoint replicates the functionality of:
        python manage.py manage_tasks cleanup --days=30 --dry-run

        Args:
            request: HTTP request with cleanup parameters

        Returns:
            Response: Success message with count of tasks cleaned up
        """
        serializer = TaskCleanupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        days = serializer.validated_data["days"]
        dry_run = serializer.validated_data["dry_run"]

        cutoff_date = timezone.now() - timedelta(days=days)
        # Task-group one-shot system tasks are kept, as in cleanup_old_tasks: init-system-tasks relies on them
        # (and their completed_at) to keep completed one-shot work from running again.
        old_tasks = Task.objects.filter(
            status__in=["completed", "failed", "cancelled"], completed_at__lt=cutoff_date
        ).exclude(is_system_task=True, name__in=get_oneshot_task_ids())

        count = old_tasks.count()

        if dry_run:
            # Return preview of what would be deleted
            preview = []
            for task in old_tasks[:10]:
                preview.append(
                    {
                        "id": task.id,
                        "name": task.name,
                        "status": task.status,
                        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                    }
                )

            return Response(
                {
                    "message": f"Would delete {count} tasks older than {days} days",
                    "count": count,
                    "preview": preview,
                    "showing_preview_of": min(10, count),
                }
            )
        else:
            old_tasks.delete()
            return Response({"message": f"Deleted {count} old tasks", "count": count})

    @extend_schema(
        summary="Get list of available task functions with enhanced metadata for dashboard.",
        description="Get list of available task functions with enhanced metadata for dashboard.",
        responses={
            200: inline_serializer(
                name="TaskFunctionResponse",
                fields={
                    "functions": inline_serializer(
                        name="FunctionInfoSerializers",
                        many=True,
                        fields={
                            "name": serializers.CharField(),
                            "description": serializers.CharField(),
                            "category": serializers.CharField(),
                            "parameters": serializers.DictField(),
                            "examples": serializers.ListField(),
                        },
                    )
                },
            ),
        },
    )
    @action(detail=False, methods=["get"])
    def available_functions(self, request: HttpRequest) -> Response:
        """
        Get list of available task functions with enhanced metadata for the dashboard.

        Returns:
            Response: List of available task functions with descriptions, parameters, and examples
        """
        from apps.tasks.tasks import TASK_FUNCTIONS, TASK_METADATA

        functions = []
        for func_name, func in TASK_FUNCTIONS.items():
            # Get enhanced metadata if available, fallback to basic info
            metadata = TASK_METADATA.get(func_name, {})

            function_info = {
                "name": func_name,
                "description": metadata.get("description")
                or (func.__doc__.split("\n")[1].strip() if func.__doc__ else "No description available"),
                "category": metadata.get("category", "General"),
                "parameters": metadata.get("parameters", {}),
                "examples": metadata.get("examples", []),
            }

            functions.append(function_info)

        # Sort by category, then by name
        functions.sort(key=lambda x: (x["category"], x["name"]))

        return Response({"functions": functions})

    def perform_destroy(self, instance):
        """
        Override destroy to protect system tasks.

        System tasks cannot be easily deleted to maintain critical functionality.
        """
        if instance.is_system_task:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "System tasks cannot be deleted. These tasks are essential for proper system operation. "
                "To disable a system task, contact your system administrator."
            )
        super().perform_destroy(instance)

    def perform_update(self, serializer):
        """
        Override update to protect critical system task fields.

        System tasks have restricted modification to prevent breaking functionality.
        """
        instance = serializer.instance
        if instance and instance.is_system_task:
            # Fields that cannot be modified on system tasks
            protected_fields = {"function_name", "is_system_task", "cron_expression", "name", "status", "task_data"}

            # Check both validated_data (writable fields the serializer accepted) and
            # raw request data (catches read-only fields like status that the serializer
            # would silently ignore but the client should not be allowed to attempt)
            attempted_protected = protected_fields & (
                set(serializer.validated_data.keys()) | set(self.request.data.keys())
            )

            if attempted_protected:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied(
                    f"System tasks cannot modify protected fields: {', '.join(protected_fields)}. "
                    "These fields are managed by the system configuration."
                )

        super().perform_update(serializer)

    @extend_schema(
        summary="Force delete a system task (admin only).",
        description="This endpoint allows administrators to delete system tasks if absolutely necessary. Use with extreme caution as it may break system functionality.",
        request=inline_serializer(
            name="ForceDeleteRequest",
            fields={
                "force_confirm": serializers.BooleanField(required=False, default=False),
            },
        ),
        responses={
            200: inline_serializer(
                name="DeleteWarningResponse",
                fields={
                    "message": serializers.CharField(),
                    "warning": serializers.CharField(),
                },
            ),
            204: inline_serializer(
                name="DeleteResponse",
                fields={},
            ),
            400: inline_serializer(
                name="DeletErrorResponse",
                fields={
                    "error": serializers.CharField(),
                    "message": serializers.CharField(),
                    "warning": serializers.CharField(),
                    "task": inline_serializer(
                        name="TaskResponse",
                        fields={
                            "name": serializers.CharField(),
                            "function_name": serializers.CharField(),
                            "is_system_task": serializers.BooleanField(),
                        },
                    ),
                },
            ),
        },
    )
    @action(detail=True, methods=["delete"])
    def force_delete(self, request, pk=None):
        """
        Force delete a system task (admin only).

        This endpoint allows administrators to delete system tasks if absolutely necessary.
        Use with extreme caution as it may break system functionality.
        """
        task = self.get_object()

        # This would normally check for admin permissions
        # For now, we'll require explicit confirmation
        force_confirm = request.data.get("force_confirm", False)

        if not force_confirm:
            return Response(
                {
                    "error": "Force deletion requires explicit confirmation",
                    "message": "System tasks are protected. Set 'force_confirm': true to override.",
                    "warning": "Deleting system tasks may break critical functionality!",
                    "task": {
                        "name": task.name,
                        "function_name": task.function_name,
                        "is_system_task": task.is_system_task,
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if task.is_system_task:
            task_name = task.name
            task.delete()
            return Response(
                {
                    "message": f"System task '{task_name}' has been force deleted",
                    "warning": "System functionality may be affected. Consider recreating system tasks.",
                },
                status=status.HTTP_200_OK,
            )
        else:
            # Regular deletion for non-system tasks
            task.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Get the status of the simple scheduler and all scheduled tasks",
        description="Returns Scheduler's and scheduled tasks' status",
        responses={
            200: inline_serializer(
                name="SchedulerStatusResponse",
                fields={
                    "scheduler": inline_serializer(
                        name="Scheduler",
                        fields={
                            "running": serializers.BooleanField(),
                            "check_interval": serializers.IntegerField(),
                            "scheduled_tasks": serializers.IntegerField(),
                            "recurring_tasks": serializers.IntegerField(),
                        },
                    ),
                    "message": serializers.CharField(),
                },
            ),
            500: ErrorResponseSerializer,
        },
    )
    @action(detail=False, methods=["get"])
    def scheduler_status(self, request: HttpRequest) -> Response:
        """
        Get the status of the simple scheduler and all scheduled tasks.
        Returns:
            Response: Scheduler status and list of scheduled tasks
        """
        try:
            from apps.tasks.cron_scheduler import get_scheduler

            scheduler = get_scheduler()

            scheduled_tasks = Task.scheduled_tasks().count()
            recurring_tasks = Task.recurring_tasks().count()

            return Response(
                {
                    "scheduler": {
                        "running": scheduler.running if scheduler else False,
                        "check_interval": scheduler.check_interval if scheduler else 30,
                        "scheduled_tasks": scheduled_tasks,
                        "recurring_tasks": recurring_tasks,
                    },
                    "message": "Simple scheduler status retrieved successfully",
                }
            )
        except Exception as e:
            error_response = build_error_response(f"Failed to get scheduler status: {str(e)}", status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Schedule a task to run immediately using the simple scheduler.",
        description="Schedule a task to run immediately using the simple scheduler.",
        request=TaskCreateSerializer,
        responses={
            200: inline_serializer(
                name="ScheduleImmediateResponse",
                fields={
                    "message": serializers.CharField(),
                    "task_id": serializers.CharField(),
                },
            ),
            500: ErrorResponseSerializer,
        },
    )
    @action(detail=False, methods=["post"])
    def schedule_immediate(self, request: HttpRequest) -> Response:
        """
        Schedule a task to run immediately using the simple scheduler.
        Args:
            request: HTTP request with task parameters
        Returns:
            Response: Success message with task ID
        """
        serializer = TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # Create task in database — scheduler periodic sync picks up immediate tasks
            task = Task.objects.create(
                name=serializer.validated_data.get("name", "Immediate Task"),
                function_name=serializer.validated_data["function_name"],
                task_data=serializer.validated_data.get("task_data", {}),
                created_by=request.user if request.user.is_authenticated else None,
                # No scheduled_time means immediate execution
            )

            return Response({"message": "Task scheduled for immediate execution", "task_id": str(task.id)})
        except Exception as e:
            error_response = build_error_response(f"Failed to schedule task: {str(e)}", status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Schedule a recurring task using cron expression.",
        description="Schedule a recurring task using cron expression.",
        request=inline_serializer(
            name="ScheduleRecurringRequest",
            fields={
                "name": serializers.CharField(required=False),
                "function_name": serializers.CharField(),
                "task_data": serializers.DictField(required=False),
                "cron_expression": serializers.CharField(),
                "max_attempts": serializers.IntegerField(required=False),
                "timeout_seconds": serializers.IntegerField(required=False),
                "user": serializers.CharField(required=False),
            },
        ),
        responses={
            200: inline_serializer(
                name="ScheduleRecurringResponse",
                fields={
                    "message": serializers.CharField(),
                    "task_id": serializers.CharField(),
                },
            ),
            400: ErrorResponseSerializer,
            500: ErrorResponseSerializer,
        },
    )
    @action(detail=False, methods=["post"])
    def schedule_recurring(self, request: HttpRequest) -> Response:
        """
        Schedule a recurring task using cron expression.
        Args:
            request: HTTP request with task parameters including cron_expression
        Returns:
            Response: Success message with task ID
        """
        serializer = TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        cron_expression = serializer.validated_data.get("cron_expression")
        if not cron_expression:
            error_response = build_error_response("cron_expression is required for recurring tasks", status_code=400)
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Create recurring task directly in database - simple scheduler will handle it
            task = Task.objects.create(
                name=serializer.validated_data.get("name", "Recurring Task"),
                function_name=serializer.validated_data["function_name"],
                task_data=serializer.validated_data.get("task_data", {}),
                cron_expression=cron_expression,
                created_by=request.user if request.user.is_authenticated else None,
            )

            return Response({"message": "Recurring task scheduled successfully", "task_id": str(task.id)})
        except Exception as e:
            error_response = build_error_response(f"Failed to schedule recurring task: {str(e)}", status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema_view(
    list=extend_schema(
        summary="List task executions.",
        description="Returns a list of task executions.",
    ),
    retrieve=extend_schema(
        summary="Get specific task execution by ID.",
        description="Returns a specific task execution by ID.",
    ),
    create=extend_schema(
        summary="Create a task execution.",
        description="Create a new task execution object.",
        request=TaskExecutionSerializer,
    ),
    update=extend_schema(
        summary="Update a specific task execution by ID.",
        description="Update a specific task execution by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific task execution by ID.",
        description="Partially update a specific task execution by ID.",
    ),
    destroy=extend_schema(
        summary="Delete a specific task execution by ID.",
        description="Delete a specific task execution record by ID.",
    ),
)
class TaskExecutionViewSet(BaseViewSet):
    """
    ViewSet for TaskExecution model to monitor task execution history.
    """

    queryset = TaskExecution.objects.select_related("task").all()
    serializer_class = TaskExecutionSerializer
    permission_classes = [IsSystemAdminOrAuditor]
    ordering_fields = ["started_at", "completed_at", "execution_time_seconds", "status"]
    ordering = ["-started_at"]

    @property
    def search_fields(self) -> list[str]:
        return ["task__name", "task__function_name", "worker_id"]

    @property
    def filterset_fields(self) -> dict[str, list[str]]:
        return {
            "task": ["exact"],
            "status": ["exact"],
            "worker_id": ["exact", "icontains"],
            "started_at": ["gte", "lte"],
            "completed_at": ["gte", "lte"],
        }
