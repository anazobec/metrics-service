from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from apps.tasks.retention import (
    DEFAULT_RETENTION_DAYS,
    get_controller_retention_days,
    resolve_retention_days,
)


def _row(*, days, enabled=True, next_run=None, job_type="cleanup_jobs"):
    return {
        "job_type": job_type,
        "schedule_enabled": enabled,
        "retention_days": days,
        "next_run": next_run,
    }


@pytest.mark.unit
class TestResolveRetentionDays:
    def test_lowest_valid_enabled_schedule_wins(self):
        assert resolve_retention_days([_row(days=90), _row(days=30), _row(days=60, enabled=False)]) == 30

    def test_non_positive_schedule_is_skipped_when_another_is_valid(self):
        assert resolve_retention_days([_row(days=0), _row(days=-1), _row(days=45)]) == 45

    def test_invalid_schedule_does_not_prevent_valid_schedule(self):
        assert resolve_retention_days([_row(days="not-a-number"), _row(days=None), _row(days=30)]) == 30

    def test_returns_none_when_no_valid_schedule_remains(self):
        assert resolve_retention_days([_row(days=0), _row(days=-1), _row(days="bad")]) is None

    def test_ties_use_soonest_next_run(self):
        rows = [
            _row(days=30, next_run="2024-06-10T00:00:00+00:00"),
            _row(days=30, next_run="2024-06-05T00:00:00+00:00"),
        ]
        assert resolve_retention_days(rows) == 30

    def test_ignores_non_cleanup_jobs(self):
        assert resolve_retention_days([_row(days=7, job_type="cleanup_activitystream")]) is None


@pytest.mark.unit
class TestGetControllerRetentionDays:
    @patch("apps.tasks.retention.get_db_connection")
    @patch("apps.tasks.retention._fetch_retention_settings")
    def test_returns_resolved_value(self, mock_fetch, mock_connection):
        mock_fetch.return_value = [_row(days=45)]
        assert get_controller_retention_days() == 45

    @patch("apps.tasks.retention.get_db_connection")
    @patch("apps.tasks.retention._fetch_retention_settings", return_value=[])
    def test_logs_and_falls_back_when_no_valid_schedules(self, mock_fetch, mock_connection):
        with patch("apps.tasks.retention.logger") as mock_logger:
            assert get_controller_retention_days() == DEFAULT_RETENTION_DAYS
        mock_logger.warning.assert_called_once_with(
            "No valid Controller cleanup_jobs retention schedule found; using 90-day fallback"
        )

    @patch("apps.tasks.retention.get_db_connection")
    @patch("apps.tasks.retention._fetch_retention_settings", side_effect=RuntimeError("database unavailable"))
    def test_logs_and_falls_back_when_controller_query_fails(self, mock_fetch, mock_connection):
        with patch("apps.tasks.retention.logger") as mock_logger:
            assert get_controller_retention_days() == DEFAULT_RETENTION_DAYS
        mock_logger.exception.assert_called_once_with("Unable to read Controller retention; using 90-day fallback")

    @patch("apps.tasks.retention._fetch_retention_settings")
    def test_uses_custom_database_alias(self, mock_fetch):
        mock_fetch.return_value = [_row(days=45)]
        with patch("apps.tasks.retention.get_db_connection") as mock_connection:
            assert get_controller_retention_days("controller") == 45
        mock_connection.assert_called_once_with("controller")


@pytest.mark.unit
def test_retention_query_returns_raw_days_text():
    from apps.tasks.retention import RETENTION_SETTINGS_QUERY, _fetch_retention_settings

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.description = [("retention_days",)]
    cursor.fetchall.return_value = [("30",)]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    assert _fetch_retention_settings(connection) == [{"retention_days": "30"}]
    assert "->> 'days' AS retention_days" in RETENTION_SETTINGS_QUERY
    assert "::int" not in RETENTION_SETTINGS_QUERY
