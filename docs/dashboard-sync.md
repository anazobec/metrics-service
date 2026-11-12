# Automation Dashboard Sync

The dashboard sync pipeline populates `apps/dashboard_reports` with AWX job
data for the automation-reports REST API (`/api/v1/dashboard_reports/`). It is
**separate from the anonymization pipeline** — both may query similar AWX data,
but dashboard data is stored in `JobData` / `JobHostSummary` for local API
consumers, not sent to Segment.

See [collectors.md](collectors.md) for the metrics rollup path that runs in
parallel on the same hourly collectors.

## Overview

```mermaid
flowchart TB
    subgraph phase1 [Phase 1 - First Start Backfill]
        Init["init-system-tasks"]
        OneShot["initial_dashboard_collection\none-shot Task"]
        Backfill["collect_dashboard_reports_initial_data"]
        JobData["JobData / JobHostSummary / labels"]
        Init --> OneShot --> Backfill --> JobData
    end

    subgraph phase2 [Phase 2 - Ongoing Hourly Hooks]
        UJ["hourly_unified_jobs XX:05\nMETRICS_COLLECTION"]
        Hook1["post_collect_hook\n_build_dashboard_sync_hook"]
        SyncJobs["sync_dashboard_job_records tasks"]
        JHS["hourly_job_host_summary XX:10"]
        Hook2["post_collect_hook\n_build_dashboard_host_summary_sync_hook"]
        SyncHS["sync_dashboard_host_summaries tasks"]
        UJ --> Hook1 --> SyncJobs --> JobData
        JHS --> Hook2 --> SyncHS --> JobHostSummary["JobHostSummary"]
    end

    API["/api/v1/dashboard_reports/"]
    JobData --> API
    JobHostSummary --> API
```

| Phase | Mechanism | When |
|-------|-----------|------|
| Backfill | One-shot system task | First enable / fresh install |
| Ongoing | Post-collect hooks on hourly metrics collectors | Every hour after backfill |

There is **no scheduled cron** for ongoing incremental dashboard collection.
`collect_dashboard_reports_data` is **deprecated** (manual/API invocation only).

## Feature Enablement Setting

Controlled by the `DASHBOARD_COLLECTION` feature enablement setting (default:
`true`). This is not a platform feature flag (DAB `AAPFlag` / Controller
`feature_flags_service` data).

- Task group: `DASHBOARD_COLLECTION_GROUP` in `task_groups.py`
- Disable: `METRICS_SERVICE_FEATURE__DASHBOARD_COLLECTION=false` or
  dynamic settings API / DB row
- Runtime check: scheduler and hooks use `get_feature_enabled_from_db()`

**Enablement interaction:**

| Mode | `METRICS_COLLECTION` | `DASHBOARD_COLLECTION` | Effect |
|------|----------------------|------------------------|--------|
| Rollups only | on | off | Hourly collectors run; no dashboard hooks |
| Backfill / manual sync | off | on | `initial_dashboard_collection` and API-triggered sync tasks can run |
| Ongoing incremental sync | on | on | Hourly collectors fire dashboard hooks (required path) |

Ongoing incremental sync is **not** independent of metrics collection: hooks are
registered in `collect_hourly_metrics.py` and only run when hourly collectors
execute. Pausing `METRICS_COLLECTION` stops ongoing dashboard updates even if
`DASHBOARD_COLLECTION` remains enabled. First-start backfill and manual sync
tasks can still run with only `DASHBOARD_COLLECTION` enabled.

## Phase 1: First-Start Backfill

```mermaid
flowchart TD
    Init["init-system-tasks\ncreate_system_tasks()"]
    Task["Task: initial_dashboard_collection\ncron=NULL, pending"]
    Sync["Scheduler periodic sync\nimmediate_tasks scan"]
    Queue["dispatcherd queue: dashboard"]
    Fn["collect_dashboard_reports_initial_data"]
    AWX["AWX DB\nunified_jobs_dashboard cursor batches"]
    Store["JobData.create_or_update_from_awx()\n+ labels + host summaries"]
    Done["Task status: completed"]

    Init --> Task --> Sync --> Queue --> Fn
    Fn --> AWX --> Store --> Done
```

### Registration

`initial_dashboard_collection` in `DASHBOARD_COLLECTION_GROUP`:

- `function`: `collect_dashboard_reports_initial_data`
- `cron`: `None` (immediate one-shot)
- Queue: `dashboard`

### Does not re-run on upgrade

`create_system_tasks()` snapshots completed one-shot system tasks before
deleting and recreating system task rows. If `initial_dashboard_collection`
already completed, its `completed` status is restored so the backfill does not
repeat on upgrade.

### Backfill logic

Implemented in `apps/dashboard_reports/tasks.py`:

1. **Date window** — `since` defaults to `JobData.last_finished_timestamp()` or
   a retention window (default 90 days, or Controller `cleanup_jobs` schedule
   when `DASHBOARD_COLLECTION.USE_CONTROLLER_RETENTION` is enabled).
2. **Query** — `metrics_utility.library.collectors.controller.unified_jobs_dashboard`
   against the AWX database. This is the same collector the hourly hook reads.
3. **Pagination** — cursor batches (`after_id` / `batch_size`; `BACKFILL_BATCH_SIZE`, default 5000).
4. **Select** — `apps/dashboard_reports/job_definition.py` keeps `failed` and `successful`
   jobs of every launch type (see [Job definition](#job-definition)).
5. **Write** — `JobData.create_or_update_from_awx()` with labels and host summaries
   (host summaries are fetched for `job` model rows).
6. **Telemetry** — `DashboardTelemetry` row per run.

## Job definition

`apps/dashboard_reports/job_definition.py` is the single definition of which
Controller jobs the dashboard stores and how raw rows are serialized. The hourly hook,
the first-start backfill and the update task all use it, so they store the same jobs.

- **Stored:** every unified job (`job`, `workflowjob`, `systemjob`, ...) with status
  `failed` or `successful`, including `sync` and `workflow` launch types.
- **Hidden at read time:** `JobData.launch_type` in `sync` / `workflow` is excluded by
  `get_job_type_filter()` (`apps/dashboard_reports/filters.py`), which every report,
  detail, export and leaderboard query uses. Rows with `launch_type IS NULL`
  (stored before the field existed) stay visible until the update task fills them.

### Update task: `update_dashboard_job_definition`

One-time task (`cron=None`, run once after deploying the `launch_type` migration):

- Re-reads the retention window (not the `JobData` watermark) through `unified_jobs_dashboard`
  in cursor batches (`BACKFILL_BATCH_SIZE`, or the `batch_size` kwarg).
- Fills `launch_type` / `job_type` of existing rows and adds jobs that were never stored,
  upserting by `job_id`. Each batch commits independently.
- Returns `processed`, `updated` (existing rows whose `launch_type` was unset), `added`
  and `failed` counts in the task result. The `DashboardTelemetry` row stores
  `processed` as `number_of_records_processed`, and `success=False` if any job failed.
- Idempotent: a second run reports `updated=0` and `added=0`. If jobs failed, rerun it.

### Collection status API

`apps/dashboard_reports/viewsets/collection_status.py` exposes:

- `initial_collection_status`
- `min_collection_timestamp`
- `next_run`

## Phase 2: Ongoing Sync via Hooks

Incremental sync piggybacks on **metrics hourly collectors** via
`post_collect_hook` in `generic_collect_metrics()`. No extra AWX queries for
job records — raw DataFrames from the collector are serialized into follow-up
tasks.

```mermaid
flowchart TD
    subgraph xx05 ["XX:05 - unified_jobs"]
        C1["collect_hourly_metrics\ncollector_type=unified_jobs"]
        G1["unified_jobs_dashboard.gather()"]
        F1["Filter: status failed/successful\nall launch types"]
        H1["_build_dashboard_sync_hook"]
        T1["Create pending Tasks:\nsync_dashboard_job_records_{hour}_{chunk}"]
        C1 --> G1 --> F1 --> H1 --> T1
    end

    subgraph xx10 ["XX:10 - job_host_summary"]
        C2["collect_hourly_metrics\ncollector_type=job_host_summary_service"]
        G2["job_host_summary_service.gather()"]
        H2["_build_dashboard_host_summary_sync_hook"]
        T2["Create pending Tasks:\nsync_dashboard_host_summaries_{hour}_{chunk}"]
        C2 --> G2 --> H2 --> T2
    end

    T1 --> Sched["Scheduler submits to dashboard queue"]
    Sched --> SJ["sync_dashboard_job_records\nwrites JobData, host_summaries=None"]
    T2 --> Sched2["Scheduler submits"]
    Sched2 --> SH["sync_dashboard_host_summaries\nupdates JobHostSummary"]

    SJ --> JD["JobData rows exist"]
    JD --> SH
```

### Ordering constraint

`hourly_unified_jobs` (XX:05) **must run before**
`hourly_job_host_summary` (XX:10).

The unified_jobs hook dispatches `sync_dashboard_job_records`, which creates
`JobData` rows. The host summary hook dispatches `sync_dashboard_host_summaries`,
which looks up jobs by `job_id`. If host summaries run first, matching
`JobData` rows do not exist and records are silently skipped with no retry.

The XX:05 → XX:10 schedule is an **operational ordering assumption**, not a
completion guarantee. Large job batches spawn multiple async
`sync_dashboard_job_records` tasks (500 jobs per chunk); host summaries may run
before all chunks finish. Skipped host summaries are not automatically retried.
Monitor pending dashboard sync tasks and `JobHostSummary` coverage when
collections are large or workers are slow.

### Hook details

| Hook | Source collector | Follow-up task | Writes |
|------|------------------|----------------|--------|
| `_build_dashboard_sync_hook` | `unified_jobs` | `sync_dashboard_job_records` | `JobData` (no host summaries) |
| `_build_dashboard_host_summary_sync_hook` | `job_host_summary_service` | `sync_dashboard_host_summaries` | `JobHostSummary` |

- Chunk size: 500 job records per task
- Tasks carry `_feature_flag: DASHBOARD_COLLECTION`
- Stale pending chunks from prior runs are deleted when chunk count shrinks

### Deprecated incremental task

`collect_dashboard_reports_data` performed scheduled incremental sync from
the last known timestamp. Ongoing sync now uses hooks. The function remains in
`TASK_FUNCTIONS` for manual/API invocation only — it is **not** in `task_groups.py`.

`DASHBOARD_COLLECTION.COLLECTION_SCHEDULE_CRON` in `apps/settings/defaults.py`
(`"0 */6 * * *"`) is unused legacy configuration from the pre-hook design.

## Cleanup Tasks

| Task | Cron | Function |
|------|------|----------|
| `cleanup_dashboard_reports_old_data` | `30 5 * * *` | Delete `JobData` beyond retention |
| `cleanup_dashboard_telemetry` | `45 5 * * *` | Delete `DashboardTelemetry` older than 60 days |

Retention for job data defaults to Controller `cleanup_jobs` schedule when
`USE_CONTROLLER_RETENTION` is enabled.

## Configuration

`DASHBOARD_COLLECTION` dict in `apps/settings/defaults.py`:

| Key | Purpose |
|-----|---------|
| `BACKFILL_BATCH_SIZE` | Cursor batch size for initial backfill (default 5000) |
| `USE_CONTROLLER_RETENTION` | Use AWX cleanup_jobs schedule for retention window |

## Separation from Anonymization

| Aspect | Dashboard sync | Metrics / anonymization |
|--------|----------------|-------------------------|
| Destination | `JobData`, `JobHostSummary` | `HourlyMetricsCollection` → Segment |
| API consumer | `/api/v1/dashboard_reports/` | Red Hat upstream |
| Ongoing mechanism | Post-collect hooks | Scheduled collectors + rollup |
| Feature enablement setting | `DASHBOARD_COLLECTION` | `METRICS_COLLECTION`, `ANONYMIZED_DATA_COLLECTION` |

Hourly collectors run both paths when flags are enabled: rollup to metrics DB
and hook-created sync tasks to dashboard tables.

## Related Documentation

- [collectors.md](collectors.md) — hourly collector schedules and rollup pipeline
- [dashboard-reports-api.md](dashboard-reports-api.md) — REST API consuming synced data
- [apscheduler.md](apscheduler.md) — how hook-created immediate tasks are submitted
