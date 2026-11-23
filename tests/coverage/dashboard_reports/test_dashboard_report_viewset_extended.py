"""
Extended tests for apps/dashboard_reports/viewsets/dashboard_report.py.

Targets uncovered lines:
- 74-83:  require_date_range decorator — start_date > end_date invalid range path
- 356-391: _get_date_range_and_kind(), _filter_raw_jobdata_queryset(), _prepare_chart_querysets()
- 416-450: get_chart_data(), _format_chart_result(), chart data formatting
- 464-473: _format_chart_result() items loop
- Line 470: details action (via API)
- 654+: details action (via API call)
- 735+: export action (via API call)
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "/api/v1/dashboard_reports/report/"


def _authenticated_client(user):
    """Return an APIClient authenticated as the given user."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# DashboardReportPagination — pinned page_size (regression test for the
# DefaultPaginator default-page-size drift: 25 expected, not ansible_base's own 50)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dashboard_report_pagination_page_size_matches_rest_framework_setting():
    from rest_framework.settings import api_settings

    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportPagination

    paginator = DashboardReportPagination()
    assert paginator.page_size == api_settings.PAGE_SIZE
    assert paginator.page_size == 25


# ---------------------------------------------------------------------------
# OpenAPI schema — list endpoint must document page_size/count_disabled/order_by
# the same way as /report/details/ and /report/export/ (regression test: the list
# action's extend_schema() previously had no `parameters=`, so drf-spectacular fell
# back to the generic auto-generated "ordering" description with no order_by mention)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_list_endpoint_schema_documents_pagination_and_order_by_alias():
    """List endpoint schema documents page_size, count_disabled and the order_by alias."""
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    list_op = schema["paths"]["/api/v1/dashboard_reports/report/"]["get"]
    params_by_name = {p["name"]: p for p in list_op["parameters"]}

    assert params_by_name["page_size"]["schema"]["default"] == 25
    assert "count_disabled" in params_by_name
    assert "order_by" in params_by_name["ordering"]["description"]


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/dashboard_reports/report/",
        "/api/v1/dashboard_reports/report/details/",
        "/api/v1/dashboard_reports/report/export/",
    ],
)
def test_report_schema_documents_period_as_optional_with_default(path):
    """period is optional on all report endpoints and documents last_30_days as its default."""
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    params_by_name = {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}

    assert not params_by_name["period"].get("required", False)
    assert params_by_name["period"]["schema"]["default"] == "last_30_days"


# ---------------------------------------------------------------------------
# require_date_range decorator — start_date > end_date path (lines 74-83)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_require_date_range_returns_error_when_start_after_end():
    """
    Directly invoke the decorator wrapper with a mocked view whose request supplies a
    valid period but then manually override start_date/end_date so that start > end.
    This exercises lines 109-114 (the start > end branch).
    """
    from apps.dashboard_reports.viewsets.dashboard_report import require_date_range

    # Build a sentinel that records whether the inner function was called
    called = []

    @require_date_range
    def fake_view(view, *args, **kwargs):
        called.append(True)
        return "reached"

    # Build a minimal view-like object
    view = MagicMock()
    view.request.query_params.get = lambda key, default=None: {
        "period": "last_7_days",
        "tz": "UTC",
    }.get(key, default)
    view.kwargs = {}

    # Patch parse_period_param to return start > end so we exercise that branch
    future = datetime(2025, 12, 31, tzinfo=UTC)
    past = datetime(2025, 1, 1, tzinfo=UTC)

    with patch(
        "apps.dashboard_reports.viewsets.dashboard_report.parse_period_param",
        return_value=(future, past, ""),  # start > end
    ):
        result = fake_view(view)

    # The inner function must NOT have been called
    assert not called
    # The result must be a DRF Response with 400 status
    from rest_framework.response import Response

    assert isinstance(result, Response)
    assert result.status_code == 400
    assert "start_date" in str(result.data) or "Invalid date range" in str(result.data)


@pytest.mark.unit
def test_require_date_range_injects_kwargs_when_valid():
    """
    When parse_period_param returns valid dates (start <= end), require_date_range
    must inject start_date / end_date / period / tz into view.kwargs and call the
    wrapped function (lines 117-122).
    """
    from apps.dashboard_reports.viewsets.dashboard_report import require_date_range

    called_with_kwargs = {}

    @require_date_range
    def fake_view(view, *args, **kwargs):
        called_with_kwargs.update(view.kwargs)
        return "ok"

    view = MagicMock()
    view.request.query_params.get = lambda key, default=None: {
        "period": "last_7_days",
        "tz": "UTC",
    }.get(key, default)
    view.kwargs = {}

    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 31, tzinfo=UTC)

    with patch(
        "apps.dashboard_reports.viewsets.dashboard_report.parse_period_param",
        return_value=(start, end, ""),
    ):
        result = fake_view(view)

    assert result == "ok"
    assert called_with_kwargs["start_date"] == start
    assert called_with_kwargs["end_date"] == end
    assert called_with_kwargs["period"] == "last_7_days"
    assert called_with_kwargs["tz"] == "UTC"


# ---------------------------------------------------------------------------
# _get_date_range_and_kind() — all four branches (lines 395-441)
# ---------------------------------------------------------------------------


def _make_viewset_with_dates(start: datetime, end: datetime):
    """Create a DashboardReportViewSet instance with start_date/end_date in kwargs."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()
    vs.kwargs = {"start_date": start, "end_date": end}
    return vs


@pytest.mark.unit
def test_get_date_range_and_kind_returns_none_when_no_kwargs():
    """When start_date/end_date are absent, return (None, None, None)."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()
    vs.kwargs = {}
    result = vs._get_date_range_and_kind()
    assert result == (None, None, None)


@pytest.mark.unit
def test_get_date_range_and_kind_hour():
    """Diff <= 1 day → kind == 'hour'."""
    start = datetime(2025, 5, 1, 8, 30, tzinfo=UTC)
    end = datetime(2025, 5, 1, 22, 0, tzinfo=UTC)
    vs = _make_viewset_with_dates(start, end)
    sd, ed, kind = vs._get_date_range_and_kind()
    assert kind == "hour"
    assert sd.minute == 0
    assert sd.second == 0


@pytest.mark.unit
def test_get_date_range_and_kind_day():
    """Diff > 1 day and <= 45 days → kind == 'day'."""
    start = datetime(2025, 5, 1, 8, 0, tzinfo=UTC)
    end = datetime(2025, 5, 20, 8, 0, tzinfo=UTC)
    vs = _make_viewset_with_dates(start, end)
    sd, ed, kind = vs._get_date_range_and_kind()
    assert kind == "day"
    assert sd.hour == 0
    assert sd.minute == 0


@pytest.mark.unit
def test_get_date_range_and_kind_month():
    """Diff > 45 days but <= 12 months → kind == 'month'."""
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 9, 1, tzinfo=UTC)
    vs = _make_viewset_with_dates(start, end)
    sd, ed, kind = vs._get_date_range_and_kind()
    assert kind == "month"
    assert sd.day == 1
    assert ed.day == 1


@pytest.mark.unit
def test_get_date_range_and_kind_year():
    """Diff > 12 months → kind == 'year'."""
    start = datetime(2023, 1, 1, tzinfo=UTC)
    end = datetime(2025, 6, 1, tzinfo=UTC)
    vs = _make_viewset_with_dates(start, end)
    sd, ed, kind = vs._get_date_range_and_kind()
    assert kind == "year"
    assert sd.month == 1
    assert sd.day == 1


# ---------------------------------------------------------------------------
# _filter_raw_jobdata_queryset() (lines 443-449)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_raw_jobdata_queryset_skips_ordering():
    """
    _filter_raw_jobdata_queryset must skip OrderingFilter and apply only
    CustomReportFilter.  Verify the loop calls filter_queryset on exactly the
    non-ordering backend and returns whatever that backend produces.
    """
    from apps.dashboard_reports.filters import CustomReportFilter
    from apps.dashboard_reports.viewsets.dashboard_report import AliasedOrderingFilter, DashboardReportViewSet

    vs = DashboardReportViewSet()
    vs.filter_backends = [CustomReportFilter, AliasedOrderingFilter]
    vs.request = MagicMock()
    vs.kwargs = {}

    sentinel_qs = MagicMock(name="filtered_qs")

    original_qs = MagicMock(name="original_qs")

    with patch.object(CustomReportFilter, "filter_queryset", return_value=sentinel_qs) as mock_filter:
        result = vs._filter_raw_jobdata_queryset(original_qs)

    mock_filter.assert_called_once_with(vs.request, original_qs, vs)
    assert result is sentinel_qs


# ---------------------------------------------------------------------------
# get_chart_data() — no dates path (lines 475-503)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_chart_data_no_dates_returns_empty():
    """When _get_date_range_and_kind returns None, get_chart_data returns empty structure."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()
    vs.kwargs = {}  # no dates injected

    result = vs.get_chart_data()
    assert result == {
        "host_chart": {"kind": "", "items": []},
        "job_chart": {"kind": "", "items": []},
    }


# ---------------------------------------------------------------------------
# _format_chart_result() — items loop (lines 464-473)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_chart_result_builds_items():
    """_format_chart_result should populate job_chart and host_chart items from the queryset rows."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()

    ts1 = datetime(2025, 5, 1, tzinfo=UTC)
    ts2 = datetime(2025, 5, 2, tzinfo=UTC)

    row1 = MagicMock()
    row1.term = ts1
    row1.runs = 10
    row1.hosts = 5

    row2 = MagicMock()
    row2.term = ts2
    row2.runs = 0
    row2.hosts = 0

    result = vs._format_chart_result([row1, row2])

    assert result["job_chart"]["items"] == [
        {"label": ts1, "value": 10},
        {"label": ts2, "value": 0},
    ]
    assert result["host_chart"]["items"] == [
        {"label": ts1, "value": 5},
        {"label": ts2, "value": 0},
    ]


@pytest.mark.unit
def test_format_chart_result_empty_queryset():
    """Empty date sequence queryset produces empty items lists."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()
    result = vs._format_chart_result([])
    assert result["job_chart"]["items"] == []
    assert result["host_chart"]["items"] == []


# ---------------------------------------------------------------------------
# get_chart_data() — with valid dates (kind set, calls _prepare_chart_querysets)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_chart_data_sets_kind_on_charts():
    """
    When _get_date_range_and_kind returns valid data, get_chart_data calls
    _prepare_chart_querysets and _format_chart_result, then attaches the correct 'kind'
    to both chart dicts.  We mock at the _format_chart_result boundary so we don't need
    to replicate the full generate_series QuerySet chain.
    """
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    start = datetime(2025, 5, 1, tzinfo=UTC)
    end = datetime(2025, 5, 10, tzinfo=UTC)

    vs = _make_viewset_with_dates(start, end)
    vs.request = MagicMock()
    vs.filter_backends = []

    # Fake the chart structure that _format_chart_result would produce
    fake_chart = {
        "job_chart": {"kind": "", "items": [{"label": start, "value": 3}]},
        "host_chart": {"kind": "", "items": [{"label": start, "value": 7}]},
    }

    with (
        patch.object(vs, "_prepare_chart_querysets", return_value=(MagicMock(), MagicMock())) as mock_prepare,
        patch.object(vs, "_format_chart_result", return_value=fake_chart),
        patch("apps.dashboard_reports.viewsets.dashboard_report.generate_series", return_value=MagicMock()),
    ):
        result = vs.get_chart_data()

    mock_prepare.assert_called_once()
    # get_chart_data must stamp the 'kind' on both sub-dicts after _format_chart_result
    assert result["job_chart"]["kind"] == "day"
    assert result["host_chart"]["kind"] == "day"
    assert len(result["job_chart"]["items"]) == 1
    assert result["job_chart"]["items"][0]["value"] == 3


# ---------------------------------------------------------------------------
# API — list endpoint (lines 379-384)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_list_without_period_defaults_to_last_30_days(user):
    """GET /report/ without ?period= should default to last_30_days."""
    client = _authenticated_client(user)
    response = client.get(BASE_URL)
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
def test_list_invalid_period_returns_400(user):
    """GET /report/?period=bad_value should return 400."""
    client = _authenticated_client(user)
    response = client.get(BASE_URL, {"period": "invalid_period"})
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.django_db
def test_list_unauthenticated_returns_401_or_403():
    """Unauthenticated GET should be rejected."""
    client = APIClient()
    response = client.get(BASE_URL, {"period": "last_30_days"})
    assert response.status_code in (401, 403)


@pytest.mark.unit
@pytest.mark.django_db
def test_list_valid_period_returns_200_empty_db(user):
    """GET /report/?period=last_30_days on empty DB returns 200 with empty results."""
    client = _authenticated_client(user)
    response = client.get(BASE_URL, {"period": "last_30_days", "tz": "UTC"})
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
def test_retrieve_returns_405(user):
    """GET /report/<pk>/ must return 405 (not supported)."""
    client = _authenticated_client(user)
    response = client.get(f"{BASE_URL}1/")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# API — details action (lines 654+)
# ---------------------------------------------------------------------------


DETAILS_URL = f"{BASE_URL}details/"


@pytest.mark.unit
@pytest.mark.django_db
def test_details_no_period_defaults_to_last_30_days(user):
    """GET /report/details/ without period defaults to last_30_days."""
    client = _authenticated_client(user)
    response = client.get(DETAILS_URL)
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
def test_details_invalid_period_returns_400(user):
    """GET /report/details/?period=garbage returns 400."""
    client = _authenticated_client(user)
    response = client.get(DETAILS_URL, {"period": "garbage"})
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.django_db
def test_details_valid_period_returns_200_empty_db(user):
    """
    GET /report/details/?period=last_7_days on an empty DB returns 200 with zeroed aggregates.
    This exercises the details action body (lines 657-732) including get_chart_data()
    but without real JobData rows so no PostgreSQL generate_series is called.
    """
    client = _authenticated_client(user)

    # Patch get_chart_data to avoid hitting PostgreSQL generate_series (docker dependency)
    with patch(
        "apps.dashboard_reports.viewsets.dashboard_report.DashboardReportViewSet.get_chart_data",
        return_value={
            "job_chart": {"kind": "day", "items": []},
            "host_chart": {"kind": "day", "items": []},
        },
    ):
        response = client.get(DETAILS_URL, {"period": "last_7_days", "tz": "UTC"})

    assert response.status_code == 200
    data = response.json()
    assert "total_number_of_job_runs" in data
    assert data["total_number_of_job_runs"] == 0
    assert "job_chart" in data
    assert "host_chart" in data


@pytest.mark.unit
@pytest.mark.django_db
def test_details_unauthenticated_returns_401_or_403():
    """Unauthenticated GET /report/details/ must be rejected."""
    client = APIClient()
    response = client.get(DETAILS_URL, {"period": "last_7_days"})
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# API — export action (lines 735+)
# ---------------------------------------------------------------------------


EXPORT_URL = f"{BASE_URL}export/"


@pytest.mark.unit
@pytest.mark.django_db
def test_export_no_period_defaults_to_last_30_days(user):
    """GET /report/export/ without period defaults to last_30_days."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL)
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
def test_export_invalid_export_format_returns_400(user):
    """GET /report/export/?period=last_7_days&export_format=pdf returns 400."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_7_days", "tz": "UTC", "export_format": "pdf"})
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.django_db
def test_export_invalid_report_type_returns_400(user):
    """GET /report/export/?period=last_7_days&report_type=unknown returns 400."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_7_days", "tz": "UTC", "report_type": "unknown"})
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.django_db
def test_export_summary_csv_empty_db(user):
    """GET /report/export/?period=last_7_days&report_type=summary returns CSV with header row."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_7_days", "tz": "UTC", "report_type": "summary"})
    assert response.status_code == 200
    assert "text/csv" in response.get("Content-Type", "")
    content = response.content.decode("utf-8")
    # Header row should be present even with empty data
    assert "Name" in content


@pytest.mark.unit
@pytest.mark.django_db
def test_export_roi_csv_empty_db(user):
    """GET /report/export/?period=last_7_days&report_type=roi returns CSV."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_7_days", "tz": "UTC", "report_type": "roi"})
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "Cost Savings" in content


@pytest.mark.unit
@pytest.mark.django_db
def test_export_trends_csv_empty_db(user):
    """GET /report/export/?period=last_7_days&report_type=trends returns CSV."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_7_days", "tz": "UTC", "report_type": "trends"})
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "Number of Job Executions" in content


@pytest.mark.unit
@pytest.mark.django_db
def test_export_content_disposition_header(user):
    """Export response must include Content-Disposition with an attachment filename."""
    client = _authenticated_client(user)
    response = client.get(EXPORT_URL, {"period": "last_30_days", "tz": "UTC", "report_type": "summary"})
    assert response.status_code == 200
    content_disp = response.get("Content-Disposition", "")
    assert "attachment" in content_disp
    assert "automation-dashboard" in content_disp


# ---------------------------------------------------------------------------
# _csv_safe static method (lines 505-513)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_csv_safe_prepends_quote_to_formula_start():
    """Strings starting with =, +, -, @, tab, or CR must be prefixed with a single quote."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    assert DashboardReportViewSet._csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert DashboardReportViewSet._csv_safe("+1234") == "'+1234"
    assert DashboardReportViewSet._csv_safe("-DROP TABLE") == "'-DROP TABLE"
    assert DashboardReportViewSet._csv_safe("@user") == "'@user"
    assert DashboardReportViewSet._csv_safe("\t tab") == "'\t tab"
    assert DashboardReportViewSet._csv_safe("\r cr") == "'\r cr"


@pytest.mark.unit
def test_csv_safe_passes_through_safe_values():
    """Safe strings must be returned unchanged; non-string types are untouched."""
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    assert DashboardReportViewSet._csv_safe("normal") == "normal"
    assert DashboardReportViewSet._csv_safe(42) == 42
    assert DashboardReportViewSet._csv_safe(None) is None
    assert DashboardReportViewSet._csv_safe("") == ""


# ---------------------------------------------------------------------------
# PassthroughRenderer attributes (lines 58-65)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_passthrough_renderer_media_type_and_format():
    """PassthroughRenderer must advertise 'text/csv' and format 'csv'."""
    from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

    r = PassthroughRenderer()
    assert r.media_type == "text/csv"
    assert r.format == "csv"


@pytest.mark.unit
def test_passthrough_renderer_accepts_extra_args():
    """render() must ignore accepted_media_type and renderer_context and return data unchanged."""
    from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

    r = PassthroughRenderer()
    data = b"hello,world"
    assert r.render(data, "text/csv", {"request": None}) == data


# ---------------------------------------------------------------------------
# get_serializer_class (lines 290-294)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_serializer_class_for_details_action():
    """get_serializer_class must return ReportDetailSerializer for the 'details' action."""
    from apps.dashboard_reports.serializers import ReportDetailSerializer, ReportSerializer
    from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

    vs = DashboardReportViewSet()
    vs.action = "details"
    assert vs.get_serializer_class() is ReportDetailSerializer

    vs.action = "list"
    assert vs.get_serializer_class() is ReportSerializer


# ---------------------------------------------------------------------------
# require_date_range — missing period defaults to last_30_days
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_require_date_range_missing_period_defaults_to_last_30_days():
    """require_date_range must default to last_30_days when period query param is absent."""
    from apps.dashboard_reports.viewsets.dashboard_report import require_date_range

    called = []

    @require_date_range
    def fake_view(view, *args, **kwargs):
        """Record that the decorator delegated to the wrapped view."""
        called.append(True)
        return "reached"

    view = MagicMock()
    view.request.query_params.get = lambda key, default=None: default
    view.kwargs = {}

    result = fake_view(view)

    assert called
    assert result == "reached"
    assert view.kwargs["period"] == "last_30_days"
    assert view.kwargs["start_date"] < view.kwargs["end_date"]


# ---------------------------------------------------------------------------
# API — list with all valid period values
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize("period", ["last_7_days", "last_14_days", "last_30_days", "last_60_days", "last_90_days"])
def test_list_all_valid_periods_return_200(user, period):
    """All valid DateFilter period values must return 200 on the list endpoint."""
    client = _authenticated_client(user)
    response = client.get(BASE_URL, {"period": period, "tz": "UTC"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# _get_date_range_and_kind — boundary: exactly 45 days (day/month boundary)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_date_range_and_kind_exactly_45_days_is_day():
    """45-day diff should still use 'day' granularity (boundary is <= 45 days)."""
    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=45)
    vs = _make_viewset_with_dates(start, end)
    _, _, kind = vs._get_date_range_and_kind()
    assert kind == "day"


@pytest.mark.unit
def test_get_date_range_and_kind_46_days_is_month():
    """46-day diff (> 45) should use 'month' granularity."""
    start = datetime(2025, 3, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=46)
    vs = _make_viewset_with_dates(start, end)
    _, _, kind = vs._get_date_range_and_kind()
    assert kind == "month"


@pytest.mark.unit
def test_get_date_range_and_kind_exactly_24h_uses_daily():
    # diff.days == 1 → daily, not hourly (intentional since PR #272)
    start = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 2, 0, 0, tzinfo=UTC)
    vs = _make_viewset_with_dates(start, end)
    _, _, kind = vs._get_date_range_and_kind()
    assert kind == "day"
