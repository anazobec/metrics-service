"""
Filter utilities for dashboard reports.

Provides date range filtering via DateFilter enum, AND/OR filter helpers for
organization, template, project and label query parameters, job-type filtering,
and the CustomReportFilter backend used by DashboardReportViewSet.

Report parameters are read through get_report_params(): from the query string for GET
requests and from the validated JSON body for the POST query actions, so both share one code path.
"""

import datetime
import functools
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from django.db import Error as DjangoDatabaseError
from django.db import models
from django.db.models import QuerySet
from django.http import QueryDict
from rest_framework import filters, serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.dashboard_reports.awx_queries import fetch_ids_matching_search
from apps.dashboard_reports.job_definition import dashboard_visibility_filter
from apps.dashboard_reports.models import JobData, label_ids_to_job_data_ids
from apps.tasks.utils import get_db_connection

logger = logging.getLogger(__name__)

FILTER_FIELDS: frozenset[str] = frozenset({"organization", "template", "label", "project"})

# Search terms are resolved against the AWX database, so their number and length are capped
# (in the query string here, in the POST body by ReportQuerySerializer).
MAX_SEARCH_TERMS = 100
MAX_SEARCH_TERM_LENGTH = 512


class ReportParams:
    """
    Report parameters taken from a validated POST query body.

    Exposes the same get()/getlist() interface as request.query_params, so the filter helpers
    and the period parsing read GET and POST parameters through one code path.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for key, or default when it is missing or null."""
        value = self._data.get(key)
        return default if value is None else value

    def getlist(self, key: str) -> list[Any]:
        """Return the values for key as a list (empty when missing or null)."""
        value = self._data.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]


def get_report_params(request: Any) -> ReportParams | QueryDict:
    """
    Return the source of report parameters for the request.

    The POST query actions attach the validated body as request.report_params; every other
    request reads the query string (request.query_params, or request.GET for a plain Django request).
    """
    params = getattr(request, "report_params", None)
    if isinstance(params, ReportParams):
        return params
    query_params = getattr(request, "query_params", None)
    return query_params if query_params is not None else request.GET


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
    """Return AND filter options parsed from the report parameters as {field: [int, ...]}."""
    return {field: sorted(values) for field in FILTER_FIELDS if (values := _collect_int_params(request, field))}


def get_or_filter_options(request: Request) -> dict[str, list[int]]:
    """Return OR filter options parsed from or__<field> report parameters as {field: [int, ...]}."""
    return {
        field: sorted(values) for field in FILTER_FIELDS if (values := _collect_int_params(request, f"or__{field}"))
    }


def get_job_type_filter(request: Request) -> models.Q:
    """Filter by requested Controller models, treating unclassified legacy rows as jobs.

    Sync and workflow launches are stored but hidden from every dashboard view; rows whose
    launch_type is still NULL (not yet updated) stay visible.
    """
    job_types = [value.strip() for value in get_report_params(request).getlist("job_type") if value.strip()]
    return dashboard_visibility_filter(job_types)


def apply_or_filters(request: Request, queryset: QuerySet[JobData]) -> QuerySet[JobData]:
    """Apply OR-combined filter conditions from or__<field> report parameters to the queryset."""
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
    """Collect all integer values for a given parameter key, skipping non-integer values."""
    return [v for value in get_report_params(request).getlist(key) if (v := _safe_int(value)) is not None]


@dataclass(frozen=True)
class FieldSelection:
    """
    Resolved selection for one filter field (organization, template, label or project).

    include: None means no positive filter on the field (every job, including historical runs of
        deleted items and rows with a NULL ID); a list keeps only jobs with one of those IDs, so an
        empty list matches nothing.
    exclude: IDs whose jobs are removed afterwards.
    """

    include: list[int] | None
    exclude: list[int]


class AWXSearchUnavailable(APIException):
    """The search terms of a report filter could not be resolved because the AWX database query failed."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Could not resolve the search terms against the AWX database. Try again later."
    default_code = "awx_search_unavailable"


def _bool_param(params: Any, key: str) -> bool:
    """Read a boolean parameter from the query string with the true values of DRF's BooleanField (which validates the POST body)."""
    value = params.get(key)
    if isinstance(value, bool):
        return value
    # BooleanField lower-cases strings before comparing them with TRUE_VALUES; do the same.
    return isinstance(value, str) and value.strip().lower() in serializers.BooleanField.TRUE_VALUES


def _search_terms(params: Any, key: str) -> list[str]:
    """
    Collect the non-blank search terms for a parameter key, stripped like the option endpoints' search.

    Enforces the same limits as the POST body (MAX_SEARCH_TERMS, MAX_SEARCH_TERM_LENGTH), so the
    query string cannot send more or longer terms to AWX; raises ValidationError (400) otherwise.
    """
    terms = [term for value in params.getlist(key) if (term := str(value).strip())]
    if len(terms) > MAX_SEARCH_TERMS:
        raise ValidationError({key: [f"Ensure this field has no more than {MAX_SEARCH_TERMS} elements."]})
    if any(len(term) > MAX_SEARCH_TERM_LENGTH for term in terms):
        raise ValidationError({key: [f"Ensure each search term has no more than {MAX_SEARCH_TERM_LENGTH} characters."]})
    return terms


def _resolve_field_selection(
    request: Request,
    field: str,
    explicit_ids: list[int],
    search_terms: list[str],
    excluded_search_terms: list[str],
    awx_connection: Callable[[], Any],
) -> FieldSelection:
    """
    Resolve the selection parameters of one field to a FieldSelection.

    With select_all_<field>: keep every job, then exclude excluded_<field> IDs and the items matching
    excluded_search_<field>. Without it: keep jobs whose ID is an explicit <field> ID or matches a
    search_<field> term, then apply the same exclusions. Without any positive parameter (no IDs, no
    search terms) there is no positive filter, as before. Search terms (already collected and validated
    by _search_terms) are resolved against AWX, through the connection returned by awx_connection
    (only called when there are search terms).
    """
    params = get_report_params(request)

    def matches(terms: list[str]) -> set[int]:
        if not terms:
            return set()
        try:
            return fetch_ids_matching_search(field, terms, db_connection=awx_connection())
        except (DjangoDatabaseError, psycopg.Error) as exc:
            # Only database errors mean AWX is unavailable; anything else is a bug and stays a 500.
            logger.exception(f"Error resolving {field} search terms against the AWX database")
            raise AWXSearchUnavailable() from exc

    if _bool_param(params, f"select_all_{field}") or not (explicit_ids or search_terms):
        include = None
    else:
        include = sorted(set(explicit_ids) | matches(search_terms))
    exclude = sorted(set(_collect_int_params(request, f"excluded_{field}")) | matches(excluded_search_terms))
    return FieldSelection(include=include, exclude=exclude)


def get_field_selections(request: Request) -> dict[str, FieldSelection]:
    """
    Return the resolved selection of every filter field, cached on the request.

    The report views apply CustomReportFilter several times per request (e.g. details and its
    chart data); caching keeps search terms from being resolved against AWX more than once.
    The selection is resolved per request, so select_all and search terms include items created later.
    """
    cached = getattr(request, "_report_field_selections", None)
    if isinstance(cached, dict):
        return cached
    filter_options = get_filter_options(request)
    # Collect (and validate) the search terms of every field before any of them opens the AWX connection.
    params = get_report_params(request)
    terms = {
        key: _search_terms(params, key)
        for field in FILTER_FIELDS
        for key in (f"search_{field}", f"excluded_search_{field}")
    }
    # One AWX connection (and liveness probe) per request, opened only if a field has search terms.
    awx_connection = functools.cache(lambda: get_db_connection("awx"))
    selections = {
        field: _resolve_field_selection(
            request,
            field,
            filter_options.get(field, []),
            terms[f"search_{field}"],
            terms[f"excluded_search_{field}"],
            awx_connection,
        )
        for field in FILTER_FIELDS
    }
    request._report_field_selections = selections
    return selections


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
            params = get_report_params(request)
            period = params.get("period", None)
            tz = params.get("tz", "UTC") or "UTC"
            if period == DateFilter.CUSTOM.value:
                start_date_str = params.get("start_date")
                end_date_str = params.get("end_date")
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

        # AND filters, including the selection parameters (select_all / search / excluded / excluded_search)
        selections = get_field_selections(request)
        if any(selection.include == [] for selection in selections.values()):
            # A selection that resolves to nothing (e.g. a search without matches) filters to nothing.
            return queryset.none()
        queryset = queryset.organizations(selections["organization"].include)
        queryset = queryset.projects(selections["project"].include)
        queryset = queryset.templates(selections["template"].include)
        queryset = queryset.labels(selections["label"].include)
        for field in ("organization", "project", "template", "label"):
            if selections[field].exclude:
                queryset = queryset.exclude_ids(field, selections[field].exclude)

        # OR filters
        queryset = apply_or_filters(request=request, queryset=queryset)

        return queryset
