"""
Additional tests for apps/tasks/v1/views.py — covering uncovered endpoints.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def perm_client(user):
    """APIClient authenticated as a superuser (passes IsSystemAdminOrAuditor)."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# cleanup endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_cleanup_dry_run(perm_client, user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="old_completed_cleanup",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="completed",
    )
    Task.objects.filter(pk=task.pk).update(completed_at=timezone.now() - timedelta(days=40))

    response = perm_client.post("/api/v1/tasks/cleanup/", {"days": 30, "dry_run": True}, format="json")

    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert "preview" in data


@pytest.mark.unit
@pytest.mark.django_db
def test_task_cleanup_actual_delete(perm_client, user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="old_for_cleanup",
        function_name="hello_world",
        task_data={},
        created_by=user,
        status="completed",
    )
    Task.objects.filter(pk=task.pk).update(completed_at=timezone.now() - timedelta(days=40))

    response = perm_client.post("/api/v1/tasks/cleanup/", {"days": 30, "dry_run": False}, format="json")

    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert not Task.objects.filter(pk=task.pk).exists()


# ---------------------------------------------------------------------------
# available_functions endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_available_functions_lists_functions(perm_client):
    response = perm_client.get("/api/v1/tasks/available_functions/")

    assert response.status_code == 200
    data = response.json()
    assert "functions" in data
    assert isinstance(data["functions"], list)
    assert any(f["name"] == "hello_world" for f in data["functions"])


@pytest.mark.unit
@pytest.mark.django_db
def test_available_functions_batch_size_default_follows_setting(perm_client, settings):
    """The advertised batch_size default is the configured BACKFILL_BATCH_SIZE, not a constant."""
    settings.DASHBOARD_COLLECTION = {"BACKFILL_BATCH_SIZE": 1234}

    response = perm_client.get("/api/v1/tasks/available_functions/")

    function = next(f for f in response.json()["functions"] if f["name"] == "update_dashboard_job_definition")
    assert function["parameters"]["batch_size"]["default"] == 1234


# ---------------------------------------------------------------------------
# scheduler_status endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_scheduler_status_endpoint(perm_client):
    mock_scheduler = MagicMock()
    mock_scheduler.running = True
    mock_scheduler.check_interval = 30

    with patch("apps.tasks.cron_scheduler.get_scheduler", return_value=mock_scheduler):
        response = perm_client.get("/api/v1/tasks/scheduler_status/")

    assert response.status_code == 200
    data = response.json()
    assert "scheduler" in data
    assert data["scheduler"]["running"] is True


# ---------------------------------------------------------------------------
# schedule_immediate endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_schedule_immediate_endpoint(perm_client, user):
    payload = {
        "name": "Immediate Test Task",
        "function_name": "hello_world",
        "task_data": {},
    }

    response = perm_client.post("/api/v1/tasks/schedule_immediate/", payload, format="json")

    assert response.status_code in (200, 201)
    if response.status_code in (200, 201):
        data = response.json()
        assert "task_id" in data or "message" in data


# ---------------------------------------------------------------------------
# schedule_recurring endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_schedule_recurring_endpoint(perm_client, user):
    payload = {
        "name": "Recurring Test Task",
        "function_name": "hello_world",
        "task_data": {},
        "cron_expression": "0 * * * *",
    }

    response = perm_client.post("/api/v1/tasks/schedule_recurring/", payload, format="json")

    assert response.status_code in (200, 201)


@pytest.mark.unit
@pytest.mark.django_db
def test_schedule_recurring_no_cron_returns_400(perm_client):
    payload = {
        "name": "Missing Cron Task",
        "function_name": "hello_world",
        "task_data": {},
    }

    response = perm_client.post("/api/v1/tasks/schedule_recurring/", payload, format="json")

    assert response.status_code in (400,)  # Missing cron_expression


# ---------------------------------------------------------------------------
# force_delete endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_force_delete_without_confirm_returns_400(perm_client, user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="sys_task_to_force_delete",
        function_name="hello_world",
        task_data={},
        created_by=user,
        is_system_task=True,
    )

    response = perm_client.delete(f"/api/v1/tasks/{task.id}/force_delete/", format="json")

    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.django_db
def test_force_delete_system_task_with_confirm(perm_client, user):
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="sys_task_force_del",
        function_name="hello_world",
        task_data={},
        created_by=user,
        is_system_task=True,
    )

    response = perm_client.delete(
        f"/api/v1/tasks/{task.id}/force_delete/",
        data={"force_confirm": True},
        format="json",
    )

    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.django_db
def test_perform_destroy_system_task_raises(perm_client, user):
    """DELETE a system task should be rejected."""
    from apps.tasks.models import Task

    task = Task.objects.create(
        name="protected_sys_task",
        function_name="hello_world",
        task_data={},
        created_by=user,
        is_system_task=True,
    )

    response = perm_client.delete(f"/api/v1/tasks/{task.id}/")

    assert response.status_code in (403, 405)
    assert Task.objects.filter(pk=task.pk).exists()


# ---------------------------------------------------------------------------
# TaskExecutionViewSet
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.django_db
def test_task_execution_list(perm_client, user):
    from apps.tasks.models import Task, TaskExecution

    task = Task.objects.create(name="exec_list_task", function_name="hello_world", task_data={}, created_by=user)
    TaskExecution.objects.create(task=task, status="completed")

    response = perm_client.get("/api/v1/tasks/executions/")

    assert response.status_code == 200
