"""Leaderboard settings observations in the daily anonymized payload."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.dynamic_settings.models import Setting
from apps.tasks.collectors.daily_anonymize_and_prepare import (
    _collect_leaderboard_telemetry,
    daily_anonymize_and_prepare,
)
from apps.tasks.collectors.send_anonymized_to_segment import _process_single_payload, send_anonymized_to_segment
from apps.tasks.models import AnonymizedMetricsPayload, DailyMetricsSummary

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def clear_leaderboard_overrides(settings):
    settings.FEATURE = {}
    settings.FEATURE_SHOW_LEADERBOARD_ENABLED = None
    Setting.objects.filter(setting_key="SHOW_LEADERBOARD").delete()


def _prepare_payload():
    summary_date = timezone.now().date() - timedelta(days=6)
    DailyMetricsSummary.objects.create(
        summary_date=summary_date,
        status="aggregated",
        aggregated_metrics={"dashboard_telemetry": [{"task_name": "sync_dashboard_job_records", "success": True}]},
    )
    with patch("metrics_utility.anonymized_rollups.anonymize_rollups", return_value={"statistics": {"jobs": 5}}):
        result = daily_anonymize_and_prepare(summary_date=summary_date.isoformat())
    assert result["status"] == "success"
    return AnonymizedMetricsPayload.objects.get(summary_date=summary_date)


def test_missing_setting_uses_api_default():
    with patch("ansible_base.feature_flags.models.AAPFlag.objects.filter") as flags:
        flags.return_value.first.return_value = None
        assert _collect_leaderboard_telemetry()["enabled"] is True


@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_override_wins_over_environment(enabled, settings):
    settings.FEATURE = {"SHOW_LEADERBOARD": not enabled}
    Setting.objects.create(setting_key="SHOW_LEADERBOARD", current_value=json.dumps(enabled))
    assert _collect_leaderboard_telemetry()["enabled"] is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_environment_override(enabled, settings):
    settings.FEATURE = {"SHOW_LEADERBOARD": enabled}
    assert _collect_leaderboard_telemetry()["enabled"] is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_installer_override(enabled, settings):
    settings.FEATURE_SHOW_LEADERBOARD_ENABLED = enabled
    assert _collect_leaderboard_telemetry()["enabled"] is enabled


@pytest.mark.parametrize("enabled", [True, False])
def test_platform_flag(enabled):
    with patch("ansible_base.feature_flags.models.AAPFlag.objects.filter") as flags:
        flags.return_value.first.return_value.value = str(enabled)
        assert _collect_leaderboard_telemetry()["enabled"] is enabled


@pytest.mark.parametrize(
    "query",
    ["apps.dynamic_settings.models.Setting.objects.filter", "ansible_base.feature_flags.models.AAPFlag.objects.filter"],
)
def test_read_failure_is_unknown(query):
    with patch(query, side_effect=RuntimeError("database unavailable")):
        observation = _collect_leaderboard_telemetry()
    assert observation["enabled"] is None
    assert observation["observed_at"]


@pytest.mark.parametrize("enabled", [True, False])
def test_payload_contains_current_observation(enabled, settings):
    settings.FEATURE = {"DASHBOARD_COLLECTION": False, "SHOW_LEADERBOARD": enabled}
    observed_at = timezone.now()
    with patch("apps.tasks.collectors.daily_anonymize_and_prepare.timezone.now", return_value=observed_at):
        payload = _prepare_payload()

    assert payload.anonymized_data["leaderboard_telemetry"] == {
        "enabled": enabled,
        "observed_at": observed_at.isoformat(),
    }
    assert payload.anonymized_data["summary_metadata"]["summary_date"] != observed_at.date().isoformat()
    assert payload.anonymized_data["statistics"] == {"jobs": 5}
    assert payload.anonymized_data["dashboard_telemetry"] == [
        {"task_name": "sync_dashboard_job_records", "success": True}
    ]


def test_unavailable_setting_does_not_block_payload():
    with patch(
        "apps.tasks.collectors.daily_anonymize_and_prepare.get_feature_enabled_from_db",
        side_effect=RuntimeError("database unavailable"),
    ):
        payload = _prepare_payload()
    assert payload.anonymized_data["leaderboard_telemetry"]["enabled"] is None
    assert payload.status == "pending"


@pytest.mark.parametrize("initial_state", [True, False])
def test_toggle_changes_next_observation(initial_state):
    setting = Setting.objects.create(setting_key="SHOW_LEADERBOARD", current_value=json.dumps(initial_state))
    assert _collect_leaderboard_telemetry()["enabled"] is initial_state
    setting.current_value = json.dumps(not initial_state)
    setting.save()
    assert _collect_leaderboard_telemetry()["enabled"] is not initial_state


def test_delivery_retry_keeps_persisted_observation(settings):
    settings.FEATURE = {"SHOW_LEADERBOARD": True}
    payload = _prepare_payload()
    observation = payload.anonymized_data["leaderboard_telemetry"].copy()
    results = {"sent": 0, "failed": 0, "skipped": 0, "recovered": 0}
    with patch("apps.tasks.collectors.send_anonymized_to_segment.send_to_segment") as send:
        send.side_effect = [{"status": "error", "error": "timeout"}, {"status": "success"}]
        _process_single_payload(payload, results)
        settings.FEATURE = {"SHOW_LEADERBOARD": False}
        payload.refresh_from_db()
        _process_single_payload(payload, results)

    assert payload.status == "sent"
    assert results["failed"] == 1
    assert results["sent"] == 1
    for call in send.call_args_list:
        assert call.kwargs["segment_data"]["leaderboard_telemetry"] == observation
        assert call.kwargs["user_id"] == payload.segment_user_id


def test_opt_out_leaves_payload_unsent(settings):
    payload = _prepare_payload()
    settings.FEATURE = {"ANONYMIZED_DATA_COLLECTION": False}
    with patch("apps.tasks.collectors.send_anonymized_to_segment.send_to_segment") as send:
        result = send_anonymized_to_segment(payload_id=payload.id)

    assert result["status"] == "success"
    assert result["total_processed"] == 0
    send.assert_not_called()
    payload.refresh_from_db()
    assert payload.status == "pending"
