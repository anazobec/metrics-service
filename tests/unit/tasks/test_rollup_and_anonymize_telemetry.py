"""Unit tests for dashboard telemetry integration in daily_metrics_rollup
and daily_anonymize_and_prepare collectors."""

import decimal
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _aggregate_dashboard_telemetry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggregateDashboardTelemetry:
    """Tests for the _aggregate_dashboard_telemetry helper in daily_metrics_rollup."""

    def test_returns_rows_for_given_date(self):
        """Returns a list of dicts with the expected keys for matching rows."""
        mock_row = MagicMock()
        mock_row.task_name = "collect_dashboard_reports_initial_data"
        mock_row.collection_duration_ms = decimal.Decimal("1500.00")
        mock_row.number_of_records_processed = 42
        mock_row.database_query_time_ms = decimal.Decimal("200.00")
        mock_row.cache_hit_rate = None

        with patch("apps.tasks.collectors.daily_metrics_rollup.DashboardTelemetry") as mock_model:
            mock_model.objects.filter.return_value = [mock_row]
            from apps.tasks.collectors.daily_metrics_rollup import _aggregate_dashboard_telemetry

            result = _aggregate_dashboard_telemetry(date.today())

        assert len(result) == 1
        entry = result[0]
        assert entry["task_name"] == "collect_dashboard_reports_initial_data"
        assert entry["collection_duration_ms"] == 1500.00
        assert entry["number_of_records_processed"] == 42
        assert entry["database_query_time_ms"] == 200.00
        assert entry["cache_hit_rate"] is None

    def test_returns_empty_list_when_no_rows(self):
        """Returns [] when no telemetry rows exist for the given date."""
        with patch("apps.tasks.collectors.daily_metrics_rollup.DashboardTelemetry") as mock_model:
            mock_model.objects.filter.return_value = []
            from apps.tasks.collectors.daily_metrics_rollup import _aggregate_dashboard_telemetry

            result = _aggregate_dashboard_telemetry(date.today())

        assert result == []

    def test_returns_empty_list_on_exception(self):
        """When DashboardTelemetry query raises an exception, returns [] and logs the error."""
        with (
            patch("apps.tasks.collectors.daily_metrics_rollup.DashboardTelemetry") as mock_model,
            patch("apps.tasks.collectors.daily_metrics_rollup.logger") as mock_logger,
        ):
            mock_model.objects.filter.side_effect = Exception("DB error")
            from apps.tasks.collectors.daily_metrics_rollup import _aggregate_dashboard_telemetry

            result = _aggregate_dashboard_telemetry(date.today())

        assert result == []
        mock_logger.exception.assert_called_once()

    def test_output_contains_no_sensitive_fields(self):
        """The aggregated dict does not expose org names, user ids, or job details."""
        mock_row = MagicMock()
        mock_row.task_name = "task"
        mock_row.collection_duration_ms = decimal.Decimal("100.00")
        mock_row.number_of_records_processed = 1
        mock_row.database_query_time_ms = decimal.Decimal("10.00")
        mock_row.cache_hit_rate = None

        with patch("apps.tasks.collectors.daily_metrics_rollup.DashboardTelemetry") as mock_model:
            mock_model.objects.filter.return_value = [mock_row]
            from apps.tasks.collectors.daily_metrics_rollup import _aggregate_dashboard_telemetry

            result = _aggregate_dashboard_telemetry(date.today())

        entry = result[0]
        sensitive_keys = {"organization_name", "user_id", "username", "job_id", "job_name"}
        assert not sensitive_keys.intersection(entry.keys())

    def test_multiple_rows_are_all_returned(self):
        """All rows for the given date are included in the result list."""

        def make_row(task_name):
            r = MagicMock()
            r.task_name = task_name
            r.collection_duration_ms = decimal.Decimal("100.00")
            r.number_of_records_processed = 0
            r.database_query_time_ms = decimal.Decimal("5.00")
            r.cache_hit_rate = None
            return r

        rows = [make_row("task_a"), make_row("task_b")]

        with patch("apps.tasks.collectors.daily_metrics_rollup.DashboardTelemetry") as mock_model:
            mock_model.objects.filter.return_value = rows
            from apps.tasks.collectors.daily_metrics_rollup import _aggregate_dashboard_telemetry

            result = _aggregate_dashboard_telemetry(date.today())

        assert len(result) == 2
        task_names = {r["task_name"] for r in result}
        assert task_names == {"task_a", "task_b"}


# ---------------------------------------------------------------------------
# daily_metrics_rollup — dashboard_telemetry appended
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDailyMetricsRollupTelemetry:
    """Tests that daily_metrics_rollup appends dashboard_telemetry to the daily summary."""

    def test_rollup_sets_dashboard_telemetry_key(self):
        """daily_metrics_rollup sets the 'dashboard_telemetry' key on the daily rollup."""
        telemetry_rows = [
            {
                "task_name": "collect_dashboard_reports_initial_data",
                "collection_duration_ms": decimal.Decimal("1000.00"),
                "number_of_records_processed": 5,
                "database_query_time_ms": decimal.Decimal("100.00"),
                "cache_hit_rate": None,
            }
        ]
        with (
            patch(
                "apps.tasks.collectors.daily_metrics_rollup._aggregate_dashboard_telemetry",
                return_value=telemetry_rows,
            ) as mock_aggregate,
            patch(
                "apps.tasks.collectors.daily_metrics_rollup._collect_and_group_hourly_collections"
            ) as mock_collections,
            patch("apps.tasks.collectors.daily_metrics_rollup._merge_hourly_rollups") as mock_merge,
            patch("apps.tasks.collectors.daily_metrics_rollup._save_daily_summary") as mock_save,
            patch("apps.tasks.collectors.daily_metrics_rollup.log_task_execution"),
            patch("apps.tasks.models.HourlyMetricsCollection") as mock_hourly,
            patch("apps.tasks.collectors.daily_metrics_rollup.create_task_result") as mock_result,
        ):
            mock_hourly.objects.filter.return_value.exists.return_value = True
            mock_collections.return_value = ({}, None, None)
            mock_merge.return_value = ({}, [])
            mock_save.return_value = (MagicMock(aggregated_metrics={}), True, 0)
            mock_result.return_value = {"status": "success"}

            from apps.tasks.collectors.daily_metrics_rollup import daily_metrics_rollup

            daily_metrics_rollup()

        # _aggregate_dashboard_telemetry should have been called
        mock_aggregate.assert_called_once()

        # The daily_rollup dict passed to _save_daily_summary should contain dashboard_telemetry
        call_args = mock_save.call_args
        rollup_arg = call_args[0][1]  # second positional arg is the daily_rollup dict
        assert "dashboard_telemetry" in rollup_arg
        assert rollup_arg["dashboard_telemetry"] == telemetry_rows

    def test_rollup_uses_summary_date_for_telemetry_query(self):
        """_aggregate_dashboard_telemetry is called with the summary_date being rolled up."""
        specific_date = date.today() - timedelta(days=2)

        with (
            patch(
                "apps.tasks.collectors.daily_metrics_rollup._aggregate_dashboard_telemetry",
                return_value=[],
            ) as mock_aggregate,
            patch(
                "apps.tasks.collectors.daily_metrics_rollup._collect_and_group_hourly_collections"
            ) as mock_collections,
            patch("apps.tasks.collectors.daily_metrics_rollup._merge_hourly_rollups") as mock_merge,
            patch("apps.tasks.collectors.daily_metrics_rollup._save_daily_summary") as mock_save,
            patch("apps.tasks.collectors.daily_metrics_rollup.log_task_execution"),
            patch("apps.tasks.models.HourlyMetricsCollection") as mock_hourly,
            patch("apps.tasks.collectors.daily_metrics_rollup.create_task_result") as mock_result,
        ):
            mock_hourly.objects.filter.return_value.exists.return_value = True
            mock_collections.return_value = ({}, None, None)
            mock_merge.return_value = ({}, [])
            mock_save.return_value = (MagicMock(aggregated_metrics={}), True, 0)
            mock_result.return_value = {"status": "success"}

            from apps.tasks.collectors.daily_metrics_rollup import daily_metrics_rollup

            daily_metrics_rollup(summary_date=specific_date.isoformat())

        mock_aggregate.assert_called_once_with(specific_date)


# ---------------------------------------------------------------------------
# daily_anonymize_and_prepare — dashboard_telemetry propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
class TestDailyAnonymizeTelemetry:
    """Tests that daily_anonymize_and_prepare forwards telemetry to the payload."""

    def test_dashboard_telemetry_included_in_anonymized_data(self):
        """When the DailyMetricsSummary metrics contain dashboard_telemetry,
        it is propagated into the AnonymizedMetricsPayload's anonymized_data."""
        from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

        telemetry = [
            {
                "task_name": "collect_dashboard_reports_initial_data",
                "collection_duration_ms": "1500.00",
                "number_of_records_processed": 10,
                "database_query_time_ms": "200.00",
                "cache_hit_rate": None,
            }
        ]

        specific_date = date(2024, 5, 20)
        DailyMetricsSummary.objects.filter(summary_date=specific_date).delete()

        DailyMetricsSummary.objects.create(
            summary_date=specific_date,
            status="aggregated",
            aggregated_metrics={
                "unified_jobs": {},
                "job_host_summary_service": {},
                "dashboard_telemetry": telemetry,
            },
        )

        mock_anonymized = {"data": {"metrics": []}, "salt": "test-salt"}

        with patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value=mock_anonymized):
            from apps.tasks.collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare

            result = daily_anonymize_and_prepare(summary_date="2024-05-20")

        assert result["status"] == "success"
        payload = AnonymizedMetricsPayload.objects.get(summary_date=specific_date)
        assert payload.anonymized_data["dashboard_telemetry"] == telemetry

    def test_api_usage_telemetry_included_in_anonymized_data(self):
        from apps.analytics.models import APIUsageCounter
        from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

        specific_date = date(2024, 8, 15)
        DailyMetricsSummary.objects.filter(summary_date=specific_date).delete()
        APIUsageCounter.record_request(specific_date, "analytics:v1:root", 2)
        APIUsageCounter.record_request(specific_date, "analytics:v1:rows", 4)
        APIUsageCounter.record_request(specific_date, "analytics:v1:collect", 5)

        DailyMetricsSummary.objects.create(
            summary_date=specific_date,
            status="aggregated",
            aggregated_metrics={"unified_jobs": {}, "job_host_summary_service": {}},
        )

        with patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value={"data": {"metrics": []}}):
            from apps.tasks.collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare

            result = daily_anonymize_and_prepare(summary_date=specific_date.isoformat())

        assert result["status"] == "success"
        payload = AnonymizedMetricsPayload.objects.get(summary_date=specific_date)
        telemetry = payload.anonymized_data["api_usage_telemetry"]
        assert telemetry["available"] is True
        assert telemetry["total_requests"] == 3
        assert telemetry["by_endpoint"]["analytics:v1:root"]["2xx"] == 1
        assert telemetry["by_endpoint"]["analytics:v1:rows"]["4xx"] == 1
        assert telemetry["by_endpoint"]["analytics:v1:collect"]["5xx"] == 1
        assert telemetry["by_endpoint"]["analytics:v1:root"]["4xx"] == 0

    def test_api_usage_telemetry_failure_is_marked_unavailable(self):
        from apps.analytics.models import APIUsageCounter
        from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

        specific_date = date(2024, 8, 16)
        DailyMetricsSummary.objects.filter(summary_date=specific_date).delete()
        DailyMetricsSummary.objects.create(
            summary_date=specific_date,
            status="aggregated",
            aggregated_metrics={"unified_jobs": {}, "job_host_summary_service": {}},
        )

        with (
            patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value={"data": {"metrics": []}}),
            patch.object(APIUsageCounter, "daily_summary", side_effect=RuntimeError("database unavailable")),
        ):
            from apps.tasks.collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare

            result = daily_anonymize_and_prepare(summary_date=specific_date.isoformat())

        assert result["status"] == "success"
        payload = AnonymizedMetricsPayload.objects.get(summary_date=specific_date)
        assert payload.anonymized_data["api_usage_telemetry"] == {
            "available": False,
            "total_requests": 0,
            "by_endpoint": {},
        }

    def test_missing_dashboard_telemetry_defaults_to_empty_list(self):
        """When dashboard_telemetry is absent from the summary metrics,
        the payload's anonymized_data sets dashboard_telemetry to []."""
        from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

        specific_date = date(2024, 6, 10)
        DailyMetricsSummary.objects.filter(summary_date=specific_date).delete()

        DailyMetricsSummary.objects.create(
            summary_date=specific_date,
            status="aggregated",
            aggregated_metrics={"unified_jobs": {}, "job_host_summary_service": {}},
        )

        mock_anonymized = {"data": {"metrics": []}, "salt": "test-salt"}

        with patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value=mock_anonymized):
            from apps.tasks.collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare

            result = daily_anonymize_and_prepare(summary_date="2024-06-10")

        assert result["status"] == "success"
        payload = AnonymizedMetricsPayload.objects.get(summary_date=specific_date)
        assert payload.anonymized_data["dashboard_telemetry"] == []
        api_telemetry = payload.anonymized_data["api_usage_telemetry"]
        assert api_telemetry["available"] is True
        assert api_telemetry["total_requests"] == 0

    def test_no_sensitive_data_in_payload(self):
        """The dashboard_telemetry transmitted in the payload contains no sensitive fields."""
        from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

        specific_date = date(2024, 7, 1)
        DailyMetricsSummary.objects.filter(summary_date=specific_date).delete()

        telemetry = [
            {
                "task_name": "cleanup_dashboard_reports_old_data",
                "collection_duration_ms": "300.00",
                "number_of_records_processed": 2,
                "database_query_time_ms": "50.00",
                "cache_hit_rate": None,
            }
        ]

        DailyMetricsSummary.objects.create(
            summary_date=specific_date,
            status="aggregated",
            aggregated_metrics={"unified_jobs": {}, "job_host_summary_service": {}, "dashboard_telemetry": telemetry},
        )

        mock_anonymized = {"data": {"metrics": []}, "salt": "test-salt"}

        with patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value=mock_anonymized):
            from apps.tasks.collectors.daily_anonymize_and_prepare import daily_anonymize_and_prepare

            daily_anonymize_and_prepare(summary_date="2024-07-01")

        payload = AnonymizedMetricsPayload.objects.get(summary_date=specific_date)
        for entry in payload.anonymized_data["dashboard_telemetry"]:
            sensitive_keys = {"organization_name", "user_id", "username", "job_id"}
            assert not sensitive_keys.intersection(entry.keys())
