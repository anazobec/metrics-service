import datetime
import decimal
import unittest.mock
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.dashboard_reports.models import (
    JobData,
    JobHostSummary,
    JobLabel,
    JobStatusChoices,
    SubscriptionCost,
    TemplateMetadata,
)

# =============================================================================
# Helper functions for building test queries
# =============================================================================

_VALID_PERIOD_DAYS = frozenset({7, 14, 30, 60, 90})

# Fixed daily cost to ensure cost calculations are deterministic regardless of the current month.
# The per_second_subscription_cost() method uses the actual current month's day count,
# which causes test failures in months with fewer days (e.g. April=30, February=28).
# 161.29 = 5000 (default monthly_subscription_cost) / 31 days — matches the expected cost constants.
FIXED_DAILY_SUBSCRIPTION_COST = decimal.Decimal("161.29")
FIXED_PER_SECOND_SUBSCRIPTION_COST = FIXED_DAILY_SUBSCRIPTION_COST / decimal.Decimal(86400)


def get_now() -> datetime.datetime:
    """Get current datetime with UTC timezone."""
    return datetime.datetime.now().astimezone(datetime.UTC)


def build_date_query(days_back: int = 10, days_forward: int = 1, hours_forward: int = 0, **extra_filters) -> dict:
    """
    Build a query dict with date range and optional filters.

    Args:
        days_back: Number of days before now for start_date - must be one of 7, 14, 30, 60, 90
        days_forward: Number of days after now for end_date
        hours_forward: Number of hours after now for end_date (alternative to days_forward)
        **extra_filters: Additional query parameters (organization, template, project, label)

    Returns:
        Query dict ready to use with API client
    """
    if days_back not in _VALID_PERIOD_DAYS:
        raise ValueError(f"days_back must be one of {sorted(_VALID_PERIOD_DAYS)}, got {days_back}")

    now = get_now()
    period = f"last_{days_back}_days"
    query = {
        "period": period,
        "tz": "UTC",
        "start_date": (now - datetime.timedelta(days=days_back)).isoformat(),
        "end_date": (now + datetime.timedelta(days=days_forward, hours=hours_forward)).isoformat(),
    }
    query.update(extra_filters)
    return query


def build_filtered_query(**filters) -> dict:
    """Build query with full date range (14 days back, 1 day forward) and optional filters."""
    return build_date_query(days_back=14, days_forward=1, **filters)


def build_recent_query(**filters) -> dict:
    """Build query for recent data (last 7 days)."""
    return build_date_query(days_back=7, days_forward=0, hours_forward=1, **filters)


# =============================================================================
# Fixtures for test data setup
# =============================================================================


@pytest.fixture
def template_metadata():
    """Create template metadata for Template A and Template B."""
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
    Fixture for JobData test data.

    job_id 1 and 2: Template A, organization_id=1, project_id=10, labels 1 and 2, finished=now
    job_id 3 and 4: Template B, organization_id=2, project_id=20, no labels, finished=5 days ago

    All filters (date, organization, template, project, labels) return job_id 1 and 2.
    """
    now = get_now()

    # Job data configuration: (job_id, template_idx, org_id, proj_id, proj_name, status, time_offset, elapsed, num_hosts, user_id, username)
    job_configs = [
        # job_id 1: Template A, org=1, proj=10, successful, now
        (1, 0, 1, 10, "Project A", JobStatusChoices.SUCCESSFUL, datetime.timedelta(minutes=5), 60, 10, 1, "test_user"),
        # job_id 2: Template A, org=1, proj=10, failed, now
        (2, 0, 1, 10, "Project A", JobStatusChoices.FAILED, datetime.timedelta(minutes=1), 5, 10, 1, "test_user"),
        # job_id 3: Template B, org=2, proj=20, failed, 7 days ago
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
        # job_id 4: Template B, org=2, proj=20, successful, 7 days ago
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

    created_jobs = JobData.objects.bulk_create(jobs)

    # Labels only for job_id 1 and 2 (first two jobs)
    labels = [
        JobLabel(job_data=created_jobs[0], label_id=1),
        JobLabel(job_data=created_jobs[0], label_id=2),
        JobLabel(job_data=created_jobs[1], label_id=1),
        JobLabel(job_data=created_jobs[1], label_id=2),
    ]
    JobLabel.objects.bulk_create(labels)

    # Host summaries for job_id 1 and 2
    summaries = [
        JobHostSummary(host_summary_id=1, job_data=created_jobs[0], host_name="host1", host_id=1),
        JobHostSummary(host_summary_id=2, job_data=created_jobs[0], host_name="host2", host_id=2),
        JobHostSummary(host_summary_id=3, job_data=created_jobs[1], host_name="host1", host_id=1),
        JobHostSummary(host_summary_id=4, job_data=created_jobs[1], host_name="host3", host_id=3),
    ]
    JobHostSummary.objects.bulk_create(summaries)

    return created_jobs


# =============================================================================
# Expected data constants for Template A (job_id 1 and 2)
# =============================================================================
# Calculations (with default SubscriptionCost: cost_employee_per_minute=1.0, include_template_creation_time=True):
# - job_id 1: elapsed=60, num_hosts=10, status=successful
# - job_id 2: elapsed=5, num_hosts=10, status=failed
# - Total: runs=2, successful=1, failed=1, elapsed=65, num_hosts=20
# - automated_costs = (40 * 1.0) + (65 * ~0.00187) = 40.12
# - manual_costs = runs * manual_minutes * rate = 2 * 240 * 1.0 = 480.00
# - time_savings = (2 * 240 * 60) - 65 - (40 * 60) = 26335 sec

EXPECTED_TEMPLATE_A_DATA = {
    "template_name": "Template A",
    "id": 1,
    "time_taken_manually_execute_minutes": 240,
    "time_taken_create_automation_minutes": 40,
    "runs": 2,
    "successful_runs": 1,
    "failed_runs": 1,
    "elapsed": "65.00",
    "elapsed_str": "1min 5sec",
    "automated_costs": "40.12",
    "manual_costs": "480.00",
    "time_savings": "26335.00",
    "time_savings_str": "7h 18min 55sec",
    "savings": "439.88",
}

# =============================================================================
# Expected data constants for Template B (job_id 3 and 4)
# =============================================================================
# - job_id 3: elapsed=50, num_hosts=1, status=failed
# - job_id 4: elapsed=300, num_hosts=1, status=successful
# - Total: runs=2, successful=1, failed=1, elapsed=350, num_hosts=2

EXPECTED_TEMPLATE_B_DATA = {
    "template_name": "Template B",
    "id": 2,
    "time_taken_manually_execute_minutes": 360,
    "time_taken_create_automation_minutes": 60,
    "runs": 2,
    "successful_runs": 1,
    "failed_runs": 1,
    "elapsed": "350.00",
    "elapsed_str": "5min 50sec",
    "automated_costs": "60.65",
    "manual_costs": "720.00",
    "time_savings": "39250.00",
    "time_savings_str": "10h 54min 10sec",
    "savings": "659.35",
}

# =============================================================================
# Expected data for details endpoint (job_id 1 and 2)
# =============================================================================

EXPECTED_DETAILS_JOB_1_AND_2 = {
    "total_number_of_job_runs": 2,
    "total_number_of_successful_jobs": 1,
    "total_number_of_failed_jobs": 1,
    "total_number_of_host_job_runs": 20,
    "total_number_of_unique_hosts": 3,
    "total_hours_of_automation": 0.02,
    "cost_of_automated_execution": 40.12,
    "cost_of_manual_automation": 480.0,
    "total_saving": 439.88,
    "total_time_saving": 7.32,
    "top_users": [
        {"id": 1, "name": "test_user", "execution_count": 2},
    ],
    "top_projects": [
        {"id": 10, "name": "Project A", "execution_count": 2},
    ],
}

# =============================================================================
# Expected data for all jobs (unfiltered)
# =============================================================================

EXPECTED_DETAILS_ALL_JOBS = {
    "total_number_of_job_runs": 4,
    "total_number_of_successful_jobs": 2,
    "total_number_of_failed_jobs": 2,
    "total_number_of_host_job_runs": 22,
    "total_number_of_unique_hosts": 3,
    "total_hours_of_automation": 0.12,
    "cost_of_automated_execution": 100.77,
    "cost_of_manual_automation": 1200.0,
    "total_saving": 1099.23,
    "total_time_saving": 18.22,
    "top_users": [
        {"id": 1, "name": "test_user", "execution_count": 2},
        {"id": 2, "name": "other_user", "execution_count": 2},
    ],
    "top_projects": [
        {"id": 10, "name": "Project A", "execution_count": 2},
        {"id": 20, "name": "Project B", "execution_count": 2},
    ],
}

# =============================================================================
# Test cases for report view
# =============================================================================

TEST_REPORT_VIEW_CASES = [
    # 1. Unfiltered - returns all data
    pytest.param(
        build_filtered_query(),
        2,
        [EXPECTED_TEMPLATE_A_DATA, EXPECTED_TEMPLATE_B_DATA],
        id="unfiltered_all_data",
    ),
    # 2. Filtered by date - last 24 hours returns job_id 1 and 2
    pytest.param(
        build_recent_query(),
        1,
        [EXPECTED_TEMPLATE_A_DATA],
        id="filtered_by_date",
    ),
    # 3. Filtered by organization
    pytest.param(
        build_filtered_query(organization=[1]),
        1,
        [EXPECTED_TEMPLATE_A_DATA],
        id="filtered_by_organization",
    ),
    # 4. Filtered by template_id
    pytest.param(
        build_filtered_query(template=[1]),
        1,
        [EXPECTED_TEMPLATE_A_DATA],
        id="filtered_by_template",
    ),
    # 5. Filtered by project
    pytest.param(
        build_filtered_query(project=[10]),
        1,
        [EXPECTED_TEMPLATE_A_DATA],
        id="filtered_by_project",
    ),
    # 6. Filtered by labels
    pytest.param(
        build_filtered_query(label=[1]),
        1,
        [EXPECTED_TEMPLATE_A_DATA],
        id="filtered_by_labels",
    ),
]

# =============================================================================
# Test cases for report view details
# =============================================================================

TEST_REPORT_VIEW_DETAIL_CASES = [
    # 1. Unfiltered - returns all data (4 jobs)
    pytest.param(
        build_filtered_query(),
        EXPECTED_DETAILS_ALL_JOBS,
        id="unfiltered_all_data",
    ),
    # 2. Filtered by date - last 24 hours
    pytest.param(
        build_recent_query(),
        EXPECTED_DETAILS_JOB_1_AND_2,
        id="filtered_by_date",
    ),
    # 3. Filtered by organization
    pytest.param(
        build_filtered_query(organization=[1]),
        EXPECTED_DETAILS_JOB_1_AND_2,
        id="filtered_by_organization",
    ),
    # 4. Filtered by template_id
    pytest.param(
        build_filtered_query(template=[1]),
        EXPECTED_DETAILS_JOB_1_AND_2,
        id="filtered_by_template",
    ),
    # 5. Filtered by project
    pytest.param(
        build_filtered_query(project=[10]),
        EXPECTED_DETAILS_JOB_1_AND_2,
        id="filtered_by_project",
    ),
    # 6. Filtered by labels
    pytest.param(
        build_filtered_query(label=[1]),
        EXPECTED_DETAILS_JOB_1_AND_2,
        id="filtered_by_labels",
    ),
]


# =============================================================================
# Helper functions for test assertions
# =============================================================================


def assert_numeric_equal(actual, expected, key: str, tolerance: float = 0.01):
    """Assert that two numeric values are equal within tolerance."""
    if isinstance(actual, int | float) or hasattr(actual, "__float__"):
        actual = float(actual)
    if isinstance(expected, int | float):
        expected = float(expected)

    if isinstance(actual, float) and isinstance(expected, float):
        assert abs(actual - expected) < tolerance, f"Expected {key}='{expected}' in response, but got '{actual}'"
    else:
        assert actual == expected, f"Expected {key}='{expected}' in response, but got '{actual}'"


def assert_valid_iso_datetime(label: str):
    """Assert that label is a valid ISO datetime string."""
    assert isinstance(label, str), f"Expected label to be str, got {type(label)}"
    try:
        datetime.datetime.fromisoformat(label.replace("Z", "+00:00"))
    except ValueError:
        pytest.fail(f"Invalid ISO datetime format in label: {label}")


def assert_chart_item_valid(item: dict, chart_name: str):
    """Assert that a chart item has valid structure and values."""
    assert "label" in item, f"Missing 'label' in {chart_name} item"
    assert "value" in item, f"Missing 'value' in {chart_name} item"
    assert_valid_iso_datetime(item["label"])
    assert isinstance(item["value"], int), f"Expected value to be int, got {type(item['value'])}"


def assert_chart_structure(data: dict):
    """Assert that job_chart and host_chart have correct structure."""
    for chart_name in ["job_chart", "host_chart"]:
        assert chart_name in data, f"Missing {chart_name} in response"
        chart = data[chart_name]
        assert "kind" in chart, f"Missing 'kind' in {chart_name}"
        assert "items" in chart, f"Missing 'items' in {chart_name}"
        assert isinstance(chart["items"], list), f"{chart_name} items should be a list"

        if chart["items"]:
            assert_chart_item_valid(chart["items"][0], chart_name)


def assert_chart_data(
    chart: dict,
    chart_name: str,
    expected_kind: str,
    expected_total: int,
    expected_non_zero_count: int | None = None,
    expected_values: list[int] | None = None,
):
    """
    Assert chart data is correct.

    Args:
        chart: The chart dict with 'kind' and 'items'
        chart_name: Name for error messages ('job_chart' or 'host_chart')
        expected_kind: Expected value for 'kind' field
        expected_total: Expected sum of all item values
        expected_non_zero_count: Expected number of items with value > 0
        expected_values: Expected sorted list of non-zero values
    """
    assert chart["kind"] == expected_kind, f"Expected {chart_name} kind='{expected_kind}', got '{chart['kind']}'"

    items = chart["items"]
    assert len(items) > 0, f"Expected {chart_name} to have items"

    non_zero_items = [item for item in items if item["value"] > 0]
    total = sum(item["value"] for item in items)

    assert total == expected_total, f"Expected {chart_name} total={expected_total}, got {total}"

    if expected_non_zero_count is not None:
        assert len(non_zero_items) == expected_non_zero_count, (
            f"Expected {expected_non_zero_count} {chart_name} items with value > 0, got {len(non_zero_items)}"
        )

    if expected_values is not None:
        actual_values = sorted([item["value"] for item in non_zero_items])
        assert actual_values == expected_values, f"Expected {chart_name} values {expected_values}, got {actual_values}"

    # Validate each non-zero item
    for item in non_zero_items:
        assert_chart_item_valid(item, chart_name)
        assert item["value"] > 0, f"Expected positive value in non-zero {chart_name} items"

    return non_zero_items


# =============================================================================
# Test class for report view data (default settings)
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestReportViewData:
    FIXED_NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)

    @pytest.fixture(autouse=True)
    def fixed_now(self):
        """Pin time to a fixed noon UTC so tests never depend on the real clock."""
        mock_dt = unittest.mock.MagicMock(wraps=datetime)
        mock_dt.datetime.now = unittest.mock.Mock(return_value=self.FIXED_NOW)
        with (
            patch(f"{__name__}.get_now", return_value=self.FIXED_NOW),
            patch("apps.dashboard_reports.filters.datetime", mock_dt),
        ):
            yield

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_SUBSCRIPTION_COST,
        ):
            yield

    """Tests for report view with default SubscriptionCost settings."""

    @pytest.mark.parametrize("query, expected_count, expected_data", TEST_REPORT_VIEW_CASES)
    def test_report_view(self, job_data, query, expected_count, expected_data, admin_client):
        """Test report list endpoint with various filters."""
        url = reverse("v1:report-list")
        response = admin_client.get(url, data=query)

        assert response.status_code == 200
        data = response.data
        assert data["count"] == expected_count
        assert len(data["results"]) == expected_count

        for i, expected_item in enumerate(expected_data):
            for key, value in expected_item.items():
                assert key in data["results"][i], f"Missing key '{key}' in result {i}"
                assert data["results"][i][key] == value, (
                    f"Expected {key}='{value}' in result {i}, got '{data['results'][i][key]}'"
                )

    @pytest.mark.parametrize("query, expected_data", TEST_REPORT_VIEW_DETAIL_CASES)
    def test_report_view_details(self, job_data, query, expected_data, admin_client):
        """Test report details endpoint with various filters."""
        url = reverse("v1:report-details")
        response = admin_client.get(url, data=query)

        assert response.status_code == 200
        data = response.data

        # Verify expected values
        for key, value in expected_data.items():
            assert key in data, f"Missing key '{key}' in response"
            assert_numeric_equal(data[key], value, key)

        # Verify chart structure
        assert_chart_structure(data)

    def test_report_view_details_charts_data(self, job_data, admin_client):
        """Test that job_chart and host_chart contain correct data values for recent jobs."""
        url = reverse("v1:report-details")
        response = admin_client.get(url, data=build_recent_query())

        assert response.status_code == 200
        data = response.data

        # Verify job_chart: kind='day', total=2 (job_id 1 and 2)
        job_non_zero = assert_chart_data(
            data["job_chart"],
            "job_chart",
            expected_kind="day",
            expected_total=2,
        )

        # Verify host_chart: kind='hour', total=20 (num_hosts for job 1 and 2)
        host_non_zero = assert_chart_data(
            data["host_chart"],
            "host_chart",
            expected_kind="day",
            expected_total=20,
        )

        # Verify that job and host charts have same labels (same time buckets)
        job_labels = {item["label"] for item in job_non_zero}
        host_labels = {item["label"] for item in host_non_zero}
        assert job_labels == host_labels, f"Job and host chart labels should match: {job_labels} vs {host_labels}"

    def test_report_view_details_charts_unfiltered(self, job_data, admin_client):
        """Test job_chart and host_chart data for unfiltered (all jobs) query."""
        url = reverse("v1:report-details")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        data = response.data

        # Verify job_chart: kind='day', total=4, 2 days with 2 jobs each
        job_non_zero = assert_chart_data(
            data["job_chart"],
            "job_chart",
            expected_kind="day",
            expected_total=4,
            expected_non_zero_count=2,
            expected_values=[2, 2],
        )

        # Verify host_chart: kind='day', total=22, today=20 hosts, 5 days ago=2 hosts
        host_non_zero = assert_chart_data(
            data["host_chart"],
            "host_chart",
            expected_kind="day",
            expected_total=22,
            expected_non_zero_count=2,
            expected_values=[2, 20],
        )

        # Verify that job and host charts have same labels (same time buckets)
        job_labels = {item["label"] for item in job_non_zero}
        host_labels = {item["label"] for item in host_non_zero}
        assert job_labels == host_labels, f"Job and host chart labels should match: {job_labels} vs {host_labels}"


@pytest.mark.unit
@pytest.mark.django_db
class TestDashboardReportViewSetEndpoints:
    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_SUBSCRIPTION_COST,
        ):
            yield

    """Unit tests for DashboardReportViewSet list and details endpoints."""

    def test_list_missing_period_returns_400(self, admin_client):
        response = admin_client.get(reverse("v1:report-list"))
        assert response.status_code == 400

    def test_details_missing_period_returns_400(self, admin_client):
        response = admin_client.get(reverse("v1:report-details"))
        assert response.status_code == 400

    def test_details_invalid_period_returns_400(self, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data={"period": "invalid_period", "tz": "UTC"},
        )
        assert response.status_code == 400

    def test_list_invalid_timezone_falls_back_to_utc(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data={"period": "last_7_days", "tz": ""},
        )
        assert response.status_code == 200

    # List Endpoint
    def test_list_valid_period_returns_200(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        assert "count" in response.data
        assert "results" in response.data

    def test_list_response_contains_required_fields(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        result = response.data["results"][0]
        expected_fields = {
            "template_name",
            "id",
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
            "time_taken_manually_execute_minutes",
            "time_taken_create_automation_minutes",
        }
        assert expected_fields <= result.keys()

    def test_list_empty_when_no_data_in_range(self, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        assert response.data["count"] == 0

    def test_list_cost_annotation_with_creation_time(self, job_data, admin_client):
        """automated_costs includes template creation time when setting is True."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        result = response.data["results"][0]
        # With creation time: (40 * 1.0) + (65 * ~0.00187) = 40.12
        assert result["automated_costs"] == "40.12"

    def test_list_cost_annotation_without_creation_time(self, job_data, admin_client):
        """automated_costs excludes template creation time when setting is False."""
        subscription_cost = SubscriptionCost.get()
        subscription_cost.include_template_creation_time_in_costs = False
        subscription_cost.save()

        try:
            response = admin_client.get(
                reverse("v1:report-list"),
                data=build_recent_query(),
            )
            assert response.status_code == 200
            result = response.data["results"][0]
            # Without creation time: 65 * ~0.00187 = 0.12
            assert result["automated_costs"] == "0.12"
        finally:
            subscription_cost.include_template_creation_time_in_costs = True
            subscription_cost.save()

    # Pagination
    def test_list_default_page_size(self, job_data, admin_client):
        """With no page_size param, all rows fit on one page under the default PAGE_SIZE=25."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        assert response.data["count"] == 2
        assert len(response.data["results"]) == 2
        assert response.data["next"] is None

    def test_list_respects_page_size_param(self, job_data, admin_client):
        """page_size query param must limit results per page (DefaultPaginator wiring regression test)."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "page_size": 1},
        )
        assert response.status_code == 200
        assert response.data["count"] == 2
        assert len(response.data["results"]) == 1
        assert response.data["next"] is not None

    def test_list_page_size_second_page(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "page_size": 1, "page": 2},
        )
        assert response.status_code == 200
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["template_name"] == "Template B"
        assert response.data["next"] is None

    def test_list_pagination_links_are_relative(self, job_data, admin_client):
        """next/previous must be relative paths (DashboardReportPagination/DefaultPaginator), not absolute URIs."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "page_size": 1},
        )
        assert response.status_code == 200
        assert response.data["next"].startswith("/api/v1/dashboard_reports/report/")
        assert "://" not in response.data["next"]

    def test_list_count_disabled_omits_count_and_links(self, job_data, admin_client):
        """count_disabled query param must drop count/next/previous, returning only results."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "count_disabled": "true"},
        )
        assert response.status_code == 200
        assert set(response.data.keys()) == {"results"}
        assert len(response.data["results"]) == 2

    # Ordering
    def test_list_ordering_default_is_template_name_ascending(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template A", "Template B"]

    def test_list_ordering_param_descending(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "ordering": "-template_metadata__template_name"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template B", "Template A"]

    def test_list_order_by_alias_matches_ordering(self, job_data, admin_client):
        """`order_by` must behave identically to `ordering` (AliasedOrderingFilter regression test)."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "order_by": "-template_metadata__template_name"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template B", "Template A"]

    def test_list_order_by_alias_orders_by_savings(self, job_data, admin_client):
        """order_by=-savings differs from the default name-ascending order, proving it is actually applied."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "order_by": "-savings"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template B", "Template A"]

    def test_list_ordering_by_time_savings_ascending(self, job_data, admin_client):
        """ordering=time_savings sorts by time saved (manual_time - elapsed - creation_time) ascending."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "ordering": "time_savings"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        # Template A has less time savings than Template B
        assert names == ["Template A", "Template B"]

    def test_list_ordering_by_time_savings_descending(self, job_data, admin_client):
        """ordering=-time_savings sorts by time saved descending (highest savings first)."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "ordering": "-time_savings"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        # Template B has more time savings than Template A
        assert names == ["Template B", "Template A"]

    def test_list_ordering_by_template_name_does_not_fragment_aggregation(self, admin_client):
        """ordering=template_name must alias to template_metadata__template_name, not JobData.template_name.

        JobData.template_name is a per-job denormalized snapshot from AWX (can differ across a
        template rename), distinct from the template_metadata__template_name the report groups by.
        Ordering by the raw JobData column instead of the alias would silently widen the queryset's
        GROUP BY and split one template's aggregate into multiple rows.
        """
        now = get_now()
        template = TemplateMetadata.objects.create(
            template_id=100,
            template_name="Renamed Template",
            time_taken_manually_execute_minutes=240,
            time_taken_create_automation_minutes=40,
        )
        JobData.objects.bulk_create(
            [
                JobData(
                    job_id=101,
                    template_name="Old Template Name",
                    template_id=100,
                    project_id=10,
                    project_name="Project A",
                    organization_id=1,
                    status=JobStatusChoices.SUCCESSFUL,
                    started=now - datetime.timedelta(minutes=15),
                    finished=now - datetime.timedelta(minutes=10),
                    elapsed=60,
                    num_hosts=1,
                    launched_by_id=1,
                    launched_by_username="test_user",
                    template_metadata=template,
                ),
                JobData(
                    job_id=102,
                    template_name="Renamed Template",
                    template_id=100,
                    project_id=10,
                    project_name="Project A",
                    organization_id=1,
                    status=JobStatusChoices.SUCCESSFUL,
                    started=now - datetime.timedelta(minutes=5),
                    finished=now,
                    elapsed=60,
                    num_hosts=1,
                    launched_by_id=1,
                    launched_by_username="test_user",
                    template_metadata=template,
                ),
            ]
        )

        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_recent_query(organization=[1]), "ordering": "template_name"},
        )
        assert response.status_code == 200
        assert response.data["count"] == 1
        assert response.data["results"][0]["runs"] == 2

    def test_list_order_by_invalid_field_falls_back_to_default(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-list"),
            data={**build_filtered_query(), "order_by": "not_a_real_field"},
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template A", "Template B"]

    def test_list_ordering_takes_precedence_over_order_by_when_both_given(self, job_data, admin_client):
        """When both are supplied, `ordering` wins and `order_by` is silently ignored (documented precedence)."""
        response = admin_client.get(
            reverse("v1:report-list"),
            data={
                **build_filtered_query(),
                "ordering": "template_metadata__template_name",
                "order_by": "-savings",
            },
        )
        assert response.status_code == 200
        names = [r["template_name"] for r in response.data["results"]]
        assert names == ["Template A", "Template B"]

    # Details Endpoint
    def test_details_valid_period_returns_200(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_recent_query(),
        )
        assert response.status_code == 200

    def test_details_response_contains_required_fields(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        expected_fields = {
            "total_number_of_job_runs",
            "total_number_of_successful_jobs",
            "total_number_of_failed_jobs",
            "total_number_of_host_job_runs",
            "total_hours_of_automation",
            "cost_of_automated_execution",
            "cost_of_manual_automation",
            "total_saving",
            "total_time_saving",
            "total_number_of_unique_hosts",
            "top_users",
            "top_projects",
            "job_chart",
            "host_chart",
        }
        assert expected_fields <= set(response.data.keys())

    def test_details_aggregation_correct(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        data = response.data
        assert data["total_number_of_job_runs"] == 2
        assert data["total_number_of_successful_jobs"] == 1
        assert data["total_number_of_failed_jobs"] == 1
        assert data["total_number_of_host_job_runs"] == 20

    def test_details_aggregation_includes_jobs_without_template_metadata(self, job_data, admin_client):
        """Regression test for AAP-85129: card totals must count jobs whose template_metadata
        could not be resolved (template_metadata=None), otherwise the summary cards undercount
        relative to the raw job listing. The per-template breakdown (list endpoint) still
        excludes these rows, since it groups by template_metadata_id.
        """
        now = get_now()
        JobData.objects.create(
            job_id=999,
            template_name="orphan-template",
            template_id=None,
            organization_id=1,
            status=JobStatusChoices.SUCCESSFUL,
            started=now - datetime.timedelta(minutes=15),
            finished=now - datetime.timedelta(minutes=10),
            elapsed=120,
            num_hosts=5,
            template_metadata=None,
        )

        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        data = response.data
        # job_id 1 and 2 (both Template A) plus the new orphan row = 3 runs, 2 successful, 1 failed.
        assert data["total_number_of_job_runs"] == 3
        assert data["total_number_of_successful_jobs"] == 2
        assert data["total_number_of_failed_jobs"] == 1

    def test_details_chart_structure(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_recent_query(),
        )
        assert response.status_code == 200
        assert_chart_structure(response.data)

    def test_details_top_users_ordered_by_execution_count(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        users = response.data["top_users"]
        counts = [u["execution_count"] for u in users]
        assert counts == sorted(counts, reverse=True)

    def test_details_top_projects_ordered_by_execution_count(self, job_data, admin_client):
        response = admin_client.get(
            reverse("v1:report-details"),
            data=build_filtered_query(),
        )
        assert response.status_code == 200
        projects = response.data["top_projects"]
        counts = [p["execution_count"] for p in projects]
        assert counts == sorted(counts, reverse=True)


# =============================================================================
# Expected data when include_template_creation_time_in_costs=False
# =============================================================================
# When this setting is False:
# - automated_costs does NOT include template creation time cost
# - time_savings does NOT subtract template creation time

EXPECTED_TEMPLATE_A_NO_CREATION_TIME = {
    "template_name": "Template A",
    "id": 1,
    "time_taken_manually_execute_minutes": 240,
    "time_taken_create_automation_minutes": 40,
    "runs": 2,
    "successful_runs": 1,
    "failed_runs": 1,
    "elapsed": "65.00",
    "elapsed_str": "1min 5sec",
    "automated_costs": "0.12",
    "manual_costs": "480.00",
    "time_savings": "28735.00",
    "time_savings_str": "7h 58min 55sec",
    "savings": "479.88",
}

EXPECTED_DETAILS_NO_CREATION_TIME = {
    "total_number_of_job_runs": 2,
    "total_number_of_successful_jobs": 1,
    "total_number_of_failed_jobs": 1,
    "total_number_of_host_job_runs": 20,
    "total_number_of_unique_hosts": 3,
    "total_hours_of_automation": 0.02,
    "cost_of_automated_execution": 0.12,
    "cost_of_manual_automation": 480.0,
    "total_saving": 479.88,
    "total_time_saving": 7.98,
    "top_users": [
        {"id": 1, "name": "test_user", "execution_count": 2},
    ],
    "top_projects": [
        {"id": 10, "name": "Project A", "execution_count": 2},
    ],
}


# =============================================================================
# Test class for report view with include_template_creation_time_in_costs=False
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestReportViewDataNoCreationTime:
    """
    Tests for report view when SubscriptionCost.include_template_creation_time_in_costs=False.

    When this setting is False:
    - automated_costs does NOT include template creation time cost
    - time_savings does NOT subtract template creation time
    """

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_SUBSCRIPTION_COST,
        ):
            yield

    @pytest.fixture(autouse=True)
    def setup_subscription_cost(self):
        """Set include_template_creation_time_in_costs=False for all tests in this class."""
        subscription_cost = SubscriptionCost.get()
        original_value = subscription_cost.include_template_creation_time_in_costs
        subscription_cost.include_template_creation_time_in_costs = False
        subscription_cost.save()
        yield
        # Reset to original value after test
        subscription_cost.include_template_creation_time_in_costs = original_value
        subscription_cost.save()

    def test_report_view_no_creation_time(self, job_data, admin_client):
        """Test report view calculations when include_template_creation_time_in_costs=False."""
        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_recent_query())

        assert response.status_code == 200
        data = response.data
        assert data["count"] == 1

        # Verify calculations without template creation time
        result = data["results"][0]
        for key, value in EXPECTED_TEMPLATE_A_NO_CREATION_TIME.items():
            assert key in result, f"Missing key '{key}' in result"
            assert result[key] == value, f"Expected {key}='{value}', got '{result[key]}'"

    def test_report_view_details_no_creation_time(self, job_data, admin_client):
        """Test report details calculations when include_template_creation_time_in_costs=False."""
        url = reverse("v1:report-details")
        response = admin_client.get(url, data=build_recent_query())

        assert response.status_code == 200
        data = response.data

        # Verify calculations without template creation time
        for key, value in EXPECTED_DETAILS_NO_CREATION_TIME.items():
            assert key in data, f"Missing key '{key}' in response"
            assert_numeric_equal(data[key], value, key)

        # Verify chart structure
        assert_chart_structure(data)


# =============================================================================
# Tests for label_ids in report list response (AAP-79243)
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestReportViewLabelIds:
    """label_ids field is injected into each aggregated report row without inflating other aggregates."""

    FIXED_NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)

    @pytest.fixture(autouse=True)
    def fixed_now(self):
        mock_dt = unittest.mock.MagicMock(wraps=datetime)
        mock_dt.datetime.now = unittest.mock.Mock(return_value=self.FIXED_NOW)
        with (
            patch(f"{__name__}.get_now", return_value=self.FIXED_NOW),
            patch("apps.dashboard_reports.filters.datetime", mock_dt),
        ):
            yield

    @pytest.fixture(autouse=True)
    def fixed_subscription_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_SUBSCRIPTION_COST,
        ):
            yield

    def test_label_ids_in_report_response(self, job_data, admin_client):
        """Report list response includes label_ids for each template row."""
        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        results = response.data["results"]
        # job_data fixture creates labels 1 and 2 for Template A jobs (jobs 1 and 2)
        template_a_row = next(r for r in results if r["template_name"] == "Template A")
        assert sorted(template_a_row["label_ids"]) == [1, 2]

    def test_label_ids_empty_for_template_without_labels(self, job_data, admin_client):
        """Template rows with no JobLabel records get label_ids=[]."""
        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        results = response.data["results"]
        template_b_row = next(r for r in results if r["template_name"] == "Template B")
        assert template_b_row["label_ids"] == []

    def test_label_ids_do_not_inflate_run_counts(self, job_data, admin_client):
        """Adding label_ids via post-fetch injection must not inflate runs/successful_runs/failed_runs."""
        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        template_a_row = next(r for r in response.data["results"] if r["template_name"] == "Template A")
        # jobs 1 (successful) and 2 (failed) for Template A — each has 2 labels
        # without distinct injection, runs would be inflated to 4 (2 jobs × 2 labels)
        assert template_a_row["runs"] == 2
        assert template_a_row["successful_runs"] == 1
        assert template_a_row["failed_runs"] == 1

    def test_null_label_ids_excluded(self, job_data, admin_client):
        """JobLabel rows with label_id=None must not appear in label_ids (label_id is nullable)."""
        # Add a null-label_id record for one of Template A's jobs
        template_a_job = JobData.objects.get(job_id=1)
        JobLabel.objects.create(job_data=template_a_job, label_id=None)

        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        template_a_row = next(r for r in response.data["results"] if r["template_name"] == "Template A")
        assert None not in template_a_row["label_ids"]
        # Existing real labels (1, 2) still present
        assert sorted(template_a_row["label_ids"]) == [1, 2]

    def test_label_ids_scoped_to_report_date_range(self, job_data, template_metadata, admin_client):
        """Labels from jobs outside the report date range must not appear in label_ids."""
        # Create a job for Template A that finished 30 days ago (outside the 14-day window)
        now = self.FIXED_NOW
        old_job = JobData.objects.create(
            job_id=999,
            template_name="Template A",
            template_id=template_metadata[0].template_id,
            organization_id=1,
            status=JobStatusChoices.SUCCESSFUL,
            started=now - datetime.timedelta(days=30, minutes=10),
            finished=now - datetime.timedelta(days=30),
            elapsed=60,
            num_hosts=1,
            template_metadata=template_metadata[0],
        )
        # Attach label 99 — should NOT appear in the 14-day report
        JobLabel.objects.create(job_data=old_job, label_id=99)

        url = reverse("v1:report-list")
        response = admin_client.get(url, data=build_filtered_query())

        assert response.status_code == 200
        template_a_row = next(r for r in response.data["results"] if r["template_name"] == "Template A")
        assert 99 not in template_a_row["label_ids"]
        assert sorted(template_a_row["label_ids"]) == [1, 2]
