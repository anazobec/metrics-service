"""Unit tests for collect_hourly_metrics — specifically _build_dashboard_sync_hook."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from apps.dashboard_reports.job_definition import serialize_dashboard_record
from apps.tasks.collectors.collect_hourly_metrics import (
    _build_dashboard_host_summary_sync_hook,
    _build_dashboard_sync_hook,
    _build_host_summary_task_chunks,
    _get_hourly_collectors,
    _group_host_summary_rows,
    _serialize_host_summary_record,
)


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal DataFrame matching the unified_jobs_dashboard schema."""
    defaults = {
        "id": 1,
        "name": "job",
        "model": "job",
        "unified_job_template_id": 10,
        "organization_id": 1,
        "organization_name": "Org",
        "started": datetime(2024, 1, 1, tzinfo=UTC),
        "finished": datetime(2024, 1, 1, 1, tzinfo=UTC),
        "status": "successful",
        "elapsed": 60.0,
        "launched_by_id": 5,
        "launched_by_username": "user",
        "project_id": 2,
        "project_name": "Project",
        "created": datetime(2024, 1, 1, tzinfo=UTC),
        "modified": datetime(2024, 1, 1, tzinfo=UTC),
        "label_ids": None,
        "num_hosts": 3,
        "launch_type": "manual",
    }
    return pd.DataFrame([{**defaults, **r} for r in rows])


HOUR_TS = datetime(2024, 1, 1, tzinfo=UTC)

# Both are lazy imports inside function bodies, so patch at the source module.
FEATURE_FLAG_PATH = "apps.tasks.task_groups.get_feature_enabled_from_db"
TASK_MODEL_PATH = "apps.tasks.models.Task"


@pytest.mark.unit
class TestBuildDashboardSyncHook:
    """Tests for _build_dashboard_sync_hook and its returned inner hook."""

    def test_returns_none_when_feature_disabled(self):
        """When DASHBOARD_COLLECTION is off, the builder returns None."""
        with patch(FEATURE_FLAG_PATH, return_value=False):
            result = _build_dashboard_sync_hook(HOUR_TS)
        assert result is None

    def test_returns_callable_when_feature_enabled(self):
        """When DASHBOARD_COLLECTION is on, the builder returns a callable hook."""
        with patch(FEATURE_FLAG_PATH, return_value=True):
            hook = _build_dashboard_sync_hook(HOUR_TS)
        assert callable(hook)

    def _enabled_hook(self):
        """Build a hook with DASHBOARD_COLLECTION enabled."""
        with patch(FEATURE_FLAG_PATH, return_value=True):
            return _build_dashboard_sync_hook(HOUR_TS)

    def test_hook_returns_early_when_raw_data_none(self):
        """hook(None) exits immediately without touching Task."""
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(None)
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_returns_early_when_dataframe_empty(self):
        """hook(empty_df) exits without creating a Task."""
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(pd.DataFrame())
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_returns_early_when_no_terminal_jobs(self):
        """If no job is in a terminal state, no Task is created."""
        df = _make_df(
            [
                {"status": "running", "launch_type": "manual"},
                {"id": 2, "status": "running", "launch_type": "sync"},
                {"id": 3, "status": "pending", "launch_type": "workflow"},
            ]
        )
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(df)
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_stores_sync_and_workflow_launch_types(self):
        """Sync and workflow jobs are stored with their launch_type; reports hide them at read time."""
        df = _make_df(
            [
                {"id": 1, "status": "successful", "launch_type": "manual"},
                {"id": 2, "status": "successful", "launch_type": "workflow"},
                {"id": 3, "status": "failed", "launch_type": "sync"},
                {"id": 4, "status": "running", "launch_type": "workflow"},
            ]
        )
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        mock_task.objects.update_or_create.assert_called_once()
        task_data = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]
        assert [(r["id"], r["launch_type"]) for r in task_data["raw_jobs"]] == [
            (1, "manual"),
            (2, "workflow"),
            (3, "sync"),
        ]

    def test_hook_creates_task_for_terminal_non_sync_jobs(self):
        """Terminal non-sync jobs cause update_or_create to be called."""
        df = _make_df(
            [
                {"status": "successful", "launch_type": "manual"},
                {"id": 2, "status": "failed", "launch_type": "scheduled"},
            ]
        )
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        mock_task.objects.update_or_create.assert_called_once()
        call_kwargs = mock_task.objects.update_or_create.call_args[1]
        assert call_kwargs["defaults"]["function_name"] == "sync_dashboard_job_records"

    def test_hook_serialises_datetime_fields_to_iso(self):
        """Datetime columns are converted to ISO strings before being stored in task_data."""
        started = datetime(2024, 1, 15, 9, 0, tzinfo=UTC)
        df = _make_df([{"status": "successful", "launch_type": "manual", "started": started}])
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        task_data = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]
        raw_jobs = task_data["raw_jobs"]
        assert raw_jobs[0]["started"] == started.isoformat()

    def test_hook_preserves_controller_model(self):
        """Keeps the Controller model in the task payload for downstream persistence."""
        df = _make_df(
            [
                {
                    "status": "successful",
                    "launch_type": "scheduled",
                    "model": "systemjob",
                }
            ]
        )
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        raw_job = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]["raw_jobs"][0]
        assert raw_job["model"] == "systemjob"

    def test_hook_schedules_batch_when_model_column_is_missing(self):
        """Schedules rows without model so the sync task can resolve them by ID."""
        df = _make_df([{"status": "successful", "launch_type": "manual"}]).drop(columns=["model"])
        hook = self._enabled_hook()

        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        mock_task.objects.update_or_create.assert_called_once()
        raw_job = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]["raw_jobs"][0]
        assert "model" not in raw_job

    @pytest.mark.parametrize("model", [None, float("nan"), ""])
    def test_hook_schedules_entire_batch_when_any_model_is_unusable(self, model):
        """Schedules rows with missing model values for batch recovery in the sync task."""
        df = _make_df(
            [
                {"id": 1, "status": "successful", "launch_type": "manual", "model": "job"},
                {"id": 2, "status": "failed", "launch_type": "manual", "model": model},
            ]
        )
        hook = self._enabled_hook()

        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        mock_task.objects.update_or_create.assert_called_once()
        raw_jobs = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]["raw_jobs"]
        assert [row["id"] for row in raw_jobs] == [1, 2]
        assert raw_jobs[0]["model"] == "job"

    def test_hook_converts_num_hosts_to_int(self):
        """num_hosts is cast to int if not None."""
        df = _make_df([{"status": "successful", "launch_type": "manual", "num_hosts": 7.0}])
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        task_data = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]
        assert task_data["raw_jobs"][0]["num_hosts"] == 7
        assert isinstance(task_data["raw_jobs"][0]["num_hosts"], int)

    def test_hook_chunks_large_batches(self):
        """Records exceeding _SYNC_TASK_CHUNK_SIZE are split across multiple tasks."""
        from apps.tasks.collectors.collect_hourly_metrics import _SYNC_TASK_CHUNK_SIZE

        # Build a DataFrame with chunk_size + 1 terminal jobs to force two chunks.
        rows = [{"id": i, "status": "successful", "launch_type": "manual"} for i in range(_SYNC_TASK_CHUNK_SIZE + 1)]
        df = _make_df(rows)
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        assert mock_task.objects.update_or_create.call_count == 2

        first_call = mock_task.objects.update_or_create.call_args_list[0]
        second_call = mock_task.objects.update_or_create.call_args_list[1]

        # Names must carry chunk index so update_or_create is idempotent on retry.
        assert first_call[1]["name"].endswith("_0")
        assert second_call[1]["name"].endswith("_1")

        # First chunk is full, second has the remainder.
        assert len(first_call[1]["defaults"]["task_data"]["raw_jobs"]) == _SYNC_TASK_CHUNK_SIZE
        assert len(second_call[1]["defaults"]["task_data"]["raw_jobs"]) == 1

    def test_hook_deletes_stale_pending_chunks_on_retry(self):
        """After upsert, pending tasks for the same hour outside the new chunk set are deleted."""
        df = _make_df([{"status": "successful", "launch_type": "manual"}])  # 1 record → 1 chunk
        hook = self._enabled_hook()
        hour_ts_str = HOUR_TS.isoformat()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        filter_call = mock_task.objects.filter.call_args
        assert filter_call is not None
        assert filter_call[1]["name__startswith"] == f"sync_dashboard_jobs_{hour_ts_str}_"
        assert filter_call[1]["status"] == "pending"
        expected_new_name = f"sync_dashboard_jobs_{hour_ts_str}_0"
        exclude_call = mock_task.objects.filter.return_value.exclude.call_args
        assert exclude_call[1]["name__in"] == {expected_new_name}
        mock_task.objects.filter.return_value.exclude.return_value.delete.assert_called_once()


@pytest.mark.unit
class TestSerializeDashboardRecord:
    """Branch-coverage tests for serialize_dashboard_record."""

    def test_datetime_field_none_left_unchanged(self):
        """When a datetime field value is None the field is not modified (covers val-is-None branch)."""
        row = {"started": None, "finished": None, "created": None, "modified": None, "num_hosts": 5}
        serialize_dashboard_record(row)
        assert row["started"] is None
        assert row["num_hosts"] == 5

    def test_num_hosts_none_left_unchanged(self):
        """When num_hosts is None the field is not modified (covers is-not-None False branch)."""
        row = {"started": datetime(2024, 1, 1, tzinfo=UTC), "num_hosts": None}
        serialize_dashboard_record(row)
        assert row["num_hosts"] is None
        assert row["started"] == datetime(2024, 1, 1, tzinfo=UTC).isoformat()

    def test_datetime_already_a_string_has_no_isoformat(self):
        """A non-datetime value without isoformat is left unchanged (covers hasattr False branch)."""
        row = {"started": "2024-01-01T00:00:00+00:00", "num_hosts": 3}
        serialize_dashboard_record(row)
        assert row["started"] == "2024-01-01T00:00:00+00:00"

    def test_numpy_int_fields_coerced_to_python_int(self):
        """numpy.int64 values in integer fields are coerced to Python int without raising."""
        import numpy as np

        row = {
            "id": np.int64(5),
            "organization_id": np.int64(3),
            "unified_job_template_id": np.int64(7),
            "launched_by_id": np.int64(1),
            "project_id": np.int64(2),
            "num_hosts": np.float64(4.0),
            "elapsed": np.float64(60.5),
            "started": None,
            "finished": None,
            "created": None,
            "modified": None,
        }
        serialize_dashboard_record(row)
        for field in ("id", "organization_id", "unified_job_template_id", "launched_by_id", "project_id", "num_hosts"):
            assert isinstance(row[field], int), f"{field}: expected int, got {type(row[field])}"
        assert isinstance(row["elapsed"], float)

    def test_nan_in_nullable_int_fields_becomes_none(self):
        """NaN in nullable FK columns (from pandas float64 upcast) is converted to None, not int(nan) ValueError."""

        row = {
            "id": 1,
            "organization_id": float("nan"),
            "unified_job_template_id": float("nan"),
            "launched_by_id": None,
            "project_id": float("nan"),
            "num_hosts": 0,
            "elapsed": float("nan"),
            "started": None,
            "finished": None,
            "created": None,
            "modified": None,
        }
        serialize_dashboard_record(row)
        assert row["organization_id"] is None
        assert row["unified_job_template_id"] is None
        assert row["launched_by_id"] is None
        assert row["project_id"] is None
        assert row["elapsed"] is None
        assert row["id"] == 1  # non-NaN int field unchanged


@pytest.mark.unit
class TestGenericCollectMetricsHook:
    """Tests for the post_collect_hook try/except block in generic_collect_metrics."""

    def _make_registry(self):
        """Return a minimal collector registry with a no-rollup test collector."""
        collector = MagicMock()
        collector.gather.return_value = MagicMock()
        return {
            "test_collector": {
                "collector_func": MagicMock(return_value=collector),
                "rollup_processor": None,
            }
        }

    @patch("apps.tasks.models.HourlyMetricsCollection")
    @patch("apps.tasks.utils.log_task_execution")
    def test_hook_is_called_with_raw_data(self, mock_log, mock_hmc):
        """When post_collect_hook is provided it receives raw_data from gather()."""
        from apps.tasks.utils import generic_collect_metrics

        mock_hmc.objects.update_or_create.return_value = (MagicMock(), True)
        registry = self._make_registry()
        raw_data = registry["test_collector"]["collector_func"].return_value.gather.return_value

        hook = MagicMock()
        generic_collect_metrics(
            collector_type="test_collector",
            collector_registry=registry,
            collection_mode="hourly",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            db_connection=MagicMock(),
            post_collect_hook=hook,
        )
        hook.assert_called_once_with(raw_data)

    @patch("apps.tasks.models.HourlyMetricsCollection")
    @patch("apps.tasks.utils.log_task_execution")
    def test_hook_exception_does_not_abort_collection(self, mock_log, mock_hmc):
        """A failing hook is logged and swallowed — the rollup pipeline still returns success."""
        from apps.tasks.utils import generic_collect_metrics

        mock_hmc.objects.update_or_create.return_value = (MagicMock(), True)
        registry = self._make_registry()

        def bad_hook(_):
            """Always raises to simulate a hook failure."""
            raise RuntimeError("hook failed")

        result = generic_collect_metrics(
            collector_type="test_collector",
            collector_registry=registry,
            collection_mode="hourly",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            db_connection=MagicMock(),
            post_collect_hook=bad_hook,
        )
        # Hook failure must not propagate to the caller — dashboard sync is secondary to
        # the anonymisation rollup pipeline that follows in the same collection task.
        assert result.get("status") == "success"

    @patch("apps.tasks.models.TaskExecution")
    @patch("apps.tasks.models.HourlyMetricsCollection")
    @patch("apps.tasks.utils.log_task_execution")
    def test_hook_exception_emits_warning_and_creates_task_execution(self, mock_log, mock_hmc, mock_te):
        """A failing hook emits a logger.warning and persists a failed TaskExecution when an execution is linked."""
        from apps.tasks.utils import generic_collect_metrics

        mock_hmc.objects.update_or_create.return_value = (MagicMock(), True)
        registry = self._make_registry()

        task_execution = MagicMock()

        def bad_hook(_):
            """Always raises to simulate a hook scheduling failure."""
            raise RuntimeError("scheduling error")

        with (
            patch("apps.tasks.utils.logger") as mock_logger,
            patch("apps.tasks.models.TaskExecution.objects.get", return_value=task_execution),
        ):
            generic_collect_metrics(
                collector_type="test_collector",
                collector_registry=registry,
                collection_mode="hourly",
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                db_connection=MagicMock(),
                task_execution_id=42,
                post_collect_hook=bad_hook,
            )

        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args_list[0][0][0]
        assert "post_collect_hook failed" in warning_msg
        assert "scheduling error" in warning_msg
        mock_te.objects.create.assert_called_once()
        create_kwargs = mock_te.objects.create.call_args[1]
        assert create_kwargs["status"] == "failed"
        assert "scheduling error" in create_kwargs["error_message"]

    @patch("apps.tasks.models.HourlyMetricsCollection")
    @patch("apps.tasks.utils.log_task_execution")
    def test_hook_exception_skips_task_execution_when_no_execution_linked(self, mock_log, mock_hmc):
        """No TaskExecution is created when task_execution_id is not provided (task_execution_instance is None)."""
        from apps.tasks.utils import generic_collect_metrics

        mock_hmc.objects.update_or_create.return_value = (MagicMock(), True)
        registry = self._make_registry()

        def bad_hook(_):
            """Always raises to simulate a hook failure."""
            raise RuntimeError("hook failed")

        with patch("apps.tasks.models.TaskExecution") as mock_te:
            generic_collect_metrics(
                collector_type="test_collector",
                collector_registry=registry,
                collection_mode="hourly",
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                db_connection=MagicMock(),
                post_collect_hook=bad_hook,
            )
        mock_te.objects.create.assert_not_called()

    @patch("apps.tasks.models.HourlyMetricsCollection")
    @patch("apps.tasks.utils.log_task_execution")
    def test_no_hook_skips_hook_block(self, mock_log, mock_hmc):
        """When post_collect_hook=None the hook block is skipped and collection succeeds."""
        from apps.tasks.utils import generic_collect_metrics

        mock_hmc.objects.update_or_create.return_value = (MagicMock(), True)
        registry = self._make_registry()

        result = generic_collect_metrics(
            collector_type="test_collector",
            collector_registry=registry,
            collection_mode="hourly",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            db_connection=MagicMock(),
            post_collect_hook=None,
        )
        assert result.get("status") == "success"


@pytest.mark.unit
class TestCollectHourlyMetricsRowLimit:
    """Cover the row_limit injection branch added for main_jobevent_service."""

    def _run(self, collector_type, mock_generic, hour_ts=None):
        """Call collect_hourly_metrics with all heavy deps patched out."""
        from apps.tasks.collectors.collect_hourly_metrics import collect_hourly_metrics

        with (
            patch("apps.tasks.collectors.collect_hourly_metrics.get_db_connection", return_value=MagicMock()),
            patch(
                "apps.tasks.collectors.collect_hourly_metrics._get_hourly_collectors",
                return_value={
                    collector_type: {"collector_func": MagicMock(), "rollup_processor": None},
                },
            ),
            patch("apps.tasks.collectors.collect_hourly_metrics.generic_collect_metrics", mock_generic),
            patch("apps.tasks.collectors.collect_hourly_metrics.settings") as mock_settings,
        ):
            mock_settings.JOBEVENT_ROW_LIMIT = 2_000_000
            kwargs = {"collector_type": collector_type}
            if hour_ts:
                kwargs["hour_timestamp"] = hour_ts.isoformat()
            collect_hourly_metrics(**kwargs)

    def test_row_limit_injected_for_main_jobevent_service(self):
        """row_limit must be added to collector_kwargs for main_jobevent_service."""
        mock_generic = MagicMock(return_value={"status": "success"})
        self._run("main_jobevent_service", mock_generic, hour_ts=datetime(2024, 1, 1, tzinfo=UTC))

        _, call_kwargs = mock_generic.call_args
        assert "row_limit" in call_kwargs["collector_kwargs"], (
            "row_limit should be injected into collector_kwargs for main_jobevent_service"
        )
        assert call_kwargs["collector_kwargs"]["row_limit"] == 2_000_000

    def test_row_limit_not_injected_for_other_collectors(self):
        """row_limit must NOT appear in collector_kwargs for other collector types."""
        mock_generic = MagicMock(return_value={"status": "success"})
        self._run("unified_jobs", mock_generic, hour_ts=datetime(2024, 1, 1, tzinfo=UTC))

        _, call_kwargs = mock_generic.call_args
        assert "row_limit" not in call_kwargs["collector_kwargs"], (
            "row_limit should not be injected for non-jobevent collectors"
        )


@pytest.mark.unit
class TestHourlyCollectorRegistry:
    """Pin the registry wiring so a key rename doesn't silently drop the hook."""

    def test_unified_jobs_uses_dashboard_collector_and_has_hook_factory(self):
        """unified_jobs entry must use unified_jobs_dashboard and register a post_collect_hook_factory."""
        with patch("metrics_utility.library.collectors.controller.unified_jobs_dashboard", create=True):
            registry = _get_hourly_collectors()

        entry = registry.get("unified_jobs")
        assert entry is not None, "unified_jobs key missing from hourly collector registry"
        assert entry.get("post_collect_hook_factory") is _build_dashboard_sync_hook, (
            "unified_jobs registry entry must wire _build_dashboard_sync_hook as post_collect_hook_factory"
        )

    def test_job_host_summary_service_has_host_summary_hook_factory(self):
        """job_host_summary_service entry must register _build_dashboard_host_summary_sync_hook."""
        registry = _get_hourly_collectors()
        entry = registry.get("job_host_summary_service")
        assert entry is not None, "job_host_summary_service key missing from hourly collector registry"
        assert entry.get("post_collect_hook_factory") is _build_dashboard_host_summary_sync_hook, (
            "job_host_summary_service must wire _build_dashboard_host_summary_sync_hook"
        )


def _make_host_summary_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal DataFrame matching the job_host_summary_service schema."""
    defaults = {
        "id": 1,
        "host_name": "web01",
        "host_remote_id": 10,
        "job_remote_id": 42,
        # Extra columns present in the real collector — hook must ignore these.
        "changed": 0,
        "failures": 0,
        "ok": 5,
    }
    return pd.DataFrame([{**defaults, **r} for r in rows])


@pytest.mark.unit
class TestBuildDashboardHostSummarySyncHook:
    """Tests for _build_dashboard_host_summary_sync_hook and its returned inner hook."""

    def test_returns_none_when_feature_disabled(self):
        """When DASHBOARD_COLLECTION is off, the factory returns None."""
        with patch(FEATURE_FLAG_PATH, return_value=False):
            result = _build_dashboard_host_summary_sync_hook(HOUR_TS)
        assert result is None

    def test_returns_callable_when_feature_enabled(self):
        """When DASHBOARD_COLLECTION is on, the factory returns a callable hook."""
        with patch(FEATURE_FLAG_PATH, return_value=True):
            hook = _build_dashboard_host_summary_sync_hook(HOUR_TS)
        assert callable(hook)

    def _enabled_hook(self):
        """Build a hook with DASHBOARD_COLLECTION enabled."""
        with patch(FEATURE_FLAG_PATH, return_value=True):
            return _build_dashboard_host_summary_sync_hook(HOUR_TS)

    def test_hook_returns_early_when_raw_data_none(self):
        """hook(None) exits immediately without touching Task."""
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(None)
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_returns_early_when_dataframe_empty(self):
        """hook(empty_df) exits without creating a Task."""
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(pd.DataFrame())
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_returns_early_when_required_columns_missing(self):
        """If the DataFrame is missing any required column, no Task is created (schema drift guard)."""
        df = pd.DataFrame([{"id": 1, "host_name": "h1"}])  # missing host_remote_id, job_remote_id
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(df)
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_creates_task_with_correct_function_name(self):
        """A valid DataFrame causes update_or_create with function_name='sync_dashboard_host_summaries'."""
        df = _make_host_summary_df([{}])
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        mock_task.objects.update_or_create.assert_called_once()
        call_kwargs = mock_task.objects.update_or_create.call_args[1]
        assert call_kwargs["defaults"]["function_name"] == "sync_dashboard_host_summaries"

    def test_hook_serialises_raw_host_summaries_into_task_data(self):
        """raw_host_summaries in task_data contains id, host_name, host_id, job_remote_id."""
        df = _make_host_summary_df([{"id": 7, "host_name": "db01", "host_remote_id": 99, "job_remote_id": 123}])
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        task_data = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]
        records = task_data["raw_host_summaries"]
        assert len(records) == 1
        assert records[0]["id"] == 7
        assert records[0]["host_name"] == "db01"
        assert records[0]["host_id"] == 99
        assert records[0]["job_remote_id"] == 123

    def test_hook_uses_update_or_create_for_idempotency(self):
        """Task creation uses update_or_create so retry is safe."""
        df = _make_host_summary_df([{}])
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)
        assert mock_task.objects.update_or_create.called

    def test_hook_chunks_by_record_count(self):
        """Total records exceeding _HOST_SUMMARY_RECORD_CHUNK_SIZE are split across multiple Tasks."""
        from apps.tasks.collectors.collect_hourly_metrics import _HOST_SUMMARY_RECORD_CHUNK_SIZE

        # One record per unique job so the boundary falls cleanly at the record limit.
        rows = [{"id": i, "job_remote_id": i} for i in range(_HOST_SUMMARY_RECORD_CHUNK_SIZE + 1)]
        df = _make_host_summary_df(rows)
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        assert mock_task.objects.update_or_create.call_count == 2
        first_call = mock_task.objects.update_or_create.call_args_list[0]
        second_call = mock_task.objects.update_or_create.call_args_list[1]
        assert first_call[1]["name"].endswith("_0")
        assert second_call[1]["name"].endswith("_1")

    def test_hook_keeps_single_job_records_in_one_chunk(self):
        """All records for a single job stay in one chunk even if the count exceeds the record limit.

        _sync_host_summaries deletes records not present in its batch, so splitting a job
        across tasks would cause the first task to delete the second task's records.
        """
        from apps.tasks.collectors.collect_hourly_metrics import _HOST_SUMMARY_RECORD_CHUNK_SIZE

        rows = [{"id": i, "job_remote_id": 42} for i in range(_HOST_SUMMARY_RECORD_CHUNK_SIZE + 10)]
        df = _make_host_summary_df(rows)
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        assert mock_task.objects.update_or_create.call_count == 1
        task_data = mock_task.objects.update_or_create.call_args[1]["defaults"]["task_data"]
        assert len(task_data["raw_host_summaries"]) == _HOST_SUMMARY_RECORD_CHUNK_SIZE + 10

    def test_hook_skips_rows_with_null_job_remote_id(self):
        """Rows where job_remote_id is None are dropped and no Task is created."""
        df = _make_host_summary_df([{"job_remote_id": None}])
        # pandas will store None as NaN for numeric column — force object dtype
        df["job_remote_id"] = df["job_remote_id"].astype(object)
        hook = self._enabled_hook()
        with patch(TASK_MODEL_PATH) as mock_task:
            hook(df)
        mock_task.objects.update_or_create.assert_not_called()

    def test_hook_deletes_stale_pending_chunks_on_retry(self):
        """After upsert, pending tasks for the same hour outside the new chunk set are deleted."""
        df = _make_host_summary_df([{}])  # 1 job → 1 chunk (_0 only)
        hook = self._enabled_hook()
        hour_ts_str = HOUR_TS.isoformat()
        with patch(TASK_MODEL_PATH) as mock_task:
            mock_task.objects.update_or_create.return_value = (MagicMock(), True)
            hook(df)

        # filter().exclude().delete() chain must be called to purge stale chunks.
        filter_call = mock_task.objects.filter.call_args
        assert filter_call is not None
        assert filter_call[1]["name__startswith"] == f"sync_dashboard_host_summaries_{hour_ts_str}_"
        assert filter_call[1]["status"] == "pending"
        # exclude must receive only the new chunk name
        expected_new_name = f"sync_dashboard_host_summaries_{hour_ts_str}_0"
        exclude_call = mock_task.objects.filter.return_value.exclude.call_args
        assert exclude_call[1]["name__in"] == {expected_new_name}
        mock_task.objects.filter.return_value.exclude.return_value.delete.assert_called_once()


@pytest.mark.unit
class TestSerializeHostSummaryRecord:
    """Branch-coverage tests for _serialize_host_summary_record."""

    def test_basic_row_coerced_correctly(self):
        """Standard row with all fields present is returned with Python-native types."""
        import numpy as np

        row = {"id": np.int64(5), "host_name": "web01", "host_remote_id": np.int64(10), "job_remote_id": np.int64(42)}
        result = _serialize_host_summary_record(row)
        assert result == {"id": 5, "host_name": "web01", "host_id": 10, "job_remote_id": 42}
        assert isinstance(result["id"], int)

    def test_null_host_remote_id_stays_none(self):
        """host_id=None (deleted AWX host) is preserved as None."""
        row = {"id": 1, "host_name": "h1", "host_remote_id": None, "job_remote_id": 7}
        result = _serialize_host_summary_record(row)
        assert result["host_id"] is None

    def test_nan_host_remote_id_becomes_none(self):
        """NaN in host_remote_id (pandas float64 upcast of nullable int) is coerced to None."""
        row = {"id": 1, "host_name": "h1", "host_remote_id": float("nan"), "job_remote_id": 7}
        result = _serialize_host_summary_record(row)
        assert result["host_id"] is None

    def test_null_host_name_stays_none(self):
        """host_name=None is preserved as None."""
        row = {"id": 1, "host_name": None, "host_remote_id": 10, "job_remote_id": 7}
        result = _serialize_host_summary_record(row)
        assert result["host_name"] is None


@pytest.mark.unit
class TestGroupHostSummaryRows:
    """Tests for _group_host_summary_rows."""

    def test_basic_grouping(self):
        """Rows with the same job_remote_id are grouped together."""
        rows = [
            {"id": 1, "host_name": "h1", "host_remote_id": 10, "job_remote_id": 42},
            {"id": 2, "host_name": "h2", "host_remote_id": 11, "job_remote_id": 42},
            {"id": 3, "host_name": "h3", "host_remote_id": 12, "job_remote_id": 99},
        ]
        result = _group_host_summary_rows(rows)
        assert set(result.keys()) == {42, 99}
        assert len(result[42]) == 2
        assert len(result[99]) == 1

    def test_drops_rows_with_null_job_remote_id(self):
        """Rows where job_remote_id serialises to None are excluded."""
        rows = [
            {"id": 1, "host_name": "h1", "host_remote_id": 10, "job_remote_id": None},
            {"id": 2, "host_name": "h2", "host_remote_id": 11, "job_remote_id": 42},
        ]
        result = _group_host_summary_rows(rows)
        assert list(result.keys()) == [42]

    def test_empty_input_returns_empty_dict(self):
        """Empty rows list produces an empty by_job dict."""
        assert _group_host_summary_rows([]) == {}

    def test_output_records_use_host_id_key(self):
        """Serialised records in the output use host_id, not host_remote_id."""
        rows = [{"id": 5, "host_name": "h1", "host_remote_id": 77, "job_remote_id": 1}]
        result = _group_host_summary_rows(rows)
        record = result[1][0]
        assert "host_id" in record
        assert "host_remote_id" not in record
        assert record["host_id"] == 77


@pytest.mark.unit
class TestBuildHostSummaryTaskChunks:
    """Tests for _build_host_summary_task_chunks."""

    def _make_records(self, n):
        return [{"id": i, "host_id": i, "host_name": f"h{i}", "job_remote_id": i} for i in range(n)]

    def test_single_chunk_when_below_limit(self):
        """All records fit in one chunk when total count is below the limit."""
        from apps.tasks.collectors.collect_hourly_metrics import _HOST_SUMMARY_RECORD_CHUNK_SIZE

        by_job = {i: [{"id": i, "host_id": i, "host_name": f"h{i}", "job_remote_id": i}] for i in range(5)}
        result = _build_host_summary_task_chunks(by_job, "2024-01-01T00:00:00+00:00")
        assert len(result) == 1
        chunk_name = list(result.keys())[0]
        assert chunk_name.endswith("_0")
        assert len(list(result.values())[0]) == 5

    def test_splits_into_two_chunks_at_limit(self):
        """Records exceeding the limit across distinct jobs split into two chunks."""
        from apps.tasks.collectors.collect_hourly_metrics import _HOST_SUMMARY_RECORD_CHUNK_SIZE

        # Each job has exactly 1 record; limit + 1 jobs → 2 chunks
        by_job = {
            i: [{"id": i, "host_id": i, "host_name": f"h{i}"}] for i in range(_HOST_SUMMARY_RECORD_CHUNK_SIZE + 1)
        }
        result = _build_host_summary_task_chunks(by_job, "2024-01-01T00:00:00+00:00")
        assert len(result) == 2
        names = list(result.keys())
        assert names[0].endswith("_0")
        assert names[1].endswith("_1")

    def test_single_oversized_job_stays_in_one_chunk(self):
        """A job with more records than the limit is never split across chunks."""
        from apps.tasks.collectors.collect_hourly_metrics import _HOST_SUMMARY_RECORD_CHUNK_SIZE

        oversized = [{"id": i} for i in range(_HOST_SUMMARY_RECORD_CHUNK_SIZE + 50)]
        by_job = {42: oversized}
        result = _build_host_summary_task_chunks(by_job, "2024-01-01T00:00:00+00:00")
        assert len(result) == 1
        assert len(list(result.values())[0]) == _HOST_SUMMARY_RECORD_CHUNK_SIZE + 50

    def test_chunk_names_include_hour_ts_str(self):
        """Chunk names embed the hour timestamp so stale-chunk cleanup targets the right hour."""
        by_job = {1: [{"id": 1}]}
        result = _build_host_summary_task_chunks(by_job, "2024-06-01T12:00:00+00:00")
        name = list(result.keys())[0]
        assert "2024-06-01T12:00:00+00:00" in name
