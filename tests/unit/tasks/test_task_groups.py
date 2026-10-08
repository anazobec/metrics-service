"""
Tests for task groups functionality.

This module tests the task group system including feature enable controls,
task categorization, and integration with the cron scheduler.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from apps.tasks.task_groups import (
    ANONYMIZATION_GROUP,
    INDIRECT_NODE_COLLECTION_GROUP,
    METRICS_COLLECTION_GROUP,
    SYSTEM_TASKS_GROUP,
    TASK_GROUPS,
    TaskGroup,
    get_all_enabled_tasks,
    get_all_tasks_for_init,
    get_oneshot_task_ids,
)

pytestmark = pytest.mark.unit

# override_settings replaces the entire FEATURE dict; include both keys when tests need
# metrics collectors plus anonymization tasks from get_all_enabled_tasks().
_FLAGS_METRICS_AND_ANON_ON = {"METRICS_COLLECTION": True, "ANONYMIZED_DATA_COLLECTION": True}
_FLAGS_METRICS_ON_ANON_OFF = {"METRICS_COLLECTION": True, "ANONYMIZED_DATA_COLLECTION": False}


class TestTaskGroup(TestCase):
    """Test the TaskGroup class."""

    def test_task_group_creation(self):
        """Test creating a task group."""
        group = TaskGroup(
            name="test_group",
            description="Test group",
            tasks=[
                {
                    "task_id": "test_task",
                    "function": "test_function",
                    "cron": "0 * * * *",
                    "enabled": True,
                    "description": "Test task",
                }
            ],
        )

        assert group.name == "test_group"
        assert group.description == "Test group"
        assert len(group.tasks) == 1

    @override_settings(FEATURE={"TEST_FLAG": True})
    def test_get_enabled_tasks_with_flag_true(self):
        """Test tasks are enabled when group's feature flag is True."""
        group = TaskGroup(
            name="test_group",
            description="Test group",
            feature_flag="TEST_FLAG",
            tasks=[
                {
                    "task_id": "test_task",
                    "function": "test_function",
                    "enabled": True,
                }
            ],
        )

        enabled_tasks = group.get_enabled_tasks()
        assert len(enabled_tasks) == 1

    @override_settings(FEATURE={"TEST_FLAG": False})
    def test_get_enabled_tasks_with_flag_false(self):
        """Test tasks are disabled when group's feature flag is False."""
        group = TaskGroup(
            name="test_group",
            description="Test group",
            feature_flag="TEST_FLAG",
            tasks=[
                {
                    "task_id": "test_task",
                    "function": "test_function",
                    "enabled": True,
                }
            ],
        )

        enabled_tasks = group.get_enabled_tasks()
        assert len(enabled_tasks) == 0

    def test_get_enabled_tasks_no_flag(self):
        """Test groups without feature flags return all enabled tasks."""
        group = TaskGroup(
            name="test_group",
            description="Test group",
            tasks=[
                {
                    "task_id": "test_task",
                    "function": "test_function",
                    "enabled": True,
                }
            ],
        )

        enabled_tasks = group.get_enabled_tasks()
        assert len(enabled_tasks) == 1

    def test_get_enabled_tasks_respects_enabled_field(self):
        """Test getting enabled tasks respects the enabled field."""
        tasks = [
            {
                "task_id": "task1",
                "function": "function1",
                "enabled": True,
            },
            {
                "task_id": "task2",
                "function": "function2",
                "enabled": False,
            },
        ]

        group = TaskGroup(
            name="test_group",
            description="Test group",
            tasks=tasks,
        )

        enabled_tasks = group.get_enabled_tasks()
        assert len(enabled_tasks) == 1
        assert enabled_tasks[0]["task_id"] == "task1"

    @override_settings(FEATURE={"TEST_FLAG": True})
    def test_get_enabled_tasks_with_task_enabled_false(self):
        """Test that individual tasks can be disabled via enabled field."""
        tasks = [
            {
                "task_id": "task1",
                "function": "function1",
                "enabled": True,
            },
            {
                "task_id": "task2",
                "function": "function2",
                "enabled": False,  # Manually disabled
            },
        ]

        group = TaskGroup(
            name="test_group",
            description="Test group",
            feature_flag="TEST_FLAG",
            tasks=tasks,
        )

        enabled_tasks = group.get_enabled_tasks()
        assert len(enabled_tasks) == 1
        assert enabled_tasks[0]["task_id"] == "task1"


class TestPredefinedTaskGroups(TestCase):
    """Test the predefined task groups."""

    def test_system_tasks_group(self):
        """Test system tasks group has no feature flag (always enabled)."""
        assert SYSTEM_TASKS_GROUP.name == "system_tasks"
        assert SYSTEM_TASKS_GROUP.feature_flag is None
        assert len(SYSTEM_TASKS_GROUP.tasks) > 0

        # Check for expected system tasks
        task_ids = [task["task_id"] for task in SYSTEM_TASKS_GROUP.tasks]
        assert "daily_task_cleanup" in task_ids
        assert "hourly_health_check" in task_ids

    @override_settings(FEATURE=_FLAGS_METRICS_ON_ANON_OFF)
    def test_metrics_collection_group_respects_feature_flag(self):
        """Test metrics collection group is gated by METRICS_COLLECTION (default-on in production settings)."""
        assert METRICS_COLLECTION_GROUP.name == "metrics_collection"
        assert METRICS_COLLECTION_GROUP.feature_flag == "METRICS_COLLECTION"

        task_ids = [task["task_id"] for task in METRICS_COLLECTION_GROUP.get_enabled_tasks()]
        assert len(task_ids) > 0
        # Hourly collection tasks
        assert "hourly_job_host_summary" in task_ids
        assert "hourly_job_events" in task_ids
        assert "hourly_unified_jobs" in task_ids
        assert "hourly_credentials" in task_ids
        # Daily snapshot collection
        assert "daily_config" in task_ids
        assert "daily_execution_environments" in task_ids
        assert "daily_feature_flags" in task_ids
        # Daily processing tasks
        assert "cleanup_metrics_data" in task_ids
        assert "daily_metrics_rollup" in task_ids
        # Anonymize and send tasks must NOT be in this group
        assert "daily_anonymize" not in task_ids
        assert "send_to_segment_daily" not in task_ids

    @override_settings(FEATURE={"METRICS_COLLECTION": False})
    def test_metrics_collection_group_disabled(self):
        """When METRICS_COLLECTION is false, the group contributes no enabled tasks."""
        assert METRICS_COLLECTION_GROUP.get_enabled_tasks() == []

    def test_anonymization_group_structure(self):
        """Test ANONYMIZATION_GROUP contains only daily_anonymize with the correct flag."""
        assert ANONYMIZATION_GROUP.name == "anonymization"
        assert ANONYMIZATION_GROUP.feature_flag == "ANONYMIZED_DATA_COLLECTION"

        task_ids = [task["task_id"] for task in ANONYMIZATION_GROUP.tasks]
        assert set(task_ids) == {"daily_anonymize"}

    @override_settings(FEATURE={"ANONYMIZED_DATA_COLLECTION": True})
    def test_anonymization_group_enabled(self):
        """Test ANONYMIZATION_GROUP returns both tasks when flag is enabled."""
        task_ids = [task["task_id"] for task in ANONYMIZATION_GROUP.get_enabled_tasks()]
        assert "daily_anonymize" in task_ids
        assert "send_to_segment_daily" not in task_ids

    @override_settings(FEATURE={"ANONYMIZED_DATA_COLLECTION": False})
    def test_anonymization_group_disabled(self):
        """Test ANONYMIZATION_GROUP returns no tasks when flag is disabled."""
        assert ANONYMIZATION_GROUP.get_enabled_tasks() == []

    @override_settings(FEATURE=_FLAGS_METRICS_AND_ANON_ON)
    def test_no_duplicate_cron_slots(self):
        """Assert that no two enabled tasks across all groups share the same cron hour:minute slot.

        Two tasks sharing the same hour and minute may run concurrently, which can cause
        data races (for example, a cleanup task deleting records that a rollup task is
        actively writing). This test prevents that class of scheduling conflict from being
        introduced silently.
        """
        from apps.tasks.task_groups import TASK_GROUPS

        all_enabled_tasks = []
        for group in TASK_GROUPS:
            all_enabled_tasks.extend(group.get_enabled_tasks())

        seen_slots: dict[tuple[int, int], str] = {}
        for task in all_enabled_tasks:
            cron = task.get("cron", "")
            if not cron:
                continue
            parts = cron.split()
            if len(parts) < 2:
                continue
            minute_raw, hour_raw = parts[0], parts[1]
            # Only compare tasks that fire at a single fixed hour:minute.
            if not (minute_raw.isdigit() and hour_raw.isdigit()):
                continue
            minute, hour = int(minute_raw), int(hour_raw)
            slot = (hour, minute)
            task_id = task["task_id"]
            assert slot not in seen_slots, (
                f"Tasks '{task_id}' and '{seen_slots[slot]}' share the same cron slot "
                f"{hour}:{minute:02d} - reschedule one to avoid concurrent execution"
            )
            seen_slots[slot] = task_id


class TestTaskGroupFunctions(TestCase):
    """Test utility functions for task groups."""

    @override_settings(FEATURE=_FLAGS_METRICS_ON_ANON_OFF)
    def test_get_all_enabled_tasks_flag_false(self):
        """Metrics collection tasks stay on; anonymization tasks absent when ANONYMIZED_DATA_COLLECTION is false."""
        all_tasks = get_all_enabled_tasks()
        task_ids = list(all_tasks.keys())

        # System tasks (always enabled)
        assert "daily_task_cleanup" in task_ids
        assert "hourly_health_check" in task_ids

        # Metrics collection tasks (METRICS_COLLECTION still true)
        assert "hourly_job_host_summary" in task_ids
        assert "hourly_job_events" in task_ids
        assert "daily_metrics_rollup" in task_ids
        assert "cleanup_metrics_data" in task_ids

        # Anonymize and send tasks (must be absent when flag is false)
        assert "daily_anonymize" not in task_ids
        assert "send_to_segment_daily" not in task_ids

        # Every returned task config must have the group metadata
        for _task_id, task_config in all_tasks.items():
            assert "group" in task_config
            assert "group_description" in task_config

    @override_settings(FEATURE=_FLAGS_METRICS_AND_ANON_ON)
    def test_get_all_enabled_tasks_flag_true(self):
        """All tasks including anonymize/send are present when flag is true."""
        all_tasks = get_all_enabled_tasks()
        task_ids = list(all_tasks.keys())

        assert "daily_anonymize" in task_ids
        assert "hourly_job_host_summary" in task_ids
        assert "hourly_job_events" in task_ids
        assert "daily_metrics_rollup" in task_ids

    @override_settings(FEATURE=_FLAGS_METRICS_ON_ANON_OFF)
    def test_get_all_tasks_for_init_includes_flagged_tasks(self):
        """get_all_tasks_for_init() returns anonymize and metrics tasks regardless of flag values."""
        all_tasks = get_all_tasks_for_init()
        task_ids = list(all_tasks.keys())

        # Anonymize task must be present regardless of the flag
        assert "daily_anonymize" in task_ids

        # The feature_flag must be stored in its config for runtime checking
        assert all_tasks["daily_anonymize"]["feature_flag"] == "ANONYMIZED_DATA_COLLECTION"
        assert all_tasks["hourly_job_host_summary"]["feature_flag"] == "METRICS_COLLECTION"

        # Collection tasks must also be present
        assert "hourly_job_host_summary" in task_ids
        assert "daily_metrics_rollup" in task_ids
        assert "daily_task_cleanup" in task_ids

    def test_get_all_tasks_for_init_includes_all_enabled_tasks(self):
        """get_all_tasks_for_init() includes all tasks with enabled=True."""
        all_tasks = get_all_tasks_for_init()
        assert "hourly_job_events" in all_tasks

    @override_settings(FEATURE={"METRICS_COLLECTION": False, "ANONYMIZED_DATA_COLLECTION": True})
    def test_get_all_enabled_tasks_metrics_collection_false_excludes_pipeline(self):
        """When METRICS_COLLECTION is false, hourly/daily collectors and rollup/cleanup are omitted."""
        all_tasks = get_all_enabled_tasks()
        task_ids = list(all_tasks.keys())

        assert "daily_task_cleanup" in task_ids
        assert "hourly_job_host_summary" not in task_ids
        assert "hourly_job_events" not in task_ids
        assert "daily_metrics_rollup" not in task_ids
        assert "cleanup_metrics_data" not in task_ids
        assert "daily_anonymize" in task_ids

    def test_get_oneshot_task_ids_returns_only_tasks_without_cron(self):
        """Only one-shot (cron=None) task-group tasks are returned, never recurring ones."""
        oneshot_ids = get_oneshot_task_ids()

        assert "initial_dashboard_collection" in oneshot_ids
        assert "initial_resource_sync" in oneshot_ids
        assert "hourly_health_check" not in oneshot_ids
        assert "daily_task_cleanup" not in oneshot_ids
        assert all(not get_all_tasks_for_init()[task_id].get("cron") for task_id in oneshot_ids)


class TestIndirectNodeCollectionGroup(TestCase):
    """Test the INDIRECT_NODE_COLLECTION_GROUP task group."""

    def test_indirect_node_collection_group_in_task_groups_registry(self):
        """INDIRECT_NODE_COLLECTION_GROUP is registered in TASK_GROUPS."""
        assert INDIRECT_NODE_COLLECTION_GROUP in TASK_GROUPS

    @override_settings(FEATURE={"INDIRECT_NODE_COLLECTION": False})
    def test_indirect_node_collection_group_disabled_by_default(self):
        """When INDIRECT_NODE_COLLECTION is false, no tasks are returned."""
        assert INDIRECT_NODE_COLLECTION_GROUP.feature_flag == "INDIRECT_NODE_COLLECTION"
        assert INDIRECT_NODE_COLLECTION_GROUP.get_enabled_tasks() == []

    @override_settings(FEATURE={"INDIRECT_NODE_COLLECTION": True})
    def test_indirect_node_collection_group_enabled(self):
        """When INDIRECT_NODE_COLLECTION is true, daily_collect_indirect_nodes is returned."""
        task_ids = [t["task_id"] for t in INDIRECT_NODE_COLLECTION_GROUP.get_enabled_tasks()]
        assert "daily_collect_indirect_nodes" in task_ids

    def test_indirect_node_collection_group_uses_correct_cron(self):
        """daily_collect_indirect_nodes task is scheduled at 55 1 * * * via collect_daily_metrics."""
        task = next(t for t in INDIRECT_NODE_COLLECTION_GROUP.tasks if t["task_id"] == "daily_collect_indirect_nodes")
        assert task["cron"] == "55 1 * * *"
        assert task["function"] == "collect_daily_metrics"
        assert task["args"]["collector_type"] == "indirect_managed_nodes"

    def test_indirect_managed_nodes_not_in_metrics_collection_group(self):
        """indirect_managed_nodes must not be scheduled by METRICS_COLLECTION_GROUP.

        Prevents a duplicate snapshot run at 55 1 * * * when both groups are active.
        INDIRECT_NODE_COLLECTION_GROUP is the sole, feature-flag-gated owner.
        """
        task_ids = [t["task_id"] for t in METRICS_COLLECTION_GROUP.tasks]
        assert "daily_indirect_managed_nodes" not in task_ids


class TestTaskGroupIntegration(TestCase):
    """Test integration between task groups and other components."""

    @patch("apps.tasks.cron_scheduler.get_scheduler")
    def test_scheduler_integration(self, mock_get_scheduler):
        """Test integration with the task scheduler."""
        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_get_scheduler.return_value = mock_scheduler

        # Verify scheduler can be retrieved and is running
        from apps.tasks.cron_scheduler import get_scheduler

        scheduler = get_scheduler()
        assert scheduler == mock_scheduler
        assert scheduler.running is True

    @override_settings(FEATURE=_FLAGS_METRICS_AND_ANON_ON)
    def test_all_flags_enabled(self):
        """Test that all tasks are present when ANONYMIZED_DATA_COLLECTION is enabled."""
        all_tasks = get_all_enabled_tasks()
        task_ids = list(all_tasks.keys())

        # System tasks
        assert any("cleanup" in task_id for task_id in task_ids)

        # Metrics collection tasks
        assert any("hourly" in task_id for task_id in task_ids)
        assert "daily_metrics_rollup" in task_ids

        # Anonymization tasks (from ANONYMIZATION_GROUP, enabled when flag is true)
        assert "daily_anonymize" in task_ids

    @override_settings(FEATURE=_FLAGS_METRICS_ON_ANON_OFF)
    def test_flag_false_stops_only_anonymize_send(self):
        """When ANONYMIZED_DATA_COLLECTION is false but METRICS_COLLECTION is true, only anonymize is absent."""
        all_tasks = get_all_enabled_tasks()
        task_ids = list(all_tasks.keys())

        # System tasks must still be present
        assert "daily_task_cleanup" in task_ids
        assert "hourly_health_check" in task_ids

        # Metrics collection tasks must still be present
        assert "hourly_job_host_summary" in task_ids
        assert "hourly_job_events" in task_ids
        assert "hourly_unified_jobs" in task_ids
        assert "hourly_credentials" in task_ids
        assert "daily_metrics_rollup" in task_ids
        assert "cleanup_metrics_data" in task_ids

        # Only this must be absent when flag is disabled
        assert "daily_anonymize" not in task_ids
