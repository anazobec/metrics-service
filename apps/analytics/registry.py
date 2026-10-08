"""Analytics collector registry — the whitelist of metrics-utility collectors.

Single source of truth for which collectors the analytics API exposes and which we may
safely call from the metrics service. Drives:

* **persistence** — only ``enabled`` collectors get their raw ``gather()`` output written
  to :class:`~apps.analytics.models.AnalyticsPayload` (see ``apps.analytics.persist``);
* **the read API** — ``/api/v1/analytics/<name>/`` only serves ``enabled`` collectors;
* **on-demand collection** — resolves the underlying collector + its default window.

Naming
------
The **public name** (API path + stored ``collector`` value) is ``group.function`` —  the
metrics-utility service group plus the real m-u collector function name, e.g.
``controller.unified_jobs_dashboard``. This is self-documenting and groups by service (room for
EDA/Hub later). Internally each entry also carries:

* ``collector_type`` — the metrics-service key that ``generic_collect_metrics`` and the
  ``_get_{hourly,snapshot,daily}_collectors()`` registries use (e.g. ``unified_jobs``);
* ``group`` / ``mu_function`` — the pieces the public name is built from.

Tiers
-----
* **ENABLED** — registered in the service, standalone-safe (read-only AWX DB, SQL only),
  customer-facing. Persisted + exposed.
* **NOT ENABLED** — known collectors that are not exposed by the analytics API. Some are used by
  other service pipelines; others are not wired for analytics collection.

Every not-enabled entry must have a ``note`` explaining its status.

FOLLOWUPS (noted back in the anstrat-1587 plan):
* CI drift test comparing this registry vs the collectors discovered in the service — foundation 03.
* Registry-driven per-collector viewset/serializer/OpenAPI generation — API 05/07.
* SDP fields ``requires_functions`` / ``service_group`` / ``is_rollup`` — add when needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The host collectors below require metrics-utility's PostgreSQL helper functions. Keep this
# marker as the single backport switch if a target release cannot provide those functions.
SERVICE_FUNCTIONS_AVAILABLE = True

# Modes that accept a since/until collection window (snapshots are current-state only).
_WINDOWED_MODES = ("hourly", "daily")


@dataclass(frozen=True)
class CollectorEntry:
    """One collector in the analytics whitelist.

    Attributes:
        name: full public/API/storage name (``group.function``).
        collector_type: metrics-service key (used by generic_collect_metrics and the
            ``_get_*_collectors()`` registries).
        mode: ``"hourly"`` / ``"snapshot"`` / ``"daily"`` (scheduling + window model); ``""`` for
            entries without an analytics collection mode.
        enabled: whether the collector is persisted and exposed by the API.
        database: which Django DB connection the collector reads (defaults to ``awx``).
        note: why a collector is disabled/excluded, or any relevant caveat.
        description: customer-facing summary of the data returned by the collector.
    """

    name: str
    collector_type: str = field(kw_only=True)
    mode: str = field(kw_only=True)
    enabled: bool = field(kw_only=True)
    database: str = field(default="awx", kw_only=True)
    note: str = field(default="", kw_only=True)
    description: str = field(default="", kw_only=True)
    group: str = field(init=False)
    mu_function: str = field(init=False)

    def __post_init__(self) -> None:
        """Split the public name into its metrics-utility group and function."""
        group, separator, mu_function = self.name.partition(".")
        if not separator or not group or not mu_function:
            raise ValueError(f"Collector name must be group.function: {self.name}")
        object.__setattr__(self, "group", group)
        object.__setattr__(self, "mu_function", mu_function)

    @property
    def accepts_since_until(self) -> bool:
        """Whether this collector accepts since/until bounds (False for snapshots)."""
        return self.mode in _WINDOWED_MODES


# ---------------------------------------------------------------------------
# ENABLED — persisted + exposed. Registered in the service and standalone-safe.
# ---------------------------------------------------------------------------
_ENABLED: list[CollectorEntry] = [
    # Hourly (collect_hourly_metrics registry)
    CollectorEntry(
        "controller.unified_jobs_dashboard",
        collector_type="unified_jobs",
        mode="hourly",
        enabled=True,
        description="Job executions, including status and timing, organization, inventory, project, template, execution environment, launcher, labels, and host count.",
    ),
    CollectorEntry(
        "controller.unified_jobs",
        collector_type="unified_jobs_base",
        mode="hourly",
        enabled=True,
        description="Unified job records with status, timing, organization, inventory, execution environment, template, project SCM type, and job details.",
    ),
    CollectorEntry(
        "controller.job_host_summary_service",
        collector_type="job_host_summary_service",
        mode="hourly",
        enabled=True,
        description="Per-job host results and counts, with host, job, template, inventory, organization, and project context.",
    ),
    CollectorEntry(
        "controller.job_host_summary",
        collector_type="job_host_summary",
        mode="hourly",
        enabled=SERVICE_FUNCTIONS_AVAILABLE,
        note="requires metrics-utility PostgreSQL YAML/JSON helper functions",
        description="Per-job host results, counts, host connection variables, and job, template, inventory, organization, and project context.",
    ),
    CollectorEntry(
        "controller.credentials_service",
        collector_type="credentials_service",
        mode="hourly",
        enabled=True,
        description="Distinct managed credential types used by jobs completed in the collection window.",
    ),
    CollectorEntry(
        "controller.main_jobevent_service",
        collector_type="main_jobevent_service",
        mode="hourly",
        enabled=True,
        description="Selected job events for jobs completed in the window, including event actions, task/play/role, host, result flags, warnings, and deprecations.",
    ),
    CollectorEntry(
        "controller.main_jobevent",
        collector_type="main_jobevent_legacy",
        mode="hourly",
        enabled=True,
        description="Runner event records associated with job host summaries, including event action, task/play/role, host, and result flags.",
    ),
    CollectorEntry(
        "controller.events_table",
        collector_type="events_table",
        mode="hourly",
        enabled=True,
        description="Raw job events modified in the window, including event details, playbook statistics, task/play/role, host, timing, warnings, and deprecations.",
    ),
    CollectorEntry(
        "controller.workflow_job_node_table",
        collector_type="workflow_job_node_table",
        mode="hourly",
        enabled=True,
        description="Workflow job node executions, their job/template/workflow/inventory references, and success, failure, and always edges.",
    ),
    CollectorEntry(
        "controller.query_info",
        collector_type="query_info",
        mode="hourly",
        enabled=True,
        description="Collection metadata: requested bounds and collection type; it does not query the Controller database.",
    ),
    # Snapshot (collect_snapshot_metrics registry; current-state, no since/until window)
    CollectorEntry(
        "controller.execution_environments",
        collector_type="execution_environments",
        mode="snapshot",
        enabled=True,
        description="Execution environment records, including image, description, ownership, organization, credential, management, and pull settings.",
    ),
    CollectorEntry(
        "controller.config",
        collector_type="config",
        mode="snapshot",
        enabled=True,
        note="already keeps raw (no rollup)",
        description="Selected Controller settings and license details, plus Controller and metrics-utility versions and runtime platform metadata.",
    ),
    CollectorEntry(
        "controller.controller_version_service",
        collector_type="controller_version_service",
        mode="snapshot",
        enabled=True,
        description="Distinct versions reported by enabled control and hybrid Controller instances.",
    ),
    CollectorEntry(
        "controller.table_metadata",
        collector_type="table_metadata",
        mode="snapshot",
        enabled=True,
        description="Estimated row counts and table, index, and total sizes for the job event, unified job, and job host summary tables.",
    ),
    CollectorEntry(
        "controller.feature_flags_service",
        collector_type="feature_flags_service",
        mode="snapshot",
        enabled=True,
        description="Enabled boolean platform feature flags and their descriptions, support levels, toggle types, and visibility.",
    ),
    CollectorEntry(
        "controller.counts",
        collector_type="counts",
        mode="snapshot",
        enabled=True,
        description="Counts of Controller objects, inventories by kind, non-sync jobs, active hosts and sessions, running and pending jobs, and database connections.",
    ),
    CollectorEntry(
        "controller.cred_type_counts",
        collector_type="cred_type_counts",
        mode="snapshot",
        enabled=True,
        description="Credential counts grouped by credential type, including type name and whether the type is managed.",
    ),
    CollectorEntry(
        "controller.host_metric_summary_monthly_table",
        collector_type="host_metric_summary_monthly_table",
        mode="snapshot",
        enabled=True,
        description="Monthly host capacity and usage summaries, including hosts added, deleted, and indirectly managed.",
    ),
    CollectorEntry(
        "controller.instance_info",
        collector_type="instance_info",
        mode="snapshot",
        enabled=True,
        description="Controller instance topology, version, capacity, CPU, memory, node type, enablement, and current and remaining capacity.",
    ),
    CollectorEntry(
        "controller.inventory_counts",
        collector_type="inventory_counts",
        mode="snapshot",
        enabled=True,
        description="Inventory names and kinds with host and source counts, source details, and smart inventory entries.",
    ),
    CollectorEntry(
        "controller.org_counts",
        collector_type="org_counts",
        mode="snapshot",
        enabled=True,
        description="Organization names with distinct user and team counts.",
    ),
    CollectorEntry(
        "controller.projects_by_scm_type",
        collector_type="projects_by_scm_type",
        mode="snapshot",
        enabled=True,
        description="Project counts grouped by source-control type, with empty types reported as manual.",
    ),
    CollectorEntry(
        "controller.unified_job_template_table",
        collector_type="unified_job_template_table",
        mode="snapshot",
        enabled=True,
        description="Unified job template identity, type, execution environment, ownership, last/current/next job references, scheduling, and status.",
    ),
    CollectorEntry(
        "controller.workflow_job_template_node_table",
        collector_type="workflow_job_template_node_table",
        mode="snapshot",
        enabled=True,
        description="Workflow job template node definitions, inventory and template references, convergence settings, and success, failure, and always edges.",
    ),
    CollectorEntry(
        "controller.main_host",
        collector_type="main_host",
        mode="snapshot",
        enabled=SERVICE_FUNCTIONS_AVAILABLE,
        description="Enabled inventory hosts with inventory and organization context, last automation time, connection variables, and selected hardware and system facts.",
    ),
    # Daily (collect_daily_metrics registry)
    CollectorEntry(
        "controller.main_host_daily",
        collector_type="main_host_daily",
        mode="daily",
        enabled=SERVICE_FUNCTIONS_AVAILABLE,
        description="Enabled hosts created or modified in the window, with the same inventory, organization, automation, connection, and selected fact data as main_host.",
    ),
    CollectorEntry(
        "controller.main_hostmetric",
        collector_type="main_hostmetric",
        mode="daily",
        enabled=SERVICE_FUNCTIONS_AVAILABLE,
        description="Host automation and deletion metrics, counters, inventory usage, and selected host identity and connection facts.",
    ),
    CollectorEntry(
        "controller.main_indirectmanagednodeaudit",
        collector_type="indirect_managed_nodes",
        mode="daily",
        enabled=True,
        description="Audit records for indirect managed nodes, including their organization and collection data.",
    ),
]

# ---------------------------------------------------------------------------
# NOT ENABLED — known collectors not exposed by the analytics API.
# ---------------------------------------------------------------------------
_NOT_ENABLED: list[CollectorEntry] = [
    CollectorEntry(
        "service.task_executions_service",
        collector_type="task_executions_service",
        mode="daily",
        enabled=False,
        database="default",
        note="used by the anonymized collection pipeline only; not exposed by the analytics API.",
    ),
    CollectorEntry(
        "controller.config_django",
        collector_type="config_django",
        mode="",
        enabled=False,
        note="imports awx.conf.license / awx.main.utils at runtime; superseded by config (SQL variant).",
    ),
    CollectorEntry(
        "others.total_workers_vcpu",
        collector_type="total_workers_vcpu",
        mode="",
        enabled=False,
        note="Prometheus/CLI billing path only; not registered in the service.",
    ),
    CollectorEntry(
        "dashboard.dashboard_jobs",
        collector_type="dashboard_jobs",
        mode="",
        enabled=False,
        note="used by the separate dashboard synchronization pipeline, not analytics collection.",
    ),
]

_ALL: tuple[CollectorEntry, ...] = (*_ENABLED, *_NOT_ENABLED)

# Public lookup by public name (group.function).
COLLECTORS: dict[str, CollectorEntry] = {e.name: e for e in _ALL}

# Reverse lookup by metrics-service collector_type — used by the persist hook, which only knows
# the internal key. Not-enabled entries are never persisted so collisions among them don't matter.
_BY_TYPE: dict[str, CollectorEntry] = {e.collector_type: e for e in _ALL}


def enabled_collectors() -> dict[str, CollectorEntry]:
    """Return the collectors that are persisted and exposed by the API, keyed by public name."""
    return {name: e for name, e in COLLECTORS.items() if e.enabled}


def get_entry(name: str) -> CollectorEntry | None:
    """Return the registry entry for a public collector name, or None if unknown."""
    return COLLECTORS.get(name)


def get_entry_by_type(collector_type: str) -> CollectorEntry | None:
    """Return the registry entry for a metrics-service collector_type, or None if unknown."""
    return _BY_TYPE.get(collector_type)
