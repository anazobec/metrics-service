"""Tests for the unified dashboard job definition: storing, read-time filtering and the update task."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from django.http import QueryDict

from apps.dashboard_reports.filters import get_job_type_filter
from apps.dashboard_reports.models import DashboardTelemetry, JobData
from apps.dashboard_reports.tasks import _store_dashboard_page, update_dashboard_job_definition

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
    """The Controller host summary query is not part of these tests."""
    with patch("apps.dashboard_reports.tasks._fetch_host_summaries", return_value={}):
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

        stats = _store_dashboard_page(MagicMock(), page)

        assert (stats.processed, stats.added, stats.updated, stats.failed) == (3, 3, 0, [])
        assert dict(JobData.objects.values_list("job_id", "launch_type")) == {
            1: "manual",
            2: "sync",
            3: "workflow",
        }

    def test_fills_launch_type_of_existing_rows_and_second_run_changes_nothing(self):
        """Existing rows with NULL launch_type are updated once; a rerun adds and updates nothing."""
        _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")))
        JobData.objects.filter(job_id=1).update(launch_type=None)
        page = _page((1, "successful", "manual"), (2, "successful", "sync"))

        first = _store_dashboard_page(MagicMock(), page)
        second = _store_dashboard_page(MagicMock(), page)

        assert (first.added, first.updated) == (1, 1)
        assert (second.added, second.updated) == (0, 0)
        assert JobData.objects.get(job_id=1).launch_type == "manual"
        assert JobData.objects.count() == 2

    def test_failed_jobs_are_reported(self):
        """A failing page returns its job IDs and counts nothing as processed."""
        with patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[1]):
            stats = _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")))

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
        _store_dashboard_page(MagicMock(), _page((1, "successful", "manual")))
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

    def test_failed_jobs_make_the_task_report_error_and_continue(self):
        """Failed pages are counted, later pages still run, and the task result is an error."""
        pages = [_page((1, "successful", "manual")), _page((2, "successful", "sync"))]
        with patch("apps.dashboard_reports.tasks._sync_jobs_atomically", side_effect=[[1], []]):
            result = self._run(pages)

        assert result["status"] == "error"
        assert result["failed"] == 1
        assert result["added"] == 1
