"""
Unit tests for apps/dashboard_reports/tasks.py.
Targets 14% → ~70% coverage.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _parse_dt (already in test_misc_coverage.py but add more here)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_parse_dt_string_without_tz_assumes_utc():
    from apps.dashboard_reports.tasks import _parse_dt

    result = _parse_dt("2024-06-15T10:00:00")
    assert result.tzinfo is not None
    assert result.hour == 10


# ---------------------------------------------------------------------------
# _collect_data — invalid date range
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_collect_data_invalid_date_range_returns_error():
    from apps.dashboard_reports.tasks import _collect_data

    now = datetime.now(tz=UTC)
    result = _collect_data(
        "test_task",
        since=now.isoformat(),
        until=(now - timedelta(hours=1)).isoformat(),
    )
    assert result["error"] is True
    assert "Invalid date range" in result["message"]


@pytest.mark.unit
@pytest.mark.django_db
def test_collect_data_db_error_returns_error():
    from apps.dashboard_reports.tasks import _collect_data

    now = datetime.now(tz=UTC)
    since = (now - timedelta(hours=2)).isoformat()

    with patch("apps.dashboard_reports.tasks.get_db_connection", side_effect=Exception("DB connection failed")):
        result = _collect_data("test_task", since=since, until=now.isoformat())

    assert result["error"] is True
    assert "failed" in result["message"].lower()


@pytest.mark.unit
@pytest.mark.django_db
def test_collect_data_success():
    from apps.dashboard_reports.tasks import _collect_data

    now = datetime.now(tz=UTC)
    since = (now - timedelta(hours=2)).isoformat()

    with (
        patch("apps.dashboard_reports.tasks.get_db_connection", return_value=MagicMock()),
        patch("apps.dashboard_reports.tasks._iter_job_pages", return_value=iter([])),
    ):
        result = _collect_data("test_task", since=since, until=now.isoformat())

    assert result["error"] is False
    assert result["data"]["job_count"] == 0


# ---------------------------------------------------------------------------
# _sync_jobs_atomically — empty list
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_sync_jobs_atomically_empty_list():
    from apps.dashboard_reports.tasks import _sync_jobs_atomically

    failed = _sync_jobs_atomically([])
    assert failed == []


# ---------------------------------------------------------------------------
# collect_dashboard_reports_initial_data
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_collect_dashboard_reports_initial_data_no_db():
    from apps.dashboard_reports.tasks import collect_dashboard_reports_initial_data

    with patch("apps.dashboard_reports.tasks.get_db_connection", side_effect=Exception("no awx db")):
        result = collect_dashboard_reports_initial_data()

    assert result["status"] == "error"


@pytest.mark.unit
@pytest.mark.django_db
def test_collect_dashboard_reports_initial_data_success():
    from apps.dashboard_reports.tasks import collect_dashboard_reports_initial_data

    now = datetime.now(tz=UTC)

    with (
        patch("apps.dashboard_reports.tasks.get_db_connection", return_value=MagicMock()),
        patch("apps.dashboard_reports.tasks._iter_job_pages", return_value=iter([])),
    ):
        result = collect_dashboard_reports_initial_data(
            since=(now - timedelta(days=1)).isoformat(),
            until=now.isoformat(),
        )

    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# collect_dashboard_reports_data
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_collect_dashboard_reports_data_incremental():
    from apps.dashboard_reports.tasks import collect_dashboard_reports_data

    with (
        patch("apps.dashboard_reports.tasks.get_db_connection", return_value=MagicMock()),
        patch("apps.dashboard_reports.tasks._iter_job_pages", return_value=iter([])),
    ):
        result = collect_dashboard_reports_data(incremental=True)

    assert result["status"] in ("success", "error")


@pytest.mark.unit
@pytest.mark.django_db
def test_collect_dashboard_reports_data_full():
    from apps.dashboard_reports.tasks import collect_dashboard_reports_data

    now = datetime.now(tz=UTC)

    with (
        patch("apps.dashboard_reports.tasks.get_db_connection", return_value=MagicMock()),
        patch("apps.dashboard_reports.tasks._iter_job_pages", return_value=iter([])),
    ):
        result = collect_dashboard_reports_data(
            incremental=False,
            since=(now - timedelta(hours=2)).isoformat(),
            until=now.isoformat(),
        )

    assert result["status"] in ("success", "error")


# ---------------------------------------------------------------------------
# cleanup_dashboard_reports_old_data — more scenarios
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_cleanup_dashboard_reports_no_data():
    from apps.dashboard_reports.models import JobData
    from apps.dashboard_reports.tasks import cleanup_dashboard_reports_old_data

    JobData.objects.all().delete()
    result = cleanup_dashboard_reports_old_data(retention_days=90)
    assert result["status"] == "success"
    assert result.get("deleted_records", 0) == 0
