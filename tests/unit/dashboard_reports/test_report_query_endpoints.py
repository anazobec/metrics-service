"""
Tests for the read-only POST report query endpoints (AAP-101932).

POST report/query/, report/details/query/ and report/export/query/ take the report filters in a
JSON body instead of the query string. They must return exactly what the matching GET endpoints
return, validate the body, be allowed for platform auditors, and never write anything.
"""

import datetime
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.dashboard_reports.models import JobData, JobLabel, JobStatusChoices, SubscriptionCost, TemplateMetadata
from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

# (GET url name, POST query url name)
ENDPOINTS = [
    ("dashboard_reports:report-list", "dashboard_reports:report-query"),
    ("dashboard_reports:report-details", "dashboard_reports:report-details-query"),
    ("dashboard_reports:report-export", "dashboard_reports:report-export-query"),
]
QUERY_URL_NAMES = [query for _, query in ENDPOINTS]

TODAY = datetime.datetime.now(datetime.UTC).date()

# Filter sets sent once as a query string (GET) and once as a JSON body (POST).
FILTER_CASES = {
    "no_filters": {},
    "period_last_7_days": {"period": "last_7_days", "tz": "UTC"},
    "custom_period_with_tz": {
        "period": "custom",
        "start_date": (TODAY - datetime.timedelta(days=4)).isoformat(),
        "end_date": TODAY.isoformat(),
        "tz": "Europe/Ljubljana",
    },
    "template": {"template": [1]},
    "organization": {"organization": [2]},
    "project": {"project": [10, 20]},
    "label": {"label": [100]},
    "combined_and_filters": {"template": [1, 2], "organization": [1], "label": [100, 200]},
    "or_filters": {"or__template": [1], "or__organization": [2]},
    "job_type": {"job_type": ["job", "workflowjob"]},
}


def _csv_rows(response) -> list[str]:
    return response.content.decode("utf-8").splitlines()


def _payload(response) -> Any:
    """Comparable response payload: CSV lines for exports, parsed JSON otherwise."""
    if response["Content-Type"].startswith("text/csv"):
        return _csv_rows(response)
    return response.json()


def _write_queries(captured: CaptureQueriesContext) -> list[str]:
    return [
        q["sql"]
        for q in captured.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


@pytest.fixture
def report_jobs(db) -> list[JobData]:
    """
    Jobs across two templates, organizations, projects and labels, plus one workflow job.

    No SubscriptionCost row is created: the query actions must work (with the default values)
    without writing one, which TestQueryEndpointsHaveNoSideEffects checks on the empty table.
    """
    template_a, template_b = TemplateMetadata.objects.bulk_create(
        [
            TemplateMetadata(template_id=1, template_name="Template A", time_taken_manually_execute_minutes=30),
            TemplateMetadata(template_id=2, template_name="Template B", time_taken_manually_execute_minutes=60),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    configs = [
        # job_id, template, org, project, status, days_ago, job_type, labels
        (1, template_a, 1, 10, JobStatusChoices.SUCCESSFUL, 1, "job", [100]),
        (2, template_a, 1, 10, JobStatusChoices.FAILED, 2, "job", [100, 200]),
        (3, template_b, 2, 20, JobStatusChoices.SUCCESSFUL, 3, "job", [200]),
        (4, template_b, 2, 20, JobStatusChoices.SUCCESSFUL, 10, None, []),
        (5, template_b, 2, 20, JobStatusChoices.SUCCESSFUL, 2, "workflowjob", []),
    ]
    jobs = []
    for job_id, tmpl, org_id, project_id, job_status, days_ago, job_type, labels in configs:
        job = JobData.objects.create(
            job_id=job_id,
            template_name=tmpl.template_name,
            template_id=tmpl.template_id,
            template_metadata=tmpl,
            organization_id=org_id,
            organization_name=f"Org {org_id}",
            project_id=project_id,
            project_name=f"Project {project_id}",
            status=job_status,
            started=now - datetime.timedelta(days=days_ago, minutes=5),
            finished=now - datetime.timedelta(days=days_ago),
            elapsed=120,
            num_hosts=2,
            launched_by_id=job_id,
            launched_by_username=f"user{job_id}",
            job_type=job_type,
        )
        JobLabel.objects.bulk_create([JobLabel(job_data=job, label_id=label_id) for label_id in labels])
        jobs.append(job)
    return jobs


@pytest.mark.unit
@pytest.mark.django_db
class TestQueryEndpointsMatchGet:
    """The POST query endpoints return the same results as the matching GET endpoints."""

    @pytest.mark.parametrize("get_name,post_name", ENDPOINTS, ids=["report", "details", "export"])
    @pytest.mark.parametrize("filters", list(FILTER_CASES.values()), ids=list(FILTER_CASES))
    def test_same_result_as_get(self, admin_client, report_jobs, get_name, post_name, filters):
        get_response = admin_client.get(reverse(get_name), data=filters)
        post_response = admin_client.post(reverse(post_name), data=filters, format="json")

        assert get_response.status_code == 200
        assert post_response.status_code == 200
        assert _payload(post_response) == _payload(get_response)

    @pytest.mark.parametrize("report_type", ["summary", "roi", "trends"])
    def test_export_report_types_match_get(self, admin_client, report_jobs, report_type):
        filters = {"template": [1], "report_type": report_type, "export_format": "csv"}
        get_response = admin_client.get(reverse("dashboard_reports:report-export"), data=filters)
        post_response = admin_client.post(reverse("dashboard_reports:report-export-query"), data=filters, format="json")

        assert post_response.status_code == 200
        assert post_response["Content-Type"].startswith("text/csv")
        assert post_response["Content-Disposition"] == get_response["Content-Disposition"]
        assert _csv_rows(post_response) == _csv_rows(get_response)

    def test_body_filters_are_applied(self, admin_client, report_jobs):
        """Guard against the comparison above passing because filters are ignored on both sides."""
        url = reverse("dashboard_reports:report-query")
        unfiltered = admin_client.post(url, data={}, format="json").json()
        filtered = admin_client.post(url, data={"template": [1]}, format="json").json()

        assert unfiltered["count"] == 2
        assert filtered["count"] == 1
        assert filtered["results"][0]["template_name"] == "Template A"

    @pytest.mark.parametrize("field", ["template", "organization", "label", "project", "or__template"])
    def test_empty_id_list_is_the_same_as_omitting_the_field(self, admin_client, report_jobs, field):
        """An empty explicit list means no filter (as on GET, which cannot express an empty list)."""
        url = reverse("dashboard_reports:report-details-query")
        omitted = admin_client.post(url, data={}, format="json").json()
        empty = admin_client.post(url, data={field: []}, format="json").json()

        assert empty == omitted
        assert empty["total_number_of_job_runs"] > 0

    def test_query_string_filters_are_ignored_on_post(self, admin_client, report_jobs):
        """On POST, the body is the only filter source; filters in the query string have no effect."""
        url = reverse("dashboard_reports:report-query")
        response = admin_client.post(f"{url}?template=1", data={}, format="json")

        assert response.json()["count"] == 2

    def test_empty_body_uses_defaults(self, admin_client, report_jobs):
        """A POST without a body behaves like a GET without query parameters."""
        get_response = admin_client.get(reverse("dashboard_reports:report-details"))
        post_response = admin_client.post(reverse("dashboard_reports:report-details-query"))

        assert post_response.status_code == 200
        assert post_response.json() == get_response.json()

    def test_null_values_are_treated_as_missing(self, admin_client, report_jobs):
        """Every field accepts null as 'not given', scalars and lists alike."""
        null_body = {
            "period": None,
            "tz": None,
            "start_date": None,
            "end_date": None,
            "job_type": None,
            "template": None,
            "or__label": None,
            "select_all_template": None,
            "search_template": None,
            "excluded_label": None,
            "excluded_search_project": None,
        }
        get_response = admin_client.get(reverse("dashboard_reports:report-details"))
        post_response = admin_client.post(
            reverse("dashboard_reports:report-details-query"), data=null_body, format="json"
        )

        assert post_response.status_code == 200
        assert post_response.json() == get_response.json()


@pytest.mark.unit
@pytest.mark.django_db
class TestQueryEndpointPaginationAndOrdering:
    """Pagination and ordering are read from the query string on POST report/query/."""

    @pytest.mark.parametrize(
        "query_string",
        [
            "page_size=1",
            "page_size=1&page=2",
            "ordering=-successful_runs",
            "order_by=-successful_runs",
            "ordering=template_name&page_size=1&page=2",
            "count_disabled=1",
        ],
    )
    def test_pagination_and_ordering_match_get(self, admin_client, report_jobs, query_string):
        get_response = admin_client.get(f"{reverse('dashboard_reports:report-list')}?{query_string}")
        post_response = admin_client.post(
            f"{reverse('dashboard_reports:report-query')}?{query_string}", data={}, format="json"
        )

        assert post_response.status_code == 200
        assert post_response.json()["results"] == get_response.json()["results"]

    @pytest.mark.parametrize("query_string", ["ordering=-successful_runs", "order_by=template_name"])
    def test_export_ordering_matches_get(self, admin_client, report_jobs, query_string):
        """POST export/query/ sorts the CSV by the ordering in the query string, like GET export/."""
        get_response = admin_client.get(f"{reverse('dashboard_reports:report-export')}?{query_string}")
        post_response = admin_client.post(
            f"{reverse('dashboard_reports:report-export-query')}?{query_string}", data={}, format="json"
        )
        unordered = admin_client.post(reverse("dashboard_reports:report-export-query"), data={}, format="json")

        assert _csv_rows(post_response) == _csv_rows(get_response)
        assert sorted(_csv_rows(post_response)) == sorted(_csv_rows(unordered))
        if "successful_runs" in query_string:
            # Template B has more successful runs, so it moves ahead of the default name order.
            assert _csv_rows(post_response) != _csv_rows(unordered)

    def test_page_size_limits_results(self, admin_client, report_jobs):
        response = admin_client.post(f"{reverse('dashboard_reports:report-query')}?page_size=1", data={}, format="json")
        data = response.json()

        assert data["count"] == 2
        assert len(data["results"]) == 1
        assert "page=2" in data["next"]

    def test_ordering_is_applied(self, admin_client, report_jobs):
        url = reverse("dashboard_reports:report-query")
        ascending = admin_client.post(f"{url}?ordering=template_name", data={}, format="json").json()["results"]
        descending = admin_client.post(f"{url}?ordering=-template_name", data={}, format="json").json()["results"]

        assert [row["template_name"] for row in ascending] == ["Template A", "Template B"]
        assert [row["template_name"] for row in descending] == ["Template B", "Template A"]


@pytest.mark.unit
@pytest.mark.django_db
class TestQueryEndpointBodyValidation:
    """Invalid bodies return 400 with a clear message on all three query endpoints."""

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_unknown_key_returns_400(self, admin_client, url_name):
        response = admin_client.post(reverse(url_name), data={"templates": [1], "page": 2}, format="json")

        assert response.status_code == 400
        assert response.json() == {"page": ["Unknown parameter."], "templates": ["Unknown parameter."]}

    def test_unknown_key_reported_even_if_a_field_is_invalid(self, admin_client):
        """Unknown keys are checked first: they are reported even when a known field is also invalid."""
        response = admin_client.post(
            reverse("dashboard_reports:report-query"), data={"foo": 1, "template": "x"}, format="json"
        )

        assert response.status_code == 400
        assert "foo" in response.json()

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    @pytest.mark.parametrize(
        "body,field",
        [
            ({"template": "1"}, "template"),
            ({"template": ["a"]}, "template"),
            ({"organization": [1.5]}, "organization"),
            ({"label": {"id": 1}}, "label"),
            ({"or__project": "x"}, "or__project"),
            ({"job_type": "job"}, "job_type"),
            ({"period": "last_999_days"}, "period"),
            ({"period": "custom", "start_date": "2026-13-01", "end_date": "2026-10-31"}, "start_date"),
            ({"tz": ["UTC"]}, "tz"),
            ({"report_type": "pdf"}, "report_type"),
            ({"export_format": "pdf"}, "export_format"),
        ],
        ids=lambda value: value if isinstance(value, str) else None,
    )
    def test_wrong_type_returns_400(self, admin_client, url_name, body, field):
        response = admin_client.post(reverse(url_name), data=body, format="json")

        assert response.status_code == 400
        assert field in response.json()

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    @pytest.mark.parametrize("fmt", ["multipart", None])
    def test_non_json_body_returns_415(self, admin_client, url_name, fmt):
        """The query actions only take a JSON body."""
        if fmt is None:
            response = admin_client.post(
                reverse(url_name), data="template=1", content_type="application/x-www-form-urlencoded"
            )
        else:
            response = admin_client.post(reverse(url_name), data={"template": "1"}, format=fmt)

        assert response.status_code == 415

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_non_object_body_returns_400(self, admin_client, url_name):
        response = admin_client.post(reverse(url_name), data=[1, 2], format="json")

        assert response.status_code == 400

    @pytest.mark.parametrize(
        "start_date",
        [
            "{iso}",
            "{year}-{month}-{day}",  # no zero padding
            "{basic}",  # YYYYMMDD, accepted by date.fromisoformat
            "{iso}T00:00",
            " {iso}",
            "{day}.{month}.{year}",
            "not-a-date",
        ],
    )
    def test_dates_accepted_exactly_like_get(self, admin_client, report_jobs, start_date):
        """GET and POST accept and reject the same date strings (both use date.fromisoformat)."""
        day = TODAY - datetime.timedelta(days=3)
        value = start_date.format(
            iso=day.isoformat(), basic=day.strftime("%Y%m%d"), year=day.year, month=day.month, day=day.day
        )
        params = {"period": "custom", "start_date": value, "end_date": TODAY.isoformat()}

        get_response = admin_client.get(reverse("dashboard_reports:report-details"), data=params)
        post_response = admin_client.post(reverse("dashboard_reports:report-details-query"), data=params, format="json")

        assert post_response.status_code == get_response.status_code
        if get_response.status_code == 200:
            assert post_response.json() == get_response.json()

    @pytest.mark.parametrize("tz", ["UTC", "Europe/Ljubljana", "", " UTC", "   ", "Not/AZone"])
    def test_timezone_accepted_exactly_like_get(self, admin_client, report_jobs, tz):
        """GET and POST accept and reject the same timezone strings (neither trims whitespace)."""
        get_response = admin_client.get(reverse("dashboard_reports:report-details"), data={"tz": tz})
        post_response = admin_client.post(
            reverse("dashboard_reports:report-details-query"), data={"tz": tz}, format="json"
        )

        assert post_response.status_code == get_response.status_code
        if get_response.status_code == 200:
            assert post_response.json() == get_response.json()

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_custom_period_without_dates_returns_400(self, admin_client, url_name):
        response = admin_client.post(reverse(url_name), data={"period": "custom"}, format="json")

        assert response.status_code == 400
        assert "start_date and end_date are required" in response.content.decode()

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_invalid_timezone_returns_400(self, admin_client, url_name):
        response = admin_client.post(reverse(url_name), data={"tz": "Not/AZone"}, format="json")

        assert response.status_code == 400

    def test_export_error_is_json(self, admin_client):
        """Export errors are rendered as JSON, not as a garbled CSV body."""
        response = admin_client.post(reverse("dashboard_reports:report-export-query"), data={"bogus": 1}, format="json")

        assert response.status_code == 400
        assert response["Content-Type"] == "application/json"
        assert response.json() == {"bogus": ["Unknown parameter."]}


@pytest.mark.unit
@pytest.mark.django_db
class TestQueryEndpointPermissions:
    """Admin and platform auditor may call the query actions; other writes stay admin-only."""

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_admin_allowed(self, admin_client, report_jobs, url_name):
        assert admin_client.post(reverse(url_name), data={}, format="json").status_code == 200

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_platform_auditor_allowed(self, api_client, platform_auditor, report_jobs, url_name):
        api_client.force_authenticate(platform_auditor)
        assert api_client.post(reverse(url_name), data={}, format="json").status_code == 200

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_regular_user_forbidden(self, api_client, rando, url_name):
        api_client.force_authenticate(rando)
        assert api_client.post(reverse(url_name), data={}, format="json").status_code == 403

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_unauthenticated_forbidden(self, api_client, url_name):
        assert api_client.post(reverse(url_name), data={}, format="json").status_code in (401, 403)

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_platform_auditor_forbidden_on_other_unsafe_requests(self, api_client, platform_auditor, method):
        """Only the listed query actions are reads; any other unsafe request stays admin-only."""
        api_client.force_authenticate(platform_auditor)
        response = getattr(api_client, method)(reverse("dashboard_reports:report-list"), data={}, format="json")

        assert response.status_code == 403

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    @pytest.mark.parametrize("method", ["put", "patch", "delete"])
    def test_platform_auditor_forbidden_on_other_methods_of_query_routes(
        self, api_client, platform_auditor, url_name, method
    ):
        api_client.force_authenticate(platform_auditor)
        response = getattr(api_client, method)(reverse(url_name), data={}, format="json")

        assert response.status_code == 403

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_get_not_allowed_on_query_routes(self, admin_client, url_name):
        assert admin_client.get(reverse(url_name)).status_code == 405

    @pytest.mark.parametrize("action", ["query", "details_query", "export_query"])
    def test_options_describes_the_request_body(self, admin_user, action):
        """
        OPTIONS lists the ReportQuerySerializer body fields for POST, not the report's output fields.

        The view is called directly: the dev-mode CORS middleware answers every OPTIONS request itself.
        """
        view = DashboardReportViewSet.as_view({"post": action})
        request = APIRequestFactory().options("/", HTTP_ACCEPT="application/json")
        force_authenticate(request, user=admin_user)

        response = view(request)

        assert response.status_code == 200
        post_fields = response.data["actions"]["POST"]
        assert {"period", "template", "select_all_template", "search_template", "excluded_template"} <= set(post_fields)
        assert "template_name" not in post_fields


@pytest.mark.unit
@pytest.mark.django_db
class TestQueryEndpointsHaveNoSideEffects:
    """The query actions are reads: they never issue INSERT/UPDATE/DELETE."""

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_no_write_queries(self, api_client, platform_auditor, report_jobs, url_name):
        api_client.force_authenticate(platform_auditor)
        body = {"period": "last_30_days", "template": [1, 2], "label": [100], "report_type": "trends"}

        with CaptureQueriesContext(connection) as captured:
            response = api_client.post(reverse(url_name), data=body, format="json")

        assert response.status_code == 200
        assert _write_queries(captured) == []

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_no_rows_changed(self, admin_client, report_jobs, url_name):
        counts_before = (JobData.objects.count(), JobLabel.objects.count(), TemplateMetadata.objects.count())

        admin_client.post(reverse(url_name), data={"template": [1]}, format="json")

        assert (JobData.objects.count(), JobLabel.objects.count(), TemplateMetadata.objects.count()) == counts_before

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_no_subscription_cost_row_created(self, api_client, platform_auditor, report_jobs, url_name):
        """Without a configured SubscriptionCost, the query actions use the defaults and do not create the row."""
        assert not SubscriptionCost.objects.exists()
        api_client.force_authenticate(platform_auditor)

        response = api_client.post(reverse(url_name), data={"report_type": "summary"}, format="json")

        assert response.status_code == 200
        assert not SubscriptionCost.objects.exists()

    @pytest.mark.parametrize(("get_name", "post_name"), ENDPOINTS)
    def test_default_subscription_cost_matches_get(self, admin_client, report_jobs, get_name, post_name):
        """On the empty table the query actions return what GET returns with the default row it creates."""
        post_response = admin_client.post(reverse(post_name), data={"report_type": "summary"}, format="json")
        get_response = admin_client.get(reverse(get_name), data={"report_type": "summary"})

        assert SubscriptionCost.objects.exists()  # created by GET (SubscriptionCost.get()), not by POST
        assert _payload(post_response) == _payload(get_response)

    @pytest.mark.parametrize("url_name", QUERY_URL_NAMES)
    def test_configured_subscription_cost_is_used(self, admin_client, report_jobs, url_name):
        """An existing SubscriptionCost is read and used, not replaced by the defaults."""
        SubscriptionCost.objects.create(
            monthly_subscription_cost="1234.00",
            engineer_avg_hourly_rate="99.00",
            include_template_creation_time_in_costs=False,
        )
        get_name = dict((post, get) for get, post in ENDPOINTS)[url_name]

        post_response = admin_client.post(reverse(url_name), data={"report_type": "summary"}, format="json")
        get_response = admin_client.get(reverse(get_name), data={"report_type": "summary"})

        assert _payload(post_response) == _payload(get_response)
