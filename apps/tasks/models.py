"""
Task models for metrics_service.

This module contains task-related models: task definitions and executions,
and models for collects, rollups, and anonymized metrics.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# Import base classes and mixins from core
from apps.tasks.mixins import StatusTrackingMixin

# Import base classes, handling both DAB and simple fallbacks
try:
    from ansible_base.activitystream.models import AuditableModel
    from ansible_base.lib.abstract_models import CommonModel, NamedCommonModel

    DAB_AVAILABLE = True
except ImportError:
    # Provide simple alternatives for immediate setup
    DAB_AVAILABLE = False

    # Simple base classes when DAB is not available
    class CommonModel(models.Model):
        created = models.DateTimeField(auto_now_add=True)
        modified = models.DateTimeField(auto_now=True)

        class Meta:
            abstract = True

    class NamedCommonModel(CommonModel):
        name = models.CharField(max_length=512)
        description = models.TextField(blank=True, default="")

        class Meta:
            abstract = True

    class AuditableModel(models.Model):
        class Meta:
            abstract = True


logger = logging.getLogger(__name__)


class Task(NamedCommonModel, AuditableModel, StatusTrackingMixin):
    """
    Database model for scheduled tasks with enhanced tracking capabilities.

    This model provides comprehensive task scheduling and execution tracking
    with support for detailed status monitoring.
    """

    class Meta:
        app_label = "tasks"
        ordering = ["id"]
        permissions = [("can_execute_task", "Can execute task")]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    # Task identification and metadata
    description = models.TextField(blank=True, default="", help_text="Task description")

    function_name = models.CharField(
        max_length=255, help_text="Name of the function to execute (must match TASK_FUNCTIONS)"
    )

    task_data = models.JSONField(default=dict, help_text="JSON data to pass to the task function")

    # Scheduling information
    scheduled_time = models.DateTimeField(
        null=True, blank=True, help_text="When the task should be executed (null for immediate execution)"
    )

    cron_expression = models.CharField(
        max_length=100, null=True, blank=True, help_text="Cron expression for recurring tasks (e.g., '0 2 * * *')"
    )

    is_system_task = models.BooleanField(
        default=False, help_text="Whether this is a system-defined task that cannot be easily deleted"
    )

    # Execution tracking
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending")

    attempts = models.PositiveIntegerField(default=0, help_text="Number of execution attempts")

    max_attempts = models.PositiveIntegerField(default=3, help_text="Maximum number of retry attempts")

    # Execution results (inherited from StatusTrackingMixin)
    result_data = models.JSONField(default=dict, blank=True, help_text="JSON result data from task execution")

    # Ownership and organization
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tasks",
        help_text="User who created this task",
    )

    def __str__(self):
        """
        Return string representation of the task.

        Returns:
            str: Task name, function name, and status
        """
        return f"{self.name} ({self.function_name}) - {self.get_status_display()}"

    def is_ready_to_run(self) -> bool:
        """
        Check if task is ready to be executed.

        Returns:
            bool: True if task is ready to run, False otherwise
        """
        if self.status != "pending":
            return False

        # Check if immediate or scheduled time has passed
        return not (self.scheduled_time and self.scheduled_time > timezone.now())

    def can_retry(self) -> bool:
        """
        Check if task can be retried.

        Returns:
            bool: True if task can be retried, False otherwise
        """
        return self.attempts < self.max_attempts and self.status == "failed"

    def retry(self, delay_seconds: int = 0, force: bool = False) -> bool:
        """
        Retry a failed task by resetting its status to pending.

        The attempts counter is NOT reset to properly enforce max_attempts limit.
        This ensures that the total number of execution attempts (automatic + manual retries)
        respects the max_attempts setting and prevents indefinite retries.

        Args:
            delay_seconds: Optional delay before the task becomes eligible for execution.
                When set, scheduled_time is set to now + delay so the periodic sync
                won't pick it up until the delay has elapsed.
            force: Bypass the can_retry() check (still requires status == "failed").

        Returns:
            bool: True if task was successfully reset for retry, False otherwise
        """
        scheduled_time = timezone.now() + timedelta(seconds=delay_seconds) if delay_seconds > 0 else None

        qs = Task.objects.filter(pk=self.pk, status="failed")
        if not force:
            qs = qs.filter(attempts__lt=models.F("max_attempts"))

        # Do NOT reset attempts — the counter must persist to enforce max_attempts.
        updated = qs.update(
            status="pending",
            error_message="",
            started_at=None,
            completed_at=None,
            scheduled_time=scheduled_time,
            modified=timezone.now(),
        )
        if not updated:
            return False

        self.status = "pending"
        self.error_message = ""
        self.started_at = None
        self.completed_at = None
        self.scheduled_time = scheduled_time
        return True

    def can_delete(self) -> bool:
        """
        Check if task can be deleted.

        System tasks cannot be easily deleted to protect critical functionality.

        Returns:
            bool: True if task can be deleted, False if it's protected
        """
        return not self.is_system_task

    def can_modify(self) -> bool:
        """
        Check if task can be modified.

        System tasks have limited modification capabilities to prevent
        breaking critical system functionality.

        Returns:
            bool: True if task can be fully modified, False if restricted
        """
        return not self.is_system_task

    def get_next_run_time(self) -> str | None:
        """
        Calculate next run time for recurring tasks.

        Returns:
            datetime or None: Next run time for recurring tasks, None if not recurring
        """
        if not self.cron_expression:
            return None

        try:
            from croniter import croniter

            cron = croniter(self.cron_expression, timezone.now())
            return cron.get_next(datetime).isoformat()
        except ImportError:
            return "croniter not available"
        except Exception:
            return "Invalid cron_expression"

    @staticmethod
    def non_recurring_filter() -> models.Q:
        """Match both database representations of an absent cron expression."""
        return models.Q(cron_expression__isnull=True) | models.Q(cron_expression="")

    @classmethod
    def ready_to_run(cls):
        """Queryset equivalent of is_ready_to_run() — pending non-recurring tasks whose scheduled_time has passed or is null."""
        return cls.objects.filter(
            cls.non_recurring_filter(),
            status="pending",
        ).filter(models.Q(scheduled_time__isnull=True) | models.Q(scheduled_time__lte=timezone.now()))

    @classmethod
    def immediate_tasks(cls):
        """Pending tasks with no scheduled time and no cron expression (run ASAP)."""
        return cls.objects.filter(cls.non_recurring_filter(), status="pending", scheduled_time__isnull=True)

    @classmethod
    def scheduled_tasks(cls):
        """Pending tasks that have a specific scheduled_time set."""
        return cls.objects.filter(status="pending", scheduled_time__isnull=False)

    @classmethod
    def recurring_tasks(cls):
        """Pending tasks driven by a cron expression."""
        return cls.objects.filter(status="pending").exclude(cls.non_recurring_filter())


class TaskExecution(CommonModel, AuditableModel):
    """
    Model to track individual task executions for history and debugging.

    This model provides detailed tracking of task execution attempts,
    including timing, results, and error information.
    """

    class Meta:
        app_label = "tasks"
        ordering = ["-started_at"]

    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="executions", help_text="The task that was executed"
    )

    status = models.CharField(max_length=30, choices=Task.STATUS_CHOICES)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    worker_id = models.CharField(
        max_length=100, null=True, blank=True, help_text="ID of the worker that executed the task"
    )

    result_data = models.JSONField(default=dict, blank=True, help_text="JSON result data from this execution")

    error_message = models.TextField(blank=True, help_text="Error message if execution failed")

    execution_time_seconds = models.FloatField(
        null=True, blank=True, help_text="Time taken to execute the task in seconds"
    )

    def __str__(self):
        """
        Return string representation of the task execution.

        Returns:
            str: Task name and execution timestamp
        """
        return f"{self.task.name} execution at {self.started_at}"

    def save(self, *args, **kwargs):
        """
        Override save to calculate execution time automatically.

        Args:
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            None
        """
        # Calculate execution time if completed
        if self.completed_at and self.started_at:
            self.execution_time_seconds = (self.completed_at - self.started_at).total_seconds()
        super().save(*args, **kwargs)


class HourlyMetricsCollection(CommonModel, AuditableModel):
    """
    Store raw hourly metrics collection data.

    This model persists raw metrics data collected every hour for later
    daily aggregation and anonymization. Provides hourly granularity
    for trend analysis while maintaining data retention policies.
    """

    class Meta:
        app_label = "tasks"
        ordering = ["-collection_timestamp"]
        indexes = [
            models.Index(fields=["collector_type", "collection_timestamp"]),
            models.Index(fields=["collection_timestamp"]),
            models.Index(fields=["status"]),
        ]
        unique_together = ["collector_type", "collection_timestamp"]
        verbose_name = "Hourly Metrics Collection"
        verbose_name_plural = "Hourly Metrics Collections"

    COLLECTOR_TYPE_CHOICES = [
        ("job_host_summary_service", "Job Host Summary Service"),
        ("unified_jobs", "Unified Jobs"),
        ("credentials_service", "Credentials Service"),
        ("main_jobevent_service", "Job Events (Event Modules)"),
        ("execution_environments", "Execution Environments"),
        ("controller_version_service", "Controller Version Service"),
        ("table_metadata", "Table Metadata"),
        ("config", "Configuration"),  # Daily only, included for completeness
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("collected", "Collected"),
        ("failed", "Failed"),
        ("processed", "Processed"),  # Included in daily rollup
    ]

    # Identification
    collector_type = models.CharField(
        max_length=50, choices=COLLECTOR_TYPE_CHOICES, help_text="Type of metrics collector"
    )

    collection_timestamp = models.DateTimeField(
        db_index=True, help_text="When this collection occurred (rounded to hour)"
    )

    # Data
    raw_data = models.JSONField(default=dict, help_text="Raw metrics data from collector")

    # Metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="collected")

    collection_parameters = models.JSONField(
        default=dict, help_text="Parameters used for collection (database, since, until, etc.)"
    )

    data_size_bytes = models.BigIntegerField(default=0, help_text="Size of raw_data in bytes")

    error_message = models.TextField(blank=True, help_text="Error message if collection failed")

    # Relationships
    task_execution = models.ForeignKey(
        "TaskExecution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hourly_collections",
        help_text="TaskExecution that created this collection",
    )

    def __str__(self):
        """
        Return string representation of the hourly collection.

        Returns:
            str: Collector type and collection timestamp
        """
        return f"{self.get_collector_type_display()} - {self.collection_timestamp}"

    def save(self, *args, **kwargs):
        """
        Auto-calculate data size on save.

        Args:
            *args: Positional arguments
            *kwargs: Keyword arguments

        Returns:
            None
        """
        import json

        self.data_size_bytes = len(json.dumps(self.raw_data).encode("utf-8"))
        super().save(*args, **kwargs)


class DailyMetricsSummary(CommonModel, AuditableModel):
    """
    Store aggregated daily metrics with references to hourly snapshots.

    This model aggregates hourly collections into daily summaries while
    preserving references to individual hourly snapshots for trend analysis.
    """

    class Meta:
        app_label = "tasks"
        ordering = ["-summary_date"]
        indexes = [
            models.Index(fields=["summary_date"]),
            models.Index(fields=["status"]),
        ]
        unique_together = ["summary_date"]
        verbose_name = "Daily Metrics Summary"
        verbose_name_plural = "Daily Metrics Summaries"

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("aggregated", "Aggregated"),
        ("anonymized", "Anonymized"),
        ("sent", "Sent to Segment"),
        ("failed", "Failed"),
    ]

    # Identification
    summary_date = models.DateField(unique=True, db_index=True, help_text="Date this summary covers (YYYY-MM-DD)")

    # Aggregated Data
    aggregated_metrics = models.JSONField(
        default=dict, help_text="Aggregated metrics for the day (sums, averages, counts)"
    )

    # References to hourly snapshots (stored as list of IDs for efficient lookup)
    hourly_collection_ids = models.JSONField(
        default=dict, help_text="Map of collector_type -> list of HourlyMetricsCollection IDs"
    )

    # Config data (collected once daily, not hourly)
    config_data = models.JSONField(default=dict, help_text="Configuration data collected once per day")

    # Metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    hourly_collections_count = models.IntegerField(default=0, help_text="Total number of hourly collections included")

    missing_hours = models.JSONField(default=list, help_text="List of hours that were missing collections")

    aggregation_completed_at = models.DateTimeField(null=True, blank=True, help_text="When aggregation was completed")

    error_message = models.TextField(blank=True, help_text="Error message if aggregation failed")

    # Relationships
    rollup_task_execution = models.ForeignKey(
        "TaskExecution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_summaries",
        help_text="TaskExecution that created this summary",
    )

    def __str__(self):
        """
        Return string representation of the daily summary.

        Returns:
            str: Summary date and status
        """
        return f"Daily Summary - {self.summary_date} ({self.get_status_display()})"

    def get_hourly_collections(self):
        """
        Retrieve all hourly collections for this summary.

        Returns:
            QuerySet: HourlyMetricsCollection objects for this summary
        """
        from django.db.models import Q

        # Return empty queryset if no hourly collections are associated
        if not self.hourly_collection_ids:
            return HourlyMetricsCollection.objects.none()

        query = Q()
        for collector_type, ids in self.hourly_collection_ids.items():
            query |= Q(id__in=ids, collector_type=collector_type)

        return HourlyMetricsCollection.objects.filter(query)


class AnonymizedMetricsPayload(CommonModel, AuditableModel):
    """
    Store anonymized metrics payloads before sending to Segment.

    This model stores anonymized metrics ready for transmission to Segment.com,
    including retry logic and transmission status tracking.
    """

    class Meta:
        app_label = "tasks"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["status", "created"]),
            models.Index(fields=["summary_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["daily_summary"],
                condition=models.Q(status__in=["pending", "sending", "sent", "unavailable"]),
                name="unique_active_payload_per_summary",
            )
        ]
        verbose_name = "Anonymized Metrics Payload"
        verbose_name_plural = "Anonymized Metrics Payloads"

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("unavailable", "Unavailable"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("retry", "Retry"),
    ]

    # Identification
    summary_date = models.DateField(db_index=True, help_text="Date this payload covers")

    # Anonymized Data
    anonymized_data = models.JSONField(help_text="Anonymized metrics payload ready for Segment")

    # Metadata
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    retry_count = models.PositiveIntegerField(default=0, help_text="Number of send attempts")

    max_retries = models.PositiveIntegerField(default=3, help_text="Maximum number of retry attempts")

    # Segment metadata
    segment_event_name = models.CharField(
        max_length=255, default="daily_metrics_rollup", help_text="Segment event name"
    )

    segment_user_id = models.CharField(max_length=255, blank=True, help_text="Segment user ID")

    segment_message_id = models.CharField(max_length=255, blank=True, help_text="Segment message ID for tracking")

    sent_at = models.DateTimeField(null=True, blank=True, help_text="When payload was successfully sent")

    error_message = models.TextField(blank=True, help_text="Error message if send failed")

    payload_size_bytes = models.BigIntegerField(default=0, help_text="Size of anonymized_data in bytes")

    # Relationships
    daily_summary = models.ForeignKey(
        "DailyMetricsSummary",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anonymized_payloads",
        help_text="DailyMetricsSummary used to create this payload",
    )

    anonymization_task_execution = models.ForeignKey(
        "TaskExecution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anonymized_payloads",
        help_text="TaskExecution that created this payload",
    )

    def __str__(self):
        """
        Return string representation of the anonymized payload.

        Returns:
            str: Summary date and status
        """
        return f"Anonymized Payload - {self.summary_date} ({self.get_status_display()})"

    def can_retry(self):
        """
        Check if payload can be retried.

        Returns:
            bool: True if retry is allowed, False otherwise
        """
        return self.retry_count < self.max_retries and self.status in ["failed", "retry"]

    def save(self, *args, **kwargs):
        """
        Auto-calculate payload size on save.

        Args:
            *args: Positional arguments
            *kwargs: Keyword arguments

        Returns:
            None
        """
        import json

        self.payload_size_bytes = len(json.dumps(self.anonymized_data).encode("utf-8"))
        super().save(*args, **kwargs)
