# APScheduler Integration

APScheduler is the **scheduling and reconciliation layer** for metrics-service
background tasks. It does not execute task Python code — it watches the
`Task` table, registers time-based triggers, and submits work to dispatcherd
workers.

See [task-system.md](task-system.md) for the overall architecture and
[task-state-machine.md](task-state-machine.md) for state transitions and
recovery semantics.

## Role in the Stack

```mermaid
flowchart TB
    subgraph scheduler_proc [Scheduler Process]
        BGS["BackgroundScheduler"]
        Interval["interval job:\nperiodic_db_sync"]
        DateJobs["DateTrigger jobs\nscheduled tasks"]
        CronJobs["CronTrigger jobs\nrecurring templates"]
        UTS["UnifiedTaskScheduler"]
        UTS --> BGS
        BGS --> Interval
        BGS --> DateJobs
        BGS --> CronJobs
    end

    DB[(Task table)]
    Disp["dispatcherd broker"]
    Worker["Worker:\nexecute_db_task"]

    Interval -->|"every check_interval s"| Sync["_periodic_database_sync"]
    Sync --> DB
    DateJobs --> Exec["_execute_database_task"]
    CronJobs --> Exec
    Exec -->|"submit_task_to_dispatcher"| Disp
    Disp --> Worker
```

| Component | File |
|-----------|------|
| `UnifiedTaskScheduler` | `apps/tasks/cron_scheduler.py` |
| Process entry point | `apps/tasks/management/commands/run_task_scheduler.py` |
| Singleton accessors | `start_scheduler()`, `get_scheduler()`, `stop_scheduler()` |

## UnifiedTaskScheduler Lifecycle

1. **Start** — `BackgroundScheduler.start()`, load existing scheduled/recurring
   tasks from DB, register `periodic_db_sync` interval job.
2. **Periodic sync** — every `check_interval` seconds (default **30**), run
   reconciliation (stuck tasks, retries, new task discovery, advisory lock cleanup).
3. **Trigger fire** — `DateTrigger` or `CronTrigger` calls `_execute_database_task`.
4. **Submit** — recurring templates spawn child tasks; all executable work goes
   to `submit_task_to_dispatcher()`.
5. **Stop** — shutdown scheduler, clear `_db_task_jobs`.

## Scheduler Internals

```mermaid
flowchart TB
    subgraph BGS_jobs [APScheduler Jobs]
        PDS["periodic_db_sync\ninterval trigger"]
        DBDate["db_task_{id}\nDateTrigger"]
        DBCron["db_recurring_{id}\nCronTrigger"]
        DBImm["db_immediate_{id}\ntracking only"]
    end

    subgraph tracking [_db_task_jobs dict]
        Map["task_id → job_id"]
    end

    PDS --> Sync["_periodic_database_sync"]
    DBDate --> Exec["_execute_database_task"]
    DBCron --> Exec
    Sync --> Map
    Exec --> Submit["submit_task_to_dispatcher"]
```

`_db_task_jobs` maps `Task.id` to APScheduler job IDs to prevent duplicate
registration and to track immediate tasks after submission.

**Past-due scheduled tasks** — When `scheduled_time` is already in the past,
`_add_database_scheduled_task()` submits immediately via `_execute_database_task()`
without registering a `DateTrigger` or `_db_task_jobs` entry. The task remains
`pending` in `Task.scheduled_tasks()` until a worker claims it, so periodic sync
may re-submit it on every interval until status changes. Future-due scheduled
tasks are tracked in `_db_task_jobs` and are not re-submitted until the trigger
fires.

## Task Shapes and Triggers

```mermaid
flowchart LR
    subgraph immediate [Immediate]
        I1["scheduled_time=NULL\ncron=NULL"]
        I2["periodic sync\nsubmits directly"]
        I1 --> I2
    end

    subgraph scheduled [Scheduled]
        S1["scheduled_time set\ncron=NULL"]
        S2["DateTrigger at\nscheduled_time"]
        S1 --> S2
    end

    subgraph recurring [Recurring Template]
        R1["cron_expression set"]
        R2["CronTrigger"]
        R3["spawn child Task\nimmediate one-shot"]
        R1 --> R2 --> R3
    end

    S2 --> Submit
    I2 --> Submit
    R3 --> Submit
```

| Shape | APScheduler trigger | On fire |
|-------|---------------------|---------|
| Immediate | none (interval sync only) | `_execute_database_task` → submit |
| Scheduled | `DateTrigger(run_date=scheduled_time)` | submit (immediate if past due; see past-due note above) |
| Recurring | `CronTrigger.from_crontab(cron_expression)` | create child `Task`, submit child |

Recurring template rows stay `pending` forever. Each fire creates a new child
with `cron_expression=None` and `scheduled_time=None`.

### Timestamp injection

Before submitting recurring child tasks, `_inject_dispatch_timestamps()` pins
time-window keys so retries collect the originally intended window:

| Function | Injected key | Value |
|----------|--------------|-------|
| `collect_hourly_metrics` | `hour_timestamp` | previous full hour |
| `collect_snapshot_metrics` | `collection_timestamp` | yesterday 23:00 |
| `collect_daily_metrics` | `hour_timestamp` | today 00:00 |

Keys are only set when absent, so manually created tasks with explicit timestamps
are unchanged.

One-shot `collect_dashboard_reports_initial_data` and `update_dashboard_job_definition` have no recurring parent to inject from. The task
executor (`_pin_collection_window()` in `execute_claimed`) stores `since` and `until` in the task's `task_data`
on the first attempt, so every retry of that Task row resolves the same retention window, even across
UTC midnight or a Controller retention change.

## Periodic Sync Pipeline

`_periodic_database_sync()` runs on the interval job. Simplified flow:

```mermaid
flowchart TD
    Start["periodic_db_sync"] --> Stuck["_fail_stuck_tasks\nrunning past TASK_TIMEOUT → failed"]
    Stuck --> Retry["_retry_failed_tasks\nfailed with attempts left → pending\nexcludes recurring templates"]
    Retry --> AWX{"awx_db_ready?"}

    AWX -- No --> End["return early\nno new scheduling"]
    AWX -- Yes --> Imm["immediate_tasks:\nsubmit new pending tasks"]
    Imm --> Sch["scheduled_tasks:\nregister DateTrigger jobs"]
    Sch --> Rec["recurring_tasks:\nregister CronTrigger jobs"]
    Rec --> Locks["_cleanup_stale_advisory_locks"]
    Locks --> End2["done"]
```

**AWX DB gate** — When AWX is not ready, `_periodic_database_sync()` returns
before `immediate_tasks()`, `scheduled_tasks()`, and `recurring_tasks()` — all
new scheduling is deferred, not only collectors. Stuck-task detection and
retries run regardless of AWX readiness. A 10-minute grace period escalates
warnings to errors if AWX stays unavailable (likely failed Controller migrations).

**Stuck tasks** — `_fail_stuck_tasks()` marks `running` tasks older than
`TASK_TIMEOUT` (from settings) as `failed`. Recovery details are in
[task-state-machine.md §4](task-state-machine.md#4-crash-recovery).

**Retries** — `_retry_failed_tasks()` calls `_schedule_retry()` for failed
non-recurring tasks with `attempts < max_attempts`. This is separate from
dispatcherd's broker-level retries.

## Feature Enablement Checks

`_task_feature_flag_enabled()` reads the feature enablement setting name from
`task_data["_feature_flag"]` and calls `get_feature_enabled_from_db()`.
Disabled tasks are skipped during:

- Initial sync of scheduled/recurring jobs
- Periodic discovery of immediate/scheduled/recurring tasks
- `_execute_database_task()` at fire time

Runtime checks allow toggling enablement settings via DB/API without restarting
the scheduler. (The `_feature_flag` field name is legacy; values are feature
enablement settings, not platform feature flags.)

## Advisory Locks

Functions in `TASK_LOCKS` (`apps/tasks/tasks.py`) acquire PostgreSQL advisory
locks during scheduled execution to prevent overlapping collector runs.

`_cleanup_stale_advisory_locks()` terminates idle database sessions holding
locks matching `TASK_LOCKS` names when idle longer than `TASK_TIMEOUT`. This
handles network partitions that leave locks alive after workers are marked failed.

## CLI and the Check-Interval Quirk

```bash
python manage.py run_task_scheduler --check-interval 60
```

Two different intervals exist:

| Setting | Default | Controls |
|---------|---------|----------|
| `UnifiedTaskScheduler(check_interval=...)` | **30 seconds** | How often `_periodic_database_sync` runs |
| `--check-interval` CLI flag | **60 seconds** | How often the management command's idle loop wakes to verify the scheduler is still running |

The CLI flag does **not** currently pass its value to `UnifiedTaskScheduler`.
The periodic sync interval defaults to **30 seconds** via
`UnifiedTaskScheduler(check_interval=30)` in `__init__`; `start_scheduler()`
instantiates the scheduler with that default.

The management command loop only checks `scheduler.running` and handles
`KeyboardInterrupt` shutdown — it does not drive scheduling.

## Failure Modes

| Failure | Effect | Recovery |
|---------|--------|----------|
| Scheduler process down | Tasks accumulate as `pending` in DB | Restart scheduler; periodic sync re-submits |
| Worker process down | Submitted tasks sit in broker; running tasks may stick | Workers restart; stuck tasks → failed → retry |
| AWX DB unavailable | Collectors not scheduled; retries/stuck detection continue | Scheduler retries every 30s until AWX ready |
| Broker publish error | Child/recurring execution stays `pending` | Next immediate-task scan re-submits |
| Enablement setting disabled | Task skipped at sync and fire time | Re-enable setting; no restart needed (DB toggle) |

Dispatcherd may retry broker delivery independently. Application-level retries
use `task.retry()` and `_retry_failed_tasks()` — see
[task-state-machine.md §3](task-state-machine.md#3-transitions-reference).

## Related Documentation

- [task-system.md](task-system.md) — task groups, dispatcherd queues, API
- [task-state-machine.md](task-state-machine.md) — state machine and backoff formulas
- [collectors.md](collectors.md) — what scheduled collector tasks do
