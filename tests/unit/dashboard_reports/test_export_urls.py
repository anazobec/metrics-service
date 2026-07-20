"""Unit tests for DashboardReportViewSet export endpoint (summary, roi, trends)."""

import csv
import datetime
import decimal
import io
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.dashboard_reports.models import (
    JobData,
    JobStatusChoices,
    SubscriptionCost,
    TemplateMetadata,
)

FIXED_DAILY_SUBSCRIPTION_COST = decimal.Decimal("161.29")
FIXED_PER_SECOND_COST = decimal.Decimal("0.001")


def get_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def build_export_query(report_type: str = "summary", days_back: int = 14, **extra) -> dict:
    query = {
        "period": f"last_{days_back}_days",
        "tz": "UTC",
        "report_type": report_type,
        "export_format": "csv",
    }
    query.update(extra)
    return query


def parse_csv(content: bytes) -> list[list[str]]:
    """Parse CSV response content into a list of rows."""
    text = content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    return list(reader)


def currency_to_decimal(value: str) -> decimal.Decimal:
    return decimal.Decimal(value.replace("$", "").replace(",", ""))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def template_metadata():
    templates = [
        TemplateMetadata(
            template_id=1,
            template_name="Template A",
            time_taken_manually_execute_minutes=240,
            time_taken_create_automation_minutes=40,
        ),
        TemplateMetadata(
            template_id=2,
            template_name="Template B",
            time_taken_manually_execute_minutes=360,
            time_taken_create_automation_minutes=60,
        ),
    ]
    return TemplateMetadata.objects.bulk_create(templates)


@pytest.fixture
def job_data(template_metadata):
    """
    Two jobs for Template A (finished recently) and two for Template B (finished ~7 days ago).
    Mirrors the fixture in test_report_view_data.py for consistency.
    """
    now = get_now()
    job_configs = [
        (1, 0, 1, 10, "Project A", JobStatusChoices.SUCCESSFUL, datetime.timedelta(hours=3), 60, 10, 1, "test_user"),
        (2, 0, 1, 10, "Project A", JobStatusChoices.FAILED, datetime.timedelta(hours=2), 5, 10, 1, "test_user"),
        (
            3,
            1,
            2,
            20,
            "Project B",
            JobStatusChoices.FAILED,
            datetime.timedelta(days=7, hours=1),
            50,
            1,
            2,
            "other_user",
        ),
        (
            4,
            1,
            2,
            20,
            "Project B",
            JobStatusChoices.SUCCESSFUL,
            datetime.timedelta(days=7, hours=2),
            300,
            1,
            2,
            "other_user",
        ),
    ]

    jobs = []
    for job_id, tmpl_idx, org_id, proj_id, proj_name, status, offset, elapsed, hosts, user_id, username in job_configs:
        tmpl = template_metadata[tmpl_idx]
        jobs.append(
            JobData(
                job_id=job_id,
                template_name=tmpl.template_name,
                template_id=tmpl.template_id,
                project_id=proj_id,
                project_name=proj_name,
                organization_id=org_id,
                status=status,
                started=now - offset - datetime.timedelta(minutes=10),
                finished=now - offset,
                elapsed=elapsed,
                num_hosts=hosts,
                launched_by_id=user_id,
                launched_by_username=username,
                template_metadata=tmpl,
            )
        )
    return JobData.objects.bulk_create(jobs)


# =============================================================================
# Tests — PassthroughRenderer
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestPassthroughRenderer:
    """Tests for the PassthroughRenderer class."""

    def test_renderer_media_type(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

        assert PassthroughRenderer.media_type == "text/csv"

    def test_renderer_format(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

        assert PassthroughRenderer.format == "csv"

    def test_render_returns_data_as_is(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

        renderer = PassthroughRenderer()
        data = b"some,csv,data"
        assert renderer.render(data) == data

    def test_render_with_none_data(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRenderer

        renderer = PassthroughRenderer()
        assert renderer.render(None) is None


# =============================================================================
# Tests — export endpoint: general behaviour
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestExportEndpointGeneral:
    """Tests for general export endpoint behaviour (routing, headers, errors)."""

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_export_endpoint_resolves(self):
        from django.urls import resolve

        match = resolve("/api/v1/dashboard_reports/report/export/")
        assert match is not None

    def test_export_missing_period_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url)
        assert response.status_code == 400

    def test_export_invalid_period_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data={"period": "last_999_days", "tz": "UTC", "report_type": "summary"})
        assert response.status_code == 400

    def test_export_returns_200_with_valid_params(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200

    def test_export_content_type_is_csv(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]

    def test_export_content_disposition_is_attachment(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        assert "attachment" in response["Content-Disposition"]

    def test_export_filename_contains_report_type(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        for report_type in ("summary", "roi", "trends"):
            response = admin_client.get(url, data=build_export_query(report_type))
            assert response.status_code == 200
            assert report_type in response["Content-Disposition"]

    def test_export_filename_contains_date(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        assert today in response["Content-Disposition"]

    def test_export_invalid_report_type_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(
            url,
            data={"period": "last_14_days", "tz": "UTC", "report_type": "invalid_type", "export_format": "csv"},
        )
        assert response.status_code == 400

    def test_export_invalid_format_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(
            url,
            data={"period": "last_14_days", "tz": "UTC", "report_type": "summary", "export_format": "xml"},
        )
        assert response.status_code == 400

    def test_export_empty_data_returns_headers_only(self, admin_client):
        """When no JobData exists the CSV should still contain the header row."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 1  # header only

    def test_export_post_not_allowed(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.post(url, data={})
        assert response.status_code == 405

    def test_export_unauthenticated_returns_403(self, api_client):
        url = reverse("dashboard_reports:report-export")
        response = api_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 403


# =============================================================================
# Tests — export summary report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportSummaryCSV:
    """Tests for report_type=summary CSV output."""

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_summary_has_correct_headers_with_creation_time(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        expected_headers = [
            "Name",
            "Number of job executions",
            "Hosts executions",
            "Time taken to manually execute (minutes)",
            "Time taken to create automation (minutes)",
            "Running time (seconds)",
            "Running time",
            "Automated costs",
            "Manual costs",
            "Savings",
        ]
        assert rows[0] == expected_headers

    def test_summary_has_correct_headers_without_creation_time(self, job_data, admin_client):
        subscription_cost = SubscriptionCost.get()
        subscription_cost.include_template_creation_time_in_costs = False
        subscription_cost.save()
        try:
            url = reverse("dashboard_reports:report-export")
            response = admin_client.get(url, data=build_export_query("summary"))
            assert response.status_code == 200
            rows = parse_csv(response.content)
            assert "Time taken to create automation (minutes)" not in rows[0]
            assert "Name" in rows[0]
        finally:
            subscription_cost.include_template_creation_time_in_costs = True
            subscription_cost.save()

    def test_summary_returns_one_row_per_template(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 3  # header + 2 template rows

    def test_summary_recent_filter_returns_only_template_a(self, job_data, admin_client):
        """last_7_days should only include Template A jobs (finished recently)."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", days_back=7))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2
        assert rows[1][0] == "Template A"

    def test_summary_data_row_template_name(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", days_back=7))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert rows[1][0] == "Template A"

    def test_summary_data_row_runs(self, job_data, admin_client):
        """Template A has 2 runs (job_id 1 and 2)."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", days_back=7))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert rows[1][1] == "2"

    def test_summary_data_row_num_hosts(self, job_data, admin_client):
        """Template A: num_hosts = 10 + 10 = 20."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", days_back=7))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert rows[1][2] == "20"

    def test_summary_organization_filter(self, job_data, admin_client):
        """Filtering by organization=1 should return only Template A."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", organization=[1]))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2
        assert rows[1][0] == "Template A"

    def test_summary_project_filter(self, job_data, admin_client):
        """Filtering by project=20 should return only Template B."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", project=[20]))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2
        assert rows[1][0] == "Template B"

    def test_summary_template_filter(self, job_data, admin_client):
        """Filtering by template=2 should return only Template B."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("summary", template=[2]))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2
        assert rows[1][0] == "Template B"


# =============================================================================
# Tests — export ROI report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportROICSV:
    """Tests for report_type=roi CSV output."""

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_roi_has_correct_headers(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        expected_headers = [
            "Cost Savings",
            "Time Saved (hours)",
            "Automation Cost",
            "Manual Cost Equivalent",
            "ROI Percentage",
            "Automation Value",
        ]
        assert rows[0] == expected_headers

    def test_roi_returns_exactly_one_data_row(self, job_data, admin_client):
        """ROI is an aggregate — should always be exactly one data row."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2  # header + 1 aggregate row

    def test_roi_empty_data_returns_headers_and_zero_row(self, admin_client):
        """When no JobData exists the ROI row should contain zeros."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 2
        for value in rows[1]:
            assert float(currency_to_decimal(value)) == 0.0, f"Expected 0 for empty data, got {value}"

    def test_roi_cost_savings_is_positive_with_data(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert float(currency_to_decimal(rows[1][0])) > 0

    def test_roi_automation_value_equals_manual_cost_plus_savings(self, job_data, admin_client):
        """automation_value = manual_cost_equivalent + cost_savings."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        cost_savings = currency_to_decimal(rows[1][0])
        manual_cost = currency_to_decimal(rows[1][3])
        automation_value = currency_to_decimal(rows[1][5])
        assert abs(automation_value - (manual_cost + cost_savings)) < decimal.Decimal("0.01")

    def test_roi_roi_percentage_calculation(self, job_data, admin_client):
        """roi_percentage = (savings / automated_costs) * 100."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("roi"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        cost_savings = currency_to_decimal(rows[1][0])
        automation_cost = currency_to_decimal(rows[1][2])
        roi_percentage = decimal.Decimal(rows[1][4])
        if automation_cost > 0:
            expected_roi = round((cost_savings / automation_cost) * 100, 2)
            assert abs(roi_percentage - expected_roi) < decimal.Decimal("0.1")

    def test_roi_organization_filter_affects_totals(self, job_data, admin_client):
        """Filtering by organization should produce different totals than unfiltered."""
        url = reverse("dashboard_reports:report-export")
        response_all = admin_client.get(url, data=build_export_query("roi"))
        response_org1 = admin_client.get(url, data=build_export_query("roi", organization=[1]))
        assert response_all.status_code == 200
        assert response_org1.status_code == 200
        savings_all = float(currency_to_decimal(parse_csv(response_all.content)[1][0]))
        savings_org1 = float(currency_to_decimal(parse_csv(response_org1.content)[1][0]))
        assert savings_org1 < savings_all


# =============================================================================
# Tests — export trends report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportTrendsCSV:
    """Tests for report_type=trends CSV output."""

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_trends_has_correct_headers(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        expected_headers = [
            "Date",
            "Number of Job Executions",
            "Successful Runs",
            "Failed Runs",
            "Total Elapsed (seconds)",
            "Total Hosts",
        ]
        assert rows[0] == expected_headers

    def test_trends_returns_at_least_one_data_row(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) >= 2

    def test_trends_empty_data_returns_headers_only(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        assert len(rows) == 1

    def test_trends_date_column_is_valid_date_format(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        for row in rows[1:]:
            datetime.datetime.strptime(row[0], "%Y-%m-%d")

    def test_trends_runs_are_non_negative_integers(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        for row in rows[1:]:
            assert int(row[1]) >= 0
            assert int(row[2]) >= 0  # successful_runs
            assert int(row[3]) >= 0  # failed_runs

    def test_trends_total_runs_matches_job_count(self, job_data, admin_client):
        """The sum of runs across all date buckets should equal total job count."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        total_runs = sum(int(row[1]) for row in rows[1:])
        assert total_runs == 4

    def test_trends_successful_plus_failed_equals_total(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        for row in rows[1:]:
            assert int(row[2]) + int(row[3]) == int(row[1])

    def test_trends_rows_ordered_by_date_ascending(self, job_data, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends"))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        dates = [datetime.datetime.strptime(row[0], "%Y-%m-%d") for row in rows[1:]]
        assert dates == sorted(dates)

    def test_trends_granularity_matches_period(self, job_data, admin_client):
        """
        Granularity is derived from the period, not a separate query param.
        last_14_days → kind=day → two distinct date buckets (jobs today and ~7 days ago).
        """
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends", days_back=14))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        # With day granularity and jobs on two different days, expect 2 data rows
        assert len(rows) == 3  # header + 2 day buckets

    def test_trends_organization_filter(self, job_data, admin_client):
        """Filtering by org=1 should return only buckets from Template A jobs."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data=build_export_query("trends", organization=[1]))
        assert response.status_code == 200
        rows = parse_csv(response.content)
        total_runs = sum(int(row[1]) for row in rows[1:])
        assert total_runs == 2  # only job_id 1 and 2


@pytest.mark.unit
class TestCsvSafe:
    """Unit tests for DashboardReportViewSet._csv_safe()."""

    @pytest.fixture
    def csv_safe(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        return DashboardReportViewSet._csv_safe

    @pytest.mark.parametrize("value", ["=SUM(A1)", "+1", "-1", "@user", "\tcell", "\rcell"])
    def test_dangerous_prefixes_are_escaped(self, csv_safe, value):
        assert csv_safe(value).startswith("'")

    @pytest.mark.parametrize("value", ["=SUM(A1)", "+1", "-1", "@user", "\tcell", "\rcell"])
    def test_original_value_preserved_after_prefix(self, csv_safe, value):
        assert csv_safe(value) == f"'{value}"

    def test_normal_string_unchanged(self, csv_safe):
        assert csv_safe("Template A") == "Template A"

    def test_empty_string_unchanged(self, csv_safe):
        assert csv_safe("") == ""

    def test_non_string_unchanged(self, csv_safe):
        assert csv_safe(42) == 42
        assert csv_safe(3.14) == 3.14
        assert csv_safe(None) is None

    def test_string_starting_with_space_unchanged(self, csv_safe):
        assert csv_safe(" =not_a_formula") == " =not_a_formula"
