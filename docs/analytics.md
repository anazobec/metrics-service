# Analytics Collection

Analytics collection stores the raw output of enabled metrics-utility collectors
in `AnalyticsPayload`. The stored row is a collection envelope: collector name,
source, collection bounds, collection timestamps, and the raw JSON payload.

The read and trigger APIs are available under `/api/v1/analytics/` and are
restricted to users with the System Administrator or Platform Auditor role.

## Collector Registry

The registry is the source of truth in
[`apps/analytics/registry.py`](../apps/analytics/registry.py). Each entry has:

- `name`: public/API/storage name, in `group.function` form
- `collector_type`: internal task registry key
- `mode`: `hourly`, `daily`, or `snapshot`
- `enabled`: whether the collector is persisted and exposed
- `database`: source database, defaulting to `awx`
- `description`: customer-facing summary of the data returned by the collector
- `note`: explanation for disabled or excluded entries

`accepts_since_until` is derived from `mode`. It describes whether the collector
can receive `since` and `until` bounds; it does not require both bounds to be
present.

## Enabled Collectors

All enabled collectors are persisted and exposed by the read API. Except for
`controller.query_info`, which reports collection metadata, these collectors
read from the Controller (`awx`) database.

| Name | `collector_type` | Mode | `accepts_since_until` | Controller data extracted |
| --- | --- | --- | --- | --- |
| `controller.unified_jobs_dashboard` | `unified_jobs` | hourly | yes | Job executions, including status and timing, organization, inventory, project, template, execution environment, launcher, labels, and host count. |
| `controller.job_host_summary_service` | `job_host_summary_service` | hourly | yes | Per-job host results and counts, with host, job, template, inventory, organization, and project context. |
| `controller.credentials_service` | `credentials_service` | hourly | yes | Distinct managed credential types used by jobs completed in the collection window. |
| `controller.main_jobevent_service` | `main_jobevent_service` | hourly | yes | Selected job events for jobs completed in the window, including event actions, task/play/role, host, result flags, warnings, and deprecations. |
| `controller.events_table` | `events_table` | hourly | yes | Raw job events modified in the window, including event details, playbook statistics, task/play/role, host, timing, warnings, and deprecations. |
| `controller.workflow_job_node_table` | `workflow_job_node_table` | hourly | yes | Workflow job node executions, their job/template/workflow/inventory references, and success, failure, and always edges. |
| `controller.query_info` | `query_info` | hourly | yes | Collection metadata: requested bounds and collection type; it does not query the Controller database. |
| `controller.execution_environments` | `execution_environments` | snapshot | no | Execution environment records, including image, description, ownership, organization, credential, management, and pull settings. |
| `controller.config` | `config` | snapshot | no | Selected Controller settings and license details, plus Controller and metrics-utility versions and runtime platform metadata. |
| `controller.controller_version_service` | `controller_version_service` | snapshot | no | Distinct versions reported by enabled control and hybrid Controller instances. |
| `controller.table_metadata` | `table_metadata` | snapshot | no | Estimated row counts and table, index, and total sizes for the job event, unified job, and job host summary tables. |
| `controller.feature_flags_service` | `feature_flags_service` | snapshot | no | Enabled boolean platform feature flags and their descriptions, support levels, toggle types, and visibility. |
| `controller.counts` | `counts` | snapshot | no | Counts of Controller objects, inventories by kind, non-sync jobs, active hosts and sessions, running and pending jobs, and database connections. |
| `controller.cred_type_counts` | `cred_type_counts` | snapshot | no | Credential counts grouped by credential type, including type name and whether the type is managed. |
| `controller.host_metric_summary_monthly_table` | `host_metric_summary_monthly_table` | snapshot | no | Monthly host capacity and usage summaries, including hosts added, deleted, and indirectly managed. |
| `controller.instance_info` | `instance_info` | snapshot | no | Controller instance topology, version, capacity, CPU, memory, node type, enablement, and current and remaining capacity. |
| `controller.inventory_counts` | `inventory_counts` | snapshot | no | Inventory names and kinds with host and source counts, source details, and smart inventory entries. |
| `controller.org_counts` | `org_counts` | snapshot | no | Organization names with distinct user and team counts. |
| `controller.projects_by_scm_type` | `projects_by_scm_type` | snapshot | no | Project counts grouped by source-control type, with empty types reported as manual. |
| `controller.unified_job_template_table` | `unified_job_template_table` | snapshot | no | Unified job template identity, type, execution environment, ownership, last/current/next job references, scheduling, and status. |
| `controller.workflow_job_template_node_table` | `workflow_job_template_node_table` | snapshot | no | Workflow job template node definitions, inventory and template references, convergence settings, and success, failure, and always edges. |
| `controller.main_host` | `main_host` | snapshot | no | Enabled inventory hosts with inventory and organization context, last automation time, connection variables, and selected hardware and system facts. |
| `controller.main_host_daily` | `main_host_daily` | daily | yes | Enabled hosts created or modified in the window, with the same inventory, organization, automation, connection, and selected fact data as `main_host`. |
| `controller.main_hostmetric` | `main_hostmetric` | daily | yes | Host automation and deletion metrics, counters, inventory usage, and selected host identity and connection facts. |

## Disabled And Excluded Collectors

These entries remain in the registry so their status and reason are visible to
developers. They are not persisted or exposed by the analytics API.

| Name | `collector_type` | Status | Reason |
| --- | --- | --- | --- |
| `service.task_executions_service` | `task_executions_service` | disabled | Reads metrics-service operational task data, not customer data; product decision needed. |
| `controller.main_indirectmanagednodeaudit` | `indirect_managed_nodes` | disabled | Requires the ANSTRAT-2160 path to reach GA. |
| `controller.job_host_summary` | `job_host_summary` | excluded | Legacy collector superseded by `job_host_summary_service`. |
| `controller.main_jobevent` | `main_jobevent_legacy` | excluded | Legacy collector superseded by `main_jobevent_service`. |
| `controller.unified_jobs` | `unified_jobs_base` | excluded | Base collector superseded by `unified_jobs_dashboard`. |
| `controller.config_django` | `config_django` | excluded | AWX in-process collector superseded by the SQL `config` collector. |
| `others.total_workers_vcpu` | `total_workers_vcpu` | excluded | Prometheus/CLI billing path, not registered in metrics-service. |
| `dashboard.dashboard_jobs` | `dashboard_jobs` | excluded | Belongs to the separate dashboard synchronization pipeline. |

## Read API

The analytics root lists enabled collectors and links to their row and collection-task endpoints:

```http
GET /api/v1/analytics/
```

The response contains entries such as:

```json
{
  "collectors": [
    {
      "name": "controller.config",
      "description": "Selected Controller settings and license details, plus Controller and metrics-utility versions and runtime platform metadata.",
      "mode": "snapshot",
      "accepts_since_until": false,
      "rows_url": "https://metrics.example.com/api/v1/analytics/controller.config/",
      "collect_url": "https://metrics.example.com/api/v1/analytics/controller.config/collect/"
    }
  ]
}
```

The collector endpoint returns paginated collection envelopes:

```http
GET /api/v1/analytics/controller.unified_jobs_dashboard/
```

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 42,
      "collector": "controller.unified_jobs_dashboard",
      "source": "local",
      "since": "2026-08-17T10:00:00Z",
      "until": "2026-08-17T11:00:00Z",
      "started_at": "2026-08-17T11:01:02Z",
      "finished_at": "2026-08-17T11:01:08Z",
      "payload": [
        {"id": 100, "status": "successful"},
        {"id": 101, "status": "failed"}
      ]
    }
  ]
}
```

The actual list response includes `count`, `next`, `previous`, and `results`.
Use `page` and `page_size` for pagination. `count_disabled=true` can omit the
count and navigation links when supported by the paginator.

### Window Filters

Filtering uses half-open interval overlap. A stored row matches a query when:

```text
stored_until IS NULL OR stored_until > query_since
stored_since IS NULL OR stored_since < query_until
```

The comparisons are applied only when the corresponding query bound is present.
Therefore:

- No query bounds returns every collection for the collector, newest first.
- `?since=2026-08-17T11:00:00Z` means `[11:00, +infinity)`.
- `?until=2026-08-17T11:00:00Z` means `(-infinity, 11:00)`.
- Both bounds filter the requested `[since, until)` interval.
- Invalid timestamps and `until <= since` return `400`.

Examples:

| Stored row | Query | Result |
| --- | --- | --- |
| `[10:00, 11:00)` | `[10:00, 11:00)` | match |
| `[10:00, 11:00)` | `[11:00, 12:00)` | no match |
| `[09:00, 10:00)` | `[10:00, 11:00)` | no match |
| `[10:00, infinity)` | `(-infinity, 11:00)` | match |
| `(-infinity, 11:00)` | `[11:00, 12:00)` | no match |
| `[null, null]` | any valid query | match |

Every successful collection is retained. Repeated windows and repeated snapshots
are not upserts.

### Triggering Collection

The collector trigger creates a normal pending `Task`. It does not execute the
collector in the web request, create a payload row, claim a window, or coordinate
with another request. The scheduler discovers and dispatches the task through the
normal task machinery.

```http
POST /api/v1/analytics/controller.config/collect/
```

For a snapshot, an empty JSON body uses the collector defaults:

```json
{}
```

The response is `202 Accepted` and includes `task_id`, `task_url`, `collector`,
and the resolved `task_data`. Each request creates a separate task, even when
the collector and bounds are identical.

Window-capable collectors accept no bound, one bound, or both bounds. A missing
key receives the normal hourly or daily default; an explicit JSON `null` leaves
that bound open. Thus `{}` fills both defaults, `{"since": null}` fills only
the default `until`, and `{"since": null, "until": null}` is unbounded.
Invalid timestamps and reversed complete ranges return `400`.
Collection tasks always use the
storage default source, `local`; source selection is not part of the task or POST
contract.

## Creating Collection Tasks

Tasks are created through `POST /api/v1/tasks/`. The task API accepts
`function_name`, JSON `task_data`, optional `scheduled_time`, and optional
`cron_expression`. Immediate tasks omit both scheduling fields. The scheduler
submits immediate tasks during its periodic sync; recurring task rows create a
new child task on each cron fire.

### One Snapshot With Defaults

For a snapshot, only the public collector name is needed:

```http
POST /api/v1/tasks/
```

```json
{
  "name": "analytics-config-once",
  "function_name": "collect_analytics_on_demand",
  "task_data": {
    "collector": "controller.config"
  },
  "scheduled_time": null,
  "cron_expression": null
}
```

The task uses the collector's default behavior. A snapshot receives no
`since`/`until` bounds.

### Recurring Cron Tasks

Use the dedicated recurring-task endpoint for recurring collection:

```http
POST /api/v1/tasks/schedule_recurring/
```

Recurring tasks must use the mode-specific collection function. The scheduler
uses the function name to inject the rolling default window at dispatch time:

| Mode | `function_name` | `task_data` | Default collection |
| --- | --- | --- | --- |
| Hourly | `collect_hourly_metrics` | `collector_type` | Previous full hour |
| Daily | `collect_daily_metrics` | `collector_type` | Previous calendar day |
| Snapshot | `collect_snapshot_metrics` | `collector_type` | Current state, no window |

For example, a daily recurring task is:

```json
{
  "name": "analytics-main-host-daily",
  "function_name": "collect_daily_metrics",
  "task_data": {
    "collector_type": "main_host_daily"
  },
  "cron_expression": "0 2 * * *"
}
```

Do not use `collect_analytics_on_demand` for a recurring hourly or daily task
when the rolling default window is wanted. Without explicit bounds, that
function passes an unbounded window to hourly/daily collectors; the scheduler's
default-window injection applies to the three mode-specific functions above.

The response contains a success message and `task_id`. The recurring row is a
template. Each cron fire creates a non-recurring child task, which is then
dispatched normally. One-off collections, including custom hourly or daily
windows, use `collect_analytics_on_demand` through the analytics trigger
endpoint described above.

### Custom Window Task

For a specific collection window, pass ISO timestamps in `task_data`:

```json
{
  "name": "analytics-unified-jobs-hour",
  "function_name": "collect_analytics_on_demand",
  "task_data": {
    "collector": "controller.unified_jobs_dashboard",
    "since": "2026-08-17T10:00:00Z",
    "until": "2026-08-17T11:00:00Z"
  },
  "scheduled_time": null,
  "cron_expression": null
}
```

The trigger POST maps directly to the same task data: a snapshot POST with no
body creates the name-only task, while a request body containing `since` and
`until` creates the custom-window task. Claims, overlap coordination, and
re-collection decisions are intentionally not part of this contract.
