"""
Tests for the report selection parameters (AAP-101932).

select_all_<field>, search_<field>, excluded_<field> and excluded_search_<field> for organization,
template, label and project, on the GET report endpoints and the POST query endpoints.
"""

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from django.db import OperationalError, connection
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from apps.dashboard_reports import awx_queries
from apps.dashboard_reports.awx_queries import (
    FILTER_FIELD_OPTION_QUERIES,
    AWXQuery,
    fetch_ids_matching_search,
    fetch_labels,
    fetch_organizations,
    fetch_projects,
    fetch_templates,
)
from apps.dashboard_reports.filters import FieldSelection, _bool_param, _search_terms, get_field_selections
from apps.dashboard_reports.models import JobData, JobLabel, JobStatusChoices, SubscriptionCost, TemplateMetadata
from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

FIELDS = ["organization", "template", "label", "project"]

# (GET url name, POST query url name)
ENDPOINTS = [
    ("dashboard_reports:report-list", "dashboard_reports:report-query"),
    ("dashboard_reports:report-details", "dashboard_reports:report-details-query"),
    ("dashboard_reports:report-export", "dashboard_reports:report-export-query"),
]
ENDPOINT_IDS = ["report", "details", "export"]

# Items that currently exist in AWX (what the filter dropdowns list). Template 9 was deleted in
# AWX but still has a historical run in JobData.
AWX_ROWS: dict[AWXQuery, list[tuple]] = {
    AWXQuery.ORGANIZATIONS: [(1, "Engineering"), (2, "Finance")],
    AWXQuery.TEMPLATES: [(1, "Deploy prod"), (2, "Deploy staging"), (3, "Backup prod")],
    AWXQuery.PROJECTS: [(10, "Infra"), (20, "Apps")],
    AWXQuery.LABELS: [(100, "prod", "Engineering"), (200, "test", "Engineering"), (300, "nightly", "Finance")],
}

# job_id: (template_id, organization_id, project_id, label_ids)
JOBS: dict[int, tuple[int | None, int | None, int | None, list[int]]] = {
    1: (1, 1, 10, [100]),
    2: (1, 1, 10, [100, 200]),
    3: (2, 2, 20, [200]),
    4: (3, 1, 20, [300]),
    5: (9, 2, 10, []),  # historical run of a template deleted in AWX
    6: (None, None, None, []),  # row without template / organization / project
}
ALL_JOBS = set(JOBS)


def fake_fetch_data_from_db(awx_query: AWXQuery, join_alias: str = "", **kwargs: Any) -> tuple[list[tuple], int]:
    """In-memory stand-in for the AWX query: case-insensitive substring match on the name, ordered by name."""
    search_str = kwargs.get("search_str")
    rows = [row for row in AWX_ROWS[awx_query] if not search_str or search_str.lower() in row[1].lower()]
    rows.sort(key=lambda row: row[1])
    total = len(rows)
    if kwargs.get("limit") is not None:
        offset = kwargs.get("offset", 0)
        rows = rows[offset : offset + kwargs["limit"]]
    return rows, total


def fake_fetch_ids_matching_search(field: str, search_terms: list[str], db_connection: Any) -> set[int]:
    """In-memory stand-in for the search resolution, with the same matching as fake_fetch_data_from_db."""
    awx_query = FILTER_FIELD_OPTION_QUERIES[field]
    return {row[0] for term in search_terms for row in fake_fetch_data_from_db(awx_query, search_str=term)[0]}


@pytest.fixture
def fake_awx():
    """
    Serve the filter option endpoints and the search resolution from the same fake AWX data.

    Yields the mocked search resolution. The real SQL of both is compared in TestSearchSqlOnPostgres.
    """
    with (
        patch.object(awx_queries, "fetch_data_from_db", side_effect=fake_fetch_data_from_db),
        patch(
            "apps.dashboard_reports.filters.fetch_ids_matching_search", side_effect=fake_fetch_ids_matching_search
        ) as resolve,
        patch("apps.dashboard_reports.filters.get_db_connection", return_value=MagicMock()),
        patch("apps.dashboard_reports.viewsets.filter_options.get_db_connection", return_value=MagicMock()),
    ):
        yield resolve


@pytest.fixture
def selection_jobs(db) -> None:
    """The jobs from JOBS, finished yesterday, one per user (launched_by_id == job_id)."""
    SubscriptionCost.get()
    metadata = {
        template_id: TemplateMetadata.objects.create(template_id=template_id, template_name=f"Template {template_id}")
        for template_id in (1, 2, 3, 9)
    }
    now = datetime.datetime.now(datetime.UTC)
    for job_id, (template_id, org_id, project_id, label_ids) in JOBS.items():
        job = JobData.objects.create(
            job_id=job_id,
            template_id=template_id,
            # Name stored at job time; for the deleted template it matches no AWX option.
            template_name="Old deploy prod" if template_id == 9 else f"Template {template_id}",
            template_metadata=metadata.get(template_id),
            organization_id=org_id,
            project_id=project_id,
            status=JobStatusChoices.SUCCESSFUL,
            started=now - datetime.timedelta(days=1, minutes=5),
            finished=now - datetime.timedelta(days=1),
            elapsed=60,
            num_hosts=1,
            launched_by_id=job_id,
            launched_by_username=f"user{job_id}",
            job_type="job",
        )
        JobLabel.objects.bulk_create([JobLabel(job_data=job, label_id=label_id) for label_id in label_ids])


@pytest.fixture(autouse=True)
def all_top_users():
    """Return every user in top_users, so it lists exactly the selected jobs."""
    with patch.object(DashboardReportViewSet, "TOP_RESULTS_LIMIT", len(JOBS) + 1):
        yield


def _post(client, url_name: str, body: dict[str, Any]):
    response = client.post(reverse(url_name), data=body, format="json")
    assert response.status_code == 200, response.content
    return response


def _payload(response) -> Any:
    if response["Content-Type"].startswith("text/csv"):
        return response.content.decode("utf-8").splitlines()
    return response.json()


def selected_jobs(client, body: dict[str, Any]) -> set[int]:
    """Job IDs kept by a selection, read from details top_users (one job per user)."""
    data = _post(client, "dashboard_reports:report-details-query", body).json()
    return {user["id"] for user in data["top_users"]}


def runs(client, body: dict[str, Any]) -> int:
    return _post(client, "dashboard_reports:report-details-query", body).json()["total_number_of_job_runs"]


def to_query_string(body: dict[str, Any]) -> dict[str, Any]:
    """The GET form of a body: booleans as 'true'/'false', lists as repeated parameters."""
    return {key: (str(value).lower() if isinstance(value, bool) else value) for key, value in body.items()}


@pytest.mark.unit
@pytest.mark.django_db
class TestSelectAll:
    """select_all_<field> behaves exactly like no filter on that field."""

    def test_unfiltered_contains_every_job(self, admin_client, selection_jobs):
        assert selected_jobs(admin_client, {}) == ALL_JOBS

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("get_name,post_name", ENDPOINTS, ids=ENDPOINT_IDS)
    def test_select_all_equals_unfiltered(self, admin_client, selection_jobs, field, get_name, post_name):
        unfiltered = _post(admin_client, post_name, {})
        select_all = _post(admin_client, post_name, {f"select_all_{field}": True})
        select_all_get = admin_client.get(reverse(get_name), data={f"select_all_{field}": "true"})

        assert _payload(select_all) == _payload(unfiltered)
        assert _payload(select_all_get) == _payload(unfiltered)

    @pytest.mark.parametrize("field", FIELDS)
    def test_select_all_includes_deleted_and_null_rows(self, admin_client, selection_jobs, field):
        assert selected_jobs(admin_client, {f"select_all_{field}": True}) == ALL_JOBS

    def test_current_awx_ids_would_drop_historical_runs(self, admin_client, selection_jobs):
        """Why select_all is needed: the current AWX template IDs miss the deleted template and NULL rows."""
        current_ids = [row[0] for row in AWX_ROWS[AWXQuery.TEMPLATES]]

        assert selected_jobs(admin_client, {"template": current_ids}) == {1, 2, 3, 4}
        assert selected_jobs(admin_client, {"select_all_template": True}) == ALL_JOBS

    def test_select_all_overrides_explicit_ids(self, admin_client, selection_jobs):
        assert selected_jobs(admin_client, {"select_all_template": True, "template": [2]}) == ALL_JOBS

    def test_select_all_false_is_ignored(self, admin_client, selection_jobs):
        assert selected_jobs(admin_client, {"select_all_template": False, "template": [2]}) == {3}

    def test_select_all_ignores_search_terms(self, admin_client, selection_jobs, fake_awx):
        """With select_all, search terms do not narrow the selection and are not resolved against AWX."""
        assert selected_jobs(admin_client, {"select_all_template": True, "search_template": ["staging"]}) == ALL_JOBS
        fake_awx.assert_not_called()


@pytest.mark.unit
@pytest.mark.django_db
class TestExclusions:
    """excluded_<field> removes exactly the given items; rows with a NULL ID stay."""

    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"select_all_template": True, "excluded_template": [1]}, {3, 4, 5, 6}),
            ({"select_all_template": True, "excluded_template": [1, 9]}, {3, 4, 6}),
            ({"select_all_organization": True, "excluded_organization": [1]}, {3, 5, 6}),
            ({"select_all_project": True, "excluded_project": [20]}, {1, 2, 5, 6}),
            ({"select_all_label": True, "excluded_label": [300]}, {1, 2, 3, 5, 6}),
        ],
        ids=["template", "template_incl_deleted", "organization", "project", "label"],
    )
    def test_all_except_n(self, admin_client, selection_jobs, body, expected):
        assert selected_jobs(admin_client, body) == expected

    def test_exclusions_without_select_all_mean_all_except(self, admin_client, selection_jobs):
        """Without positive parameters there is no positive filter, so exclusions apply to every job."""
        assert selected_jobs(admin_client, {"excluded_template": [1]}) == {3, 4, 5, 6}

    def test_explicit_ids_minus_exclusions(self, admin_client, selection_jobs):
        assert selected_jobs(admin_client, {"template": [1, 2], "excluded_template": [2]}) == {1, 2}

    def test_excluding_unknown_id_changes_nothing(self, admin_client, selection_jobs):
        assert selected_jobs(admin_client, {"select_all_template": True, "excluded_template": [12345]}) == ALL_JOBS

    def test_label_exclusion_removes_jobs_with_any_excluded_label(self, admin_client, selection_jobs):
        """Job 2 has labels 100 and 200: excluding 100 removes it even though 200 is selected."""
        assert selected_jobs(admin_client, {"label": [200], "excluded_label": [100]}) == {3}
        assert selected_jobs(admin_client, {"select_all_label": True, "excluded_label": [100]}) == {3, 4, 5, 6}

    def test_exclusions_on_several_fields(self, admin_client, selection_jobs):
        body = {"select_all_template": True, "excluded_template": [3], "excluded_organization": [2]}
        assert selected_jobs(admin_client, body) == {1, 2, 6}

    def test_all_except_differs_from_remaining_ids_only_by_null_rows(self, admin_client, selection_jobs):
        """'All templates except 1, 3, 9' keeps the job without a template, the explicit list [2] does not."""
        all_except = {"select_all_template": True, "excluded_template": [1, 3, 9]}

        assert selected_jobs(admin_client, all_except) == {3, 6}
        assert selected_jobs(admin_client, {"template": [2]}) == {3}
        # The per-template report has no row for job 6 (no template metadata), so both list only Template 2.
        assert (
            _post(admin_client, "dashboard_reports:report-query", all_except).json()
            == _post(admin_client, "dashboard_reports:report-query", {"template": [2]}).json()
        )


@pytest.mark.unit
@pytest.mark.django_db
class TestSearch:
    """search_<field> and excluded_search_<field> resolve against AWX like the filter dropdowns."""

    @pytest.mark.parametrize(
        "field,option_url,term",
        [
            ("template", "/api/v1/dashboard_reports/templates/", "prod"),
            ("organization", "/api/v1/dashboard_reports/organizations/", "eng"),
            ("project", "/api/v1/dashboard_reports/projects/", "APP"),
            ("label", "/api/v1/dashboard_reports/labels/", "prod"),
        ],
    )
    def test_search_selects_the_dropdown_options(self, admin_client, selection_jobs, fake_awx, field, option_url, term):
        """A search term selects exactly the options the dropdown lists for ?search=<term>."""
        options = admin_client.get(option_url, data={"search": term, "page_size": 100})
        assert options.status_code == 200
        option_ids = [item["id"] for item in options.json()["results"]]
        assert option_ids

        for post_name in ("dashboard_reports:report-query", "dashboard_reports:report-details-query"):
            by_search = _post(admin_client, post_name, {f"search_{field}": [term]})
            by_ids = _post(admin_client, post_name, {field: option_ids})
            assert by_search.json() == by_ids.json()

    def test_search_is_case_insensitive_substring(self, admin_client, selection_jobs, fake_awx):
        assert selected_jobs(admin_client, {"search_template": ["PROD"]}) == {1, 2, 4}

    def test_multiple_terms_are_combined(self, admin_client, selection_jobs, fake_awx):
        assert selected_jobs(admin_client, {"search_template": ["staging", "backup"]}) == {3, 4}

    def test_search_and_explicit_ids_are_combined(self, admin_client, selection_jobs, fake_awx):
        assert selected_jobs(admin_client, {"search_template": ["staging"], "template": [1]}) == {1, 2, 3}

    def test_search_minus_exclusions(self, admin_client, selection_jobs, fake_awx):
        assert selected_jobs(admin_client, {"search_template": ["prod"], "excluded_template": [3]}) == {1, 2}

    def test_all_except_search(self, admin_client, selection_jobs, fake_awx):
        body = {"select_all_template": True, "excluded_search_template": ["prod"]}
        assert selected_jobs(admin_client, body) == {3, 5, 6}

    def test_search_minus_excluded_search(self, admin_client, selection_jobs, fake_awx):
        body = {"search_template": ["deploy"], "excluded_search_template": ["staging"]}
        assert selected_jobs(admin_client, body) == {1, 2}

    def test_label_search_and_excluded_search(self, admin_client, selection_jobs, fake_awx):
        assert selected_jobs(admin_client, {"search_label": ["prod"]}) == {1, 2}
        assert selected_jobs(admin_client, {"select_all_label": True, "excluded_search_label": ["test"]}) == {
            1,
            4,
            5,
            6,
        }

    def test_search_does_not_match_names_stored_in_job_data(self, admin_client, selection_jobs, fake_awx):
        """Job 5's stored template name contains 'Old', but no AWX template does: the search selects nothing."""
        assert selected_jobs(admin_client, {"search_template": ["Old"]}) == set()

    @pytest.mark.parametrize("get_name,post_name", ENDPOINTS, ids=ENDPOINT_IDS)
    def test_search_without_matches_returns_no_data(self, admin_client, selection_jobs, fake_awx, get_name, post_name):
        """A selection that resolves to nothing filters to nothing; it does not fall back to 'no filter'."""
        nothing = _post(admin_client, post_name, {"search_template": ["no-such-template"]})
        nothing_get = admin_client.get(reverse(get_name), data={"search_template": "no-such-template"})

        if post_name == "dashboard_reports:report-query":
            assert nothing.json()["count"] == 0
        elif post_name == "dashboard_reports:report-details-query":
            assert nothing.json()["total_number_of_job_runs"] == 0
            assert nothing.json()["top_users"] == []
        else:
            assert len(_payload(nothing)) == 1  # header row only
        assert _payload(nothing_get) == _payload(nothing)

    def test_empty_match_on_one_field_empties_the_report(self, admin_client, selection_jobs, fake_awx):
        body = {"select_all_template": True, "search_organization": ["no-such-org"]}
        assert runs(admin_client, body) == 0

    def test_excluded_search_without_matches_changes_nothing(self, admin_client, selection_jobs, fake_awx):
        body = {"select_all_template": True, "excluded_search_template": ["no-such-template"]}
        assert selected_jobs(admin_client, body) == ALL_JOBS

    def test_search_resolved_once_per_request(self, admin_client, selection_jobs, fake_awx):
        """details applies the filter several times (cards and charts); all terms of a list are resolved once."""
        _post(admin_client, "dashboard_reports:report-details-query", {"search_template": ["prod", "staging"]})

        fake_awx.assert_called_once()
        assert fake_awx.call_args.args[:2] == ("template", ["prod", "staging"])

    @pytest.mark.parametrize("get_name,post_name", ENDPOINTS, ids=ENDPOINT_IDS)
    def test_awx_unavailable_returns_503(self, admin_client, selection_jobs, get_name, post_name):
        """An unreachable AWX database during search resolution returns a JSON 503, also on the CSV export."""
        with patch("apps.dashboard_reports.filters.get_db_connection", side_effect=OperationalError("awx down")):
            post_response = admin_client.post(reverse(post_name), data={"search_template": ["prod"]}, format="json")
            get_response = admin_client.get(reverse(get_name), data={"search_template": "prod"})

        for response in (post_response, get_response):
            assert response.status_code == 503
            assert response["Content-Type"] == "application/json"
            assert "AWX database" in response.json()["detail"]

    def test_awx_query_failure_returns_503(self, admin_client, selection_jobs):
        """A failing query on the raw psycopg AWX connection also returns 503."""
        with (
            patch("apps.dashboard_reports.filters.get_db_connection", return_value=MagicMock()),
            patch(
                "apps.dashboard_reports.filters.fetch_ids_matching_search",
                side_effect=psycopg.errors.QueryCanceled("timeout"),
            ),
        ):
            response = admin_client.post(
                reverse("dashboard_reports:report-query"), data={"excluded_search_label": ["x"]}, format="json"
            )

        assert response.status_code == 503

    def test_non_database_error_is_not_reported_as_503(self, admin_client, selection_jobs):
        """Only database errors mean AWX is unavailable; a bug surfaces as an error, not as a 503."""
        with (
            patch("apps.dashboard_reports.filters.get_db_connection", return_value=MagicMock()),
            patch("apps.dashboard_reports.filters.fetch_ids_matching_search", side_effect=KeyError("bug")),
            pytest.raises(KeyError),
        ):
            admin_client.post(reverse("dashboard_reports:report-query"), data={"search_label": ["x"]}, format="json")

    def test_one_awx_connection_per_request(self, admin_client, selection_jobs, fake_awx):
        """Search terms on several fields share one AWX connection (one liveness probe) per request."""
        body = {
            "search_template": ["prod"],
            "excluded_search_template": ["staging"],
            "search_label": ["prod"],
            "excluded_search_organization": ["fin"],
        }
        with patch("apps.dashboard_reports.filters.get_db_connection", return_value=MagicMock()) as get_connection:
            _post(admin_client, "dashboard_reports:report-details-query", body)

        get_connection.assert_called_once_with("awx")
        assert fake_awx.call_count == 4

    def test_awx_not_queried_without_search_terms(self, admin_client, selection_jobs):
        """Requests without search terms never touch AWX, so they keep working while it is unavailable."""
        body = {"select_all_template": True, "excluded_template": [1], "label": [200]}
        with patch(
            "apps.dashboard_reports.filters.get_db_connection", side_effect=OperationalError("awx down")
        ) as get_connection:
            assert selected_jobs(admin_client, body) == {3}

        get_connection.assert_not_called()

    def test_selection_is_resolved_per_request(self, admin_client, selection_jobs, fake_awx):
        """Search terms are resolved on every request, so a shared 'all matching prod' follows AWX changes."""
        body = {"search_template": ["prod"]}
        assert selected_jobs(admin_client, body) == {1, 2, 4}

        renamed = [(1, "Deploy prod"), (2, "Deploy prod (staging)"), (3, "Backup prod")]
        with patch.dict(AWX_ROWS, {AWXQuery.TEMPLATES: renamed}):
            assert selected_jobs(admin_client, body) == {1, 2, 3, 4}


@pytest.mark.unit
@pytest.mark.django_db
class TestGetAndPostMatch:
    """The selection parameters behave the same in the query string and in the POST body."""

    BODIES = {
        "select_all": {"select_all_template": True},
        "all_except": {"select_all_template": True, "excluded_template": [1]},
        "search": {"search_template": ["prod"]},
        "search_and_ids": {"search_template": ["staging"], "template": [1]},
        "excluded_search": {"select_all_label": True, "excluded_search_label": ["test"]},
        "label_exclusion": {"label": [200], "excluded_label": [100]},
        "several_fields": {"select_all_project": True, "excluded_organization": [2], "search_template": ["deploy"]},
    }

    @pytest.mark.parametrize("get_name,post_name", ENDPOINTS, ids=ENDPOINT_IDS)
    @pytest.mark.parametrize("body", list(BODIES.values()), ids=list(BODIES))
    def test_get_equals_post(self, admin_client, selection_jobs, fake_awx, get_name, post_name, body):
        get_response = admin_client.get(reverse(get_name), data=to_query_string(body))
        post_response = _post(admin_client, post_name, body)

        assert get_response.status_code == 200
        assert _payload(get_response) == _payload(post_response)

    @pytest.mark.parametrize(
        "value,expected",
        [("true", {3, 4, 5, 6}), ("1", {3, 4, 5, 6}), ("yes", {3, 4, 5, 6}), ("on", {3, 4, 5, 6}), ("false", {3, 4})],
    )
    def test_get_select_all_values(self, admin_client, selection_jobs, value, expected):
        url = reverse("dashboard_reports:report-details")
        params = {"select_all_template": value, "template": [2, 3], "excluded_template": [1]}
        response = admin_client.get(url, data=params)

        assert {user["id"] for user in response.json()["top_users"]} == expected

    def test_get_ignores_blank_search_terms(self, admin_client, selection_jobs, fake_awx):
        """A blank search term in the query string is ignored, like an empty dropdown search."""
        response = admin_client.get(reverse("dashboard_reports:report-details"), data={"search_template": " "})

        assert response.json()["total_number_of_job_runs"] == len(JOBS)
        fake_awx.assert_not_called()

    @pytest.mark.parametrize(
        "params,field",
        [
            ({"search_template": [f"t{i}" for i in range(101)]}, "search_template"),
            ({"excluded_search_label": [f"x{i}" for i in range(101)]}, "excluded_search_label"),
            ({"search_project": ["x" * 513]}, "search_project"),
            ({"excluded_search_organization": ["x" * 513]}, "excluded_search_organization"),
        ],
        ids=["too_many_terms", "too_many_excluded_terms", "term_too_long", "excluded_term_too_long"],
    )
    def test_get_search_limits_return_400(self, admin_client, selection_jobs, fake_awx, params, field):
        """The query string has the same search term limits as the POST body; AWX is not queried."""
        # A valid search on another field must not open the AWX connection before the limits are checked.
        params = {**params, "search_template" if field != "search_template" else "search_label": ["prod"]}
        with patch("apps.dashboard_reports.filters.get_db_connection") as get_conn:
            response = admin_client.get(reverse("dashboard_reports:report-details"), data=params)

        assert response.status_code == 400
        assert field in response.json()
        get_conn.assert_not_called()
        fake_awx.assert_not_called()

    def test_get_max_search_terms_accepted(self, admin_client, selection_jobs, fake_awx):
        """Requests exactly at the limits are valid on GET as well."""
        params = {"search_template": [f"t{i}" for i in range(100)], "excluded_search_label": ["x" * 512]}
        response = admin_client.get(reverse("dashboard_reports:report-details"), data=params)

        assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
class TestSelectionBodyValidation:
    """The selection parameters in the POST body are type-checked."""

    @pytest.mark.parametrize(
        "body,field",
        [
            ({"select_all_template": "maybe"}, "select_all_template"),
            ({"select_all_label": [True]}, "select_all_label"),
            ({"search_project": "prod"}, "search_project"),
            ({"search_template": [""]}, "search_template"),
            ({"excluded_organization": ["a"]}, "excluded_organization"),
            ({"excluded_label": 5}, "excluded_label"),
            ({"excluded_search_template": "x"}, "excluded_search_template"),
            ({"select_all_templates": True}, "select_all_templates"),
            ({"search_template": ["t"] * 101}, "search_template"),
            ({"excluded_search_label": ["x" * 513]}, "excluded_search_label"),
        ],
        ids=[
            "select_all_not_bool",
            "select_all_list",
            "search_not_list",
            "search_blank",
            "excluded_not_int",
            "excluded_not_list",
            "excluded_search_not_list",
            "plural_key",
            "too_many_terms",
            "term_too_long",
        ],
    )
    def test_invalid_selection_returns_400(self, admin_client, body, field):
        response = admin_client.post(reverse("dashboard_reports:report-query"), data=body, format="json")

        assert response.status_code == 400
        assert field in response.json()

    def test_max_search_terms_accepted(self, admin_client, selection_jobs, fake_awx):
        body = {"search_template": [f"t{i}" for i in range(100)], "excluded_search_label": ["x" * 512]}
        response = admin_client.post(reverse("dashboard_reports:report-query"), data=body, format="json")

        assert response.status_code == 200


@pytest.mark.unit
class TestSelectionHelpers:
    """Unit tests for the parameter helpers and get_field_selections."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, True),
            (False, False),
            ("true", True),
            ("TRUE", True),
            (" true ", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("t", True),
            ("false", False),
            ("0", False),
            ("tRuE", True),
            ("maybe", False),
            ("", False),
        ],
    )
    def test_bool_param(self, value, expected):
        assert _bool_param({"flag": value}, "flag") is expected

    def test_bool_param_missing(self):
        assert _bool_param({}, "flag") is False

    def test_search_terms_are_stripped_and_blank_terms_dropped(self):
        params = MagicMock()
        params.getlist.return_value = [" prod ", "", "  ", "stage"]

        assert _search_terms(params, "search_template") == ["prod", "stage"]

    def test_no_selection_parameters(self):
        request = APIRequestFactory().get("/")

        assert get_field_selections(request) == {field: FieldSelection(include=None, exclude=[]) for field in FIELDS}

    def test_explicit_and_excluded_ids_are_sorted_and_deduplicated(self):
        request = APIRequestFactory().get("/", {"template": ["3", "1", "3"], "excluded_template": ["9", "2", "9"]})

        assert get_field_selections(request)["template"] == FieldSelection(include=[1, 3], exclude=[2, 9])

    def test_search_resolution(self):
        request = APIRequestFactory().get("/", {"search_label": "prod", "label": "7", "excluded_search_label": "x"})
        resolved = {("label", ("prod",)): {100, 101}, ("label", ("x",)): {101}}

        with (
            patch(
                "apps.dashboard_reports.filters.fetch_ids_matching_search",
                side_effect=lambda field, terms, db_connection: resolved[(field, tuple(terms))],
            ),
            patch("apps.dashboard_reports.filters.get_db_connection"),
        ):
            selection = get_field_selections(request)["label"]

        # include and exclude overlap on purpose: exclusion wins when the filter is applied.
        assert selection == FieldSelection(include=[7, 100, 101], exclude=[101])

    def test_selections_are_cached_on_the_request(self):
        request = APIRequestFactory().get("/", {"search_template": "prod"})

        with (
            patch("apps.dashboard_reports.filters.fetch_ids_matching_search", return_value={1}) as fetch,
            patch("apps.dashboard_reports.filters.get_db_connection"),
        ):
            first = get_field_selections(request)
            second = get_field_selections(request)

        assert first is second
        fetch.assert_called_once()


@pytest.mark.unit
@pytest.mark.django_db
class TestJobDataSelectionMethods:
    """The queryset methods behind the selection: one array parameter, NULL-safe exclusion."""

    @pytest.fixture
    def jobs(self, selection_jobs) -> None:
        """Alias: the JOBS rows, without the HTTP layer."""

    def _job_ids(self, queryset) -> set[int]:
        return set(queryset.values_list("job_id", flat=True))

    def test_id_filter_uses_one_array_parameter(self, jobs):
        ids = list(range(1, 5001))
        sql, params = JobData.objects.templates(ids).query.sql_with_params()

        assert "= ANY(%s)" in sql
        assert list(params) == [ids]

    def test_id_filter(self, jobs):
        assert self._job_ids(JobData.objects.templates([1, 2])) == {1, 2, 3}
        assert self._job_ids(JobData.objects.organizations([2])) == {3, 5}
        assert self._job_ids(JobData.objects.projects([20])) == {3, 4}
        assert self._job_ids(JobData.objects.labels([100])) == {1, 2}

    @pytest.mark.parametrize(
        "field,ids,expected",
        [
            ("template", [1], {3, 4, 5, 6}),
            ("organization", [1, 2], {6}),
            ("project", [10], {3, 4, 6}),
            ("label", [100], {3, 4, 5, 6}),
            ("label", [200], {1, 4, 5, 6}),
        ],
    )
    def test_exclude_ids(self, jobs, field, ids, expected):
        assert self._job_ids(JobData.objects.exclude_ids(field, ids)) == expected

    def test_label_exclusion_uses_not_exists(self, jobs):
        """NOT EXISTS plans as an anti-join; NOT IN (subquery) over all labelled jobs may not."""
        sql = str(JobData.objects.exclude_ids("label", [100]).query)

        assert "NOT EXISTS" in sql
        assert " IN (SELECT" not in sql

    def test_exclude_ids_keeps_null_rows(self, jobs):
        assert 6 in self._job_ids(JobData.objects.exclude_ids("template", [1, 2, 3, 9]))

    def test_exclude_ids_empty_is_noop(self, jobs):
        assert self._job_ids(JobData.objects.exclude_ids("template", [])) == ALL_JOBS


def _mock_awx_connection(rows: list[tuple]) -> tuple[MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [("id",)]
    cursor.fetchall.return_value = rows
    db_connection = MagicMock()
    db_connection.cursor.return_value = cursor
    return db_connection, cursor


@pytest.mark.unit
class TestFetchIdsMatchingSearch:
    """Search resolution: one query over the field's option query, all terms as one array of ILIKE patterns."""

    @pytest.mark.parametrize("field", FIELDS)
    def test_one_query_over_the_option_query_of_the_field(self, field):
        db_connection, cursor = _mock_awx_connection([(1,), (2,)])

        assert fetch_ids_matching_search(field, ["a", "b"], db_connection=db_connection) == {1, 2}

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args.args
        assert f"FROM ({FILTER_FIELD_OPTION_QUERIES[field].value}) AS _options" in sql
        assert "_options.name ILIKE ANY(%s)" in sql
        assert "LIMIT" not in sql
        assert "ORDER BY" not in sql
        assert params == [["%a%", "%b%"]]

    def test_patterns_are_escaped_like_the_option_endpoints(self):
        db_connection, cursor = _mock_awx_connection([])

        fetch_ids_matching_search("template", ["a_b%", "c\\d"], db_connection=db_connection)

        assert cursor.execute.call_args.args[1] == [["%a\\_b\\%%", "%c\\\\d%"]]

    def test_no_terms_runs_no_query(self):
        db_connection, _ = _mock_awx_connection([])

        assert fetch_ids_matching_search("template", [], db_connection=db_connection) == set()
        db_connection.cursor.assert_not_called()

    def test_database_errors_are_raised(self):
        """A database error must propagate (and become a 503), never turn into 'no matches'."""
        db_connection, cursor = _mock_awx_connection([])
        cursor.execute.side_effect = psycopg.errors.OperationalError("awx down")

        with pytest.raises(psycopg.errors.OperationalError):
            fetch_ids_matching_search("template", ["a"], db_connection=db_connection)


# Minimal copies of the AWX tables the option queries read, created as temporary tables in the test DB.
AWX_TEMP_TABLES = [
    "CREATE TEMP TABLE main_organization (id integer PRIMARY KEY, name text NOT NULL)",
    "CREATE TEMP TABLE main_unifiedjobtemplate (id integer PRIMARY KEY, name text NOT NULL)",
    "CREATE TEMP TABLE main_jobtemplate (unifiedjobtemplate_ptr_id integer PRIMARY KEY)",
    "CREATE TEMP TABLE main_project (unifiedjobtemplate_ptr_id integer PRIMARY KEY)",
    "CREATE TEMP TABLE main_label (id integer PRIMARY KEY, name text NOT NULL, organization_id integer NOT NULL)",
]
# Names that exercise case-insensitivity and the escaping of _, % and backslash.
TRICKY_NAMES = ["Deploy prod", "deploy_prod", "deployXprod", "50% off", "50 off", "back\\slash", "PROD-2", "other"]
SEARCH_TERMS = ["prod", "PROD", "_prod", "y_p", "%", "50%", "\\", "k\\s", "o", "no-match"]


@pytest.fixture
def awx_tables(db):
    """Fill temporary AWX tables with TRICKY_NAMES (ids 1..n per table) and return the connection."""
    with connection.cursor() as cursor:
        for statement in AWX_TEMP_TABLES:
            cursor.execute(statement)
        for item_id, name in enumerate(TRICKY_NAMES, start=1):
            cursor.execute("INSERT INTO main_organization VALUES (%s, %s)", [item_id, name])
            cursor.execute("INSERT INTO main_label VALUES (%s, %s, %s)", [item_id, name, 1])
            # Templates use ids 1..n, projects ids 101..; both live in main_unifiedjobtemplate.
            cursor.execute("INSERT INTO main_unifiedjobtemplate VALUES (%s, %s)", [item_id, name])
            cursor.execute("INSERT INTO main_jobtemplate VALUES (%s)", [item_id])
            cursor.execute("INSERT INTO main_unifiedjobtemplate VALUES (%s, %s)", [100 + item_id, name])
            cursor.execute("INSERT INTO main_project VALUES (%s)", [100 + item_id])
    return connection


@pytest.mark.unit
@pytest.mark.django_db
class TestSearchSqlOnPostgres:
    """The real SQL of the search resolution selects exactly what the filter option endpoints list."""

    OPTION_FETCHERS = {
        "organization": fetch_organizations,
        "template": fetch_templates,
        "project": fetch_projects,
        "label": fetch_labels,
    }

    def _dropdown_ids(self, field: str, term: str) -> set[int]:
        items, _ = self.OPTION_FETCHERS[field](db_connection=connection, search_str=term, limit=1000, offset=0)
        return {item["id"] for item in items}

    @pytest.mark.parametrize("field", FIELDS)
    @pytest.mark.parametrize("term", SEARCH_TERMS)
    def test_single_term_matches_dropdown(self, awx_tables, field, term):
        assert fetch_ids_matching_search(field, [term], db_connection=connection) == self._dropdown_ids(field, term)

    @pytest.mark.parametrize("field", FIELDS)
    def test_several_terms_match_union_of_dropdowns(self, awx_tables, field):
        terms = ["_prod", "50%", "\\"]
        expected = set().union(*(self._dropdown_ids(field, term) for term in terms))

        assert fetch_ids_matching_search(field, terms, db_connection=connection) == expected

    def test_wildcards_are_literal(self, awx_tables):
        """'_' and '%' match themselves only, not any character."""
        names = {
            name
            for item_id, name in enumerate(TRICKY_NAMES, start=1)
            if item_id in fetch_ids_matching_search("organization", ["y_p", "50%"], db_connection=connection)
        }

        assert names == {"deploy_prod", "50% off"}
