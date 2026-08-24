"""Unit tests for the DashboardCollectionStatusViewSet."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from apps.dashboard_reports.urls import router
from apps.dashboard_reports.viewsets.collection_status import DashboardCollectionStatusViewSet

PATCH_FLAG = "apps.dashboard_reports.viewsets.collection_status.get_feature_enabled_from_db"
PATCH_TASK = "apps.dashboard_reports.viewsets.collection_status.Task"
PATCH_PERM = "ansible_base.rbac.api.permissions.IsSystemAdminOrAuditor.has_permission"
PATCH_MIN_TS = "apps.dashboard_reports.viewsets.collection_status.JobData.min_timestamp"
PATCH_SETTING = "apps.dashboard_reports.viewsets.collection_status.Setting"
factory = APIRequestFactory()
view = DashboardCollectionStatusViewSet.as_view({"get": "list"})
create_view = DashboardCollectionStatusViewSet.as_view({"post": "create"})


@pytest.mark.unit
class TestDashboardCollectionStatusViewSet:
    """Tests for DashboardCollectionStatusViewSet.list()."""

    @pytest.fixture(autouse=True)
    def bypass_permissions(self):
        with patch(PATCH_PERM, return_value=True):
            yield

    def _get(self):
        request = factory.get("/api/v1/dashboard_reports/collection_status/")
        request.user = MagicMock()
        return view(request)

    @patch(PATCH_FLAG, return_value=False)
    @patch(PATCH_TASK)
    def test_flag_disabled_returns_nulls(self, mock_task_class, mock_flag):
        """When disabled, all fields except enabled are null and DB is not queried."""
        response = self._get()
        assert response.status_code == 200
        assert response.data == {
            "enabled": False,
            "next_run": None,
            "initial_collection_status": None,
            "min_collection_timestamp": None,
            "show_gamification": False,
            "show_dashboard": False,
        }
        mock_task_class.objects.filter.assert_not_called()

    @patch(PATCH_FLAG, return_value=False)
    @patch(PATCH_TASK)
    def test_min_timestamp_not_queried_when_disabled(self, mock_task_class, mock_flag):
        """JobData.min_timestamp() should NOT be called when feature flag is disabled."""
        with patch(PATCH_MIN_TS) as mock_min_ts:
            self._get()
            mock_min_ts.assert_not_called()

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_flag_enabled_tasks_exist(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = "2026-04-15T06:00:00+00:00"
        mock_initial_task = MagicMock()
        mock_initial_task.status = "completed"
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.status_code == 200
        assert response.data["enabled"] is True
        assert response.data["next_run"] == "2026-04-15T06:00:00+00:00"
        assert response.data["initial_collection_status"] == "completed"
        assert response.data["min_collection_timestamp"] is not None

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_flag_enabled_tasks_not_found(self, mock_task_class, mock_flag, mock_min_ts):
        mock_task_class.objects.filter.return_value.first.return_value = None
        response = self._get()
        assert response.status_code == 200
        assert response.data == {
            "enabled": True,
            "next_run": None,
            "initial_collection_status": None,
            "min_collection_timestamp": None,
            "show_gamification": True,
            "show_dashboard": True,
        }

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_min_timestamp_queried_when_enabled(self, mock_task_class, mock_flag, mock_min_ts):
        """JobData.min_timestamp() IS called when feature flag is enabled."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        self._get()
        mock_min_ts.assert_called_once()

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_min_timestamp_returned_in_response(self, mock_task_class, mock_flag, mock_min_ts):
        """min_collection_timestamp in response matches JobData.min_timestamp() value."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        expected = datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC)
        response = self._get()
        assert response.data["min_collection_timestamp"] == expected

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_initial_status_running(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = None
        mock_initial_task = MagicMock()
        mock_initial_task.status = "running"
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.data["initial_collection_status"] == "running"

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_initial_status_failed(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = None
        mock_initial_task = MagicMock()
        mock_initial_task.status = "failed"
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.data["initial_collection_status"] == "failed"

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_queries_correct_tasks(self, mock_task_class, mock_flag, mock_min_ts):
        """next_run comes from hourly_unified_jobs (the hook driver), not the removed data task."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        self._get()
        calls = mock_task_class.objects.filter.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs == {
            "name": "hourly_unified_jobs",
            "is_system_task": True,
        }
        assert calls[1].kwargs == {
            "function_name": "collect_dashboard_reports_initial_data",
            "is_system_task": True,
        }

    def test_permission_class(self):
        """The viewset no longer gates all actions via class-level permission_classes;
        IsSystemAdminOrAuditor is checked manually inside list()/create() instead."""
        from rest_framework.permissions import IsAuthenticated

        assert DashboardCollectionStatusViewSet.permission_classes == [IsAuthenticated]


@pytest.mark.unit
class TestDashboardCollectionStatusURL:
    """Tests for URL registration and reversal."""

    def test_url_reverse(self):
        url = reverse("v1:collection_status-list")
        assert "collection_status" in url

    def test_viewset_registered_in_router(self):
        registered = [reg[1] for reg in router.registry]
        assert DashboardCollectionStatusViewSet in registered


@pytest.mark.unit
class TestDashboardCollectionStatusCreate:
    """Tests for DashboardCollectionStatusViewSet.create() (POST show_gamification toggle)."""

    def _post(self, data):
        request = factory.post("/api/v1/dashboard_reports/collection_status/", data, format="json")
        request.user = MagicMock()
        return create_view(request)

    @patch(PATCH_PERM, return_value=False)
    def test_non_admin_forbidden(self, mock_perm):
        """Non admin/auditor users get 403 and no Setting write is attempted."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_gamification": True})
            mock_setting.objects.update_or_create.assert_not_called()
        assert response.status_code == 403

    @patch(PATCH_PERM, return_value=True)
    def test_non_boolean_value_rejected(self, mock_perm):
        """Non-boolean show_gamification value returns 400 and does not touch the DB."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_gamification": "true"})
            mock_setting.objects.update_or_create.assert_not_called()
        assert response.status_code == 400
        assert "show_gamification" in response.data

    @patch(PATCH_PERM, return_value=True)
    def test_missing_value_rejected(self, mock_perm):
        """Missing show_gamification key returns 400."""
        response = self._post({})
        assert response.status_code == 400

    @patch(PATCH_PERM, return_value=True)
    def test_sets_flag_true(self, mock_perm):
        """POST with True persists SHOW_GAMIFICATION=true via update_or_create."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_gamification": True})
            mock_setting.objects.update_or_create.assert_called_once()
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["setting_key"] == "SHOW_GAMIFICATION"
            assert kwargs["defaults"]["current_value"] == json.dumps(True)
        assert response.status_code == 200
        assert response.data == {"show_gamification": True}

    @patch(PATCH_PERM, return_value=True)
    def test_sets_flag_false(self, mock_perm):
        """POST with False persists SHOW_GAMIFICATION=false via update_or_create."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_gamification": False})
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["defaults"]["current_value"] == json.dumps(False)
        assert response.status_code == 200
        assert response.data == {"show_gamification": False}

    @patch(PATCH_PERM, return_value=True)
    def test_last_modified_by_set_to_request_user(self, mock_perm):
        """The requesting user is recorded as last_modified_by."""
        request = factory.post(
            "/api/v1/dashboard_reports/collection_status/", {"show_gamification": True}, format="json"
        )
        sentinel_user = MagicMock()
        request.user = sentinel_user
        with patch(PATCH_SETTING) as mock_setting:
            create_view(request)
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["defaults"]["last_modified_by"] is sentinel_user
