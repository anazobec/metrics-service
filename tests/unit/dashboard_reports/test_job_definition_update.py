"""Tests for the unified dashboard job definition: storing, read-time filtering and the update task."""

import datetime as dt
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from django.http import QueryDict
from django.urls import reverse

from apps.dashboard_reports.filters import get_job_type_filter
from apps.dashboard_reports.job_definition import prepare_dashboard_records, select_dashboard_jobs
from apps.dashboard_reports.models import DashboardTelemetry, JobData
from apps.dashboard_reports.tasks import (
    _store_dashboard_page,
    sync_dashboard_job_records,
    update_dashboard_job_definition,
)

NOW = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def _page(*rows):
    """Build a unified_jobs_dashboard-like DataFrame from (id, status, launch_type) tuples."""
    return pd.DataFrame(
        [
            {
                "id": job_id,
                "name": f"job {job_id}",
                "model": "job",
                "unified_job_template_id": 7,
                "organization_id": 1,
                "organization_name": "Org",
                "started": NOW,
                "finished": NOW,
                "status": status,
                "launch_type": launch_type,
                "elapsed": 1.5,
                "launched_by_id": None,
                "launched_by_username": None,
                "project_id": None,
                "project_name": None,
                "created": NOW,
                "modified": NOW,
                "label_ids": None,
                "num_hosts": 0,
            }
            for job_id, status, launch_type in rows
        ]
    )


@pytest.fixture(autouse=True)
def no_host_summaries():
    """The Controller host summary and connection are not part of these tests."""
    with (
        patch("apps.dashboard_reports.tasks._fetch_host_summaries", return_value={}),
        patch("apps.dashboard_reports.tasks.get_db_connection"),
    ):
        yield


@pytest.mark.unit
@pytest.mark.django_db
class TestStoreDashboardPage:
    """_store_dashboard_page is the single store path shared by the backfill and the update task."""

    def test_stores_all_launch_types_and_terminal_statuses_only(self):
        """Sync and workflow jobs are stored with launch_type; non-terminal jobs are skipped."""
        page = _page(
            (1, "successful", "manual"),
            (2, "failed", "sync"),
            (3, "successful", "workflow"),
            (4, "running", "manual"),
        )

        stats = _store_dashboard_page(MagicMock(), page, "test_task")

        assert (stats.processed, stats.added, stats.updated, stats.failed) == (3, 3, 0, [])
        assert dict(JobData.objects.values_list("job_id", "launch_type")) == {
            1: "manual",
            2: "sync",
            3: "workflow",
        }

    def test_fills_launch_type_of_existing_rows_and_second_run_changes_nothing(self):
        """Existing rows with NULL launch_type are updated once; a rerun adds and updates nothing."""
        _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")), "test_task")
        JobData.objects.filter(job_id=1).update(launch_type=None)
        page = _page((1, "successful", "manual"), (2, "successful", "sync"))

        first = _store_dashboard_page(MagicMock(), page, "test_task")
        second = _store_dashboard_page(MagicMock(), page, "test_task")

        assert (first.added, first.updated) == (1, 1)
        assert (second.added, second.updated) == (0, 0)
        assert JobData.objects.get(job_id=1).launch_type == "manual"
        assert JobData.objects.count() == 2

    def test_missing_models_are_resolved_from_controller(self):
        """Rows without a model get job_type from Controller instead of being stored as NULL."""
        page = _page((1, "successful", "manual"), (2, "successful", "manual"))
        page["model"] = [None, "job"]

        with patch("apps.dashboard_reports.tasks.fetch_job_types_for_ids", return_value={1: "workflowjob"}) as fetch:
            stats = _store_dashboard_page(MagicMock(), page, "test_task")

        fetch.assert_called_once()
        assert fetch.call_args.args[1] == [1]
        assert stats.failed == []
        assert dict(JobData.objects.values_list("job_id", "job_type")) == {1: "workflowjob", 2: "job"}

    def test_unrecoverable_models_fail_the_page(self):
        """When Controller cannot be queried nothing is stored, the page job IDs are reported failed and the error is logged under the caller task name."""
        page = _page((1, "successful", "manual"))
        page["model"] = [None]

        with (
            patch("apps.dashboard_reports.tasks.fetch_job_types_for_ids", side_effect=RuntimeError("db down")),
            patch("apps.dashboard_reports.tasks.log_task_execution") as log_execution,
        ):
            stats = _store_dashboard_page(MagicMock(), page, "test_task")

        assert stats.failed == [1]
        assert JobData.objects.count() == 0
        log_execution.assert_any_call(
            task_name="test_task",
            operation="error",
            details="Unable to recover Controller models for 1 queued dashboard job record(s)",
        )

    def test_failed_jobs_are_reported(self):
        """A failing page returns its job IDs and counts nothing as processed."""
        with patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[1]):
            stats = _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")), "test_task")

        assert stats.failed == [1]
        assert stats.processed == 0


@pytest.mark.unit
@pytest.mark.django_db
class TestReadTimeLaunchTypeFilter:
    """Reports hide sync and workflow jobs but keep legacy rows without launch_type."""

    def test_hides_sync_and_workflow_keeps_legacy_null(self):
        """Default report filter returns only non-hidden launch types and NULL legacy rows."""
        _store_dashboard_page(
            MagicMock(),
            _page(
                (1, "successful", "manual"),
                (2, "successful", "sync"),
                (3, "successful", "workflow"),
                (4, "successful", "scheduled"),
            ),
            "test_task",
        )
        JobData.objects.filter(job_id=4).update(launch_type=None)
        request = MagicMock()
        request.query_params = QueryDict("")

        visible = JobData.objects.filter(get_job_type_filter(request)).values_list("job_id", flat=True)

        assert sorted(visible) == [1, 4]


@pytest.mark.unit
@pytest.mark.django_db
class TestUpdateDashboardJobDefinition:
    """update_dashboard_job_definition reports counts and is idempotent."""

    def _run(self, pages):
        with (
            patch("apps.dashboard_reports.tasks.get_db_connection", return_value=MagicMock()),
            patch("apps.dashboard_reports.tasks._iter_job_pages", return_value=iter(pages)),
            patch("apps.dashboard_reports.tasks._retention_window_start", return_value=NOW),
            patch("apps.dashboard_reports.tasks._configured_batch_size", return_value=100),
        ):
            return update_dashboard_job_definition()

    def test_fills_adds_and_is_idempotent(self):
        """First run fills and adds, the second changes nothing; telemetry is recorded."""
        _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")), "test_task")
        JobData.objects.filter(job_id=1).update(launch_type=None)
        pages = [_page((1, "successful", "manual"), (2, "failed", "sync"))]

        first = self._run(pages)
        second = self._run(pages)

        assert first["status"] == "success"
        counts = {k: first[k] for k in ("processed", "updated", "added", "failed")}
        assert counts == {"processed": 2, "updated": 1, "added": 1, "failed": 0}
        counts = {k: second[k] for k in ("processed", "updated", "added", "failed")}
        assert counts == {"processed": 2, "updated": 0, "added": 0, "failed": 0}
        telemetry = DashboardTelemetry.objects.filter(task_name="update_dashboard_job_definition")
        assert telemetry.count() == 2
        assert all(t.success and t.number_of_records_processed == 2 for t in telemetry)

    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_skips_while_initial_collection_is_in_progress(self, status):
        """A pending or running initial collection already uses the unified definition, so the update skips."""
        from apps.tasks.models import Task

        task = Task.objects.create(
            name="initial_dashboard_collection",
            function_name="collect_dashboard_reports_initial_data",
            is_system_task=True,
        )
        Task.objects.filter(pk=task.pk).update(status=status)
        with patch("apps.dashboard_reports.tasks._iter_job_pages") as mock_pages:
            result = update_dashboard_job_definition()

        assert result["status"] == "success"
        assert result["skipped"] is True
        mock_pages.assert_not_called()
        assert not DashboardTelemetry.objects.filter(task_name="update_dashboard_job_definition").exists()

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_runs_when_initial_collection_is_finished_or_failed(self, status):
        """A failed initial collection must not block the update: its retry never revisits older rows."""
        from apps.tasks.models import Task

        task = Task.objects.create(
            name="initial_dashboard_collection",
            function_name="collect_dashboard_reports_initial_data",
            is_system_task=True,
        )
        Task.objects.filter(pk=task.pk).update(status=status)

        result = self._run([_page((1, "successful", "manual"))])

        assert result.get("skipped") is None
        assert result["added"] == 1

    def test_failed_jobs_make_the_task_report_error_and_continue(self):
        """Failed pages are counted, later pages still run, and the task result is an error."""
        pages = [_page((1, "successful", "manual")), _page((2, "successful", "sync"))]
        with patch("apps.dashboard_reports.tasks._sync_jobs_atomically", side_effect=[[1], []]):
            result = self._run(pages)

        assert result["status"] == "error"
        assert result["failed"] == 1
        assert result["added"] == 1


def _mixed_page():
    """A page with every kind of row: jobs, hidden launch types, non-job models, a running job, a missing model."""
    page = _page(
        (1, "successful", "manual"),
        (2, "failed", "scheduled"),
        (3, "successful", "sync"),
        (4, "successful", "workflow"),
        (5, "successful", "manual"),
        (6, "successful", "manual"),
        (7, "running", "manual"),
        (8, "successful", "manual"),
    )
    page["model"] = ["job", "job", "job", "job", "workflowjob", "projectupdate", "job", None]
    return page


@pytest.mark.unit
@pytest.mark.django_db
class TestHourlyHookAndBackfillParity:
    """The hourly hook and the backfill/update path store the same jobs from the same raw page."""

    @staticmethod
    def _snapshot():
        return set(JobData.objects.values_list("job_id", "job_type", "launch_type"))

    def test_same_raw_page_stores_same_jobs(self):
        """job_id, job_type and launch_type match, including a row whose model must be recovered."""
        page = _mixed_page()

        with patch("apps.dashboard_reports.tasks.fetch_job_types_for_ids", return_value={8: "job"}):
            records = prepare_dashboard_records(select_dashboard_jobs(page))
            assert sync_dashboard_job_records(raw_jobs=records)["status"] == "success"
            hook_rows = self._snapshot()

            JobData.objects.all().delete()
            _store_dashboard_page(MagicMock(), page, "test_task")
            backfill_rows = self._snapshot()

        assert hook_rows == backfill_rows
        assert {row[0] for row in hook_rows} == {1, 2, 3, 4, 5, 6, 8}
        assert (8, "job", "manual") in hook_rows


@pytest.mark.unit
@pytest.mark.django_db
class TestReportEndpointsAgree:
    """Reports, details, export and leaderboard hide sync/workflow launches and non-job models alike."""

    def test_all_endpoints_exclude_hidden_and_non_job_rows(self, admin_client):
        """Only the ordinary manual job is visible everywhere; hidden rows are stored but never reported."""
        now = datetime.now(UTC)
        page = _page(
            (1, "successful", "manual"),
            (2, "successful", "sync"),
            (3, "successful", "workflow"),
            (4, "successful", "manual"),
            (5, "successful", "manual"),
        )
        page["model"] = ["job", "job", "job", "workflowjob", "projectupdate"]
        page["unified_job_template_id"] = [1, 2, 3, 4, 5]
        page["finished"] = now - dt.timedelta(hours=1)
        page["started"] = now - dt.timedelta(hours=2)
        _store_dashboard_page(MagicMock(), page, "test_task")
        assert JobData.objects.count() == 5
        query = {
            "period": "last_7_days",
            "tz": "UTC",
            "start_date": (now - dt.timedelta(days=7)).isoformat(),
            "end_date": (now + dt.timedelta(hours=1)).isoformat(),
        }

        report = admin_client.get(reverse("v1:report-list"), data=query)
        details = admin_client.get(reverse("v1:report-details"), data=query)
        export = admin_client.get(
            reverse("dashboard_reports:report-export"),
            data={"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "csv"},
        )
        leaderboard = admin_client.get(reverse("v1:leaderboard-list"))

        assert [row["template_name"] for row in report.data["results"]] == ["job 1"]
        assert details.data["total_number_of_job_runs"] == 1
        for hidden in ("job 2", "job 3", "job 4", "job 5"):
            assert hidden.encode() not in export.content
        assert b"job 1" in export.content
        assert leaderboard.data["job_runs"] == 1
