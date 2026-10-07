# test_tasks.py - Unit tests for dashboard_reports tasks

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apps.dashboard_reports.models import JobData
from apps.dashboard_reports.tasks import (
    _MISSING,
    DEFAULT_RETENTION_DAYS,
    _collect_data,
    _collect_jobs,
    _get_job_id_range,
    _get_renamed_kwarg,
    _parse_dt,
    _process_batches,
    _resolve_collection_params,
    _sync_jobs_atomically,
    backfill_dashboard_organization_names,
    cleanup_dashboard_reports_old_data,
    collect_dashboard_reports_data,
    collect_dashboard_reports_initial_data,
    get_retention_days,
    sync_dashboard_host_summaries,
    sync_dashboard_job_records,
)


@pytest.mark.unit
class TestParseDt:
    """Tests for _parse_dt helper function."""

    def test_returns_tz_aware_datetime_unchanged(self):
        """_parse_dt returns a tz-aware datetime as the same object."""
        dt = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        assert _parse_dt(dt) is dt

    def test_normalizes_naive_datetime_to_utc(self):
        """_parse_dt attaches UTC to a naive datetime."""
        dt = datetime(2024, 1, 15, 12, 0)
        result = _parse_dt(dt)
        assert result == datetime(2024, 1, 15, 12, 0, tzinfo=UTC)

    def test_parses_iso_string(self):
        """_parse_dt converts an ISO-format string to a datetime."""
        result = _parse_dt("2024-01-15T12:00:00")
        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_returns_none_unchanged(self):
        """_parse_dt returns None when given None."""
        assert _parse_dt(None) is None

    def test_raises_for_unsupported_types(self):
        """_parse_dt raises TypeError for non-str, non-datetime, non-None values."""
        with pytest.raises(TypeError, match="_parse_dt"):
            _parse_dt(42)


@pytest.mark.unit
class TestSyncJobsAtomically:
    """Tests for _sync_jobs_atomically helper (DB-free via mocking)."""

    @patch("apps.dashboard_reports.tasks.transaction.atomic")
    @patch("apps.dashboard_reports.tasks.JobData.create_or_update_from_awx")
    def test_all_jobs_succeed(self, mock_create, mock_atomic):
        """_sync_jobs_atomically returns empty list when all jobs sync successfully."""
        mock_atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_atomic.return_value.__exit__ = MagicMock(return_value=False)
        mock_create.return_value = None
        failed = _sync_jobs_atomically([{"id": 1}, {"id": 2}])
        assert failed == []

    @patch("apps.dashboard_reports.tasks.transaction.atomic")
    @patch("apps.dashboard_reports.tasks.JobData.create_or_update_from_awx")
    def test_partial_failure_returns_failed_ids(self, mock_create, mock_atomic):
        """_sync_jobs_atomically collects failed job IDs and rolls back."""
        mock_atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_atomic.return_value.__exit__ = MagicMock(return_value=False)
        mock_create.side_effect = Exception("db error")
        failed = _sync_jobs_atomically([{"id": 10}, {"id": 11}])
        assert set(failed) == {10, 11}

    @patch("apps.dashboard_reports.tasks.transaction.atomic")
    def test_empty_jobs_list_returns_empty(self, mock_atomic):
        """_sync_jobs_atomically returns empty list with no jobs."""
        mock_atomic.return_value.__enter__ = MagicMock(return_value=None)
        mock_atomic.return_value.__exit__ = MagicMock(return_value=False)
        failed = _sync_jobs_atomically([])
        assert failed == []


# --- Fixtures ---
@pytest.fixture
def mock_collect_data():
    """Patch _collect_data for dashboard_reports tasks."""
    with patch("apps.dashboard_reports.tasks._collect_data") as mock:
        yield mock


@pytest.fixture
def mock_create_task_result():
    """Patch create_task_result for dashboard_reports tasks."""
    with patch("apps.dashboard_reports.tasks.create_task_result") as mock:
        yield mock


@pytest.fixture
def mock_collect_data_deps():
    """Patch the internal helpers called by _collect_data."""
    since = datetime(2024, 1, 1, tzinfo=UTC)
    until = datetime(2024, 2, 1, tzinfo=UTC)
    with (
        patch("apps.dashboard_reports.tasks._resolve_collection_params") as mock_params,
        patch("apps.dashboard_reports.tasks.get_db_connection") as mock_db_conn,
        patch("apps.dashboard_reports.tasks._get_job_id_range") as mock_id_range,
        patch("apps.dashboard_reports.tasks._process_batches") as mock_batches,
        patch("apps.dashboard_reports.tasks.log_task_execution") as mock_log,
    ):
        mock_params.return_value = ("awx", since, until, 5000)
        mock_db_conn.return_value = MagicMock()
        mock_id_range.return_value = (100, 200)
        mock_batches.return_value = (42, None)
        yield mock_params, mock_db_conn, mock_id_range, mock_batches, mock_log, since, until


@pytest.mark.unit
class TestCollectJobs:
    """Tests for the _collect_jobs helper."""

    @patch("apps.dashboard_reports.tasks.dashboard_jobs")
    def test_passes_all_args_to_dashboard_jobs(self, mock_dashboard_jobs):
        """_collect_jobs forwards all keyword args including pagination params."""
        db_connection = MagicMock()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        until = datetime(2024, 2, 1, tzinfo=UTC)
        expected_result = MagicMock()
        mock_dashboard_jobs.return_value.gather.return_value = expected_result

        result = _collect_jobs(db_connection, since, until, after_id=50, batch_size=100)

        mock_dashboard_jobs.assert_called_once_with(
            db=db_connection, since=since, until=until, after_id=50, batch_size=100, date_field="finished"
        )
        mock_dashboard_jobs.return_value.gather.assert_called_once()
        assert result == expected_result

    @patch("apps.dashboard_reports.tasks.dashboard_jobs")
    def test_defaults_pagination_params_to_none(self, mock_dashboard_jobs):
        """_collect_jobs defaults after_id and batch_size to None."""
        db_connection = MagicMock()
        since = datetime(2024, 1, 1, tzinfo=UTC)
        until = datetime(2024, 2, 1, tzinfo=UTC)
        mock_dashboard_jobs.return_value.gather.return_value = {}

        _collect_jobs(db_connection, since, until)

        mock_dashboard_jobs.assert_called_once_with(
            db=db_connection, since=since, until=until, after_id=None, batch_size=None, date_field="finished"
        )


@pytest.mark.unit
class TestCollectData:
    """Tests for the _collect_data core logic."""

    def test_success_returns_job_count(self, mock_collect_data_deps):
        """_collect_data returns correct job_count on success."""
        _, _, _, _, _, since, until = mock_collect_data_deps
        result = _collect_data("test_task")
        assert result["error"] is False
        assert result["data"]["job_count"] == 42
        assert result["data"]["date_range"]["start"] == since.isoformat()
        assert result["data"]["date_range"]["end"] == until.isoformat()

    def test_invalid_date_range_returns_error(self, mock_collect_data_deps):
        """_collect_data returns an error when since >= until."""
        mock_params, *_ = mock_collect_data_deps
        since = datetime(2024, 2, 1, tzinfo=UTC)
        until = datetime(2024, 1, 1, tzinfo=UTC)
        mock_params.return_value = ("awx", since, until, 5000)

        result = _collect_data("test_task")

        assert result["error"] is True
        assert "Invalid date range" in result["message"]

    def test_db_connection_error_returns_error(self, mock_collect_data_deps):
        """_collect_data returns error when DB connection fails."""
        _, mock_db_conn, *_ = mock_collect_data_deps
        mock_db_conn.side_effect = Exception("connection refused")

        result = _collect_data("test_task")

        assert result["error"] is True
        assert "Database error" in result["message"]

    def test_no_jobs_in_range_returns_zero_count(self, mock_collect_data_deps):
        """_collect_data returns job_count=0 when min_id is None (no jobs in window)."""
        _, _, mock_id_range, *_ = mock_collect_data_deps
        mock_id_range.return_value = (None, None)

        result = _collect_data("test_task")

        assert result["error"] is False
        assert result["data"]["job_count"] == 0

    def test_batch_error_propagates(self, mock_collect_data_deps):
        """_collect_data returns error when _process_batches reports a failure."""
        _, _, _, mock_batches, *_ = mock_collect_data_deps
        mock_batches.return_value = (0, "Collecting jobs failed: timeout")

        result = _collect_data("test_task")

        assert result["error"] is True
        assert "Collecting jobs failed" in result["message"]

    def test_invalid_settings_produce_clean_error(self, mock_collect_data_deps):
        """A ValueError from _resolve_collection_params is caught and returned as a task error."""
        mock_params, *_ = mock_collect_data_deps
        mock_params.side_effect = ValueError("BACKFILL_BATCH_SIZE must be a positive integer, got 'oops'")

        result = _collect_data("test_task")

        assert result["error"] is True
        assert "BACKFILL_BATCH_SIZE" in result["message"]

    def test_cursor_starts_at_min_id_minus_one(self, mock_collect_data_deps):
        """_collect_data always starts batching from min_id - 1 (safe cursor for concurrent writes)."""
        _, _, mock_id_range, mock_batches, *_ = mock_collect_data_deps
        mock_id_range.return_value = (500, 1000)

        _collect_data("test_task")

        call_kwargs = mock_batches.call_args
        # after_id is the 4th positional arg: (db_connection, since, until, max_id, after_id, ...)
        after_id_arg = call_kwargs[0][4]
        assert after_id_arg == 499  # min_id - 1


@pytest.mark.unit
class TestCollectDashboardReportsInitialData:
    """Tests for collect_dashboard_reports_initial_data."""

    def test_error_from_collect_data_propagates(self):
        """collect_dashboard_reports_initial_data returns error when _collect_data fails."""
        with (
            patch("apps.dashboard_reports.tasks._collect_data") as mock_collect,
            patch("apps.dashboard_reports.tasks.create_task_result") as mock_result,
            patch("apps.dashboard_reports.tasks._save_telemetry_details"),
        ):
            mock_collect.return_value = {"error": True, "message": "fail"}
            collect_dashboard_reports_initial_data()
            mock_result.assert_called_with("error", error="fail")

    def test_success_returns_data(self):
        """collect_dashboard_reports_initial_data returns success with data on success."""
        with (
            patch("apps.dashboard_reports.tasks._collect_data") as mock_collect,
            patch("apps.dashboard_reports.tasks.create_task_result") as mock_result,
            patch("apps.dashboard_reports.tasks._save_telemetry_details"),
        ):
            mock_collect.return_value = {"error": False, "data": {"job_count": 10}}
            collect_dashboard_reports_initial_data()
            mock_result.assert_called_with("success", data={"job_count": 10})

    def test_no_follow_up_task_creation(self):
        """collect_dashboard_reports_initial_data does NOT create a follow-up Task object."""
        with (
            patch("apps.dashboard_reports.tasks._collect_data") as mock_collect,
            patch("apps.dashboard_reports.tasks.create_task_result"),
            patch("apps.dashboard_reports.tasks._save_telemetry_details"),
            patch("apps.tasks.models.Task") as mock_task,
        ):
            mock_collect.return_value = {"error": False, "data": {}}
            collect_dashboard_reports_initial_data()
            mock_task.objects.get_or_create.assert_not_called()


@pytest.mark.unit
class TestCollectDashboardReportsData:
    """Tests for the deprecated collect_dashboard_reports_data task."""

    def test_error_in_collect_data(self, mock_collect_data, mock_create_task_result):
        """Returns error result when _collect_data reports failure."""
        mock_collect_data.return_value = {"error": True, "message": "fail"}
        collect_dashboard_reports_data()
        mock_create_task_result.assert_called_with("error", error="fail")

    def test_success_in_collect_data(self, mock_collect_data, mock_create_task_result):
        """Returns success result with data when _collect_data succeeds."""
        mock_collect_data.return_value = {"error": False, "data": {"foo": "bar"}}
        collect_dashboard_reports_data()
        mock_create_task_result.assert_called_with("success", data={"foo": "bar"})

    def test_missing_data_key(self, mock_collect_data, mock_create_task_result):
        """Returns success with empty dict when data key is absent from _collect_data result."""
        mock_collect_data.return_value = {"error": False}
        collect_dashboard_reports_data()
        mock_create_task_result.assert_called_with("success", data={})

    def test_error_message_fallback(self, mock_collect_data, mock_create_task_result):
        """Uses a generic error message when _collect_data returns no message."""
        mock_collect_data.return_value = {"error": True}
        collect_dashboard_reports_data()
        args, kwargs = mock_create_task_result.call_args
        assert args[0] == "error"
        assert "An unknown error occurred" in kwargs["error"]


@pytest.mark.unit
class TestSyncDashboardJobRecords:
    """Tests for sync_dashboard_job_records — the hourly hook-driven sync task."""

    @pytest.fixture(autouse=True)
    def patch_telemetry(self):
        with patch("apps.dashboard_reports.tasks._save_telemetry_details"):
            yield

    def _raw_job(self, job_id=1, num_hosts=5, label_ids=None):
        """Return a minimal raw job dict suitable for passing as raw_jobs input."""
        return {
            "id": job_id,
            "name": "Test Job",
            "unified_job_template_id": 10,
            "organization_id": 1,
            "organization_name": "Org",
            "started": "2024-01-01T00:00:00+00:00",
            "finished": "2024-01-01T01:00:00+00:00",
            "status": "successful",
            "elapsed": 3600.0,
            "launched_by_id": 5,
            "launched_by_username": "user",
            "project_id": 2,
            "project_name": "Project",
            "created": "2024-01-01T00:00:00+00:00",
            "modified": "2024-01-01T01:00:00+00:00",
            "label_ids": label_ids,
            "num_hosts": num_hosts,
        }

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_success_with_jobs(self, mock_result, mock_log, mock_sync):
        """sync_dashboard_job_records returns success with correct job_count."""
        raw_jobs = [self._raw_job(job_id=1), self._raw_job(job_id=2)]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        mock_result.assert_called_once()
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 2

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[1])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_failed_jobs_returns_error(self, mock_result, mock_log, mock_sync):
        """sync_dashboard_job_records returns error when _sync_jobs_atomically reports failures."""
        sync_dashboard_job_records(raw_jobs=[self._raw_job()], hour_timestamp="2024-01-01T00:00:00")
        args, kwargs = mock_result.call_args
        assert args[0] == "error"
        assert "Failed to sync" in kwargs["error"]

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_num_hosts_none_coerced_to_zero(self, mock_result, mock_log, mock_sync):
        """num_hosts=None in raw data is coerced to 0 rather than raising IntegrityError."""
        raw_jobs = [self._raw_job(num_hosts=None)]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["num_hosts"] == 0

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_label_ids_parsed_from_csv(self, mock_result, mock_log, mock_sync):
        """label_ids CSV string is parsed into a list of ints."""
        raw_jobs = [self._raw_job(label_ids="3,7,12")]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["labels"] == [3, 7, 12]

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_label_ids_with_spaces_parsed_correctly(self, mock_result, mock_log, mock_sync):
        """label_ids CSV with spaces around values (e.g. '3, 7, 12') is parsed without ValueError."""
        raw_jobs = [self._raw_job(label_ids="3, 7, 12")]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["labels"] == [3, 7, 12]

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_label_ids_zero_is_not_treated_as_empty(self, mock_result, mock_log, mock_sync):
        """label_ids=0 (falsy but valid scalar) must not be collapsed to an empty list."""
        raw_jobs = [self._raw_job(label_ids=0)]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["labels"] == [0]

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_empty_raw_jobs(self, mock_result, mock_log, mock_sync):
        """sync_dashboard_job_records handles empty raw_jobs gracefully."""
        sync_dashboard_job_records(raw_jobs=[], hour_timestamp="2024-01-01T00:00:00")
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 0

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_host_summaries_set_to_none(self, mock_result, mock_log, mock_sync):
        """host_summaries is always None so backfill records are preserved by hourly sync."""
        raw_jobs = [self._raw_job()]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["host_summaries"] is None

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_label_ids_column_absent_sets_labels_none(self, mock_result, mock_log, mock_sync):
        """When label_ids key is absent from the row (older metrics_utility), labels=None to preserve existing records."""
        raw_job = self._raw_job()
        raw_job.pop("label_ids")  # simulate column not returned by older collector
        sync_dashboard_job_records(raw_jobs=[raw_job], hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["labels"] is None

    @patch("apps.dashboard_reports.tasks._sync_jobs_atomically", return_value=[])
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("apps.dashboard_reports.tasks.create_task_result")
    def test_label_ids_null_in_row_sets_labels_empty_list(self, mock_result, mock_log, mock_sync):
        """When label_ids key is present but None (job has no labels in AWX), labels=[] to clear stale records."""
        raw_jobs = [self._raw_job(label_ids=None)]
        sync_dashboard_job_records(raw_jobs=raw_jobs, hour_timestamp="2024-01-01T00:00:00")
        assembled = mock_sync.call_args[0][0]
        assert assembled[0]["labels"] == []


@pytest.mark.unit
class TestCollectDataConnectionHandling:
    """Connection must never be closed by _collect_data (shared singleton)."""

    def test_does_not_close_connection_on_success(self, mock_collect_data_deps):
        """Connection is not closed after a successful collection (shared singleton must remain open)."""
        _, mock_db_conn, _, mock_batches, *_ = mock_collect_data_deps
        mock_connection = MagicMock()
        mock_db_conn.return_value = mock_connection
        mock_batches.return_value = (1, None)

        _collect_data("test_task")

        mock_connection.close.assert_not_called()

    def test_does_not_close_connection_on_batch_error(self, mock_collect_data_deps):
        """Connection is not closed even when a batch error is reported."""
        _, mock_db_conn, _, mock_batches, *_ = mock_collect_data_deps
        mock_connection = MagicMock()
        mock_db_conn.return_value = mock_connection
        mock_batches.return_value = (0, "Collecting jobs failed: timeout")

        _collect_data("test_task")

        mock_connection.close.assert_not_called()


# --- Cleanup tests ---
@pytest.mark.unit
class TestCleanupDashboardReportsOldData:
    """Tests for cleanup_dashboard_reports_old_data."""

    @pytest.fixture
    def mock_jobdata_objects(self):
        """Patch JobData ORM calls."""
        with patch("apps.dashboard_reports.tasks.JobData") as mock_jobdata:
            yield mock_jobdata

    @pytest.fixture
    def mock_log_task_execution(self):
        """Patch log_task_execution."""
        with patch("apps.dashboard_reports.tasks.log_task_execution") as mock_log:
            yield mock_log

    @pytest.fixture
    def mock_create_task_result_cleanup(self):
        """Patch create_task_result for cleanup tests."""
        with patch("apps.dashboard_reports.tasks.create_task_result") as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def mock_save_telemetry(self):
        """Patch _save_telemetry_details so unit tests don't hit the DB."""
        with patch("apps.dashboard_reports.tasks._save_telemetry_details"):
            yield

    @pytest.fixture(autouse=True)
    def mock_retention_days(self):
        """Patch get_retention_days so cleanup tests don't need an AWX DB connection."""
        with patch("apps.dashboard_reports.tasks.get_retention_days", return_value=DEFAULT_RETENTION_DAYS) as mock:
            yield mock

    @pytest.fixture
    def with_controller_retention(self):
        """Enable the controller-retention path so get_retention_days() is called."""
        with patch("apps.dashboard_reports.tasks._use_controller_retention", return_value=True):
            yield

    def test_cleanup_success(self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup):
        """Successful deletion returns a success result with the correct record count."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 5
        mock_create_task_result_cleanup.return_value = {"result": "success"}
        result = cleanup_dashboard_reports_old_data()
        assert result["result"] == "success"
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "success"
        assert kwargs["data"]["deleted_records"] == 5
        assert "cutoff_date" in kwargs["data"]
        assert "retention_days" in kwargs["data"]

    def test_cleanup_no_records(self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup):
        """Returns success with deleted_records=0 when nothing falls outside the retention window."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data()
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "success"
        assert kwargs["data"]["deleted_records"] == 0

    def test_cleanup_exception(self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup):
        """Returns an error result when the delete call raises."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 3
        mock_jobdata_objects.objects.filter.return_value.delete.side_effect = Exception("db error")
        cleanup_dashboard_reports_old_data()
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "error"
        assert "Cleanup failed" in kwargs["error"]

    def test_cleanup_defaults_to_get_retention_days(
        self,
        with_controller_retention,
        mock_retention_days,
        mock_jobdata_objects,
        mock_log_task_execution,
        mock_create_task_result_cleanup,
    ):
        """When controller retention is enabled and retention_days is omitted, get_retention_days() determines the default."""
        mock_retention_days.return_value = 60
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data()
        _, kwargs = mock_create_task_result_cleanup.call_args
        assert kwargs["data"]["retention_days"] == 60

    def test_cleanup_passes_db_name_to_get_retention_days(
        self,
        with_controller_retention,
        mock_retention_days,
        mock_jobdata_objects,
        mock_log_task_execution,
        mock_create_task_result_cleanup,
    ):
        """The 'awx_database' kwarg is forwarded to get_retention_days so retention and collection use the same connection."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data(awx_database="custom_db")
        mock_retention_days.assert_called_once_with("custom_db")

    def test_cleanup_passes_default_db_name_when_no_database_kwarg(
        self,
        with_controller_retention,
        mock_retention_days,
        mock_jobdata_objects,
        mock_log_task_execution,
        mock_create_task_result_cleanup,
    ):
        """When 'awx_database' kwarg is absent, get_retention_days is called with the 'awx' alias."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data()
        mock_retention_days.assert_called_once_with("awx")

    def test_cleanup_deprecated_database_kwarg_still_honored(
        self,
        with_controller_retention,
        mock_retention_days,
        mock_jobdata_objects,
        mock_log_task_execution,
        mock_create_task_result_cleanup,
    ):
        """The pre-rename 'database' kwarg still resolves db_name rather than being silently dropped."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data(database="legacy_db")
        mock_retention_days.assert_called_once_with("legacy_db")

    def test_cleanup_deprecated_retention_period_days_kwarg_still_honored(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """The pre-rename 'retention_period_days' kwarg still sets retention_days rather than being silently dropped."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data(retention_period_days=15)
        _, kwargs = mock_create_task_result_cleanup.call_args
        assert kwargs["data"]["retention_days"] == 15

    def test_cleanup_defaults_to_default_retention_days(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """Falls back to DEFAULT_RETENTION_DAYS when get_retention_days returns the default."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 0
        cleanup_dashboard_reports_old_data()
        _, kwargs = mock_create_task_result_cleanup.call_args
        assert kwargs["data"]["retention_days"] == DEFAULT_RETENTION_DAYS

    def test_cleanup_custom_retention(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """An explicit retention_days kwarg overrides the settings default."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 2
        cleanup_dashboard_reports_old_data(retention_days=30)
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert kwargs["data"]["retention_days"] == 30

    def test_cleanup_cutoff_date(self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup):
        """Cutoff date in the result is midnight on the expected day."""
        mock_jobdata_objects.objects.filter.return_value.count.return_value = 1
        cleanup_dashboard_reports_old_data(retention_days=1)
        _, kwargs = mock_create_task_result_cleanup.call_args
        actual_cutoff = kwargs["data"]["cutoff_date"]
        expected_date = (datetime.now(tz=UTC) - timedelta(days=1)).date().isoformat()
        assert actual_cutoff.startswith(expected_date)

    def test_cleanup_invalid_retention_string(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """Non-integer retention_days returns an error result."""
        cleanup_dashboard_reports_old_data(retention_days="not-a-number")
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "error"
        assert "Invalid retention_days" in kwargs["error"]

    def test_cleanup_negative_retention_returns_error(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """Negative retention_days is rejected rather than clamped, since a cutoff of 'now' would delete everything."""
        cleanup_dashboard_reports_old_data(retention_days=-5)
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "error"
        assert "retention_days must be > 0" in kwargs["error"]

    def test_cleanup_zero_retention_returns_error(
        self, mock_jobdata_objects, mock_log_task_execution, mock_create_task_result_cleanup
    ):
        """Zero retention_days is rejected rather than deleting all records."""
        cleanup_dashboard_reports_old_data(retention_days=0)
        args, kwargs = mock_create_task_result_cleanup.call_args
        assert args[0] == "error"
        assert "retention_days must be > 0" in kwargs["error"]

    @pytest.mark.integration
    @pytest.mark.django_db
    def test_cleanup_with_db_data(self, db):
        """Test cleanup_dashboard_reports_old_data with real JobData records in the database."""
        cutoff = datetime.now().astimezone(UTC) - timedelta(days=90)
        old_job1 = JobData.objects.create(
            job_id=1001, finished=cutoff - timedelta(days=1), status="successful", elapsed=1.0
        )
        old_job2 = JobData.objects.create(
            job_id=1002, finished=cutoff - timedelta(days=10), status="successful", elapsed=1.0
        )
        new_job1 = JobData.objects.create(
            job_id=1003, finished=cutoff + timedelta(days=1), status="successful", elapsed=1.0
        )
        new_job2 = JobData.objects.create(
            job_id=1004, finished=cutoff + timedelta(days=10), status="successful", elapsed=1.0
        )
        result = cleanup_dashboard_reports_old_data(retention_days=90)
        assert result["deleted_records"] == 2

        remaining_ids = set(JobData.objects.values_list("id", flat=True))
        assert old_job1.id not in remaining_ids
        assert old_job2.id not in remaining_ids
        assert new_job1.id in remaining_ids
        assert new_job2.id in remaining_ids


# ---------------------------------------------------------------------------
# _resolve_collection_params
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestResolveCollectionParams:
    """Tests for _resolve_collection_params — all branches, no DB needed."""

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_explicit_since_until_used_directly(self, mock_jobdata):
        """When since and until are provided they are used without touching DB."""

        since = datetime(2024, 1, 1, tzinfo=UTC)
        until = datetime(2024, 2, 1, tzinfo=UTC)
        db_name, got_since, got_until, batch_size = _resolve_collection_params(
            "collect_dashboard_reports_data", {"since": since.isoformat(), "until": until.isoformat()}
        )
        assert got_since == since
        assert got_until == until
        assert db_name == "awx"
        mock_jobdata.last_finished_timestamp.assert_not_called()

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_since_falls_back_to_last_timestamp(self, mock_jobdata):
        """When since is absent and last_timestamp() has a value, it is used as since."""
        ts = datetime(2024, 3, 15, tzinfo=UTC)
        mock_jobdata.last_finished_timestamp.return_value = ts
        _, got_since, _, _ = _resolve_collection_params("collect_dashboard_reports_data", {})
        assert got_since == ts

    @patch("apps.dashboard_reports.tasks._use_controller_retention", return_value=True)
    @patch("apps.dashboard_reports.tasks.get_retention_days", return_value=30)
    @patch("apps.dashboard_reports.tasks.JobData")
    def test_since_computed_from_retention_days_when_no_timestamp(self, mock_jobdata, mock_retention, _mock_use_ctrl):
        """When controller retention is enabled and last_timestamp() is None, compute since from get_retention_days()."""
        from django.test import override_settings

        mock_jobdata.last_finished_timestamp.return_value = None
        until = datetime(2024, 6, 1, tzinfo=UTC)

        with override_settings(DASHBOARD_COLLECTION={"BACKFILL_BATCH_SIZE": 500}):
            _, got_since, _, batch_size = _resolve_collection_params(
                "collect_dashboard_reports_data", {"until": until.isoformat()}
            )

        expected_since = (until - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        assert got_since == expected_since
        assert batch_size == 500
        mock_retention.assert_called_once_with("awx")

    @patch("apps.dashboard_reports.tasks._use_controller_retention", return_value=True)
    @patch("apps.dashboard_reports.tasks.get_retention_days", return_value=30)
    @patch("apps.dashboard_reports.tasks.JobData")
    def test_custom_database_kwarg_forwarded_to_retention(self, mock_jobdata, mock_retention, _mock_use_ctrl):
        """When controller retention is enabled and no prior timestamp exists, db_name is passed to get_retention_days."""
        mock_jobdata.last_finished_timestamp.return_value = None
        until = datetime(2024, 6, 1, tzinfo=UTC)
        _resolve_collection_params(
            "collect_dashboard_reports_data", {"awx_database": "custom_db", "until": until.isoformat()}
        )
        mock_retention.assert_called_once_with("custom_db")

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_custom_database_kwarg(self, mock_jobdata):
        """The 'awx_database' kwarg overrides the DEFAULT_AWX_DB_NAME."""
        mock_jobdata.last_finished_timestamp.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        db_name, *_ = _resolve_collection_params("collect_dashboard_reports_data", {"awx_database": "custom_db"})
        assert db_name == "custom_db"

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_default_batch_size_applied(self, mock_jobdata):
        """BACKFILL_BATCH_SIZE defaults to 5_000 when not set."""
        mock_jobdata.last_finished_timestamp.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        _, _, _, batch_size = _resolve_collection_params("collect_dashboard_reports_data", {})
        assert batch_size == 5_000

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_invalid_batch_size_raises_value_error(self, mock_jobdata):
        """A non-integer BACKFILL_BATCH_SIZE raises ValueError with a descriptive message."""
        from django.test import override_settings

        mock_jobdata.last_finished_timestamp.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        with (
            override_settings(DASHBOARD_COLLECTION={"BACKFILL_BATCH_SIZE": "not-a-number"}),
            pytest.raises(ValueError, match="BACKFILL_BATCH_SIZE"),
        ):
            _resolve_collection_params("collect_dashboard_reports_data", {})

    @patch("apps.dashboard_reports.tasks._use_controller_retention", return_value=True)
    @patch("apps.dashboard_reports.tasks.get_retention_days", return_value=0)
    @patch("apps.dashboard_reports.tasks.JobData")
    def test_zero_retention_days_raises_value_error(self, mock_jobdata, mock_retention, _mock_use_ctrl):
        """When controller retention is enabled, get_retention_days returning 0 raises ValueError."""
        mock_jobdata.last_finished_timestamp.return_value = None
        with pytest.raises(ValueError, match="Retention days must be > 0"):
            _resolve_collection_params("collect_dashboard_reports_data", {})

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_deprecated_database_kwarg_still_honored(self, mock_jobdata):
        """The old 'database' kwarg name (pre-rename) still resolves db_name, with a warning."""
        mock_jobdata.last_finished_timestamp.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        db_name, *_ = _resolve_collection_params("collect_dashboard_reports_data", {"database": "legacy_db"})
        assert db_name == "legacy_db"

    @patch("apps.dashboard_reports.tasks.JobData")
    def test_new_kwarg_takes_precedence_over_deprecated(self, mock_jobdata):
        """When both 'awx_database' and 'database' are given, the new name wins."""
        mock_jobdata.last_finished_timestamp.return_value = datetime(2024, 1, 1, tzinfo=UTC)
        db_name, *_ = _resolve_collection_params(
            "collect_dashboard_reports_data", {"awx_database": "new_db", "database": "old_db"}
        )
        assert db_name == "new_db"


# ---------------------------------------------------------------------------
# _get_renamed_kwarg
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetRenamedKwarg:
    """Tests for the _get_renamed_kwarg deprecated-alias helper."""

    def test_new_key_present_returns_its_value(self):
        assert _get_renamed_kwarg({"new": 1, "old": 2}, "new", "old", "task") == 1

    def test_only_old_key_present_returns_its_value_and_warns(self):
        with patch("apps.dashboard_reports.tasks.logger") as mock_logger:
            result = _get_renamed_kwarg({"old": 2}, "new", "old", "task")
        assert result == 2
        mock_logger.warning.assert_called_once()
        assert "deprecated" in mock_logger.warning.call_args[0][0]

    def test_neither_key_present_returns_missing_sentinel(self):
        assert _get_renamed_kwarg({}, "new", "old", "task") is _MISSING


# ---------------------------------------------------------------------------
# get_retention_days
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetRetentionDays:
    """Tests for get_retention_days — all branches, no real AWX DB needed."""

    def _row(self, job_type="cleanup_jobs", enabled=True, retention_days=30, next_run=None):
        return {
            "job_type": job_type,
            "template_name": "Cleanup",
            "schedule_name": "Daily Cleanup",
            "schedule_enabled": enabled,
            "rrule": "FREQ=DAILY",
            "next_run": next_run,
            "retention_days": retention_days,
        }

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings", side_effect=Exception("db down"))
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_db_error_returns_default(self, mock_conn, mock_fetch):
        """Returns DEFAULT_RETENTION_DAYS when the AWX DB is unreachable."""
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings", return_value=([], 0))
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_empty_result_returns_default(self, mock_conn, mock_fetch):
        """Returns DEFAULT_RETENTION_DAYS when no schedules are returned."""
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_no_active_schedules_returns_default(self, mock_conn, mock_fetch):
        """Returns DEFAULT_RETENTION_DAYS when all cleanup_jobs schedules are disabled."""
        mock_fetch.return_value = ([self._row(enabled=False, retention_days=7)], 1)
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_null_retention_days_ignored(self, mock_conn, mock_fetch):
        """Rows with retention_days=None are excluded; falls back to DEFAULT_RETENTION_DAYS when all are None."""
        mock_fetch.return_value = ([self._row(retention_days=None)], 1)
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_single_active_schedule(self, mock_conn, mock_fetch):
        """Returns retention_days of the single active cleanup_jobs schedule."""
        mock_fetch.return_value = ([self._row(retention_days=45)], 1)
        assert get_retention_days() == 45

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_non_integer_retention_days_returns_default(self, mock_conn, mock_fetch):
        """Falls back to DEFAULT_RETENTION_DAYS when the only schedule's retention_days isn't a valid integer."""
        mock_fetch.return_value = ([self._row(retention_days="not-a-number")], 1)
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_invalid_retention_days_skipped_others_kept(self, mock_conn, mock_fetch):
        """A schedule with a non-integer retention_days is skipped individually; other valid schedules still count."""
        rows = [self._row(retention_days="not-a-number"), self._row(retention_days=45)]
        mock_fetch.return_value = (rows, 2)
        assert get_retention_days() == 45

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_lowest_retention_days_wins(self, mock_conn, mock_fetch):
        """When multiple schedules exist, the lowest retention_days is used."""
        rows = [self._row(retention_days=60), self._row(retention_days=30), self._row(retention_days=90)]
        mock_fetch.return_value = (rows, 3)
        assert get_retention_days() == 30

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_tie_broken_by_soonest_next_run(self, mock_conn, mock_fetch):
        """On equal retention_days, the schedule with the soonest next_run wins."""
        rows = [
            self._row(retention_days=30, next_run="2024-06-10T00:00:00+00:00"),
            self._row(retention_days=30, next_run="2024-06-05T00:00:00+00:00"),
        ]
        mock_fetch.return_value = (rows, 2)
        result = get_retention_days()
        assert result == 30

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_bad_next_run_type_skips_tiebreak(self, mock_conn, mock_fetch):
        """A TypeError from _parse_dt during tie-break is caught; current best is kept without crashing."""
        rows = [
            self._row(retention_days=30, next_run="2024-06-01T00:00:00+00:00"),
            self._row(retention_days=30, next_run=99999),  # int → TypeError from _parse_dt
        ]
        mock_fetch.return_value = (rows, 2)
        assert get_retention_days() == 30

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_activitystream_schedules_ignored(self, mock_conn, mock_fetch):
        """cleanup_activitystream schedules do not influence JobData retention."""
        rows = [
            self._row(job_type="cleanup_activitystream", retention_days=7),
            self._row(job_type="cleanup_jobs", retention_days=60),
        ]
        mock_fetch.return_value = (rows, 2)
        assert get_retention_days() == 60

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_only_activitystream_present_returns_default(self, mock_conn, mock_fetch):
        """Returns DEFAULT_RETENTION_DAYS when only cleanup_activitystream schedules exist."""
        mock_fetch.return_value = ([self._row(job_type="cleanup_activitystream", retention_days=7)], 1)
        assert get_retention_days() == DEFAULT_RETENTION_DAYS

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_default_awx_db_name_passed_to_connection(self, mock_conn, mock_fetch):
        """get_db_connection is called with the 'awx' alias by default."""
        mock_fetch.return_value = ([self._row(retention_days=30)], 1)
        get_retention_days()
        mock_conn.assert_called_once_with("awx")

    @patch("apps.dashboard_reports.tasks.fetch_retention_settings")
    @patch("apps.dashboard_reports.tasks.get_db_connection")
    def test_custom_db_name_passed_to_connection(self, mock_conn, mock_fetch):
        """A custom db_name argument is forwarded to get_db_connection."""
        mock_fetch.return_value = ([self._row(retention_days=30)], 1)
        get_retention_days("custom_db")
        mock_conn.assert_called_once_with("custom_db")


# ---------------------------------------------------------------------------
# _get_job_id_range
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetJobIdRange:
    """Tests for _get_job_id_range — mocks both the query builder and DB cursor."""

    def _mock_db(self, fetchone_return):
        """Return a mock DB connection whose cursor context manager yields fetchone_return."""
        db = MagicMock()
        cursor_ctx = MagicMock()
        cursor_ctx.__enter__ = MagicMock(return_value=cursor_ctx)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        cursor_ctx.fetchone.return_value = fetchone_return
        db.cursor.return_value = cursor_ctx
        return db

    def test_returns_min_max_when_row_exists(self):
        """_get_job_id_range returns (min_id, max_id) from the cursor row."""
        since = datetime(2024, 1, 1, tzinfo=UTC)
        until = datetime(2024, 2, 1, tzinfo=UTC)
        db = self._mock_db(fetchone_return=(100, 999))

        with patch(
            "metrics_utility.library.collectors.dashboard.get_min_max_job_id_query",
            create=True,
            return_value=("SELECT 1", []),
        ):
            result = _get_job_id_range(db, since, until)

        assert result == (100, 999)

    def test_returns_none_none_when_no_rows(self):
        """_get_job_id_range returns (None, None) when cursor returns nothing."""
        since = datetime(2024, 1, 1, tzinfo=UTC)
        until = datetime(2024, 2, 1, tzinfo=UTC)
        db = self._mock_db(fetchone_return=None)

        with patch(
            "metrics_utility.library.collectors.dashboard.get_min_max_job_id_query",
            create=True,
            return_value=("SELECT 1", []),
        ):
            result = _get_job_id_range(db, since, until)

        assert result == (None, None)


# ---------------------------------------------------------------------------
# _process_batches
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestProcessBatches:
    """Tests for _process_batches — mocks _collect_jobs and _sync_jobs_atomically."""

    _since = datetime(2024, 1, 1, tzinfo=UTC)
    _until = datetime(2024, 2, 1, tzinfo=UTC)

    def _call(self, after_id, max_id, collect_side_effect=None, sync_return=None):
        """Invoke _process_batches with mocked _collect_jobs and _sync_jobs_atomically."""
        db = MagicMock()
        with (
            patch("apps.dashboard_reports.tasks._collect_jobs") as mock_collect,
            patch("apps.dashboard_reports.tasks._sync_jobs_atomically") as mock_sync,
            patch("apps.dashboard_reports.tasks.log_task_execution"),
        ):
            if collect_side_effect is not None:
                mock_collect.side_effect = collect_side_effect
            if sync_return is not None:
                mock_sync.return_value = sync_return
            result = _process_batches(db, self._since, self._until, max_id, after_id, 100, "test_task")
        return result, mock_collect, mock_sync

    def test_never_loops_when_cursor_at_max(self):
        """No collection happens when after_id >= max_id."""
        (total, err), mock_collect, _ = self._call(after_id=500, max_id=500)
        assert total == 0
        assert err is None
        mock_collect.assert_not_called()

    def test_single_successful_batch(self):
        """A single batch with results commits and returns (count, None)."""
        batch = {"results": [{"id": 101}, {"id": 102}], "count": 2}
        collect_side_effects = [batch, {"results": [], "count": 0}]

        (total, err), _, _ = self._call(
            after_id=99, max_id=200, collect_side_effect=collect_side_effects, sync_return=[]
        )
        assert err is None
        assert total == 2

    def test_empty_batch_breaks_loop(self):
        """An empty results list causes the loop to break without error."""
        batch = {"results": [], "count": 0}
        (total, err), mock_collect, _ = self._call(after_id=99, max_id=200, collect_side_effect=[batch])
        assert err is None
        assert total == 0
        mock_collect.assert_called_once()

    def test_collect_exception_returns_error(self):
        """An exception from _collect_jobs returns an error tuple."""
        (total, err), _, _ = self._call(after_id=99, max_id=200, collect_side_effect=Exception("timeout"))
        assert err is not None
        assert "Collecting jobs failed" in err

    def test_failed_sync_returns_error(self):
        """Failed jobs from _sync_jobs_atomically returns an error tuple."""
        batch = {"results": [{"id": 101}], "count": 1}
        (total, err), _, _ = self._call(after_id=99, max_id=200, collect_side_effect=[batch], sync_return=[101])
        assert err is not None
        assert "Failed to sync" in err

    def test_multi_batch_accumulates_count(self):
        """Two successful batches accumulate total_synced correctly."""
        batch1 = {"results": [{"id": 101}, {"id": 102}], "count": 2}
        batch2 = {"results": [{"id": 103}], "count": 1}
        empty = {"results": [], "count": 0}

        (total, err), _, _ = self._call(
            after_id=99, max_id=200, collect_side_effect=[batch1, batch2, empty], sync_return=[]
        )
        assert err is None
        assert total == 3

    def test_cursor_advances_to_max_id_of_previous_batch(self):
        """The second _collect_jobs call receives after_id = max(ids) from the first batch."""
        batch1 = {"results": [{"id": 101}, {"id": 105}], "count": 2}
        empty = {"results": [], "count": 0}

        _, mock_collect, _ = self._call(after_id=99, max_id=200, collect_side_effect=[batch1, empty], sync_return=[])

        assert mock_collect.call_count == 2
        second_call_kwargs = mock_collect.call_args_list[1][1]
        assert second_call_kwargs["after_id"] == 105  # max id from batch1, not batch1["count"]


@pytest.mark.unit
class TestSyncDashboardHostSummaries:
    """Tests for sync_dashboard_host_summaries — the hourly hook-driven host summary sync task."""

    @pytest.fixture(autouse=True)
    def mock_save_telemetry(self):
        """Patch _save_telemetry_details so unit tests don't hit the DB."""
        with patch("apps.dashboard_reports.tasks._save_telemetry_details") as mock:
            yield mock

    def _raw_record(self, host_summary_id=1, host_name="web01", host_id=10, job_remote_id=42):
        """Return a minimal raw host summary dict as produced by the hook (wire format)."""
        return {
            "id": host_summary_id,
            "host_name": host_name,
            "host_id": host_id,
            "job_remote_id": job_remote_id,
        }

    def _make_job_data(self, job_id, pk=None):
        """Return a MagicMock with job_id and pk set to avoid auto-spec surprises."""
        jd = MagicMock()
        jd.job_id = job_id
        jd.pk = pk if pk is not None else job_id
        return jd

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_syncs_host_summaries_for_known_job(self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result):
        """When JobData exists for job_remote_id, _sync_host_summaries is called with remapped fields."""
        job_data = self._make_job_data(job_id=123, pk=1)
        mock_objects.filter.return_value = [job_data]
        mock_jhs.objects.filter.return_value = []

        record = self._raw_record(host_summary_id=7, host_name="db01", host_id=99, job_remote_id=123)
        sync_dashboard_host_summaries(raw_host_summaries=[record], hour_timestamp="2024-01-01T00:00:00")

        mock_sync.assert_called_once_with(
            job_data,
            [{"id": 7, "host_id": 99, "host_name": "db01"}],
            {},
        )
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 1

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    def test_skips_silently_when_job_data_not_found(self, mock_log, mock_objects, mock_jhs, mock_result):
        """When no JobData matches job_remote_id, the job is skipped without error."""
        mock_objects.filter.return_value = []
        mock_jhs.objects.filter.return_value = []

        sync_dashboard_host_summaries(raw_host_summaries=[self._raw_record()], hour_timestamp="2024-01-01T00:00:00")

        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 0

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_continues_after_individual_job_failure(self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result):
        """An exception on one job is logged and remaining jobs are still processed."""
        job_a = self._make_job_data(job_id=1, pk=1)
        job_b = self._make_job_data(job_id=2, pk=2)
        mock_objects.filter.return_value = [job_a, job_b]
        mock_jhs.objects.filter.return_value = []
        mock_sync.side_effect = [RuntimeError("oops"), None]

        records = [self._raw_record(job_remote_id=1), self._raw_record(job_remote_id=2)]
        sync_dashboard_host_summaries(raw_host_summaries=records, hour_timestamp="2024-01-01T00:00:00")

        assert mock_sync.call_count == 2
        args, kwargs = mock_result.call_args
        assert args[0] == "error"
        assert kwargs["data"]["job_count"] == 1  # only job_b succeeded
        assert kwargs["data"]["failed"] == 1

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_groups_multiple_summaries_per_job(self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result):
        """Multiple host summary records for the same job are grouped and passed together."""
        job_data = self._make_job_data(job_id=42, pk=1)
        mock_objects.filter.return_value = [job_data]
        mock_jhs.objects.filter.return_value = []

        records = [
            self._raw_record(host_summary_id=1, host_name="web01", job_remote_id=42),
            self._raw_record(host_summary_id=2, host_name="web02", job_remote_id=42),
        ]
        sync_dashboard_host_summaries(raw_host_summaries=records, hour_timestamp="2024-01-01T00:00:00")

        # One batch filter call for JobData (not one get per job).
        mock_objects.filter.assert_called_once()
        passed_summaries = mock_sync.call_args[0][1]
        assert len(passed_summaries) == 2
        assert {s["host_name"] for s in passed_summaries} == {"web01", "web02"}

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    def test_empty_raw_host_summaries(self, mock_log, mock_result):
        """Empty input returns success with job_count=0 without querying the DB."""
        sync_dashboard_host_summaries(raw_host_summaries=[], hour_timestamp="2024-01-01T00:00:00")
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 0

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_null_host_id_is_preserved(self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result):
        """host_id=None (deleted AWX host) is passed as None to _sync_host_summaries."""
        job_data = self._make_job_data(job_id=42, pk=1)
        mock_objects.filter.return_value = [job_data]
        mock_jhs.objects.filter.return_value = []

        record = self._raw_record(host_id=None)
        sync_dashboard_host_summaries(raw_host_summaries=[record], hour_timestamp="2024-01-01T00:00:00")

        passed_summaries = mock_sync.call_args[0][1]
        assert passed_summaries[0]["host_id"] is None

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    def test_records_with_null_job_remote_id_are_dropped(self, mock_log, mock_result):
        """Records where job_remote_id is None are ignored without error."""
        record = {"id": 1, "host_name": "h1", "host_id": 5, "job_remote_id": None}
        sync_dashboard_host_summaries(raw_host_summaries=[record], hour_timestamp="2024-01-01T00:00:00")
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 0

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    def test_records_with_null_id_are_dropped(self, mock_log, mock_result):
        """Records where id is None are ignored without error."""
        record = {"id": None, "host_name": "h1", "host_id": 5, "job_remote_id": 42}
        sync_dashboard_host_summaries(raw_host_summaries=[record], hour_timestamp="2024-01-01T00:00:00")
        args, kwargs = mock_result.call_args
        assert args[0] == "success"
        assert kwargs["data"]["job_count"] == 0

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_uses_pre_fetched_existing_host_summaries(self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result):
        """Existing JobHostSummary objects are pre-fetched in one query and passed per job."""
        job_data = self._make_job_data(job_id=42, pk=10)
        mock_objects.filter.return_value = [job_data]

        existing_hs = MagicMock()
        existing_hs.job_data_id = 10
        existing_hs.host_summary_id = 99
        mock_jhs.objects.filter.return_value = [existing_hs]

        record = self._raw_record(host_summary_id=99, job_remote_id=42)
        sync_dashboard_host_summaries(raw_host_summaries=[record], hour_timestamp="2024-01-01T00:00:00")

        # Confirm the existing hs was passed in the dict keyed by host_summary_id.
        passed_existing = mock_sync.call_args[0][2]
        assert 99 in passed_existing
        assert passed_existing[99] is existing_hs

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_telemetry_saved_on_success(
        self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result, mock_save_telemetry
    ):
        """_save_telemetry_details is called with success=True and the synced count."""
        job_data = self._make_job_data(job_id=42, pk=42)
        mock_objects.filter.return_value = [job_data]
        mock_jhs.objects.filter.return_value = []

        sync_dashboard_host_summaries(raw_host_summaries=[self._raw_record()], hour_timestamp="2024-01-01T00:00:00")

        mock_save_telemetry.assert_called_once()
        _, kwargs = mock_save_telemetry.call_args
        assert kwargs["task_name"] == "sync_dashboard_host_summaries"
        assert kwargs["success"] is True
        assert kwargs["number_of_records_processed"] == 1
        assert kwargs["collection_duration_ms"] >= 0

    @patch("apps.dashboard_reports.tasks.create_task_result")
    @patch("apps.dashboard_reports.tasks.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.tasks.JobHostSummary")
    @patch("apps.dashboard_reports.tasks.JobData.objects")
    @patch("apps.dashboard_reports.tasks.log_task_execution")
    @patch("django.db.transaction.atomic", new=contextlib.nullcontext)
    def test_telemetry_saved_on_failure(
        self, mock_log, mock_objects, mock_jhs, mock_sync, mock_result, mock_save_telemetry
    ):
        """_save_telemetry_details is called with success=False when any job sync fails."""
        job_data = self._make_job_data(job_id=42, pk=42)
        mock_objects.filter.return_value = [job_data]
        mock_jhs.objects.filter.return_value = []
        mock_sync.side_effect = RuntimeError("db error")

        sync_dashboard_host_summaries(raw_host_summaries=[self._raw_record()], hour_timestamp="2024-01-01T00:00:00")

        mock_save_telemetry.assert_called_once()
        _, kwargs = mock_save_telemetry.call_args
        assert kwargs["success"] is False
        assert kwargs["number_of_records_processed"] == 0


@pytest.mark.unit
class TestParseDtNaT:
    """Test _parse_dt handling of pandas NaT serialised as the string 'NaT'."""

    def test_nat_string_returns_none(self):
        """_parse_dt treats the literal string 'NaT' (pandas NaT serialised) as a missing value."""
        assert _parse_dt("NaT") is None

    def test_valid_iso_string_still_parses(self):
        """Non-NaT ISO strings are unaffected by the NaT guard."""
        result = _parse_dt("2024-06-01T12:00:00+00:00")
        assert result == datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.django_db
class TestBackfillDashboardOrganizationNames:
    """Tests for backfill_dashboard_organization_names / fill_missing_organization_names."""

    @pytest.fixture
    def mock_fetch_orgs(self) -> Iterator[MagicMock]:
        """Patch the AWX DB connection and organization lookup (org 99 does not exist in AWX)."""
        with (
            patch("apps.dashboard_reports.tasks.get_db_connection"),
            patch(
                "apps.dashboard_reports.tasks.fetch_organizations",
                return_value=([{"id": 1, "name": "Org A"}, {"id": 2, "name": "Org B"}], 2),
            ) as mock_fetch,
        ):
            yield mock_fetch

    @staticmethod
    def _job(job_id: int, organization_id: int | None, organization_name: str | None = None) -> JobData:
        return JobData.objects.create(
            job_id=job_id,
            template_name="T",
            organization_id=organization_id,
            organization_name=organization_name,
            started=datetime(2025, 3, 1, tzinfo=UTC),
            finished=datetime(2025, 3, 1, 0, 5, tzinfo=UTC),
            elapsed=300,
        )

    @staticmethod
    def _names() -> dict[int, str | None]:
        return dict(JobData.objects.values_list("job_id", "organization_name"))

    def test_runs_without_waiting_for_initial_collection(self, mock_fetch_orgs: MagicMock) -> None:
        """The backfill does not depend on the initial-collection task (it may have run long ago, or not yet)."""
        self._job(1, organization_id=1)

        result = backfill_dashboard_organization_names()

        assert result["status"] == "success"
        assert result["updated_records"] == 1

    def test_nothing_to_fix_skips_awx_query(self, mock_fetch_orgs: MagicMock) -> None:
        """When no row is missing a name the AWX DB is not queried."""
        self._job(1, organization_id=1, organization_name="Org A")

        result = backfill_dashboard_organization_names()

        assert result["status"] == "success"
        assert result["updated_records"] == 0
        mock_fetch_orgs.assert_not_called()

    def test_updates_only_rows_with_existing_organization(self, mock_fetch_orgs: MagicMock) -> None:
        """Only unnamed rows of existing orgs are filled; other rows are unchanged and counted as unresolved."""
        self._job(1, organization_id=1)
        self._job(2, organization_id=2)
        self._job(3, organization_id=1, organization_name="Synced After Fix")
        self._job(4, organization_id=None)
        self._job(5, organization_id=99)  # not found / deleted in AWX
        self._job(6, organization_id=99)

        result = backfill_dashboard_organization_names()

        assert result["status"] == "success"
        assert result["updated_records"] == 2
        assert result["unresolved_records"] == 2
        assert result["unresolved_ids"] == [99]
        assert self._names() == {1: "Org A", 2: "Org B", 3: "Synced After Fix", 4: None, 5: None, 6: None}

    def test_second_run_changes_nothing(self, mock_fetch_orgs: MagicMock) -> None:
        """Re-running only touches rows that still have no name, so a second run updates nothing."""
        self._job(1, organization_id=1)
        self._job(2, organization_id=99)
        backfill_dashboard_organization_names()
        names_after_first = self._names()

        result = backfill_dashboard_organization_names()

        assert result["status"] == "success"
        assert result["updated_records"] == 0
        assert result["unresolved_records"] == 1
        assert self._names() == names_after_first

    def test_processes_rows_in_batches(self, mock_fetch_orgs: MagicMock) -> None:
        """All rows are updated when they span several batches, including unresolved rows in between."""
        for job_id in range(1, 8):
            self._job(job_id, organization_id=99 if job_id == 4 else (1 if job_id % 2 else 2))

        with patch("apps.dashboard_reports.tasks.log_task_execution") as mock_log:
            result = backfill_dashboard_organization_names(batch_size=2)

        assert result["updated_records"] == 6
        assert result["unresolved_records"] == 1
        batch_logs = [c for c in mock_log.call_args_list if c.kwargs["operation"] == "processing"]
        assert len(batch_logs) == 3
        names = self._names()
        assert names[4] is None
        assert {names[i] for i in (1, 3, 5, 7)} == {"Org A"}
        assert {names[i] for i in (2, 6)} == {"Org B"}

    def test_numeric_string_batch_size_is_accepted(self, mock_fetch_orgs: MagicMock) -> None:
        """A numeric string batch_size (e.g. from API task_data) is accepted."""
        self._job(1, organization_id=1)

        result = backfill_dashboard_organization_names(batch_size="2")

        assert result["status"] == "success"
        assert result["updated_records"] == 1

    def test_uses_awx_database_kwarg(self, mock_fetch_orgs: MagicMock) -> None:
        """The AWX database name can be overridden via the awx_database kwarg."""
        self._job(1, organization_id=1)
        with patch("apps.dashboard_reports.tasks.get_db_connection") as mock_conn:
            backfill_dashboard_organization_names(awx_database="awx_other")

        mock_conn.assert_called_once_with("awx_other")

    @pytest.mark.parametrize("batch_size", [0, -1, "abc", True, 2.9, None])
    def test_invalid_batch_size_returns_error(self, mock_fetch_orgs: MagicMock, batch_size: Any) -> None:
        """An invalid batch_size is rejected before any rows are touched."""
        self._job(1, organization_id=1)

        result = backfill_dashboard_organization_names(batch_size=batch_size)

        assert result["status"] == "error"
        assert "batch_size" in result["error"]
        assert JobData.objects.get(job_id=1).organization_name is None

    def test_awx_error_returns_error(self) -> None:
        """A failing AWX query returns an error result, is logged, and leaves rows unchanged."""
        self._job(1, organization_id=1)
        with (
            patch("apps.dashboard_reports.tasks.get_db_connection"),
            patch("apps.dashboard_reports.tasks.fetch_organizations", side_effect=RuntimeError("boom")),
            patch("apps.dashboard_reports.tasks.log_task_execution") as mock_log,
        ):
            result = backfill_dashboard_organization_names()

        assert result["status"] == "error"
        assert "boom" in result["error"]
        assert mock_log.call_args.kwargs["operation"] == "failed"
        assert JobData.objects.get(job_id=1).organization_name is None
