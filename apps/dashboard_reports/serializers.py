"""
Serializers for the dashboard reports module.

Provides serializers for filter options, report data, subscription cost, and
template metadata used by the dashboard reporting API endpoints.
"""

import decimal
from typing import TYPE_CHECKING, Any

from rest_framework import serializers

from apps.dashboard_reports.models import DashboardTelemetry, FilterSet, JobData, SubscriptionCost, TemplateMetadata
from apps.dashboard_reports.utils import sec2time

if TYPE_CHECKING:
    _ReportSerializerBase = serializers.ModelSerializer[JobData]
else:
    _ReportSerializerBase = serializers.ModelSerializer

SUCCESSFUL_RUNS_IN_WINDOW = "Successful runs in the window"


class FilterOptionWithIdSerializer(serializers.Serializer):
    """Serializer for a single filter dropdown option with an integer ID and display name."""

    id = serializers.IntegerField(help_text="Option ID")
    name = serializers.CharField(help_text="Option display name")


class ReportSerializer(_ReportSerializerBase):
    """
    Serializer for per-template aggregated report rows.

    Each row represents a single job template with aggregated run counts, elapsed
    time, cost estimates, and savings calculations across the selected date range.
    """

    # NOTE: id must be sourced from template_metadata_id (which cannot be null), because we
    # link the logic of changing times for manually execute and time taken to create
    # automation, and if template_id can be null then the GUI also gives an error.
    id = serializers.IntegerField(source="template_metadata_id", read_only=True)
    # The queryset groups by template_metadata__template_name (FK traversal) rather than the
    # denormalized JobData.template_name field, so we must source from the traversal key.
    template_name = serializers.CharField(source="template_metadata__template_name", read_only=True)
    time_taken_manually_execute_minutes = serializers.IntegerField(
        read_only=True, help_text="Estimated time to perform this task manually (minutes)"
    )
    time_taken_create_automation_minutes = serializers.IntegerField(
        read_only=True, help_text="Estimated time spent creating this automation (minutes)"
    )
    runs = serializers.IntegerField(read_only=True, help_text="Total number of times this job template was executed")
    successful_runs = serializers.IntegerField(read_only=True, help_text="Number of successful runs")
    failed_runs = serializers.IntegerField(read_only=True, help_text="Number of failed runs")
    elapsed = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total elapsed time for all runs (seconds)"
    )
    elapsed_str = serializers.SerializerMethodField(help_text="Total elapsed time for all runs (human-readable string)")
    automated_costs = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True, help_text="Estimated costs of running this job template on AAP"
    )
    manual_costs = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True, help_text="Estimated costs if all jobs were run manually"
    )
    time_savings = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        read_only=True,
        help_text="Estimated time savings in seconds by automating this job template",
    )
    time_savings_str = serializers.SerializerMethodField(
        help_text="Estimated time savings by automating this job template (human-readable string)"
    )
    savings = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        read_only=True,
        help_text="Estimated cost savings by automating this job template",
    )
    label_ids = serializers.ListField(
        child=serializers.IntegerField(),
        default=list,
        read_only=True,
        help_text="AWX label IDs associated with jobs of this template in the selected date range",
    )

    class Meta:
        """Serializer meta configuration for ReportSerializer."""

        model = JobData
        fields = (
            "template_name",
            "id",
            "time_taken_manually_execute_minutes",
            "time_taken_create_automation_minutes",
            "runs",
            "successful_runs",
            "failed_runs",
            "elapsed",
            "elapsed_str",
            "automated_costs",
            "manual_costs",
            "time_savings",
            "time_savings_str",
            "savings",
            "label_ids",
        )

    def _get_time_str(self, obj: dict[str, Any], key: str) -> str:
        """Helper to convert a time field to human-readable string."""
        value = obj.get(key)
        if value is None:
            return ""
        if value < 0:
            return f"-{sec2time(-value)}"
        return sec2time(value)

    def get_elapsed_str(self, obj: dict[str, Any]) -> str:
        """Return total elapsed time as a human-readable string."""
        return self._get_time_str(obj, "elapsed")

    def get_time_savings_str(self, obj: dict[str, Any]) -> str:
        """Return estimated time savings as a human-readable string."""
        return self._get_time_str(obj, "time_savings")


class TopUserSerializer(serializers.Serializer):
    """Serializer for top user entries (user ID, username, and execution count)."""

    id = serializers.IntegerField(
        read_only=True, source="launched_by_id", help_text="ID of the user who executed the job"
    )

    name = serializers.CharField(
        read_only=True, source="launched_by_username", help_text="Username of the user who executed the job"
    )

    execution_count = serializers.IntegerField(
        read_only=True, source="count", help_text="Number of times this user executed a job"
    )


class TopProjectSerializer(serializers.Serializer):
    """Serializer for top project entries (project ID, name, and execution count)."""

    id = serializers.IntegerField(
        read_only=True, source="project_id", help_text="ID of the project associated with the job"
    )

    name = serializers.CharField(
        read_only=True, source="project_name", help_text="Name of the project associated with the job"
    )

    execution_count = serializers.IntegerField(
        read_only=True, source="count", help_text="Number of times jobs associated with this project were executed"
    )


class ChartDataItemSerializer(serializers.Serializer):
    """Serializer for a single time-series data point (timestamp label and integer value)."""

    label = serializers.DateTimeField(read_only=True, help_text="Label for the data point (e.g. timestamp)")
    value = serializers.IntegerField(read_only=True, help_text="Value for the data point (e.g. number of job runs)")


class ReportChartSerializer(serializers.Serializer):
    """Serializer for a chart series including the time granularity kind and data items."""

    kind = serializers.CharField(
        read_only=True, help_text="Type of date range for the series (e.g. hour, day, month, year)"
    )
    items = ChartDataItemSerializer(many=True, read_only=True, help_text="Data points for the chart series")


class ReportDetailSerializer(serializers.Serializer):
    """
    Serializer for the dashboard detail endpoint response.

    Aggregates total run counts, cost figures, time savings, unique host count,
    top users, top projects, and chart series data for the selected date range.
    """

    total_number_of_job_runs = serializers.IntegerField(
        read_only=True, source="total_runs", help_text="Total number of job runs"
    )
    total_number_of_successful_jobs = serializers.IntegerField(
        read_only=True, source="total_successful_runs", help_text="Total number of successful job runs"
    )
    total_number_of_failed_jobs = serializers.IntegerField(
        read_only=True, source="total_failed_runs", help_text="Total number of failed job runs"
    )
    total_number_of_host_job_runs = serializers.IntegerField(
        read_only=True,
        source="total_num_hosts",
        help_text="Total number of host job runs (sum of all hosts across all jobs)",
    )
    total_hours_of_automation = serializers.SerializerMethodField(help_text="Total hours of automation")
    cost_of_automated_execution = serializers.SerializerMethodField(help_text="Total cost of automated execution")
    cost_of_manual_automation = serializers.SerializerMethodField(help_text="Total cost of manual execution")
    total_saving = serializers.SerializerMethodField(
        help_text="Total savings from automation (manual costs - automated costs)"
    )
    total_time_saving = serializers.SerializerMethodField(help_text="Total time savings from automation (in hours)")
    total_number_of_unique_hosts = serializers.IntegerField(
        read_only=True, help_text="Total number of unique hosts across all job runs"
    )

    top_users = TopUserSerializer(many=True, read_only=True, help_text="List of top users who executed the most jobs")

    top_projects = TopProjectSerializer(
        many=True, read_only=True, help_text="List of top projects associated with the most job executions"
    )

    job_chart = ReportChartSerializer(read_only=True, help_text="Chart data showing job executions over time")

    host_chart = ReportChartSerializer(read_only=True, help_text="Chart data showing host job runs over time")

    def _get_rounded_value(self, obj: dict[str, Any], key: str, divisor: float = 1) -> float:
        """Helper to get a rounded value from obj, optionally dividing by a divisor."""
        value = obj.get(key)
        return round(value / divisor, 2) if value is not None else 0

    def get_total_hours_of_automation(self, obj: dict[str, Any]) -> float:
        """Return total elapsed automation time converted from seconds to hours."""
        return self._get_rounded_value(obj, "total_elapsed", divisor=3600)

    def get_cost_of_automated_execution(self, obj: dict[str, Any]) -> float:
        """Return total cost of running jobs on AAP (automated execution cost)."""
        return self._get_rounded_value(obj, "total_automated_costs")

    def get_cost_of_manual_automation(self, obj: dict[str, Any]) -> float:
        """Return total estimated cost if all jobs were executed manually."""
        return self._get_rounded_value(obj, "total_manual_costs")

    def get_total_saving(self, obj: dict[str, Any]) -> float:
        """Return total cost savings from automation (manual costs minus automated costs)."""
        return self._get_rounded_value(obj, "total_savings")

    def get_total_time_saving(self, obj: dict[str, Any]) -> float:
        """Return total time savings from automation converted from seconds to hours."""
        return self._get_rounded_value(obj, "total_time_savings", divisor=3600)


class SubscriptionCostSerializer(serializers.ModelSerializer):
    """Serializer for viewing and updating the SubscriptionCost singleton record."""

    class Meta:
        """Serializer meta configuration for SubscriptionCostSerializer."""

        model = SubscriptionCost
        fields = [
            "id",
            "monthly_subscription_cost",
            "engineer_avg_hourly_rate",
            "include_template_creation_time_in_costs",
        ]
        extra_kwargs = {
            "id": {"read_only": True, "help_text": "ID of the subscription cost entry"},
            "monthly_subscription_cost": {
                "required": False,
                "help_text": "Monthly subscription cost for AAP subscription",
                "min_value": decimal.Decimal("0.00"),
            },
            "engineer_avg_hourly_rate": {
                "required": False,
                "help_text": "Average hourly rate for engineers performing manual work",
                "min_value": decimal.Decimal("0.00"),
            },
            "include_template_creation_time_in_costs": {
                "required": False,
                "help_text": (
                    "Include template creation time in cost calculations. If false, "
                    "costs related to template creation time will be excluded."
                ),
            },
        }


class TemplateMetadataSerializer(serializers.ModelSerializer):
    """Serializer for TemplateMetadata, exposing user-overridable time estimate fields."""

    template_id = serializers.IntegerField(read_only=True, help_text="ID of the associated job template")
    time_taken_manually_execute_minutes = serializers.IntegerField(
        allow_null=True, min_value=0, help_text="User override: Estimated time to perform this task manually (minutes)"
    )
    time_taken_create_automation_minutes = serializers.IntegerField(
        allow_null=True, min_value=0, help_text="User override: Estimated time spent creating this automation (minutes)"
    )

    class Meta:
        """Serializer meta configuration for TemplateMetadataSerializer."""

        model = TemplateMetadata
        fields = [
            "template_id",
            "time_taken_manually_execute_minutes",
            "time_taken_create_automation_minutes",
        ]


class FilterSetSerializer(serializers.ModelSerializer):
    """Serializer for FilterSet, exposing name, filter criteria, and default flag."""

    class Meta:
        """Serializer meta configuration for FilterSetSerializer."""

        model = FilterSet
        fields = [
            "id",
            "name",
            "filters",
            "is_default",
        ]
        extra_kwargs = {
            "id": {"read_only": True, "help_text": "ID of the filter set entry"},
            "name": {"help_text": "Name of the filter set"},
            "filters": {"help_text": "JSON object containing the filter criteria"},
            "is_default": {
                "help_text": "Indicates whether this filter set is the default for the user (only one default per user allowed)"
            },
        }

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Ensure the user doesn't already have a filter set with this name."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None:
            return attrs
        name = attrs.get("name", self.instance.name if self.instance is not None else None)
        queryset = FilterSet.objects.filter(user=user, name=name)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError({"name": "Report name already exists. Please choose a different name."})
        return attrs


class DashboardTelemetrySerializer(serializers.ModelSerializer):
    """Serializer for DashboardTelemetry"""

    task_name = serializers.CharField(
        max_length=512,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Name of the task that produced this telemetry entry",
    )
    collection_run_date = serializers.DateField(help_text="UTC date on which the task ran")
    collection_duration_ms = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        help_text="Collection duration (ms)",
    )
    number_of_records_processed = serializers.IntegerField(help_text="Number of records processed")
    database_query_time_ms = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        allow_null=True,
        help_text="Database query time (ms)",
    )
    cache_hit_rate = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text="Cache hit rate",
    )
    success = serializers.BooleanField(
        default=True,
        help_text="Whether the task completed without an error",
    )

    class Meta:
        """Serializer meta configuration for DashboardTelemetrySerializer."""

        model = DashboardTelemetry
        fields = [
            "task_name",
            "collection_run_date",
            "collection_duration_ms",
            "number_of_records_processed",
            "database_query_time_ms",
            "cache_hit_rate",
            "success",
        ]


class FeaturedTemplateSerializer(serializers.Serializer):
    """Most-used job template by successful run count in the window."""

    id = serializers.IntegerField(allow_null=True, help_text="AWX template ID")
    name = serializers.CharField(allow_null=True, help_text="Template name")
    run_count = serializers.IntegerField(help_text=SUCCESSFUL_RUNS_IN_WINDOW)


class StreakDaySerializer(serializers.Serializer):
    """One UTC calendar day of the automation streak series."""

    date = serializers.DateField(help_text="UTC calendar day")
    successful_runs = serializers.IntegerField(help_text="Successful job runs on that day")


class AutomationStreakSerializer(serializers.Serializer):
    """Daily successful-run series plus the current consecutive-day streak."""

    streak = serializers.IntegerField(help_text="Consecutive active days ending today")
    daily = StreakDaySerializer(many=True, help_text="One entry per day in the 30-day window")


class OrgStreakOrganizationSerializer(serializers.Serializer):
    """The organization the org streak is scoped to."""

    id = serializers.IntegerField(help_text="AWX organization ID")
    name = serializers.CharField(allow_null=True, help_text="Organization name")
    run_count = serializers.IntegerField(help_text=SUCCESSFUL_RUNS_IN_WINDOW)


class OrgStreakSerializer(AutomationStreakSerializer):
    """Automation streak for the busiest organization in the window."""

    organization = OrgStreakOrganizationSerializer(help_text="Organization the streak belongs to")


class OrganizationLeaderboardRowSerializer(serializers.Serializer):
    """One organization on the org leaderboard."""

    rank = serializers.IntegerField(help_text="1-based position")
    name = serializers.CharField(allow_null=True, help_text="Organization name")
    runs = serializers.IntegerField(help_text=SUCCESSFUL_RUNS_IN_WINDOW)


class OrganizationLeaderboardSerializer(serializers.Serializer):
    """Top 10 organizations by successful runs, plus the user's org standing."""

    user_organization_rank = serializers.IntegerField(
        allow_null=True, help_text="Rank of the current user's organization (None if it has no runs)"
    )
    total_organizations = serializers.IntegerField(help_text="Organizations the rank is out of")
    leaderboard = OrganizationLeaderboardRowSerializer(many=True)


class ActivityLevelRowSerializer(serializers.Serializer):
    """One user on an activity-level leaderboard."""

    rank = serializers.IntegerField(help_text="1-based position")
    username = serializers.CharField(
        help_text="Full username for the current user, otherwise the first two letters upper-cased"
    )
    runs = serializers.IntegerField(help_text="Value of this activity level's metric")
    is_current_user = serializers.BooleanField(required=False, help_text="Present and true only for the current user")


class ActivityLevelSerializer(serializers.Serializer):
    """A per-user leaderboard for one activity level (volume / breadth / consistency)."""

    id = serializers.CharField(help_text="Activity level id: volume, breadth or consistency")
    current_user_rank = serializers.IntegerField(
        allow_null=True, help_text="Rank of the current user (None if they have no runs)"
    )
    total_users = serializers.IntegerField(help_text="Users the rank is out of")
    leaderboard = ActivityLevelRowSerializer(many=True)


class DashboardLeaderboardsSerializer(serializers.Serializer):
    """Full response for the dashboard leaderboards endpoint (trailing 30 days)."""

    job_runs = serializers.IntegerField(help_text="Total successful job runs in the window")
    active_organizations = serializers.IntegerField(
        help_text="Organizations with at least one successful job run in the window"
    )
    featured_template = FeaturedTemplateSerializer(allow_null=True)
    enterprise_streak = AutomationStreakSerializer()
    org_streak = OrgStreakSerializer(allow_null=True)
    organization_leaderboard = OrganizationLeaderboardSerializer()
    org_achievements = serializers.ListField(
        child=serializers.CharField(), help_text="Earned org achievement ids: sustained, rising, top_tier"
    )
    activity_levels = ActivityLevelSerializer(many=True)
    user_achievements = serializers.ListField(
        child=serializers.CharField(),
        help_text="Earned user achievement ids: ignition, week_warrior, month_warrior, explorer, centurion, reliable, accelerator",
    )
