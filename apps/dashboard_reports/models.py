"""
Models for the dashboard reports module.

Includes SubscriptionCost for cost calculations, FilterSet for saved user filter
configurations, TemplateMetadata for AWX job template time estimates, and JobData
with related JobLabel and JobHostSummary for storing AWX job execution records.
"""

import calendar
import decimal
import logging
from datetime import UTC, datetime
from typing import Any, Self

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Case, F, Sum, When
from django.db.models.functions import Cast
from metrics_utility.library.collectors.dashboard import AWXJobType

# Import base classes, handling both DAB and simple fallbacks
try:
    from ansible_base.lib.abstract_models import CommonModel

    DAB_AVAILABLE = True
except ImportError:
    # Provide simple alternative when DAB is not available
    DAB_AVAILABLE = False

    class CommonModel(models.Model):
        """Minimal fallback base model providing created/modified timestamps when DAB is unavailable."""

        created = models.DateTimeField(auto_now_add=True)
        modified = models.DateTimeField(auto_now=True)

        class Meta:
            """Mark CommonModel as abstract so it is not created as a DB table."""

            abstract = True


logger = logging.getLogger(__name__)
DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES = 60


def _month_range_iter(start_date, end_date):
    """
    Helper generator to iterate over each (year, month) tuple between start_date and end_date (inclusive).
    Advances month and year correctly, handling year rollover.
    """
    year, month = start_date.year, start_date.month
    while (year < end_date.year) or (year == end_date.year and month <= end_date.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def _get_month_overlap_days(current_year, current_month, start_date, end_date):
    """
    Helper function to calculate the number of days in a month and the actual overlap range
    for a given start_date and end_date. Returns (month_days, month_start_day, month_end_day).
    """
    month_days = calendar.monthrange(current_year, current_month)[1]
    month_start_day = start_date.day if (current_year == start_date.year and current_month == start_date.month) else 1
    month_end_day = end_date.day if (current_year == end_date.year and current_month == end_date.month) else month_days
    return month_days, month_start_day, month_end_day


class SubscriptionCostObjectManager(models.Manager):
    """Custom manager that enforces the singleton constraint on SubscriptionCost."""

    @transaction.atomic
    def create(self, **kwargs):
        """
        Override create to ensure only one SubscriptionCost instance exists.
        If an instance already exists, update it instead of creating a new one.
        """
        instance, _ = self.update_or_create(pk=1, defaults=kwargs)
        return instance


class SubscriptionCost(CommonModel):
    """
    Stores subscription cost information for the AAP subscription, including monthly cost and average engineer hourly rate.
    This is used for cost calculations in the dashboard reports.

    There should typically only be one record in this table, which can be updated as needed when subscription costs change.
    """

    monthly_subscription_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(decimal.Decimal("0.00"))],
        help_text="Monthly subscription cost for AAP subscription",
    )

    engineer_avg_hourly_rate = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(decimal.Decimal("0.00"))],
        help_text="Average hourly rate for engineers performing manual tasks (used for cost calculations in reports)",
    )

    include_template_creation_time_in_costs = models.BooleanField(
        default=True,
        help_text="Include template creation time in cost calculations. If false, costs related to template creation time will be excluded.",
    )

    class Meta:
        """Database and display configuration for SubscriptionCost."""

        db_table = "dashboard_subscription_cost"
        verbose_name = "Subscription Cost"
        verbose_name_plural = "Subscription Costs"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(pk=1),
                name="dashboard_subscription_cost_singleton_pk",
            ),
        ]

    objects = SubscriptionCostObjectManager()

    def __str__(self) -> str:
        """Return a string representation showing monthly cost and engineer hourly rate."""
        return f"SubscriptionCost: Monthly={self.monthly_subscription_cost}, Engineer Hourly Rate={self.engineer_avg_hourly_rate}"

    @classmethod
    def get(cls):
        """
        Returns the single SubscriptionCost instance, or creates a default one if it doesn't exist.

        Default values ($5,000/month subscription cost, $60/hour engineer rate) are placeholder
        estimates documented in the Automation Dashboard setup guide. Admins should update these
        via the subscription-cost API endpoint to reflect actual costs before relying on report
        cost calculations.
        """

        instance, created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "monthly_subscription_cost": decimal.Decimal("5000.00"),
                "engineer_avg_hourly_rate": decimal.Decimal("60.00"),
                "include_template_creation_time_in_costs": True,
            },
        )
        if created:
            logger.info(
                "Created SubscriptionCost with default placeholder values ($5,000/month, $60/hr). "
                "Update via the subscription-cost API to reflect actual costs."
            )
        return instance

    @property
    def cost_employee_per_minute(self) -> decimal.Decimal:
        """
        Calculate and return the cost of employee time per minute based on the average hourly rate.
        Ensures engineer_avg_hourly_rate is always a decimal.Decimal.
        """
        return decimal.Decimal(str(self.engineer_avg_hourly_rate)) / decimal.Decimal(60)

    def daily_subscription_cost(self, start: datetime | None = None, end: datetime | None = None) -> decimal.Decimal:
        """
        Calculate and return the daily subscription cost based on the monthly subscription cost.
        Assumes 30 days in a month for calculation.
        """
        now = datetime.now(UTC)
        default_days_in_month = calendar.monthrange(now.year, now.month)[1]
        monthly_cost = decimal.Decimal(str(self.monthly_subscription_cost))
        default_daily_cost = monthly_cost / decimal.Decimal(default_days_in_month)

        if start is None or end is None:
            return default_daily_cost

        if start > end:
            start, end = end, start

        if start.year == end.year and start.month == end.month:
            days_in_month = calendar.monthrange(start.year, start.month)[1]
            return monthly_cost / decimal.Decimal(days_in_month)

        total_days = 0
        total_cost = decimal.Decimal(0)
        for current_year, current_month in _month_range_iter(start, end):
            month_days, month_start_day, month_end_day = _get_month_overlap_days(
                current_year, current_month, start, end
            )
            overlap_days = month_end_day - month_start_day + 1
            if overlap_days <= 0:
                continue
            proportional_cost = monthly_cost * decimal.Decimal(overlap_days) / decimal.Decimal(month_days)
            total_days += overlap_days
            total_cost += proportional_cost
        return total_cost / decimal.Decimal(total_days) if total_days > 0 else default_daily_cost

    def per_second_subscription_cost(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> decimal.Decimal:
        """
        Calculates the weighted subscription cost per elapsed second for a given date range.
        Distributes the proportional monthly cost across all job elapsed seconds in the period.

        Returns a value in units of (currency / second), suitable for direct multiplication
        by a job's elapsed field (which is in seconds) to produce the job's share of the
        subscription cost.

        Formula: cost_per_elapsed_second = period_cost / sum(elapsed_seconds_in_period)
        Verification: sum(job.elapsed * cost_per_elapsed_second) == period_cost
        """
        monthly_cost = decimal.Decimal(str(self.monthly_subscription_cost))

        if start is None or end is None:
            now = datetime.now(UTC)
            days_in_month = calendar.monthrange(now.year, now.month)[1]
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(day=days_in_month, hour=23, minute=59, second=59, microsecond=999999)

        if start > end:
            start, end = end, start

        # Normalize end to end-of-day to ensure the filter matches the day-count formula
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)

        elapsed_result = JobData.objects.filter(
            status__in=[JobStatusChoices.SUCCESSFUL, JobStatusChoices.FAILED],
            finished__gte=start,
            finished__lte=end,
        ).aggregate(total_seconds=Sum("elapsed"))

        total_elapsed_raw = elapsed_result["total_seconds"]
        if total_elapsed_raw is None or total_elapsed_raw == 0:
            # Return a near-zero sentinel rather than 0 to avoid ZeroDivisionError at call
            # sites that multiply this rate by elapsed seconds. In practice this path is
            # only hit when no successful/failed jobs exist in the period, so the rate is
            # never applied to any real job.
            return decimal.Decimal("0").quantize(decimal.Decimal("0.0000000001"))

        total_elapsed_decimal = decimal.Decimal(str(total_elapsed_raw))

        if start.year == end.year and start.month == end.month:
            days_in_month = decimal.Decimal(str(calendar.monthrange(start.year, start.month)[1]))
            total_period_days = decimal.Decimal(str((end.date() - start.date()).days + 1))
            period_cost = monthly_cost * total_period_days / days_in_month
        else:
            period_cost = decimal.Decimal("0")
            for current_year, current_month in _month_range_iter(start, end):
                _, month_start_day, month_end_day = _get_month_overlap_days(current_year, current_month, start, end)
                overlap_days = month_end_day - month_start_day + 1
                if overlap_days > 0:
                    days_in_month_dec = decimal.Decimal(str(calendar.monthrange(current_year, current_month)[1]))
                    period_cost += monthly_cost * (decimal.Decimal(str(overlap_days)) / days_in_month_dec)

        cost_per_unit = period_cost / total_elapsed_decimal
        return cost_per_unit.quantize(decimal.Decimal("0.0000000001"), rounding=decimal.ROUND_HALF_UP)


class FilterSet(CommonModel):
    """
    Saved filter configurations (saved views) for dashboard filtering.

    Allows users to save commonly-used filter combinations for quick access.
    Users can have multiple filter sets, but only one can be marked as default.

    Example:
        filter_set = FilterSet.objects.create(
            name="Last 30 days - Production",
            filters={'organizations': [1, 2], 'date_range': 'last_30_days'}
        )
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="filter_sets",
        help_text="User who created this filter set",
    )

    name = models.CharField(
        max_length=255, db_index=True, help_text='Display name for this filter set (e.g. "Last 30 days")'
    )

    filters = models.JSONField(
        help_text="Filter configuration: {organizations: [], projects: [], labels: [], date_range: {}}"
        # TODO: Add serializer-level schema validation to enforce allowed keys and value types,
        # preventing bad filter configs from causing silent failures when the filter is applied.
    )

    is_default = models.BooleanField(
        default=False, help_text="Whether this is the user's default filter set (only one allowed per user)"
    )

    class Meta:
        """Database and display configuration for FilterSet."""

        db_table = "dashboard_filter_set"
        verbose_name = "Filter Set"
        verbose_name_plural = "Filter Sets"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["user", "is_default"], name="dashboard_fs_user_default_idx"),
            models.Index(fields=["user", "-modified"], name="dashboard_fs_user_mod_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "is_default"],
                condition=models.Q(is_default=True),
                name="one_default_per_user",
                violation_error_message="User can only have one default filter set",
            )
        ]

    def __str__(self) -> str:
        """Return the filter set name."""
        return self.name


class TemplateMetadata(CommonModel):
    """
    Stores metadata for AWX job templates, including name, description, and time estimates.
    Used for reporting and cost calculations.
    """

    template_id = models.IntegerField(
        unique=True,
        db_index=True,
        help_text="AWX job template ID (from AWX database main_jobtemplate table)",
    )

    template_name = models.CharField(max_length=512, db_index=True, help_text="Template name for display (from AWX)")

    time_taken_manually_execute_minutes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="User override: Estimated time to perform this task manually (minutes)",
        validators=[MinValueValidator(0)],
    )

    time_taken_create_automation_minutes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="User override: Estimated time spent creating this automation (minutes)",
        validators=[MinValueValidator(0)],
    )

    class Meta:
        """Database and display configuration for TemplateMetadata."""

        db_table = "dashboard_template_metadata"
        ordering = ["template_name"]
        verbose_name = "Template Metadata"
        verbose_name_plural = "Template Metadata"

    def __str__(self) -> str:
        """Return string representation of the template metadata."""
        return f"Metadata for {self.template_name} (ID: {self.template_id})"

    @classmethod
    def get_min_awx_id(cls) -> int:
        """
        Returns a negative integer for new AWX template IDs if none exist,
        otherwise returns the minimum existing template_id minus one.
        """
        min_id = cls.objects.aggregate(models.Min("template_id")).get("template_id__min", None)
        return min_id - 1 if min_id is not None and min_id < 0 else -1

    @classmethod
    def get_by_awx_id_or_name(cls, name: str, awx_id: int | None = None, elapsed: decimal.Decimal | None = None):
        """
        Retrieves TemplateMetadata by AWX ID or name. If not found, creates a new instance.
        Sets default manual and automation time estimates if not present.
        """
        instance = cls._lookup_by_id(awx_id) or cls._lookup_or_create_by_name(name, awx_id)

        update_fields = cls._apply_time_estimate_defaults(instance, elapsed)
        if update_fields:
            instance.save(update_fields=update_fields)

        return instance

    @classmethod
    def _lookup_by_id(cls, awx_id: int | None) -> "TemplateMetadata | None":
        """Return the TemplateMetadata row matching ``awx_id``, or ``None`` if not found."""
        if awx_id is None:
            return None
        try:
            return cls.objects.get(template_id=awx_id)
        except cls.DoesNotExist:
            return None

    @classmethod
    def _lookup_or_create_by_name(cls, name: str, awx_id: int | None) -> "TemplateMetadata":
        """Return the TemplateMetadata row matching ``name``, creating one if needed."""
        try:
            instance = cls.objects.get(template_name=name)
            # If the record was created with a synthetic negative placeholder ID and we
            # now have the real AWX ID, promote it so later ID-based lookups work correctly.
            if awx_id is not None and instance.template_id < 0:
                instance.template_id = awx_id
                instance.save(update_fields=["template_id"])
            return instance
        except cls.DoesNotExist:
            return cls._create_with_race_handling(name, awx_id)
        except cls.MultipleObjectsReturned:
            # template_name is not unique; pick the best candidate — prefer records with a
            # real (positive) AWX ID so that subsequent ID-based lookups will match.
            logger.warning(
                f"Multiple TemplateMetadata records found for name '{name}'; "
                "selecting the one with the highest template_id."
            )
            return cls.objects.filter(template_name=name).order_by("-template_id").first()

    @classmethod
    def _create_with_race_handling(cls, name: str, awx_id: int | None) -> "TemplateMetadata":
        """Create a TemplateMetadata row, recovering gracefully from concurrent-insert races."""
        # TODO: Add an integration test covering this concurrent-insert path to verify that
        # a second worker correctly recovers from the IntegrityError and returns the winning row.
        try:
            # Wrap in a nested savepoint so that a unique-constraint violation only aborts
            # this inner block — not the outer transaction.atomic() on create_or_update_from_awx.
            # Without this, PostgreSQL marks the outer transaction as aborted after the failed
            # INSERT, causing the recovery filter() call below to also fail.
            with transaction.atomic():
                instance = cls.objects.create(
                    template_name=name,
                    template_id=awx_id if awx_id is not None else cls.get_min_awx_id(),
                )
            logger.info(f"Created new TemplateMetadata '{instance}' from AWX data.")
            return instance
        except IntegrityError:
            # A concurrent transaction created the same record between our get() and
            # create() calls. Fetch whichever row won the race.
            logger.warning(f"Race condition creating TemplateMetadata for '{name}'; fetching existing record.")
            instance = cls.objects.filter(template_name=name).order_by("-template_id").first()
            if instance is None:
                raise
            return instance

    @classmethod
    def _apply_time_estimate_defaults(cls, instance: "TemplateMetadata", elapsed: decimal.Decimal | None) -> list[str]:
        """
        Set default time estimates on instance if they are not already present.
        Returns the list of field names that were changed (for use with save(update_fields=...)).
        """
        update_fields = []
        if elapsed is not None and instance.time_taken_manually_execute_minutes is None:
            # Default: 2× the elapsed time.
            estimated = int(decimal.Decimal(elapsed / 60 * 2).quantize(decimal.Decimal(1), rounding=decimal.ROUND_UP))
            instance.time_taken_manually_execute_minutes = min(estimated, 1_000_000)
            update_fields.append("time_taken_manually_execute_minutes")
        if instance.time_taken_create_automation_minutes is None:
            instance.time_taken_create_automation_minutes = DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES
            update_fields.append("time_taken_create_automation_minutes")
        return update_fields


class JobStatusChoices(models.TextChoices):
    """Text choices for AWX job execution status values."""

    NEW = "new", "New"
    PENDING = "pending", "Pending"
    WAITING = "waiting", "Waiting"
    RUNNING = "running", "Running"
    SUCCESSFUL = "successful", "Successful"
    FAILED = "failed", "Failed"
    ERROR = "error", "Error"
    CANCELED = "canceled", "Canceled"


def label_ids_to_job_data_ids(label_ids: list[int]):
    """Return a queryset of JobData IDs that have any of the given AWX label IDs.

    Shared by JobDataFilterMethods.labels() and filters.apply_or_filters() to avoid
    duplicating the subquery pattern.
    """
    return JobLabel.objects.filter(label_id__in=label_ids).values_list("job_data_id", flat=True)


class JobDataFilterMethods:
    """
    Mixin providing chainable filter methods for JobData querysets.

    Shared between JobDataQuerySet and JobDataManager so that custom filtering
    (date ranges, org/template/project/label) is available on both.
    """

    def before_date(self, dt: datetime | None) -> Self:
        """Filter jobs finished on or before dt (no-op if dt is None)."""
        if dt is not None:
            return self.filter(finished__lte=dt)
        return self

    def after_date(self, dt: datetime | None) -> Self:
        """Filter jobs finished on or after dt (no-op if dt is None)."""
        if dt is not None:
            return self.filter(finished__gte=dt)
        return self

    def organizations(self, ids: list[int] | None) -> Self:
        """Filter jobs belonging to the given organization IDs (no-op if ids is empty)."""
        if ids:
            return self.filter(organization_id__in=ids)
        return self

    def templates(self, ids: list[int] | None) -> Self:
        """Filter jobs for the given template IDs (no-op if ids is empty)."""
        if ids:
            return self.filter(template_id__in=ids)
        return self

    def projects(self, ids: list[int] | None) -> Self:
        """Filter jobs for the given project IDs (no-op if ids is empty)."""
        if ids:
            return self.filter(project_id__in=ids)
        return self

    def labels(self, ids: list[int] | None) -> Self:
        """Filter jobs that have any of the given label IDs (no-op if ids is empty)."""
        if ids:
            return self.filter(id__in=label_ids_to_job_data_ids(ids))
        return self


class JobDataQuerySet(JobDataFilterMethods, models.QuerySet):
    """QuerySet for JobData with custom chainable filter methods."""


class JobDataManager(JobDataFilterMethods, models.Manager):
    """Manager for JobData that returns a JobDataQuerySet with custom filter methods."""

    def get_queryset(self) -> JobDataQuerySet:
        """Return the custom JobDataQuerySet."""
        return JobDataQuerySet(self.model, using=self._db)


class JobData(CommonModel):
    """
    Stores AWX job execution data for reporting, including status, timing, host counts, and related template/project/org info.
    """

    job_id = models.IntegerField(
        unique=True,
        db_index=True,
        help_text="AWX job ID (from AWX database main_unifiedjob table)",
    )

    template_name = models.CharField(max_length=512, help_text="Job template name (from AWX)")

    template_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX template ID",
    )

    project_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX project ID",
    )

    project_name = models.CharField(
        max_length=512, null=True, blank=True, help_text="Project name for display (from AWX)"
    )

    organization_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX organization ID",
    )

    organization_name = models.CharField(
        max_length=512, null=True, blank=True, help_text="Organization name for display (from AWX)"
    )

    status = models.CharField(
        choices=JobStatusChoices.choices, default=JobStatusChoices.SUCCESSFUL, max_length=25, db_index=True
    )

    started = models.DateTimeField(
        null=True,
        default=None,
        help_text="Job start timestamp (from AWX database main_unifiedjob table)",
    )
    finished = models.DateTimeField(
        null=True,
        default=None,
        db_index=True,
        help_text="Job finish timestamp (from AWX database main_unifiedjob table)",
    )
    elapsed = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        help_text="Job elapsed time in seconds (from AWX database main_unifiedjob table)",
    )

    num_hosts = models.PositiveIntegerField(
        default=0, help_text="Number of hosts involved in the job (calculated from AWX job host summaries)"
    )

    launched_by_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX user ID of the user who launched the job (from AWX database main_unifiedjob table)",
    )

    launched_by_username = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="AWX username of the user who launched the job (from AWX database main_unifiedjob table)",
    )

    template_metadata = models.ForeignKey(
        TemplateMetadata,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
        help_text="Reference to TemplateMetadata for this job's template",
    )

    awx_created = models.DateTimeField(help_text="Creation timestamp from AWX", null=True, blank=True)
    awx_modified = models.DateTimeField(help_text="Modification timestamp from AWX", null=True, blank=True)

    class Meta:
        """Database and display configuration for JobData."""

        db_table = "dashboard_job_data"
        ordering = ["-started"]
        verbose_name = "Job Data"
        verbose_name_plural = "Jobs Data"
        indexes = [
            models.Index(fields=["template_id"], name="dashboard_jd_template_idx"),
            models.Index(fields=["project_id"], name="dashboard_jd_project_idx"),
            models.Index(fields=["organization_id"], name="dashboard_jd_organization_idx"),
        ]

    objects = JobDataManager()

    def __str__(self) -> str:
        """Return a string representation showing job ID, template name, and status."""
        return f"Job {self.job_id} - Template: {self.template_name} - Status: {self.status}"

    @classmethod
    def last_timestamp(cls) -> datetime | None:
        """Returns the latest 'awx_modified' timestamp from JobData, or None if no records exist."""
        latest_awx_modified = cls.objects.filter(awx_modified__isnull=False).aggregate(models.Max("awx_modified"))[
            "awx_modified__max"
        ]
        return latest_awx_modified

    @classmethod
    def last_finished_timestamp(cls) -> datetime | None:
        """Returns the latest 'finished' timestamp from JobData, or None if no records exist.

        Use this as the incremental watermark for queries that filter on ``finished``
        (date_field='finished') so the watermark field matches the query field and
        the Controller DB index (main_unifiedjob_finished_*) is used correctly.
        Using MAX(awx_modified) as a watermark for a finished-based query can miss
        jobs that completed in the gap between MAX(finished) and MAX(awx_modified).
        """
        return cls.objects.filter(finished__isnull=False).aggregate(models.Max("finished"))["finished__max"]

    @classmethod
    def min_timestamp(cls) -> datetime | None:
        """Returns the earliest 'finished' timestamp from JobData, or None if no records exist."""
        earliest_finished = cls.objects.filter(finished__isnull=False).aggregate(models.Min("finished"))[
            "finished__min"
        ]
        return earliest_finished

    @classmethod
    @transaction.atomic
    def create_or_update_from_awx(cls, awx_job: AWXJobType):
        """
        Creates or updates a JobData instance from AWX job, label, and host summary data.
        Updates related JobLabel and JobHostSummary records atomically.
        """
        template_metadata = TemplateMetadata.get_by_awx_id_or_name(
            name=awx_job["name"], awx_id=awx_job["unified_job_template_id"], elapsed=awx_job["elapsed"]
        )
        # None signals "column was not available in collected data — preserve existing labels".
        # [] signals "job currently has no labels in AWX — remove any stale records".
        labels = awx_job.get("labels")
        host_summaries = awx_job.get("host_summaries", [])

        job_data, created = cls.objects.update_or_create(
            job_id=awx_job["id"],
            defaults={
                "template_name": awx_job["name"],
                "template_id": awx_job["unified_job_template_id"],
                "project_id": awx_job["project_id"],
                "project_name": awx_job["project_name"],
                "organization_id": awx_job["organization_id"],
                "organization_name": awx_job.get("organization_name"),
                "status": awx_job["status"],
                "started": awx_job["started"],
                "finished": awx_job["finished"],
                "elapsed": awx_job["elapsed"],
                "launched_by_id": awx_job["launched_by_id"],
                "launched_by_username": awx_job["launched_by_username"],
                "template_metadata": template_metadata,
                "awx_created": awx_job["created"],
                "awx_modified": awx_job["modified"],
                "num_hosts": awx_job["num_hosts"],
            },
        )
        logger.info(f"{'Created' if created else 'Updated'} JobData {job_data}")

        if labels is not None:
            existing_labels = {} if created else {o.label_id: o for o in JobLabel.objects.filter(job_data=job_data)}
            cls._sync_labels(job_data, labels, existing_labels)
        if host_summaries is not None:
            existing_summaries = (
                {} if created else {o.host_summary_id: o for o in JobHostSummary.objects.filter(job_data=job_data)}
            )
            cls._sync_host_summaries(job_data, host_summaries, existing_summaries)

    @classmethod
    def _sync_labels(cls, job_data: "JobData", awx_label_ids: list, existing: dict) -> None:
        """Sync JobLabel records: create new, delete stale."""
        to_create = [
            JobLabel(job_data=job_data, label_id=lid) for lid in awx_label_ids if existing.pop(lid, None) is None
        ]
        if to_create:
            JobLabel.objects.bulk_create(to_create)
            logger.info(f"Created {len(to_create)} new JobLabel records for JobData {job_data}")
        if existing:
            JobLabel.objects.filter(pk__in=[o.pk for o in existing.values()]).delete()
            logger.info(f"Deleted {len(existing)} stale JobLabel records for JobData {job_data}")

    @classmethod
    def _sync_host_summaries(cls, job_data: "JobData", awx_host_summaries: list, existing: dict) -> None:
        """Sync JobHostSummary records: bulk-update existing, bulk-create new, delete stale."""
        to_create = []
        to_update = []
        for awx_hs in awx_host_summaries:
            existing_hs = existing.pop(awx_hs["id"], None)
            if existing_hs is not None:
                existing_hs.host_id = awx_hs["host_id"]
                existing_hs.host_name = awx_hs["host_name"]
                to_update.append(existing_hs)
            else:
                to_create.append(
                    JobHostSummary(
                        job_data=job_data,
                        host_id=awx_hs["host_id"],
                        host_name=awx_hs["host_name"],
                        host_summary_id=awx_hs["id"],
                    )
                )
        if to_update:
            # batch_size=500 prevents excessively large SQL statements for jobs with many hosts
            JobHostSummary.objects.bulk_update(to_update, ["host_id", "host_name"], batch_size=500)
            logger.info(f"Updated {len(to_update)} JobHostSummary records for JobData {job_data}")
        if to_create:
            JobHostSummary.objects.bulk_create(to_create)
            logger.info(f"Created {len(to_create)} new JobHostSummary records for JobData {job_data}")
        if existing:
            JobHostSummary.objects.filter(pk__in=[o.pk for o in existing.values()]).delete()
            logger.info(f"Deleted {len(existing)} stale JobHostSummary records for JobData {job_data}")


class JobLabel(CommonModel):
    """
    Stores label associations for a JobData instance (AWX job).
    Using for filtering and reporting based on AWX labels.
    """

    job_data = models.ForeignKey(JobData, on_delete=models.CASCADE, related_name="labels")
    label_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX label ID",
    )

    class Meta:
        """Database and display configuration for JobLabel."""

        db_table = "dashboard_job_data_label"
        verbose_name = "Job Data Label"
        verbose_name_plural = "Job Data Labels"
        indexes = [
            models.Index(fields=["job_data", "label_id"], name="dashboard_jl_job_label_idx"),
        ]

    def __str__(self) -> str:
        """Return a string showing the parent job template name and label ID."""
        return f"{self.job_data.template_name}: {self.label_id}"


class JobHostSummary(CommonModel):
    """
    Stores host summary statistics for a JobData instance (AWX job).
    Used for host-level reporting and unique host counts.
    """

    job_data = models.ForeignKey(JobData, on_delete=models.CASCADE, related_name="host_summaries")
    host_summary_id = models.IntegerField(
        unique=True,
        null=True,
        blank=True,
        help_text="AWX host summary ID (from AWX database main_hostsummary table)",
    )
    host_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX host ID (from AWX database main_host table)",
    )

    host_name = models.CharField(max_length=512, db_index=True, help_text="Host name for display (from AWX)")

    class Meta:
        """Database and display configuration for JobHostSummary."""

        db_table = "dashboard_job_data_host_summary"
        verbose_name = "Job Data Host Summary"
        verbose_name_plural = "Job Data Host Summaries"
        indexes = [
            models.Index(fields=["job_data", "host_id"], name="dashboard_jhs_job_host_idx"),
        ]

    def __str__(self) -> str:
        """Return a string showing the host name and parent job template name."""
        return f"{self.host_name}: {self.job_data.template_name}"

    @classmethod
    def unique_count(
        cls, start: datetime | None = None, end: datetime | None = None, options: dict[str, Any] | None = None
    ) -> int:
        """
        Returns the count of unique hosts across all JobData records.
        Filters by finished date range and additional options
        (organization, project, template, label) if provided in the options dict.
        """
        options = options or {}
        queryset = cls.objects

        # Apply date range filters
        if start is not None:
            queryset = queryset.filter(job_data__finished__gte=start)
        if end is not None:
            queryset = queryset.filter(job_data__finished__lte=end)

        # Mapping of option keys to their corresponding filter field
        filter_mapping = {
            "organization": "job_data__organization_id__in",
            "project": "job_data__project_id__in",
            "template": "job_data__template_id__in",
        }

        # Apply filters from options using the mapping
        for option_key, filter_field in filter_mapping.items():
            values = options.get(option_key)
            if values:
                queryset = queryset.filter(**{filter_field: values})

        # Handle labels separately (requires subquery)
        labels = options.get("label")
        if labels:
            labels_qs = JobLabel.objects.filter(label_id__in=labels).values_list("job_data_id", flat=True)
            queryset = queryset.filter(job_data_id__in=labels_qs)

        # Annotate a stable surrogate identifier: prefer host_id (cast to text) when
        # present, fall back to host_name for hosts without a stable AWX ID.
        queryset = queryset.annotate(
            stable_host=Case(
                When(host_id__isnull=False, then=Cast(F("host_id"), output_field=models.TextField())),
                default=F("host_name"),
                output_field=models.TextField(),
            )
        )
        return queryset.values("stable_host").distinct().count()


class DashboardTelemetry(CommonModel):
    """
    Stores dashboard telemetry information for the AAP telemetry, including collection duration in ms,
    number of records processed, database query time in ms and cache hit rate.
    This is used for telemetry in the dashboard reports.
    """

    task_name = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Name of the task that produced this telemetry entry",
    )

    collection_run_date = models.DateField(
        help_text="UTC date on which the task ran",
    )

    success = models.BooleanField(
        default=True,
        help_text="Whether the task completed without an error",
    )

    collection_duration_ms = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(decimal.Decimal("0.00"))],
        help_text="Collection duration (ms)",
    )

    number_of_records_processed = models.IntegerField(
        validators=[MinValueValidator(0)],
        help_text="Number of records processed",
    )

    database_query_time_ms = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(decimal.Decimal("0.00"))],
        help_text="Database query time (ms)",
        null=True,  # None when not tracked (e.g. batched backfill tasks)
        blank=True,
    )

    cache_hit_rate = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(decimal.Decimal("0.00"))],
        help_text="Cache hit rate",
        null=True,  # some tasks don't create cache, in which case cache hit rate would be None
        blank=True,
    )

    class Meta:
        """Database and display configuration for DashboardTelemetry."""

        db_table = "dashboard_telemetry"
        verbose_name = "Dashboard Telemetry"
        verbose_name_plural = "Dashboard Telemetry Entries"
        indexes = [models.Index(fields=["collection_run_date", "task_name"], name="dash_tel_run_task_idx")]

    def __str__(self) -> str:
        """Return a string representation showing task name, collection duration, and records processed."""
        return f"DashboardTelemetry: Collection Duration (ms)={self.collection_duration_ms}, Number of Records Processed={self.number_of_records_processed}, DB Query Time (ms)={self.database_query_time_ms}, Cache Hit Rate={self.cache_hit_rate}"
