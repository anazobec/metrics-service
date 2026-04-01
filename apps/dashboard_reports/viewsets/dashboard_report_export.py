import decimal
import logging
import csv
from datetime import UTC, datetime
from functools import wraps
from typing import Any, List

from dateutil.relativedelta import relativedelta
from django.db import models
from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Trunc
from django_generate_series.models import generate_series
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.core.permissions import DeveloperModeRequired
from apps.dashboard_reports.filters import CustomReportFilter, DateFilter, get_filter_options
from apps.dashboard_reports.models import JobData, JobHostSummary, JobStatusChoices, SubscriptionCost
from apps.dashboard_reports.serializers import (
    ReportDetailSerializer,
)
from apps.tasks.decorators import require_date_range
from apps.tasks.api_utils import build_error_response
from enum import Enum

logger = logging.getLogger(__name__)


class ReportType(Enum):
    SUMMARY = "summary"
    ROI = "roi"
    TRENDS = "trends"

    @classmethod
    def to_list(cls) -> list[str]:
        return [item.value for item in cls]


class DashboardReportExportViewSet(ReadOnlyModelViewSet):
    """
    ViewSet for exporting dashboard reports as CSV or PDF.
    Provides an endpoint to export the same data as the details endpoint,
    but in a flat format suitable for spreadsheets.

    Endpoints:
        GET /api/v1/dashboard_reports/export/ - Export report data as CSV or PDF

    Query Parameters:
        period (string): Filter for report start date. Options: 'last_7_days', 'last_14_days', 'last_30_days', 'last_60_days', 'last_90_days' (required)
        tz (string): Timezone string (default: UTC)
        organization_id (int): Filter by organization ID
        format (string): Export format, either 'csv' or 'pdf' (default: 'csv')
        report_type (string): Type of report to export, either 'summary', 'roi', or 'trends' (default: 'summary')
    """

    query_parameters = [
        OpenApiParameter(
            name="period",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=True,
            enum=DateFilter.to_list(),
            description="Filter for report period.",
        ),
        OpenApiParameter(
            name="tz",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            default="UTC",
            required=True,
            description="Timezone string (default: UTC)",
        ),
        OpenApiParameter(
            name="organization_id",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=False,
            description="Filter by organization ID",
        ),
        OpenApiParameter(
            name="format",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            default="csv",
            required=False,
            enum=["csv", "pdf"],
            description="Export format, either 'csv' or 'pdf' (default: 'csv')",
        ),
        OpenApiParameter(
            name="report_type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            default="summary",
            required=False,
            enum=ReportType.to_list(),
            description="Type of report to export, either 'summary', 'roi', or 'trends' (default: 'summary')",
        ),
        OpenApiParameter(
            name="ordering",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description="Field to order by (e.g. 'template_name', 'successful_runs', 'savings', etc.)",
        ),
    ]

    versioning_class = None  # Disable versioning for this viewset
    permission_classes = [DeveloperModeRequired]  # TODO: Replace with appropriate permissions

    filter_backends = [CustomReportFilter, filters.OrderingFilter]

    ordering_fields: list[str] = [
        "template_name",
        "successful_runs",
        "failed_runs",
        "num_hosts",
        "elapsed",
        "manual_time",
        "manual_costs",
        "automated_costs",
        "savings",
        "runs",
    ]

    ordering = ["template_name"]

    def get_queryset(self) -> QuerySet[JobData]:
        """
        Builds annotated queryset for dashboard reporting, including cost and time calculations.
        """

        subscription_cost = SubscriptionCost.get()
        average_cost_employee_minute = subscription_cost.cost_employee_per_minute
        period = self.kwargs.get("period", None)
        tz = self.kwargs.get("tz", "UTC")

        if not period:
            start_date, end_date = None, None
        else:
            start_date, end_date = DateFilter.to_start_date_end_date(
                value=DateFilter[period.upper()].value, tz_string=tz
            )

        aap_subscription_per_second = subscription_cost.per_second_subscription_cost(start_date, end_date)
        enable_template_creation_time = subscription_cost.include_template_creation_time_in_costs

        if enable_template_creation_time:
            automated_costs = (F("time_taken_create_automation_minutes") * average_cost_employee_minute) + (
                F("elapsed") * aap_subscription_per_second
            )
            time_savings = (
                F("manual_time") - F("elapsed") - (F("time_taken_create_automation_minutes") * decimal.Decimal(60))
            )
        else:
            automated_costs = F("elapsed") * aap_subscription_per_second
            time_savings = F("manual_time") - F("elapsed")

        manual_costs = F("num_hosts") * F("time_taken_manually_execute_minutes") * average_cost_employee_minute
        manual_time = F("num_hosts") * (F("time_taken_manually_execute_minutes") * 60)

        qs = JobData.objects.values(
            "template_name",
            "template_id",
            time_taken_manually_execute_minutes=F("template_metadata__time_taken_manually_execute_minutes"),
            time_taken_create_automation_minutes=F("template_metadata__time_taken_create_automation_minutes"),
        ).annotate(
            runs=Count("id"),
            successful_runs=Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)),
            failed_runs=Count("id", filter=Q(status=JobStatusChoices.FAILED)),
            elapsed=Sum("elapsed"),
            num_hosts=Sum("num_hosts"),
            automated_costs=automated_costs,
            manual_costs=manual_costs,
            manual_time=manual_time,
            time_savings=time_savings,
            savings=(F("manual_costs") - F("automated_costs")),
        )
        return qs

    def _get_report_details(
        self,
        start_date: str,
        end_date: str,
        filter_options: dict[str, List[int]],
    ) -> dict[str, Any]:

        filtered_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())

        ### TOP USERS ###
        top_users_qs = (
            filtered_qs.filter(launched_by_id__isnull=False)
            .values("launched_by_id", "launched_by_username")
            .annotate(count=Count("id"))
            .order_by("-count", "launched_by_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### TOP PROJECTS ###
        top_projects_qs = (
            filtered_qs.filter(project_id__isnull=False)
            .values("project_id", "project_name")
            .annotate(count=Count("id"))
            .order_by("-count", "project_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### AGGREGATED DATA ###
        query_set = self.get_queryset()
        qs = self.filter_queryset(query_set)
        report_data_qs = qs.aggregate(
            total_runs=Coalesce(Sum("runs"), Value(0)),
            total_successful_runs=Coalesce(Sum("successful_runs"), Value(0)),
            total_failed_runs=Coalesce(Sum("failed_runs"), Value(0)),
            total_num_hosts=Coalesce(Sum("num_hosts"), Value(0)),
            total_elapsed=Coalesce(Sum("elapsed"), Value(decimal.Decimal("0"))),
            total_manual_time=Coalesce(Sum("manual_time"), Value(0)),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )

        ### Unique hosts count ###
        unique_hosts_count = JobHostSummary.unique_count(start_date, end_date, filter_options)

        ### CHART DATA ###
        chart_data = self.get_chart_data()

        ### Serialize data ###
        report_data = self.get_serializer(
            {
                **report_data_qs,
                "top_users": top_users_qs,
                "top_projects": top_projects_qs,
                "total_number_of_unique_hosts": unique_hosts_count,
                **chart_data,
            }
        ).data

        return report_data


    # Export report as CSV endpoint
    @extend_schema(parameters=query_parameters)
    @action(detail=False, methods=["get"], url_path="export")
    @require_date_range
    def export_csv(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Exports the report data as a CSV file.
        The CSV includes all the same data as the details endpoint, but in a flat format suitable for spreadsheets.
        """
        options = get_filter_options(request=request)

        period = self.kwargs.get("period")
        tz = self.kwargs.get("tz", "UTC")
        start_date, end_date = DateFilter.to_start_date_end_date(value=DateFilter[period.upper()].value, tz_string=tz)

        report_type = ReportType(request.GET.get("report_type", "summary"))
        format = request.GET.get("format", "csv").lower()  # can also be pdf

        report_data = self._get_report_details(
            start_date=start_date,
            end_date=end_date,
            filter_options=options,
        )

        # csv export of report_data
        response = Response()
        match(format):
            case "csv":
                response = Response(content_type="text/csv")
                response["Content-Disposition"] = f'attachment; filename="dashboard_report_{report_type.value}_{end_date}-{period}.csv"'

                writer = csv.writer(response)
                # Write headers
                writer.writerow(["Metric", "Value"])

                # Write summary data
                for key, value in report_data.items():
                    if key not in ["top_users", "top_projects", "host_chart", "job_chart"]:
                        writer.writerow([key, value])

                # Write top users
                writer.writerow([])
                writer.writerow(["Top Users"])
                writer.writerow(["Username", "Run Count"])
                for user in report_data["top_users"]:
                    writer.writerow([user["launched_by_username"], user["count"]])
                # Write top projects
                writer.writerow([])
                writer.writerow(["Top Projects"])
                writer.writerow(["Project Name", "Run Count"])
                for project in report_data["top_projects"]:
                    writer.writerow([project["project_name"], project["count"]])
            case "pdf":
                # Placeholder for PDF export logic
                response = build_error_response("PDF export not implemented yet", status_code=501)
                return Response(response, status=status.HTTP_501_NOT_IMPLEMENTED)
            case _:
                response = build_error_response("Invalid format. Supported formats: csv, pdf", status_code=400)
                return Response(response, status=status.HTTP_400_BAD_REQUEST)

        return response

