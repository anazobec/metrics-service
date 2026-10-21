"""Payload shapes for the metrics-utility collectors exposed by analytics.

Each schema mirrors the output of the metrics-utility collector of the same name
(``metrics_utility/library/collectors/controller/<name>.py``). DataFrame collectors are
arrays of records whose fields follow the SQL ``SELECT`` list; dict collectors are objects.
DataFrame values are stored after ``DataFrame.to_json(orient="records", date_format="iso")``,
so timestamps are ISO 8601 strings and missing values are null.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayloadField:
    """Describe one JSON property returned by a collector."""

    name: str
    type: str
    description: str = ""
    nullable: bool = True
    fields: tuple[PayloadField, ...] = ()
    items: PayloadField | None = None
    additional_properties: PayloadField | None = None


@dataclass(frozen=True)
class PayloadSchema:
    """Describe the top-level JSON shape returned by a collector."""

    name: str
    kind: str
    description: str
    fields: tuple[PayloadField, ...] = ()
    item_fields: tuple[PayloadField, ...] = ()
    additional_properties: PayloadField | None = None


def _field(name: str, type_: str, description: str = "", *, nullable: bool = True, **kwargs) -> PayloadField:
    return PayloadField(name, type_, description, nullable, **kwargs)


def _object(name: str, fields: tuple[PayloadField, ...] = (), description: str = "", **kwargs) -> PayloadField:
    return _field(name, "object", description, fields=fields, **kwargs)


def _array(name: str, items: PayloadField, description: str = "") -> PayloadField:
    return _field(name, "array", description, items=items)


def _map_value(name: str, type_: str = "object", fields: tuple[PayloadField, ...] = ()) -> PayloadField:
    return PayloadField(name, type_, fields=fields)


def _pick(catalog: dict[str, PayloadField], *names: str) -> tuple[PayloadField, ...]:
    """Select fields from a catalog in collector column order."""
    return tuple(catalog[name] for name in names)


def _catalog(*fields: PayloadField) -> dict[str, PayloadField]:
    return {field.name: field for field in fields}


_INSTALLED_COLLECTIONS = _object(
    "installed_collections",
    description="Ansible collections installed in the job's execution environment, keyed by collection FQCN.",
    additional_properties=_map_value("InstalledCollection", "object", (_field("version", "string"),)),
)

_JOB_FIELDS = (
    _field("id", "integer", "Unified job identifier", nullable=False),
    _field("polymorphic_ctype_id", "integer", "Django content type ID of the concrete job type."),
    _field("model", "string", "Concrete job type, for example job, project_update, or workflow_job."),
    _field("organization_id", "integer"),
    _field("organization_name", "string"),
    _field("execution_environment_image", "string"),
    _field("inventory_id", "integer"),
    _field("inventory_name", "string"),
    _field("execution_environment_id", "integer"),
    _field("created", "datetime"),
    _field("modified", "datetime"),
    _field("name", "string"),
    _field("unified_job_template_id", "integer"),
    _field("launch_type", "string", "How the job was launched, for example manual, relaunch, scheduled, or workflow."),
    _field("schedule_id", "integer"),
    _field("execution_node", "string"),
    _field("controller_node", "string"),
    _field("cancel_flag", "boolean"),
    _field("status", "string"),
    _field("failed", "boolean"),
    _field("started", "datetime"),
    _field("finished", "datetime"),
    _field("elapsed", "string", 'Job run time in seconds, as a decimal string (for example "120.000").'),
    _field("job_explanation", "string"),
    _field("instance_group_id", "integer"),
    _INSTALLED_COLLECTIONS,
    _field("ansible_version", "string"),
    _field("forks", "integer"),
    _field("job_template_name", "string"),
    _field("scm_type", "string", "Source-control type of the job's project."),
)

UNIFIED_JOBS = PayloadSchema(
    name="UnifiedJobsPayload",
    kind="array",
    description="Unified job records returned by unified_jobs, filtered by job finish time.",
    item_fields=tuple(field for field in _JOB_FIELDS if field.name != "modified"),
)

UNIFIED_JOBS_DASHBOARD = PayloadSchema(
    name="UnifiedJobsDashboardPayload",
    kind="array",
    description="Job execution records returned by unified_jobs_dashboard, filtered by job finish time.",
    item_fields=_JOB_FIELDS
    + (
        _field("project_id", "integer"),
        _field("project_name", "string"),
        _field(
            "launched_by_id", "integer", "User ID that launched the job; null unless launch_type is manual or relaunch."
        ),
        # The collector also returns launched_by_username, which is deliberately undeclared: it is
        # collected only for the in-service dashboard sync and withheld from everything leaving the
        # service (ANSTRAT-1587 requirement #8, AAP-96704, ansible/metrics-service#495).
        _field("label_ids", "string", "Comma-separated label IDs."),
        _field("num_hosts", "integer", "Number of job host summaries recorded for the job."),
    ),
)

_HOST_SUMMARY = _catalog(
    _field("id", "integer", "Job host summary identifier", nullable=False),
    _field("created", "datetime"),
    _field("modified", "datetime"),
    _field("host_name", "string"),
    _field("host_remote_id", "integer", "Controller host ID."),
    _field("ansible_host_variable", "string", "ansible_host value from the host variables."),
    _field("ansible_connection_variable", "string", "ansible_connection value from the host variables."),
    _field("changed", "integer", "Number of tasks that reported changes on the host."),
    _field("dark", "integer", "Number of tasks for which the host was unreachable."),
    _field("failures", "integer", "Number of failed tasks on the host."),
    _field("ok", "integer", "Number of successful tasks on the host."),
    _field("processed", "integer"),
    _field("skipped", "integer", "Number of skipped tasks on the host."),
    _field("failed", "boolean", "Whether the host failed in the job."),
    _field("ignored", "integer", "Number of failed tasks ignored on the host."),
    _field("rescued", "integer", "Number of failed tasks rescued on the host."),
    _field("job_created", "datetime"),
    _field("job_remote_id", "integer", "Controller job ID."),
    _field("job_template_remote_id", "integer", "Controller unified job template ID."),
    _field("job_template_name", "string"),
    _field("ansible_version", "string"),
    _field("launch_type", "string"),
    _field("inventory_remote_id", "integer", "Controller inventory ID."),
    _field("inventory_name", "string"),
    _field("organization_remote_id", "integer", "Controller organization ID."),
    _field("organization_name", "string"),
    _field("project_remote_id", "integer", "Controller project ID."),
    _field("project_name", "string"),
    _field("model", "string", "Concrete job type of the related job."),
)

JOB_HOST_SUMMARY = PayloadSchema(
    name="JobHostSummaryPayload",
    kind="array",
    description="Per-job host summary records returned by job_host_summary, filtered by summary modification time.",
    item_fields=_pick(
        _HOST_SUMMARY,
        "id",
        "created",
        "modified",
        "host_name",
        "host_remote_id",
        "ansible_host_variable",
        "ansible_connection_variable",
        "changed",
        "dark",
        "failures",
        "ok",
        "processed",
        "skipped",
        "failed",
        "ignored",
        "rescued",
        "job_created",
        "job_remote_id",
        "job_template_remote_id",
        "job_template_name",
        "inventory_remote_id",
        "inventory_name",
        "organization_remote_id",
        "organization_name",
        "project_remote_id",
        "project_name",
    ),
)

JOB_HOST_SUMMARY_SERVICE = PayloadSchema(
    name="JobHostSummaryServicePayload",
    kind="array",
    description="Per-job host summary records returned by job_host_summary_service, filtered by job finish time.",
    item_fields=_pick(
        _HOST_SUMMARY,
        "id",
        "created",
        "modified",
        "host_name",
        "host_remote_id",
        "changed",
        "dark",
        "failures",
        "ok",
        "processed",
        "skipped",
        "failed",
        "ignored",
        "rescued",
        "job_created",
        "job_remote_id",
        "job_template_remote_id",
        "job_template_name",
        "ansible_version",
        "launch_type",
        "inventory_remote_id",
        "inventory_name",
        "organization_remote_id",
        "organization_name",
        "project_remote_id",
        "project_name",
        "model",
    ),
)

CREDENTIALS = PayloadSchema(
    name="CredentialsServicePayload",
    kind="array",
    description="Managed credential types used by jobs in the collection window.",
    item_fields=(_field("credential_type", "string", "Managed credential type name.", nullable=False),),
)

_DEPRECATION = _object(
    "deprecation",
    (
        _field("msg", "string"),
        _field("version", "string", "Version in which the deprecated feature is removed."),
        _field("date", "string", "Date after which the deprecated feature is removed."),
        _field("collection_name", "string"),
    ),
)

_EVENT = _catalog(
    _field("main_jobhostsummary_id", "integer", "Job host summary that selected this event."),
    _field("main_jobhostsummary_created", "datetime"),
    _field("id", "integer", "Job event identifier", nullable=False),
    _field("created", "datetime"),
    _field("modified", "datetime"),
    _field("job_created", "datetime", "Creation time of the event's job; the job event partition key."),
    _field("job_finished", "datetime"),
    _field("ansible_version", "string"),
    _field("uuid", "string"),
    _field("parent_uuid", "string"),
    _field("event", "string", "Ansible callback event name, for example runner_on_ok."),
    _field("task_action", "string", "Module or action name as written in the task."),
    _field("resolved_action", "string", "Fully-qualified module or action name resolved by Ansible."),
    _field("collection_name", "string", "Collection namespace.name parsed from resolved_action, when it is a FQCN."),
    _field("collection_version", "string", "Version of collection_name installed for the job, when known."),
    _field("resolved_role", "string"),
    _object(
        "playbook_on_stats",
        description="Event data of playbook_on_stats events (per-host result counts), without artifact_data; null for other events.",
    ),
    _field("duration", "string", "Task duration in seconds, as recorded in the event data."),
    _field("start", "datetime"),
    _field("end", "datetime"),
    _field("task_uuid", "string"),
    _field("ignore_errors", "boolean", "Whether the failed task had ignore_errors set; false when unknown."),
    _field("failed", "boolean"),
    _field("changed", "boolean"),
    _field("playbook", "string"),
    _field("play", "string"),
    _field("task", "string"),
    _field("role", "string"),
    _field("job_remote_id", "integer", "Controller job ID."),
    _field("job_id", "integer"),
    _field("host_remote_id", "integer", "Controller host ID."),
    _field("host_id", "integer"),
    _field("host_name", "string"),
    _array("warnings", _field("warning", "string"), "Warnings reported in the task result."),
    _array("deprecations", _DEPRECATION, "Deprecations reported in the task result."),
    _field("event_data_length", "integer", "Stored size of the raw event data, in bytes."),
    _field("job_failed", "boolean"),
    _field("job_started", "datetime"),
)

MAIN_JOBEVENT = PayloadSchema(
    name="MainJobeventPayload",
    kind="array",
    description="Runner events for job hosts whose summaries were modified in the window, returned by main_jobevent.",
    item_fields=_pick(
        _EVENT,
        "main_jobhostsummary_id",
        "main_jobhostsummary_created",
        "id",
        "created",
        "modified",
        "job_created",
        "event",
        "task_action",
        "resolved_action",
        "resolved_role",
        "duration",
        "failed",
        "changed",
        "playbook",
        "play",
        "task",
        "role",
        "job_remote_id",
        "host_remote_id",
        "host_name",
    ),
)

MAIN_JOBEVENT_SERVICE = PayloadSchema(
    name="MainJobeventServicePayload",
    kind="array",
    description="Selected runner and annotation events for jobs finished in the window, returned by main_jobevent_service.",
    item_fields=_pick(
        _EVENT,
        "id",
        "created",
        "modified",
        "job_created",
        "job_finished",
        "ansible_version",
        "uuid",
        "parent_uuid",
        "event",
        "task_action",
        "resolved_action",
        "collection_name",
        "collection_version",
        "resolved_role",
        "duration",
        "start",
        "end",
        "task_uuid",
        "ignore_errors",
        "failed",
        "changed",
        "playbook",
        "play",
        "task",
        "role",
        "job_remote_id",
        "job_id",
        "host_remote_id",
        "host_id",
        "host_name",
        "warnings",
        "deprecations",
        "event_data_length",
        "job_failed",
        "job_started",
    ),
)

EVENTS_TABLE = PayloadSchema(
    name="EventsTablePayload",
    kind="array",
    description="Raw job events modified in the window, returned by events_table.",
    item_fields=_pick(
        _EVENT,
        "id",
        "created",
        "modified",
        "job_created",
        "uuid",
        "parent_uuid",
        "event",
        "task_action",
        "resolved_action",
        "collection_name",
        "collection_version",
        "resolved_role",
        "playbook_on_stats",
        "failed",
        "changed",
        "playbook",
        "play",
        "task",
        "role",
        "job_id",
        "host_id",
        "host_name",
        "start",
        "end",
        "duration",
        "warnings",
        "deprecations",
    ),
)

_EDGE_FIELDS = (
    _array("success_nodes", _field("node_id", "integer"), "Nodes run when this node succeeds."),
    _array("failure_nodes", _field("node_id", "integer"), "Nodes run when this node fails."),
    _array("always_nodes", _field("node_id", "integer"), "Nodes run regardless of this node's result."),
)

WORKFLOW_JOB_NODE = PayloadSchema(
    name="WorkflowJobNodeTablePayload",
    kind="array",
    description="Workflow job node executions and their edge relationships.",
    item_fields=(
        _field("id", "integer", "Workflow job node identifier", nullable=False),
        _field("created", "datetime"),
        _field("modified", "datetime"),
        _field("job_id", "integer", "Unified job spawned by this node."),
        _field("unified_job_template_id", "integer"),
        _field("workflow_job_id", "integer"),
        _field("inventory_id", "integer"),
        *_EDGE_FIELDS,
        _field("do_not_run", "boolean"),
        _field("all_parents_must_converge", "boolean"),
    ),
)

QUERY_INFO = PayloadSchema(
    name="QueryInfoPayload",
    kind="object",
    description="Collection metadata returned by query_info.",
    fields=(
        _field("last_run", "string", 'String representation of the requested start bound ("None" when unbounded).'),
        _field("current_time", "string", 'String representation of the requested end bound ("None" when unbounded).'),
        _field("collection_type", "string"),
    ),
)

EXECUTION_ENVIRONMENTS = PayloadSchema(
    name="ExecutionEnvironmentsPayload",
    kind="array",
    description="Execution environment records.",
    item_fields=(
        _field("id", "integer", "Execution environment identifier", nullable=False),
        _field("created", "datetime"),
        _field("modified", "datetime"),
        _field("description", "string"),
        _field("image", "string"),
        _field("managed", "boolean"),
        _field("created_by_id", "integer"),
        _field("credential_id", "integer"),
        _field("modified_by_id", "integer"),
        _field("organization_id", "integer"),
        _field("name", "string"),
        _field("pull", "string", "Image pull policy."),
    ),
)

_PLATFORM_FIELDS = (
    _array("dist", _field("part", "string"), "Linux distribution name, version, and codename."),
    _field("release", "string"),
    _field("system", "string"),
    _field("type", "string", "Install type: openshift, k8s, or traditional."),
)

CONFIG = PayloadSchema(
    name="ConfigPayload",
    kind="object",
    description="Controller configuration, license, version, and platform metadata.",
    fields=(
        _array("authentication_backends", _field("backend", "string")),
        _field("controller_url_base", "string"),
        _field("external_logger_enabled", "boolean"),
        _field("external_logger_type", "string"),
        _field("install_uuid", "string"),
        _field("instance_uuid", "string"),
        _array("logging_aggregators", _field("logger", "string")),
        _field("pendo_tracking", "string"),
        _field("subscription_usage_model", "string"),
        _field("account_number", "string"),
        _field("automated_instances", "integer"),
        _field("automated_since", "integer", "Unix timestamp of the first automation."),
        _field("compliant", "boolean"),
        _field("current_instances", "integer"),
        _field("date_expired", "boolean", "Whether the subscription has expired."),
        _field("date_warning", "boolean", "Whether the subscription is close to expiring."),
        _field("free_instances", "integer"),
        _field("grace_period_remaining", "integer", "Seconds of grace period remaining."),
        _field("license_date", "integer", "Unix timestamp when the subscription ends."),
        _field("license_expiry", "integer", "Seconds until the subscription ends."),
        _field("license_type", "string"),
        _field("pool_id", "string"),
        _field("product_name", "string"),
        _field("satellite", "boolean"),
        _field("sku", "string"),
        _field("subscription_id", "string"),
        _field("subscription_name", "string"),
        _field("support_level", "string"),
        _field("total_licensed_instances", "integer"),
        _field("trial", "boolean"),
        _field("usage", "string", "Subscription usage, for example Production or Development."),
        _field("valid_key", "boolean"),
        _object(
            "billing_provider_params",
            description="Billing provider metadata supplied by the collection environment; keys depend on the provider.",
        ),
        _field("controller_version", "string"),
        _field("metrics_utility_version", "string"),
        _object("platform", _PLATFORM_FIELDS),
    ),
)

CONTROLLER_VERSION = PayloadSchema(
    name="ControllerVersionServicePayload",
    kind="array",
    description="Distinct versions reported by enabled Controller instances.",
    item_fields=(_field("controller_version", "string", nullable=False),),
)

TABLE_METADATA = PayloadSchema(
    name="TableMetadataPayload",
    kind="array",
    description="Estimated row counts and relation sizes for selected Controller tables.",
    item_fields=(
        _field("schemaname", "string"),
        _field("tablename", "string"),
        _field("estimated_row_count", "integer"),
        _field("total_size_bytes", "integer"),
        _field("table_size_bytes", "integer"),
        _field("indexes_size_bytes", "integer"),
    ),
)

FEATURE_FLAGS = PayloadSchema(
    name="FeatureFlagsServicePayload",
    kind="array",
    description="Enabled boolean platform feature flags.",
    item_fields=(
        _field("name", "string", nullable=False),
        _field("condition", "string"),
        _field("value", "string"),
        _field("description", "string"),
        _field("support_level", "string"),
        _field("toggle_type", "string"),
        _field("visibility", "boolean"),
    ),
)

COUNTS = PayloadSchema(
    name="CountsPayload",
    kind="object",
    description="Controller object, inventory, session, job, and database counts.",
    fields=(
        *(
            _field(name, "integer")
            for name in (
                "organization",
                "team",
                "user",
                "inventory",
                "credential",
                "project",
                "job_template",
                "workflow_job_template",
                "host",
                "schedule",
                "notification_template",
            )
        ),
        _object(
            "inventories",
            description="Inventory counts keyed by inventory kind; always includes normal and smart.",
            additional_properties=_map_value("InventoryKindCount", "integer"),
        ),
        _field("unified_job", "integer", "Unified jobs, excluding project syncs."),
        _field("active_host_count", "integer"),
        _field("active_sessions", "integer"),
        _field("active_user_sessions", "integer"),
        _field("active_anonymous_sessions", "integer"),
        _field("running_jobs", "integer"),
        _field("pending_jobs", "integer"),
        _field("database_connections", "integer"),
    ),
)

CRED_TYPE_COUNTS = PayloadSchema(
    name="CredTypeCountsPayload",
    kind="object",
    description="Credential counts keyed by credential type ID.",
    additional_properties=_map_value(
        "CredentialTypeCount",
        "object",
        (
            _field("name", "string"),
            _field("credential_count", "integer"),
            _field("managed", "boolean"),
        ),
    ),
)

HOST_METRIC_SUMMARY_MONTHLY = PayloadSchema(
    name="HostMetricSummaryMonthlyTablePayload",
    kind="array",
    description="Monthly host capacity and usage summaries.",
    item_fields=(
        _field("id", "integer", nullable=False),
        _field("date", "date", "First day of the summarized month."),
        _field("license_capacity", "integer"),
        _field("license_consumed", "integer"),
        _field("hosts_added", "integer"),
        _field("hosts_deleted", "integer"),
        _field("indirectly_managed_hosts", "integer"),
    ),
)

INSTANCE_INFO = PayloadSchema(
    name="InstanceInfoPayload",
    kind="object",
    description="Controller instance topology and consumed capacity keyed by UUID.",
    additional_properties=_map_value(
        "InstanceInfoRecord",
        "object",
        (
            _field("uuid", "string"),
            _field("version", "string"),
            _field("capacity", "integer"),
            _field("cpu", "string", 'CPU count, as a decimal string (for example "1.0").'),
            _field("memory", "integer", "Memory in bytes."),
            _field("managed_by_policy", "boolean"),
            _field("enabled", "boolean"),
            _field("consumed_capacity", "integer", "Capacity used by running and waiting jobs."),
            _field("remaining_capacity", "integer"),
            _field("node_type", "string"),
        ),
    ),
)

INVENTORY_COUNTS = PayloadSchema(
    name="InventoryCountsPayload",
    kind="object",
    description="Inventory details keyed by inventory ID.",
    additional_properties=_map_value(
        "InventoryCount",
        "object",
        (
            _field("name", "string"),
            _field("kind", "string", "Empty for normal inventories, smart for smart inventories."),
            _field("hosts", "integer"),
            _field("sources", "integer"),
            _array(
                "source_list",
                _object(
                    "source",
                    (
                        _field("name", "string"),
                        _field("source", "string"),
                        _field("num_hosts", "integer"),
                    ),
                ),
            ),
        ),
    ),
)

ORG_COUNTS = PayloadSchema(
    name="OrgCountsPayload",
    kind="object",
    description="Organization user and team counts keyed by organization ID.",
    additional_properties=_map_value(
        "OrganizationCount",
        "object",
        (_field("name", "string"), _field("users", "integer"), _field("teams", "integer")),
    ),
)

PROJECTS_BY_SCM_TYPE = PayloadSchema(
    name="ProjectsByScmTypePayload",
    kind="object",
    description="Project counts keyed by source-control type, with manual for projects without one.",
    additional_properties=_map_value("ProjectScmTypeCount", "integer"),
)

UNIFIED_JOB_TEMPLATE = PayloadSchema(
    name="UnifiedJobTemplateTablePayload",
    kind="array",
    description="Unified job template identity, scheduling, and status records.",
    item_fields=(
        _field("id", "integer", nullable=False),
        _field("polymorphic_ctype_id", "integer"),
        _field("model", "string", "Concrete template type, for example job_template or project."),
        _field("execution_environment_image", "string"),
        _field("created", "datetime"),
        _field("modified", "datetime"),
        _field("created_by_id", "integer"),
        _field("modified_by_id", "integer"),
        _field("name", "string"),
        _field("current_job_id", "integer"),
        _field("last_job_id", "integer"),
        _field("last_job_failed", "boolean"),
        _field("last_job_run", "datetime"),
        _field("next_job_run", "datetime"),
        _field("next_schedule_id", "integer"),
        _field("status", "string"),
    ),
)

WORKFLOW_JOB_TEMPLATE_NODE = PayloadSchema(
    name="WorkflowJobTemplateNodeTablePayload",
    kind="array",
    description="Workflow job template node definitions and edge relationships.",
    item_fields=(
        _field("id", "integer", "Workflow job template node identifier", nullable=False),
        _field("created", "datetime"),
        _field("modified", "datetime"),
        _field("unified_job_template_id", "integer"),
        _field("workflow_job_template_id", "integer"),
        _field("inventory_id", "integer"),
        *_EDGE_FIELDS,
        _field("all_parents_must_converge", "boolean"),
    ),
)

MAIN_HOST = PayloadSchema(
    name="MainHostPayload",
    kind="array",
    description="Enabled inventory hosts and selected host facts.",
    item_fields=(
        _field("host_name", "string", nullable=False),
        _field("host_id", "integer"),
        _field("inventory_remote_id", "integer", "Controller inventory ID."),
        _field("inventory_name", "string"),
        _field("organization_remote_id", "integer", "Controller organization ID."),
        _field("organization_name", "string"),
        _field("last_automation", "datetime", "Creation time of the host's most recent job."),
        _field("ansible_host_variable", "string", "ansible_host value from the host variables."),
        _object(
            "canonical_facts",
            (
                _field("ansible_product_serial", "string"),
                _field("ansible_machine_id", "string"),
                _field("ansible_host", "string"),
                _field("host_name", "string"),
                _field("ansible_port", "integer"),
            ),
            "Host identity facts used for deduplication.",
        ),
        _object(
            "facts",
            (
                _field("ansible_connection_variable", "string"),
                _field("ansible_virtualization_type", "string"),
                _field("ansible_virtualization_role", "string"),
                _field("ansible_system_vendor", "string"),
                _field("ansible_product_name", "string"),
                _field("ansible_architecture", "string"),
                _field("ansible_processor", "string", "Processor facts as JSON text."),
                _field("ansible_form_factor", "string"),
                _field("ansible_bios_vendor", "string"),
                _field("ansible_bios_version", "string"),
                _field("ansible_board_serial", "string"),
            ),
            "Selected hardware and system facts.",
        ),
    ),
)

MAIN_HOSTMETRIC = PayloadSchema(
    name="MainHostmetricPayload",
    kind="array",
    description="Host automation, deletion, and identity metrics.",
    item_fields=(
        _field("hostname", "string", nullable=False),
        _field("host_id", "integer", "Matching inventory host ID, or 0 when the host no longer exists."),
        _field("first_automation", "datetime"),
        _field("last_automation", "datetime"),
        _field("automated_counter", "integer"),
        _field("deleted_counter", "integer"),
        _field("last_deleted", "datetime"),
        _field("deleted", "boolean"),
        _field("id", "integer", "Host metric identifier"),
        _field("used_in_inventories", "integer"),
        _field("ansible_product_serial", "string"),
        _field("ansible_machine_id", "string"),
        _field("ansible_host_variable", "string"),
        _field("ansible_connection_variable", "string"),
    ),
)

INDIRECT_MANAGED_NODES = PayloadSchema(
    name="IndirectManagedNodeAuditPayload",
    kind="array",
    description="Indirect managed node audit records for jobs finished in the window.",
    item_fields=(
        _field("id", "integer", "Audit record identifier", nullable=False),
        _field("created", "datetime"),
        _field("host_name", "string", "Ansible name of the indirectly managed node."),
        _field("host_remote_id", "integer", "Controller host ID, when the node matches an inventory host."),
        _object("canonical_facts", description="Facts used for managed node deduplication; keys depend on the query."),
        _object("facts", description="Additional non-canonical facts about the managed node."),
        _array("events", _field("module", "string"), "Fully-qualified names of modules that ran against the node."),
        _field("task_runs", "integer", "How many times registered modules were invoked on the node."),
        _field("job_created", "datetime"),
        _field("job_remote_id", "integer", "Controller job ID."),
        _field("job_template_remote_id", "integer", "Controller unified job template ID."),
        _field("job_template_name", "string"),
        _field("inventory_remote_id", "integer", "Controller inventory ID."),
        _field("inventory_name", "string"),
        _field("organization_remote_id", "integer", "Controller organization ID."),
        _field("organization_name", "string"),
        _field("project_remote_id", "integer", "Controller project ID."),
        _field("project_name", "string"),
    ),
)


# Keyed by metrics-utility collector function name (the second half of the public name).
PAYLOAD_SCHEMAS = {
    "unified_jobs_dashboard": UNIFIED_JOBS_DASHBOARD,
    "unified_jobs": UNIFIED_JOBS,
    "job_host_summary_service": JOB_HOST_SUMMARY_SERVICE,
    "job_host_summary": JOB_HOST_SUMMARY,
    "credentials_service": CREDENTIALS,
    "main_jobevent_service": MAIN_JOBEVENT_SERVICE,
    "main_jobevent": MAIN_JOBEVENT,
    "events_table": EVENTS_TABLE,
    "workflow_job_node_table": WORKFLOW_JOB_NODE,
    "query_info": QUERY_INFO,
    "execution_environments": EXECUTION_ENVIRONMENTS,
    "config": CONFIG,
    "controller_version_service": CONTROLLER_VERSION,
    "table_metadata": TABLE_METADATA,
    "feature_flags_service": FEATURE_FLAGS,
    "counts": COUNTS,
    "cred_type_counts": CRED_TYPE_COUNTS,
    "host_metric_summary_monthly_table": HOST_METRIC_SUMMARY_MONTHLY,
    "instance_info": INSTANCE_INFO,
    "inventory_counts": INVENTORY_COUNTS,
    "org_counts": ORG_COUNTS,
    "projects_by_scm_type": PROJECTS_BY_SCM_TYPE,
    "unified_job_template_table": UNIFIED_JOB_TEMPLATE,
    "workflow_job_template_node_table": WORKFLOW_JOB_TEMPLATE_NODE,
    "main_host": MAIN_HOST,
    "main_host_daily": MAIN_HOST,
    "main_hostmetric": MAIN_HOSTMETRIC,
    "main_indirectmanagednodeaudit": INDIRECT_MANAGED_NODES,
}
