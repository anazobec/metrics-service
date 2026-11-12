"""
Filter utilities for dashboard reports.

Provides date range filtering via DateFilter enum, AND/OR filter helpers for
organization, template, project and label query parameters, job-type filtering,
and the CustomReportFilter backend used by DashboardReportViewSet.
"""

import datetime
from collections.abc import Sequence
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import models
from django.db.models import QuerySet
from rest_framework import filters
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.dashboard_reports.job_definition import HIDDEN_LAUNCH_TYPES
from apps.dashboard_reports.models import JobData, label_ids_to_job_data_ids

FILTER_FIELDS: frozenset[str] = frozenset({"organization", "template", "label", "project"})


def validate_custom_period_dates(start_date_str: str | None, end_date_str: str | None) -> None:
    """Validate that both dates are provided for custom period."""
    if not start_date_str or not end_date_str:
        raise ValueError({"detail": "start_date and end_date are required when period is 'custom'."})


class DateFilter(Enum):
    """Enumeration of supported relative date range filter values for dashboard reports."""

    LAST_7_DAYS = "last_7_days"
    LAST_14_DAYS = "last_14_days"
    LAST_30_DAYS = "last_30_days"
    LAST_60_DAYS = "last_60_days"
    LAST_90_DAYS = "last_90_days"
    CUSTOM = "custom"

    @classmethod
    def to_list(cls) -> Sequence[str]:
        """Return a list of all valid date filter string values."""
        return [v.value for v in DateFilter]

    @classmethod
    def get_num_last_days(cls, value: str) -> int | None:
        """Extract the number of days from a DateFilter value string (e.g. 'last_30_days' → 30)."""
        return int(value.replace("last_", "").replace("_days", "")) if value is not None else None

    @staticmethod
    def get_timezone(timezone_str: str) -> ZoneInfo:
        """Return a ZoneInfo object for the given timezone string, or raise ValueError."""
        try:
            return ZoneInfo(timezone_str)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid timezone: {timezone_str!r}. "
                f"Please use a valid IANA timezone (e.g., 'UTC', 'America/New_York', 'Europe/London'). "
                f"See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones for valid values."
            ) from exc

    @classmethod
    def custom_range_to_start_date_end_date(
        cls, start_date_str: str, end_date_str: str, tz_string: str
    ) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Convert custom date range strings to datetime.date objects.

        Args:
            start_date_str (str): Start date string in 'YYYY-MM-DD' format.
            end_date_str (str ): End date string in 'YYYY-MM-DD' format.
            tz_string (str): Timezone string
        Returns:
            tuple[datetime.datetime, datetime.datetime]: Tuple of start and end dates as datetime.datetime objects.
        """

        tz = cls.get_timezone(tz_string)

        try:
            start = datetime.datetime.combine(
                datetime.date.fromisoformat(start_date_str),
                datetime.time.min,
                tzinfo=tz,
            )
            end = datetime.datetime.combine(
                datetime.date.fromisoformat(end_date_str),
                datetime.time.max,
                tzinfo=tz,
            )
        except ValueError as exc:
            raise ValueError(
                f"Invalid date format: start_date={start_date_str!r}, end_date={end_date_str!r}. "
                "Expected format: 'YYYY-MM-DD'."
            ) from exc

        # Validate date order
        if start > end:
            raise ValueError(f"start_date ({start_date_str}) must be before or equal to end_date ({end_date_str})")

        return start, end

    @classmethod
    def to_start_date_end_date(
        cls, value: str, tz_string: str
    ) -> tuple[datetime.datetime, datetime.datetime] | tuple[None, None]:
        """
        Convert a DateFilter string to a (start_date, end_date) tuple of timezone-aware datetimes.

        Returns (None, None) if the value cannot be parsed.
        """
        num_of_last_days = cls.get_num_last_days(value=value)
        if num_of_last_days is None:
            return None, None

        # NOTE: get start and end days
        # end_date -> current date
        # start_date -> start_date - num_of_last_days
        # return start_date, end_date

        tz = cls.get_timezone(tz_string)

        end_date = datetime.datetime.now(tz)  # current date
        start_date = end_date - datetime.timedelta(num_of_last_days)  # current date - last_n_days

        return start_date, end_date


def _safe_int(value: str) -> int | None:
    """Safely convert a string to int, returning None if conversion fails."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def get_filter_options(request: Request) -> dict[str, list[int]]:
    """Return AND filter options parsed from request query parameters as {field: [int, ...]}."""
    return {field: sorted(values) for field in FILTER_FIELDS if (values := _collect_int_params(request, field))}


def get_or_filter_options(request: Request) -> dict[str, list[int]]:
    """Return OR filter options parsed from or__<field> request query parameters as {field: [int, ...]}."""
    return {
        field: sorted(values) for field in FILTER_FIELDS if (values := _collect_int_params(request, f"or__{field}"))
    }


def get_job_type_filter(request: Request) -> models.Q:
    """Filter by requested Controller models, treating unclassified legacy rows as jobs.

    Sync and workflow launches are stored but hidden from every dashboard view; rows whose
    launch_type is still NULL (not yet updated) stay visible.
    """
    job_types = [value.strip() for value in request.query_params.getlist("job_type") if value.strip()]
    if not job_types:
        job_types = ["job"]
    job_type_filter = models.Q(job_type__in=job_types)
    if "job" in job_types:
        job_type_filter |= models.Q(job_type__isnull=True)
    return job_type_filter & ~models.Q(launch_type__in=HIDDEN_LAUNCH_TYPES)


def apply_or_filters(request: Request, queryset: QuerySet[JobData]) -> QuerySet[JobData]:
    """Apply OR-combined filter conditions from or__<field> query params to the queryset."""
    or_options = get_or_filter_options(request=request)
    if not or_options:
        return queryset

    q = models.Q()
    for field, values in or_options.items():
        if field == "label":
            q |= models.Q(id__in=label_ids_to_job_data_ids(values))
        elif field == "organization":
            q |= models.Q(organization_id__in=values)
        elif field == "template":
            q |= models.Q(template_id__in=values)
        elif field == "project":
            q |= models.Q(project_id__in=values)

    return queryset.filter(q)


def _collect_int_params(request: Request, key: str) -> list[int]:
    """Collect all integer values for a given query parameter key, skipping non-integer values."""
    return [v for value in request.query_params.getlist(key) if (v := _safe_int(value)) is not None]


class CustomReportFilter(filters.BaseFilterBackend):
    """DRF filter backend that applies date range and AND/OR filters to a JobData queryset."""

    def filter_queryset(self, request: Request, queryset: QuerySet[JobData], view: APIView) -> QuerySet[JobData]:
        """
        Apply date range and AND/OR field filters from the request to the JobData queryset.

        Reads start_date/end_date from view.kwargs (injected by @require_date_range) and
        falls back to parsing the period/tz query params directly if not present.
        """
        # Reuse the already-parsed window injected by @require_date_range to avoid
        # calling now() a second time and potentially shifting the window mid-request.
        start_date = view.kwargs.get("start_date")
        end_date = view.kwargs.get("end_date")

        if start_date is None or end_date is None:
            period = request.query_params.get("period", None)
            tz = request.query_params.get("tz", "UTC") or "UTC"
            if period == DateFilter.CUSTOM.value:
                start_date_str = request.query_params.get("start_date")
                end_date_str = request.query_params.get("end_date")
                validate_custom_period_dates(start_date_str, end_date_str)
                try:
                    start_date, end_date = DateFilter.custom_range_to_start_date_end_date(
                        start_date_str=start_date_str, end_date_str=end_date_str, tz_string=tz
                    )
                except ValueError as exc:
                    raise ValidationError({"detail": str(exc)}) from exc
            else:
                start_date, end_date = DateFilter.to_start_date_end_date(value=period, tz_string=tz)

        queryset = queryset.after_date(start_date)
        queryset = queryset.before_date(end_date)
        queryset = queryset.filter(get_job_type_filter(request))

        # AND filters
        filter_options = get_filter_options(request)
        queryset = queryset.organizations(filter_options.get("organization", None))
        queryset = queryset.projects(filter_options.get("project", None))
        queryset = queryset.templates(filter_options.get("template", None))
        queryset = queryset.labels(filter_options.get("label", None))

        # OR filters
        queryset = apply_or_filters(request=request, queryset=queryset)

        return queryset
