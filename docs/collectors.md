# Metrics Collectors

Collectors gather data from the AWX Controller database (and optionally the
metrics-service database), compute anonymized rollups via **metrics-utility**,
and store results for daily aggregation and optional upstream transmission.

Collector **schedules** live in `apps/tasks/task_groups.py`. Execution is
handled by the task system ([task-system.md](task-system.md)).

Dashboard sync hooks on two hourly collectors feed a separate pipeline — see
[dashboard-sync.md](dashboard-sync.md).

## Collection Pipeline

```mermaid
flowchart TD
    subgraph hourly [Hourly - METRICS_COLLECTION]
        H1["unified_jobs XX:05"]
        H2["job_host_summary XX:10"]
        H3["credentials XX:15"]
        H4["job_events XX:20"]
    end

    subgraph daily_snap [Daily Snapshots - 01:00-01:45]
        S1["execution_environments"]
        S2["config"]
        S3["controller_version"]
        S4["table_metadata"]
        S5["feature_flags"]
    end

    subgraph daily_range [Daily Time-Range - 01:50-01:55]
        D1["task_executions_service"]
        D2["indirect_managed_nodes\nINDIRECT_NODE_COLLECTION"]
    end

    H1 & H2 & H3 & H4 & S1 & S2 & S3 & S4 & S5 & D1 & D2 --> HMC["HourlyMetricsCollection\nrollup JSON per collector"]
    HMC --> Rollup["daily_metrics_rollup\n02:00"]
    Rollup --> DMS["DailyMetricsSummary"]
    DMS --> Anon["daily_anonymize_and_prepare\n03:00\nANONYMIZED_DATA_COLLECTION"]
    Anon --> Segment["send_anonymized_to_segment\none-shot task"]
    Segment --> RH["Segment / Red Hat upstream"]
```

## Shared Collection Logic

All collectors delegate to `generic_collect_metrics()` in
`apps/tasks/utils.py`:

1. Resolve collector from a registry (`collector_type` key).
2. Call `metrics_utility` collector `gather()` against the target database.
3. Run optional `post_collect_hook(raw_data)` (dashboard sync uses this).
4. Compute rollup via `metrics_utility.anonymized_rollups` processor.
5. Persist rollup to `HourlyMetricsCollection`.

Collector SQL and gather logic live in the external **metrics-utility** package
(`metrics_utility.library.collectors`). metrics-service owns scheduling,
persistence, and downstream rollup/anonymization.

### Collector entry points

| Module | Function | Mode |
|--------|----------|------|
| `collect_hourly_metrics.py` | `collect_hourly_metrics` | Hourly time window |
| `collect_snapshot_metrics.py` | `collect_snapshot_metrics` | Point-in-time snapshot |
| `collect_daily_metrics.py` | `collect_daily_metrics` | Previous full calendar day |

## Schedule Timeline

```mermaid
flowchart TB
    subgraph hourly ["Hourly collectors — every hour at minute offset"]
        direction LR
        H05["XX:05 unified_jobs"]
        H10["XX:10 job_host_summary"]
        H15["XX:15 credentials"]
        H20["XX:20 job_events"]
        H05 --> H10 --> H15 --> H20
    end

    subgraph dailySnap ["Daily snapshots — 01:00-01:45 UTC"]
        direction TB
        S1["01:00 execution_environments"]
        S2["01:30 config"]
        S3["01:35 controller_version"]
        S4["01:40 table_metadata"]
        S5["01:45 feature_flags_service"]
        S1 --> S2 --> S3 --> S4 --> S5
    end

    subgraph dailyRange ["Daily time-range — 01:50-01:55 UTC"]
        direction TB
        R1["01:50 task_executions"]
        R2["01:55 indirect_nodes"]
        R1 --> R2
    end

    subgraph pipeline ["Daily pipeline"]
        direction TB
        P1["02:00 daily_metrics_rollup"]
        P2["03:00 daily_anonymize"]
        P3["04:00 cleanup_metrics_data"]
        P1 --> P2 --> P3
    end

    hourly --> dailySnap --> dailyRange --> pipeline
```

Cron expressions are defined in `METRICS_COLLECTION_GROUP` and
`INDIRECT_NODE_COLLECTION_GROUP` in `task_groups.py`.

## Hourly Collectors

Registry: `_get_hourly_collectors()` in `collect_hourly_metrics.py`.

| `collector_type` | metrics-utility source | Rollup class | Cron |
|--------------------|------------------------|--------------|------|
| `unified_jobs` | `unified_jobs_dashboard` | `JobsAnonymizedRollup` | `5 * * * *` |
| `job_host_summary_service` | `job_host_summary_service` | `JobHostSummaryAnonymizedRollup` | `10 * * * *` |
| `credentials_service` | `credentials_service` | `CredentialsAnonymizedRollup` | `15 * * * *` |
| `main_jobevent_service` | `main_jobevent_service` | `EventModulesAnonymizedRollup` | `20 * * * *` |

`unified_jobs` uses `unified_jobs_dashboard` (extra fields for dashboard sync).
The registry key remains `unified_jobs` for backward compatibility.

**Dashboard hooks** — `unified_jobs` and `job_host_summary_service` register
`post_collect_hook_factory` functions when `DASHBOARD_COLLECTION` is enabled.
See [dashboard-sync.md](dashboard-sync.md).

## Daily Snapshot Collectors

Registry: `_get_snapshot_collectors()` in `collect_snapshot_metrics.py`.

| `collector_type` | Source | Rollup | Cron |
|------------------|--------|--------|------|
| `execution_environments` | `execution_environments` | `ExecutionEnvironmentsAnonymizedRollup` | `0 1 * * *` |
| `config` | `config` | none (raw) | `30 1 * * *` |
| `controller_version_service` | `controller_version_service` | `ControllerVersionAnonymizedRollup` | `35 1 * * *` |
| `table_metadata` | `table_metadata` | `TableMetadataAnonymizedRollup` | `40 1 * * *` |
| `feature_flags_service` | `feature_flags_service` | `FeatureFlagsAnonymizedRollup` | `45 1 * * *` |

Snapshots are stored with a timestamp of yesterday 23:00 so
`daily_metrics_rollup` includes them in the daily window.

## Daily Time-Range Collectors

Registry: `_get_daily_collectors()` in `collect_daily_metrics.py`.

| `collector_type` | Source DB | Rollup | Cron | Enablement setting |
|------------------|-----------|--------|------|------|
| `task_executions_service` | `default` (metrics-service) | `TaskExecutionsAnonymizedRollup` | `50 1 * * *` | `METRICS_COLLECTION` |
| `indirect_managed_nodes` | `awx` | `IndirectManagedNodesAnonymizedRollup` | `55 1 * * *` | `INDIRECT_NODE_COLLECTION` |

These run once per day over the previous full calendar day (explicit
`since`/`until` window).

## Downstream Processing

### Daily rollup (`daily_metrics_rollup`)

Merges all `HourlyMetricsCollection` rollups for the target day into a single
`DailyMetricsSummary` record. Runs at 02:00 after all daily collectors complete.

### Anonymization (`daily_anonymize_and_prepare`)

Controlled by `ANONYMIZED_DATA_COLLECTION`. Anonymizes the daily summary and
creates a one-shot `send_anonymized_to_segment` task. Uses extended
`max_attempts` (7) for Segment transmission retries.

### Segment send (`send_anonymized_to_segment`)

Transmits anonymized payloads to Segment. Created dynamically by the anonymize
step, not registered as a recurring system task.

### Cleanup (`cleanup_metrics_data`)

Daily at 04:00. Removes old hourly collections, daily summaries, and payloads
based on retention settings in task args. Analytics payloads use the
Controller `cleanup_jobs` retention schedule unless an explicit manual override
is supplied. See [analytics-retention.md](analytics-retention.md).

## Data Models

In `apps/tasks/models.py`:

| Model | Contents |
|-------|----------|
| `HourlyMetricsCollection` | Per-collector rollup JSON, timestamp, collector type |
| `DailyMetricsSummary` | Merged daily rollup across all collectors |
| `AnonymizedMetricsPayload` | Prepared payload for Segment transmission |

## Feature Enablement Settings

These **feature enablement settings** control whether collector task groups run.
They are separate from **platform feature flags** (DAB `AAPFlag` / the
`feature_flags_service` snapshot collector, which records Controller platform
flag state for metrics rollups).

| Enablement setting | Effect when disabled |
|--------------------|---------------------|
| `METRICS_COLLECTION` | All hourly/daily collectors, rollup, metrics cleanup skipped |
| `ANONYMIZED_DATA_COLLECTION` | Anonymization and Segment send skipped; local collection can continue |
| `INDIRECT_NODE_COLLECTION` | `indirect_managed_nodes` collector skipped |
| `DASHBOARD_COLLECTION` | Dashboard post-collect hooks skipped (metrics rollups still run) |

Enablement settings are checked at task execution time via `task_data["_feature_flag"]`.

## Related Documentation

- [task-system.md](task-system.md) — how collector tasks are scheduled and executed
- [dashboard-sync.md](dashboard-sync.md) — dashboard sync hooks on hourly collectors
- [anonymization-and-transmission.md](anonymization-and-transmission.md) — Segment transmission after rollup
- [apscheduler.md](apscheduler.md) — timestamp injection for recurring collector fires
