# test_sync_dashboard_on_demand.py - Unit tests for the manual dashboard sync task

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from apps.dashboard_reports.models import DashboardTelemetry, JobData, JobHostSummary
from apps.dashboard_reports.tasks import _on_demand_windows, sync_dashboard_on_demand

MODULE = "apps.dashboard_reports.tasks"
SINCE = datetime(2024, 1, 1, tzinfo=UTC)
UNTIL = datetime(2024, 1, 2, tzinfo=UTC)
COLLECTORS = "metrics_utility.library.collectors.controller"
HOURLY = "apps.tasks.collectors.collect_hourly_metrics"


def _ok(**data):
    return {"status": "success", **data}


@pytest.fixture
def mocks():
    """Patch collectors, helpers, DB access, and both sync writers."""
    with (
        patch(f"{MODULE}.get_db_connection") as db,
        patch(f"{COLLECTORS}.unified_jobs_dashboard") as jobs_collector,
        patch(f"{COLLECTORS}.job_host_summary_service") as hs_collector,
        patch(f"{HOURLY}._build_dashboard_job_records", return_value=[{"id": 1}]) as build_jobs,
        patch(f"{HOURLY}._build_host_summary_by_job", return_value={1: [{"id": 9, "job_remote_id": 1}]}) as build_hs,
        patch(f"{MODULE}.sync_dashboard_job_records", return_value=_ok(job_count=1)) as sync_jobs,
        patch(f"{MODULE}.sync_dashboard_host_summaries", return_value=_ok(job_count=1)) as sync_hs,
        patch(f"{MODULE}._save_telemetry_details") as telemetry,
        patch(f"{MODULE}.JobData.last_finished_timestamp", return_value=SINCE) as watermark,
        patch("apps.tasks.task_groups.get_feature_enabled_from_db", return_value=True) as feature_enabled,
    ):
        yield MagicMock(
            db=db,
            jobs_collector=jobs_collector,
            hs_collector=hs_collector,
            build_jobs=build_jobs,
            build_hs=build_hs,
            sync_jobs=sync_jobs,
            sync_hs=sync_hs,
            telemetry=telemetry,
            feature_enabled=feature_enabled,
            watermark=watermark,
        )


@pytest.mark.unit
class TestOnDemandWindows:
    def test_single_window_when_range_within_one_day(self):
        assert list(_on_demand_windows(SINCE, SINCE + timedelta(hours=5))) == [(SINCE, SINCE + timedelta(hours=5))]

    def test_splits_into_daily_windows(self):
        windows = list(_on_demand_windows(SINCE, SINCE + timedelta(days=2, hours=3)))
        assert len(windows) == 3
        assert windows[0] == (SINCE, SINCE + timedelta(days=1))
        assert windows[-1][1] == SINCE + timedelta(days=2, hours=3)


@pytest.mark.unit
class TestSyncDashboardOnDemand:
    def test_explicit_range_runs_both_paths(self, mocks):
        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "success"
        assert result["date_range"] == {"start": SINCE.isoformat(), "end": UNTIL.isoformat()}
        assert result["job_count"] == 1
        assert result["host_summary_job_count"] == 1
        mocks.jobs_collector.assert_called_once()
        assert mocks.jobs_collector.call_args.kwargs["since"] == SINCE
        assert mocks.jobs_collector.call_args.kwargs["until"] == UNTIL
        mocks.hs_collector.assert_called_once()
        mocks.sync_jobs.assert_called_once_with(
            raw_jobs=[{"id": 1}], hour_timestamp=SINCE.isoformat(), save_telemetry=False
        )
        mocks.sync_hs.assert_called_once_with(
            raw_host_summaries=[{"id": 9, "job_remote_id": 1}], hour_timestamp=SINCE.isoformat(), save_telemetry=False
        )
        assert mocks.telemetry.call_args.kwargs["success"] is True
        assert mocks.telemetry.call_args.kwargs["number_of_records_processed"] == 2

    def test_default_window_uses_watermark_until_now(self, mocks):
        watermark = datetime.now(UTC) - timedelta(days=2)
        mocks.watermark.return_value = watermark

        result = sync_dashboard_on_demand()

        assert result["status"] == "success"
        assert result["date_range"]["start"] == watermark.isoformat()
        assert datetime.fromisoformat(result["date_range"]["end"]) > watermark

    def test_multi_day_range_syncs_each_window(self, mocks):
        until = SINCE + timedelta(days=3)

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=until.isoformat())

        assert result["status"] == "success"
        assert mocks.sync_jobs.call_count == 3
        assert mocks.sync_hs.call_count == 3
        assert result["job_count"] == 3

    def test_empty_window_skips_writers(self, mocks):
        mocks.build_jobs.return_value = []
        mocks.build_hs.return_value = {}

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "success"
        assert result["job_count"] == 0
        mocks.sync_jobs.assert_not_called()
        mocks.sync_hs.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"since": UNTIL.isoformat(), "until": SINCE.isoformat()},
            {"since": SINCE.isoformat(), "until": SINCE.isoformat()},
            {"since": "not-a-date"},
            {"until": 123},
        ],
    )
    def test_invalid_range_returns_error(self, mocks, kwargs):
        result = sync_dashboard_on_demand(**kwargs)

        assert result["status"] == "error"
        assert result["error"]
        assert "not-a-date" not in result["error"]
        mocks.jobs_collector.assert_not_called()
        mocks.telemetry.assert_called_once()
        assert mocks.telemetry.call_args.kwargs["success"] is False

    def test_disabled_dashboard_collection_returns_error(self, mocks):
        mocks.feature_enabled.return_value = False

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "error"
        assert "DASHBOARD_COLLECTION is disabled" in result["error"]
        mocks.feature_enabled.assert_called_once_with("DASHBOARD_COLLECTION")
        mocks.db.assert_not_called()
        mocks.jobs_collector.assert_not_called()
        mocks.sync_jobs.assert_not_called()
        assert mocks.telemetry.call_args.kwargs["success"] is False

    def test_range_over_cap_returns_error(self, mocks):
        until = SINCE + timedelta(days=7, seconds=1)

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=until.isoformat())

        assert result["status"] == "error"
        assert "collect_dashboard_reports_initial_data" in result["error"]
        mocks.db.assert_not_called()
        mocks.jobs_collector.assert_not_called()
        assert mocks.telemetry.call_args.kwargs["success"] is False

    def test_range_at_cap_is_allowed(self, mocks):
        until = SINCE + timedelta(days=7)

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=until.isoformat())

        assert result["status"] == "success"
        assert mocks.sync_jobs.call_count == 7
        assert result["synced_until"] == until.isoformat()

    def test_default_window_beyond_cap_returns_error(self, mocks):
        mocks.watermark.return_value = datetime.now(UTC) - timedelta(days=90)

        result = sync_dashboard_on_demand()

        assert result["status"] == "error"
        mocks.jobs_collector.assert_not_called()

    def test_failure_reports_last_synced_window(self, mocks):
        mocks.sync_jobs.side_effect = [_ok(job_count=1), {"status": "error", "error": "boom"}]
        until = SINCE + timedelta(days=3)

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=until.isoformat())

        assert result["status"] == "error"
        assert result["synced_until"] == (SINCE + timedelta(days=1)).isoformat()

    def test_job_sync_failure_skips_host_summaries(self, mocks):
        mocks.sync_jobs.return_value = {"status": "error", "error": "Failed to sync 1 job(s): [1]"}

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "error"
        assert "sync_dashboard_job_records failed" in result["error"]
        mocks.sync_hs.assert_not_called()
        assert mocks.telemetry.call_args.kwargs["success"] is False

    def test_host_summary_failure_is_not_reported_as_success(self, mocks):
        mocks.sync_hs.return_value = {"status": "error", "job_count": 0, "failed": 2}

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "error"
        assert "2 job(s) failed" in result["error"]
        assert result["job_count"] == 1
        assert result["host_summary_job_count"] == 0

    def test_failure_stops_remaining_windows(self, mocks):
        mocks.sync_jobs.return_value = {"status": "error", "error": "boom"}
        until = SINCE + timedelta(days=3)

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=until.isoformat())

        assert result["status"] == "error"
        assert mocks.sync_jobs.call_count == 1

    def test_collector_exception_returns_error(self, mocks):
        mocks.jobs_collector.return_value.gather.side_effect = RuntimeError("db down")

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "error"
        assert "Collection failed" in result["error"]
        assert "db down" not in result["error"]
        mocks.sync_jobs.assert_not_called()

    def test_db_connection_error_returns_error(self, mocks):
        mocks.db.side_effect = RuntimeError("no connection")

        result = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())

        assert result["status"] == "error"
        assert "Database error" in result["error"]
        assert "no connection" not in result["error"]


@pytest.mark.unit
@pytest.mark.django_db
class TestSyncDashboardOnDemandIdempotency:
    """Real writers and record builders; only the Controller collectors are mocked."""

    @staticmethod
    def _jobs_frame():
        finished = SINCE + timedelta(hours=1)
        return pd.DataFrame(
            [
                {
                    "id": job_id,
                    "name": f"job-{job_id}",
                    "model": "job",
                    "unified_job_template_id": 10,
                    "organization_id": 1,
                    "organization_name": "Default",
                    "started": finished - timedelta(minutes=5),
                    "finished": finished,
                    "status": "successful",
                    "launch_type": "manual",
                    "elapsed": 300.0,
                    "launched_by_id": 1,
                    "launched_by_username": "admin",
                    "project_id": 5,
                    "project_name": "proj",
                    "created": finished - timedelta(minutes=6),
                    "modified": finished,
                    "label_ids": None,
                    "num_hosts": 2,
                }
                for job_id in (101, 102)
            ]
        )

    @staticmethod
    def _summaries_frame():
        return pd.DataFrame(
            [
                {"id": 1, "host_name": "h1", "host_remote_id": 11, "job_remote_id": 101},
                {"id": 2, "host_name": "h2", "host_remote_id": 12, "job_remote_id": 101},
                {"id": 3, "host_name": "h1", "host_remote_id": 11, "job_remote_id": 102},
            ]
        )

    @staticmethod
    def _snapshot():
        jobs = sorted(JobData.objects.values_list("job_id", "template_name", "status", "organization_id", "elapsed"))
        summaries = sorted(
            JobHostSummary.objects.values_list("host_summary_id", "host_id", "host_name", "job_data__job_id")
        )
        return jobs, summaries

    def test_rerun_does_not_duplicate_or_change_data(self):
        with (
            patch("apps.tasks.task_groups.get_feature_enabled_from_db", return_value=True),
            patch(f"{MODULE}.get_db_connection"),
            patch(f"{COLLECTORS}.unified_jobs_dashboard") as jobs_collector,
            patch(f"{COLLECTORS}.job_host_summary_service") as hs_collector,
        ):
            jobs_collector.return_value.gather.return_value = self._jobs_frame()
            hs_collector.return_value.gather.return_value = self._summaries_frame()

            first = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())
            after_first = self._snapshot()
            second = sync_dashboard_on_demand(since=SINCE.isoformat(), until=UNTIL.isoformat())
            after_second = self._snapshot()

        assert first["status"] == second["status"] == "success"
        assert first["job_count"] == second["job_count"] == 2
        assert len(after_first[0]) == 2
        assert len(after_first[1]) == 3
        assert after_second == after_first

    def test_records_single_telemetry_row(self):
        with (
            patch("apps.tasks.task_groups.get_feature_enabled_from_db", return_value=True),
            patch(f"{MODULE}.get_db_connection"),
            patch(f"{COLLECTORS}.unified_jobs_dashboard") as jobs_collector,
            patch(f"{COLLECTORS}.job_host_summary_service") as hs_collector,
        ):
            jobs_collector.return_value.gather.return_value = self._jobs_frame()
            hs_collector.return_value.gather.return_value = self._summaries_frame()

            sync_dashboard_on_demand(since=SINCE.isoformat(), until=(SINCE + timedelta(days=3)).isoformat())

        assert list(DashboardTelemetry.objects.values_list("task_name", flat=True)) == ["sync_dashboard_on_demand"]
