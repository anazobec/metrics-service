"""Tests for the analytics collector registry (the whitelist)."""

import pytest
from metrics_utility.library.collectors.controller import __all__ as controller_collectors
from metrics_utility.library.collectors.others import __all__ as other_collectors
from metrics_utility.library.collectors.service import __all__ as service_collectors

from apps.analytics import registry


@pytest.mark.unit
def test_public_name_is_group_dot_function():
    entry = registry.get_entry("controller.unified_jobs_dashboard")
    assert entry is not None
    assert entry.name == "controller.unified_jobs_dashboard"
    assert entry.group == "controller"
    assert entry.mu_function == "unified_jobs_dashboard"
    # Internal metrics-service key differs from the public function name here.
    assert entry.collector_type == "unified_jobs"


@pytest.mark.unit
def test_enabled_collectors_are_all_enabled():
    enabled = registry.enabled_collectors()
    assert enabled, "expected at least one enabled collector"
    assert all(e.enabled for e in enabled.values())
    assert all(e.description for e in enabled.values())
    assert {
        "controller.job_host_summary",
        "controller.main_jobevent",
        "controller.unified_jobs",
    } <= set(enabled)
    # Pipeline-only, dashboard-only, AWX in-process, and external Prometheus collectors stay off.
    assert "service.task_executions_service" not in enabled
    assert "dashboard.dashboard_jobs" not in enabled
    assert "controller.config_django" not in enabled
    assert "others.total_workers_vcpu" not in enabled


@pytest.mark.unit
def test_enabled_collectors_have_named_payload_schemas():
    enabled = registry.enabled_collectors().values()

    assert all(entry.payload_schema.name for entry in enabled)
    assert all(entry.payload_schema.description for entry in enabled)
    assert {entry.payload_schema.kind for entry in enabled} == {"array", "object"}


@pytest.mark.unit
def test_payload_schema_types_match_representative_collector_fields():
    jobs = registry.get_entry("controller.unified_jobs_dashboard").payload_schema
    assert {field.name: field.type for field in jobs.item_fields}["id"] == "integer"
    assert {field.name: field.type for field in jobs.item_fields}["finished"] == "datetime"
    assert {field.name: field.type for field in jobs.item_fields}["failed"] == "boolean"

    counts = registry.get_entry("controller.counts").payload_schema
    assert counts.kind == "object"
    assert {field.name: field.type for field in counts.fields}["running_jobs"] == "integer"
    assert counts.additional_properties is None

    instance_info = registry.get_entry("controller.instance_info").payload_schema
    assert instance_info.additional_properties.type == "object"
    assert {field.name: field.type for field in instance_info.additional_properties.fields}["capacity"] == "integer"


def _item_types(name):
    return {field.name: field.type for field in registry.get_entry(name).payload_schema.item_fields}


@pytest.mark.unit
def test_payload_schemas_follow_collector_variants():
    # The base collector has no modified column; the dashboard variant does.
    assert "modified" not in _item_types("controller.unified_jobs")
    assert "modified" in _item_types("controller.unified_jobs_dashboard")

    # JobHostSummary.failed is a boolean flag, unlike the integer task counters.
    assert _item_types("controller.job_host_summary")["failed"] == "boolean"
    assert _item_types("controller.job_host_summary")["ansible_host_variable"] == "string"
    assert "ansible_host_variable" not in _item_types("controller.job_host_summary_service")

    for name in ("controller.events_table", "controller.main_jobevent_service"):
        assert {"collection_name", "collection_version"} <= set(_item_types(name))
    assert "main_jobhostsummary_id" in _item_types("controller.main_jobevent")

    audit = _item_types("controller.main_indirectmanagednodeaudit")
    assert audit["events"] == "array"
    assert audit["task_runs"] == "integer"


@pytest.mark.unit
def test_config_payload_schema_matches_license_types():
    config = {field.name: field.type for field in registry.get_entry("controller.config").payload_schema.fields}
    assert config["date_expired"] == "boolean"
    assert config["license_date"] == "integer"
    assert config["usage"] == "string"


@pytest.mark.unit
def test_payload_schemas_are_all_used_by_enabled_collectors():
    from apps.analytics.payload_schemas import PAYLOAD_SCHEMAS

    enabled = registry.enabled_collectors().values()
    assert set(PAYLOAD_SCHEMAS) == {entry.mu_function for entry in enabled}
    assert all(entry.payload_schema is PAYLOAD_SCHEMAS[entry.mu_function] for entry in enabled)


@pytest.mark.unit
def test_enabled_collector_requires_payload_schema():
    with pytest.raises(ValueError, match="payload_schema"):
        registry.CollectorEntry("controller.example", collector_type="example", mode="hourly", enabled=True)

    disabled = registry.CollectorEntry("controller.example", collector_type="example", mode="", enabled=False)
    assert disabled.payload_schema is None


@pytest.mark.unit
def test_accepts_since_until_by_mode():
    assert registry.get_entry("controller.unified_jobs_dashboard").accepts_since_until is True  # hourly
    assert registry.get_entry("controller.events_table").accepts_since_until is True  # hourly
    assert registry.get_entry("controller.config").accepts_since_until is False  # snapshot
    assert registry.get_entry("controller.counts").accepts_since_until is False  # snapshot


@pytest.mark.unit
def test_get_entry_by_type_maps_internal_key():
    entry = registry.get_entry_by_type("unified_jobs")
    assert entry is not None
    assert entry.name == "controller.unified_jobs_dashboard"
    assert registry.get_entry_by_type("does_not_exist") is None


@pytest.mark.unit
def test_indirect_managed_node_audit_is_enabled():
    entry = registry.get_entry("controller.main_indirectmanagednodeaudit")
    assert entry is not None
    assert entry.collector_type == "indirect_managed_nodes"
    assert entry.mode == "daily"
    assert entry.enabled is True
    assert entry in registry.enabled_collectors().values()


@pytest.mark.unit
def test_registry_covers_metrics_utility_collectors():
    expected = {
        *(f"controller.{name}" for name in controller_collectors),
        *(f"service.{name}" for name in service_collectors),
        *(f"others.{name}" for name in other_collectors),
        "dashboard.dashboard_jobs",
    }
    assert set(registry.COLLECTORS) == expected
    assert len(registry.COLLECTORS) == len(registry._ALL)


@pytest.mark.unit
def test_not_enabled_collectors_have_reasons():
    assert all(entry.note for entry in registry.COLLECTORS.values() if not entry.enabled)


@pytest.mark.unit
def test_enabled_collectors_are_wired_to_collection_tasks():
    from apps.tasks.collectors.collect_daily_metrics import _get_daily_collectors
    from apps.tasks.collectors.collect_hourly_metrics import _get_hourly_collectors
    from apps.tasks.collectors.collect_snapshot_metrics import _get_snapshot_collectors

    task_collectors = {
        *(_get_hourly_collectors()),
        *(_get_snapshot_collectors()),
        *(_get_daily_collectors()),
    }
    assert {entry.collector_type for entry in registry.enabled_collectors().values()} <= task_collectors
