"""Integration tests for the dashboard collection status endpoint
GET/POST /api/v1/dashboard_reports/collection_status/."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient

from apps.dashboard_reports.models import JobData
from apps.dynamic_settings.models import Setting
from apps.tasks.cleanup.cleanup_old_tasks import cleanup_old_tasks
from apps.tasks.models import Task, TaskExecution
from apps.tasks.tasks_system import create_system_tasks, execute_db_task
from tests.test_utils import get_test_password

User = get_user_model()

COLLECTION_STATUS_ENDPOINT = "/api/v1/dashboard_reports/collection_status/"


@pytest.mark.integration
class TestCollectionStatusEndpoint(TestCase):
    """Integration tests for the /collection_status/ endpoint (GET + POST)."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            username="collection_status_admin",
            email="collection_status_admin@example.com",
            password=get_test_password(),
        )
        self.regular_user = User.objects.create_user(
            username="collection_status_user",
            email="collection_status_user@example.com",
            password=get_test_password(),
        )

    def tearDown(self):
        Setting.objects.filter(setting_key="SHOW_LEADERBOARD").delete()
        super().tearDown()

    def test_endpoint_resolves(self):
        """The URL /api/v1/dashboard_reports/collection_status/ resolves correctly."""
        match = resolve(COLLECTION_STATUS_ENDPOINT)
        assert match is not None

    def test_get_returns_200(self):
        """Authenticated request returns HTTP 200."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200

    def test_get_unauthenticated_returns_403(self):
        """Unauthenticated GET requests are rejected."""
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 403

    def test_get_default_show_leaderboard_true(self):
        """With no Setting row present, show_leaderboard defaults to True."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.json()["show_leaderboard"] is True

    def test_post_as_admin_sets_flag_true(self):
        """Admin POST with show_leaderboard=True persists a Setting row and is reflected in GET."""
        self.client.force_authenticate(user=self.admin)

        post_response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert post_response.status_code == 200
        assert post_response.json() == {"show_leaderboard": True}

        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is True
        assert setting.last_modified_by == self.admin

        get_response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert get_response.json()["show_leaderboard"] is True

    def test_post_as_admin_sets_flag_false(self):
        """Admin POST with show_leaderboard=False persists a Setting row and is reflected in GET."""
        self.client.force_authenticate(user=self.admin)
        Setting.objects.create(
            setting_key="SHOW_LEADERBOARD", current_value=json.dumps(True), last_modified_by=self.admin
        )

        post_response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": False}, format="json")
        assert post_response.status_code == 200
        assert post_response.json() == {"show_leaderboard": False}

        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is False

        get_response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert get_response.json()["show_leaderboard"] is False

    def test_post_updates_existing_row_not_duplicated(self):
        """A second POST updates the same Setting row instead of creating a new one."""
        self.client.force_authenticate(user=self.admin)

        self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": False}, format="json")

        assert Setting.objects.filter(setting_key="SHOW_LEADERBOARD").count() == 1
        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is False

    def test_post_as_regular_user_forbidden(self):
        """Non-admin/auditor users cannot toggle show_leaderboard."""
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert response.status_code == 403
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()

    def test_post_unauthenticated_forbidden(self):
        """Unauthenticated POST requests are rejected."""
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert response.status_code == 403
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()

    def test_post_non_boolean_value_returns_400(self):
        """A non-boolean show_leaderboard value is rejected with 400."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": "yes"}, format="json")
        assert response.status_code == 400
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()


@pytest.mark.integration
class TestCollectionStatusLastSync(TestCase):
    """Integration tests for last_sync: no sync -> trigger sync -> completed execution reflected."""

    def setUp(self):
        """Enable DASHBOARD_COLLECTION and authenticate as a superuser."""
        super().setUp()
        # last_sync is only computed when DASHBOARD_COLLECTION is enabled; set it explicitly
        # rather than relying on the ambient default, which isn't guaranteed across environments.
        Setting.objects.update_or_create(setting_key="DASHBOARD_COLLECTION", defaults={"current_value": "true"})
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            username="last_sync_admin",
            email="last_sync_admin@example.com",
            password=get_test_password(),
        )
        self.client.force_authenticate(user=self.admin)

    def _create_completed_sync_execution(self, completed_at, function_name="sync_dashboard_job_records"):
        """Create a Task + a completed TaskExecution for the given dashboard sync function."""
        task = Task.objects.create(
            name=f"{function_name}_{completed_at.isoformat()}_0",
            function_name=function_name,
        )
        execution = TaskExecution.objects.create(task=task, status="completed")
        execution.completed_at = completed_at
        execution.save()
        return execution

    def test_no_sync_yet_before_any_completed_execution(self):
        """With no TaskExecution rows at all, last_sync is 'None'."""
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200
        assert response.json()["last_sync"] is None

    def test_last_sync_reflects_completed_execution_after_it_finishes(self):
        """Simulates: check status (no sync yet) -> a sync_dashboard_job_records task runs and
        completes -> check status again -> last_sync now matches that execution's completed_at."""
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.json()["last_sync"] is None

        completed_at = datetime(2026, 9, 29, 11, 3, 6, tzinfo=UTC)
        self._create_completed_sync_execution(completed_at)

        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200
        returned = datetime.fromisoformat(response.json()["last_sync"].replace("Z", "+00:00"))
        assert returned == completed_at

    def _create_completed_initial_collection(self, completed_at):
        """Create the initial_dashboard_collection system task as it looks after a successful run."""
        task = Task.objects.create(
            name="initial_dashboard_collection",
            function_name="collect_dashboard_reports_initial_data",
            is_system_task=True,
            cron_expression=None,
            status="completed",
        )
        Task.objects.filter(pk=task.pk).update(completed_at=completed_at)
        return task

    def _get_last_sync(self):
        """GET the collection status and return (body, parsed last_sync)."""
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200
        body = response.json()
        last_sync = body["last_sync"]
        return body, datetime.fromisoformat(last_sync.replace("Z", "+00:00")) if last_sync else None

    def test_last_sync_reflects_initial_collection_when_no_hourly_sync(self):
        """With only a completed initial collection, last_sync is the initial task's completed_at."""
        completed_at = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
        self._create_completed_initial_collection(completed_at)

        _, last_sync = self._get_last_sync()
        assert last_sync == completed_at

    def test_last_sync_prefers_later_hourly_sync_over_initial_collection(self):
        """When both have completed and the hourly sync finished after the initial collection,
        last_sync is the sync_dashboard_job_records completed_at."""
        sync_completed_at = datetime(2026, 9, 29, 11, 3, 6, tzinfo=UTC)
        self._create_completed_initial_collection(datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC))
        self._create_completed_sync_execution(sync_completed_at)

        _, last_sync = self._get_last_sync()
        assert last_sync == sync_completed_at

    def test_last_sync_survives_init_system_tasks_after_zero_job_initial_collection(self):
        """Regression: a successful initial collection that writes zero jobs, followed by init-system-tasks
        (which deletes and recreates system tasks, cascading their executions), still reports its
        completion time as last_sync when hourly collection is disabled."""
        Setting.objects.update_or_create(setting_key="METRICS_COLLECTION", defaults={"current_value": "false"})
        create_system_tasks()
        initial_task = Task.objects.get(name="initial_dashboard_collection", is_system_task=True)

        # run_with_lock closes DB connections, which would break the test transaction; run the function directly.
        with (
            patch(
                "apps.tasks.tasks_system.run_with_lock",
                side_effect=lambda _lock_key, _task_name, func, **kwargs: func(**kwargs),
            ),
            patch(
                "apps.dashboard_reports.tasks._collect_data",
                return_value={"error": False, "data": {"job_count": 0}},
            ),
        ):
            execute_db_task(task_id=initial_task.id)

        initial_task.refresh_from_db()
        assert initial_task.status == "completed"
        assert initial_task.completed_at is not None
        assert not JobData.objects.exists()

        create_system_tasks()

        assert not TaskExecution.objects.filter(task__function_name="collect_dashboard_reports_initial_data").exists()
        body, last_sync = self._get_last_sync()
        assert body["initial_collection_status"] == "completed"
        assert last_sync == initial_task.completed_at

    def test_last_sync_survives_cleanup_old_tasks(self):
        """Regression: the daily cleanup_old_tasks (5 days) must not delete the completed initial collection,
        so last_sync keeps its completion time when hourly collection is disabled."""
        Setting.objects.update_or_create(setting_key="METRICS_COLLECTION", defaults={"current_value": "false"})
        completed_at = timezone.now() - timedelta(days=6)
        initial_task = self._create_completed_initial_collection(completed_at)
        JobData.objects.create(
            job_id=1001,
            template_name="Test Template",
            status="successful",
            started=datetime(2025, 3, 1, 10, 0, 0, tzinfo=UTC),
            finished=datetime(2025, 3, 1, 10, 8, 20, tzinfo=UTC),
            elapsed=500,
        )

        cleanup_old_tasks(days_old=5)

        assert Task.objects.filter(pk=initial_task.pk).exists()
        body, last_sync = self._get_last_sync()
        assert body["initial_collection_status"] == "completed"
        assert last_sync == completed_at

    def test_last_sync_ignores_running_or_pending_executions(self):
        """A running/pending TaskExecution for the same task must not surface as last_sync."""
        task = Task.objects.create(name="sync_dashboard_jobs_pending_0", function_name="sync_dashboard_job_records")
        TaskExecution.objects.create(task=task, status="running")
        TaskExecution.objects.create(task=task, status="pending")

        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.json()["last_sync"] is None

    def test_failed_execution_does_not_overwrite_last_known_good_sync(self):
        """A later failed execution must not overwrite the last successful sync timestamp."""
        success_completed_at = datetime(2026, 9, 29, 11, 3, 6, tzinfo=UTC)
        self._create_completed_sync_execution(success_completed_at)

        failed_task = Task.objects.create(
            name="sync_dashboard_jobs_failed_0", function_name="sync_dashboard_job_records"
        )
        failed = TaskExecution.objects.create(task=failed_task, status="failed")
        failed.completed_at = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)  # later than the successful run
        failed.save()

        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200
        returned = datetime.fromisoformat(response.json()["last_sync"].replace("Z", "+00:00"))
        assert returned == success_completed_at

    def test_post_missing_value_returns_400(self):
        """A POST body without show_leaderboard is rejected with 400."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {}, format="json")
        assert response.status_code == 400
