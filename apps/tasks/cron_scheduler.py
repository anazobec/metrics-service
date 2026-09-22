"""
Task scheduler using APScheduler.

This module provides a task scheduler that handles both task group definitions
and database tasks without database polling, using APScheduler for optimal performance.
"""

import logging
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from django.conf import settings as django_settings
from django.db import close_old_connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

STUCK_TASK_TIMEOUT_SECONDS: int = django_settings.TASK_TIMEOUT

# Functions that pin hour_timestamp to the previous full hour at dispatch time.
_PREVIOUS_HOUR_FUNCTIONS = {"collect_hourly_metrics"}


def _inject_dispatch_timestamps(function_name: str, task_data: dict) -> dict:
    """
    Inject a fixed time-window timestamp into task_data at the moment a recurring
    task is dispatched by the scheduler.

    Time-sensitive collectors compute their target window from timezone.now() when
    no explicit timestamp is present. If the task is retried hours later, "now" has
    shifted and the retry collects the wrong window. By pinning the timestamp here —
    before the task is even submitted — every retry of the same execution copy
    operates on the originally intended window.

    Only sets the key when it is absent, so manually created tasks that already
    carry an explicit timestamp are left untouched.
    """
    task_data = task_data.copy()

    if function_name in _PREVIOUS_HOUR_FUNCTIONS and "hour_timestamp" not in task_data:
        now = timezone.now()
        task_data["hour_timestamp"] = (now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)).isoformat()

    elif function_name == "collect_snapshot_metrics" and "collection_timestamp" not in task_data:
        now = timezone.now()
        task_data["collection_timestamp"] = (
            now.replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)
        ).isoformat()

    elif function_name == "collect_daily_metrics" and "hour_timestamp" not in task_data:
        now = timezone.now()
        task_data["hour_timestamp"] = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    return task_data


class UnifiedTaskScheduler:
    """
    Task scheduler using APScheduler.

    This scheduler handles both task group definitions and database tasks
    without database polling, providing optimal performance for all task types.
    """

    def __init__(self, check_interval: int = 30):
        """Initialize the task scheduler."""
        self.scheduler = BackgroundScheduler()
        self.running = False
        self._lock = threading.Lock()
        self.check_interval = check_interval

        # Database task tracking
        self._db_task_jobs: dict[int, str] = {}  # task_id -> job_id mapping

        # DB readiness tracking for error escalation
        self._db_not_ready_since = None  # Timestamp when DB first became unready
        self._db_ready_grace_period = timedelta(minutes=10)  # Grace period before escalating to ERROR

    def _check_db_readiness_and_log(self) -> bool:
        """
        Check DB readiness and log appropriate messages based on grace period.

        Returns:
            True if DB is ready, False otherwise
        """
        from apps.tasks.utils import awx_db_ready

        if not awx_db_ready():
            # Track when DB first became unready
            if self._db_not_ready_since is None:
                self._db_not_ready_since = timezone.now()

            # Check if we've exceeded the grace period
            elapsed = timezone.now() - self._db_not_ready_since
            if elapsed > self._db_ready_grace_period:
                logger.error(
                    f"AWX database still not ready after {elapsed.total_seconds():.0f}s "
                    f"(grace period: {self._db_ready_grace_period.total_seconds():.0f}s). "
                    "This likely indicates controller migrations failed. "
                    "Skipping task scheduling (will retry in 30s)"
                )
            else:
                logger.warning(
                    f"AWX database not ready yet ({elapsed.total_seconds():.0f}s elapsed), "
                    f"skipping task scheduling (will retry in 30s)"
                )
            return False

        # DB is ready - clear the tracking timestamp
        if self._db_not_ready_since is not None:
            elapsed = timezone.now() - self._db_not_ready_since
            logger.info(f"AWX database is now ready (was unready for {elapsed.total_seconds():.0f}s)")
            self._db_not_ready_since = None

        return True

    def start(self):
        """Start the cron scheduler."""
        with self._lock:
            if self.running:
                logger.warning("Scheduler is already running")
                return

            try:
                # Start the scheduler
                self.scheduler.start()
                self.running = True

                # Load database tasks into scheduler
                self._sync_database_tasks()

                # Add periodic task to check for new database tasks
                self.scheduler.add_job(
                    func=self._periodic_database_sync,
                    trigger="interval",
                    seconds=self.check_interval,
                    id="periodic_db_sync",
                    name="Periodic Database Task Sync",
                    replace_existing=True,
                    max_instances=1,  # Prevent overlapping executions
                )

                logger.info("Task scheduler started")
                logger.info(f"Loaded {len(self._db_task_jobs)} database tasks")
                logger.info(f"Periodic database sync will run every {self.check_interval} seconds")

            except Exception as e:
                logger.exception(f"Failed to start cron scheduler: {str(e)}")
                raise

    def stop(self):
        """Stop the cron scheduler."""
        with self._lock:
            if not self.running:
                return

            try:
                self.scheduler.shutdown()
                self.running = False
                self._db_task_jobs.clear()
                logger.info("Task scheduler stopped")
            except Exception as e:
                logger.exception(f"Error stopping cron scheduler: {str(e)}")

    def _task_feature_flag_enabled(self, task, feature_flags: dict[str, bool] | None = None) -> bool:
        """Return False if the task carries a feature flag that is currently disabled.

        ``feature_flags`` is scoped to one scheduler synchronization pass. It avoids
        resolving the same flag once per task while preserving the uncached default
        behavior for task execution, where flags must be re-read on every fire.
        """
        feature_flag = task.task_data.get("_feature_flag") if task.task_data else None
        if not feature_flag:
            return True

        if feature_flags is not None and feature_flag in feature_flags:
            return feature_flags[feature_flag]

        from .task_groups import get_feature_enabled_from_db

        enabled = get_feature_enabled_from_db(feature_flag)
        if feature_flags is not None:
            feature_flags[feature_flag] = enabled
        return enabled

    def _sync_database_tasks(self):
        """Synchronize database tasks with the scheduler."""
        try:
            from .models import Task

            # Get all pending database tasks that are scheduled or recurring
            scheduled_tasks = Task.scheduled_tasks()
            recurring_tasks = Task.recurring_tasks()

            added_scheduled = 0
            added_recurring = 0
            feature_flags: dict[str, bool] = {}

            # Add scheduled tasks whose feature flag is currently enabled
            for task in scheduled_tasks:
                if self._task_feature_flag_enabled(task, feature_flags):
                    self._add_database_scheduled_task(task)
                    added_scheduled += 1

            # Add recurring tasks whose feature flag is currently enabled
            for task in recurring_tasks:
                if self._task_feature_flag_enabled(task, feature_flags):
                    self._add_database_recurring_task(task)
                    added_recurring += 1

            logger.info(f"Synchronized {added_scheduled} scheduled and {added_recurring} recurring database tasks")

        except Exception as e:
            logger.exception(f"Error synchronizing database tasks: {e}")

    def _process_immediate_tasks(self, tasks, feature_flags: dict[str, bool]) -> int:
        """Execute new ready-to-run immediate tasks and return the count handled."""
        new_immediate = 0
        for task in tasks:
            if task.id not in self._db_task_jobs and task.is_ready_to_run():
                if not self._task_feature_flag_enabled(task, feature_flags):
                    continue
                logger.info(f"Found new immediate task: {task.name} (ID: {task.id}) - executing now")
                # Track immediate task to prevent duplicate submissions
                self._db_task_jobs[task.id] = f"db_immediate_{task.id}"
                self._execute_database_task(task.id)
                new_immediate += 1
        return new_immediate

    def _process_scheduled_tasks(self, tasks, feature_flags: dict[str, bool]) -> int:
        """Add new scheduled tasks and return the count handled."""
        new_scheduled = 0
        for task in tasks:
            if task.id not in self._db_task_jobs:
                if not self._task_feature_flag_enabled(task, feature_flags):
                    continue
                logger.info(f"Found new scheduled task: {task.name} (ID: {task.id})")
                self._add_database_scheduled_task(task)
                new_scheduled += 1
        return new_scheduled

    def _process_recurring_tasks(self, tasks, feature_flags: dict[str, bool]) -> int:
        """Add new recurring tasks and return the count handled."""
        new_recurring = 0
        for task in tasks:
            if task.id not in self._db_task_jobs:
                if not self._task_feature_flag_enabled(task, feature_flags):
                    continue
                logger.info(f"Found new recurring task: {task.name} (ID: {task.id})")
                self._add_database_recurring_task(task)
                new_recurring += 1
        return new_recurring

    def _periodic_database_sync(self):
        """Periodically check for new database tasks and add them to the scheduler."""
        close_old_connections()

        # Always check for stuck tasks, regardless of AWX DB readiness.
        # Stuck task timeout reconciliation should not be paused during controller startup.
        try:
            self._fail_stuck_tasks()
        except Exception as e:
            logger.exception(f"Error failing stuck tasks: {e}")

        try:
            self._retry_failed_tasks()
        except Exception as e:
            logger.exception(f"Error retrying failed tasks: {e}")

        # Early exit if AWX DB is not ready yet (during controller startup).
        # This prevents collector tasks from being scheduled before migrations complete,
        # without blocking API responses or individual tasks. The scheduler will retry
        # every 30s until the database becomes available.
        if not self._check_db_readiness_and_log():
            return

        try:
            from .models import Task

            # Get all pending database tasks (immediate, scheduled, or recurring)
            immediate_tasks = Task.immediate_tasks()
            scheduled_tasks = Task.scheduled_tasks()
            recurring_tasks = Task.recurring_tasks()
            feature_flags: dict[str, bool] = {}

            new_immediate = self._process_immediate_tasks(immediate_tasks, feature_flags)
            new_scheduled = self._process_scheduled_tasks(scheduled_tasks, feature_flags)
            new_recurring = self._process_recurring_tasks(recurring_tasks, feature_flags)

            if new_immediate > 0 or new_scheduled > 0 or new_recurring > 0:
                logger.info(
                    f"Periodic sync: {new_immediate} immediate, {new_scheduled} scheduled, {new_recurring} recurring tasks"
                )

            # Clean up advisory locks held by sessions that outlived their process
            self._cleanup_stale_advisory_locks()

        except Exception as e:
            logger.exception(f"Error in periodic database sync: {e}")

    def _fail_stuck_tasks(self):
        """Mark tasks stuck in running status as failed after their timeout."""
        from .models import Task, TaskExecution

        now = timezone.now()
        stuck_to_fail = Task.objects.filter(
            status="running", started_at__lt=now - timedelta(seconds=STUCK_TASK_TIMEOUT_SECONDS)
        )
        if stuck_to_fail:
            ids = [t.id for t in stuck_to_fail]
            error_msg = "Task timed out — worker died before completion"
            with transaction.atomic():
                TaskExecution.objects.filter(task__id__in=ids, status="running").update(
                    status="failed", error_message=error_msg, completed_at=now
                )
                Task.objects.filter(id__in=ids, status="running").update(
                    status="failed", error_message=error_msg, completed_at=now
                )
            logger.warning(f"Failed {len(ids)} stuck task(s): {ids}")

    def _retry_failed_tasks(self):
        """Schedule retries for failed tasks that still have attempts remaining."""
        from django.db.models import F

        from .models import Task
        from .tasks_system import _schedule_retry

        retryable = Task.objects.filter(Task.non_recurring_filter(), status="failed").exclude(
            attempts__gte=F("max_attempts")
        )

        for task in retryable:
            _schedule_retry(task)

    def _cleanup_stale_advisory_locks(self):
        """Terminate database sessions holding advisory locks that appear stale.

        A session is considered stale when it has been idle longer than the task
        timeout — at that point the corresponding Task has already been marked
        failed by the stuck-task detector above, but a network partition can keep
        the PostgreSQL session (and its lock) alive for hours.

        Only cleans up locks matching TASK_LOCKS function names, not arbitrary
        advisory locks that other applications might hold.
        """
        from django.db import connection

        from .tasks import TASK_LOCKS

        try:
            with connection.cursor() as cursor:
                # Compute our lock IDs the same way lock.py does:
                # hashtext(name)::bigint, then Python-style modulo 2**63
                cursor.execute("SELECT hashtext(name)::bigint FROM unnest(%s::text[]) AS name", [list(TASK_LOCKS)])
                our_lock_ids = [row[0] % (2**63) for row in cursor.fetchall()]

                cursor.execute(
                    """
                    SELECT l.pid
                    FROM pg_locks l
                    JOIN pg_stat_activity a ON l.pid = a.pid
                    WHERE l.locktype = 'advisory'
                      AND l.granted = true
                      AND a.pid != pg_backend_pid()
                      AND a.state = 'idle'
                      AND a.state_change < NOW() - interval '1 second' * %s
                      AND ((l.classid::bigint << 32) | (l.objid::bigint & x'ffffffff'::bigint)) = ANY(%s)
                    """,
                    [STUCK_TASK_TIMEOUT_SECONDS, our_lock_ids],
                )
                stale_pids = [row[0] for row in cursor.fetchall()]

                for pid in stale_pids:
                    cursor.execute("SELECT pg_terminate_backend(%s)", [pid])
                    logger.warning(f"Terminated stale session {pid} holding an advisory lock")

                if stale_pids:
                    logger.warning(f"Cleaned up {len(stale_pids)} stale advisory lock(s)")
        except Exception as e:
            logger.exception(f"Error cleaning up stale advisory locks: {e}")

    def _add_database_scheduled_task(self, task):
        """Add a one-time scheduled database task to the scheduler."""
        if task.id in self._db_task_jobs:
            return  # Already scheduled

        try:
            job_id = f"db_task_{task.id}"

            # Check if the scheduled time is in the past
            now = timezone.now()
            if task.scheduled_time <= now:
                # Execute immediately if past due. Not added to _db_task_jobs, so
                # periodic sync re-submits until a worker claims the task (status != pending).
                logger.info(
                    f"Task {task.name} (ID: {task.id}) is past due (scheduled: {task.scheduled_time}, now: {now}), executing immediately"
                )
                self._execute_database_task(task.id)
                return

            # Create date trigger for the scheduled time
            trigger = DateTrigger(run_date=task.scheduled_time)

            # Add job to scheduler
            self.scheduler.add_job(
                func=self._execute_database_task,
                trigger=trigger,
                args=[task.id],
                id=job_id,
                name=f"DB Task: {task.name}",
                replace_existing=True,
                max_instances=1,
            )

            self._db_task_jobs[task.id] = job_id
            logger.info(f"Added scheduled database task: {task.name} (ID: {task.id}) at {task.scheduled_time}")

        except Exception as e:
            logger.exception(f"Failed to add scheduled database task {task.id}: {e}")

    def _add_database_recurring_task(self, task):
        """Add a recurring database task to the scheduler."""
        if task.id in self._db_task_jobs:
            return  # Already scheduled

        try:
            job_id = f"db_recurring_{task.id}"

            # Create cron trigger from expression
            trigger = CronTrigger.from_crontab(task.cron_expression)

            # Add job to scheduler
            self.scheduler.add_job(
                func=self._execute_database_task,
                trigger=trigger,
                args=[task.id],
                id=job_id,
                name=f"DB Recurring: {task.name}",
                replace_existing=True,
                max_instances=1,
            )

            self._db_task_jobs[task.id] = job_id
            logger.info(f"Added recurring database task: {task.name} (ID: {task.id}) with cron: {task.cron_expression}")

        except Exception as e:
            logger.exception(f"Failed to add recurring database task {task.id}: {e}")

    def _execute_database_task(self, task_id: int):
        """Execute a database task by submitting it to dispatcherd."""
        close_old_connections()
        try:
            from .models import Task
            from .task_groups import get_feature_enabled_from_db

            # Get the task (don't filter by status for recurring tasks)
            try:
                task = Task.objects.get(id=task_id)
            except Task.DoesNotExist:
                logger.warning(f"Database task {task_id} not found")
                self._remove_database_task(task_id)
                return

            if task.status in ("cancelled", "completed"):
                logger.warning(f"Task '{task.name}' has status '{task.status}' and will not be executed")
                self._remove_database_task(task_id)
                return

            # Check feature flag at runtime so toggling takes effect without restart
            feature_flag = task.task_data.get("_feature_flag") if task.task_data else None
            if feature_flag and not get_feature_enabled_from_db(feature_flag):
                logger.debug(f"Skipping task '{task.name}': feature flag '{feature_flag}' is disabled")
                if not task.cron_expression:
                    self._remove_database_task(task_id)
                return

            # Handle recurring tasks by creating a new execution record
            if task.cron_expression:
                # Create a new task record for this execution
                execution_task = Task.objects.create(
                    name=f"{task.name} (Execution {timezone.now().strftime('%Y-%m-%d %H:%M:%S')})",
                    function_name=task.function_name,
                    task_data=_inject_dispatch_timestamps(task.function_name, task.task_data or {}),
                    scheduled_time=None,  # Execute immediately
                    cron_expression=None,  # This is not a recurring task
                    max_attempts=task.max_attempts,
                    created_by=task.created_by,
                    is_system_task=task.is_system_task,
                )

                logger.info(
                    f"Created execution record for recurring task: {task.name} → {execution_task.name} (ID: {execution_task.id})"
                )

                # Submit the execution task (not the original recurring task)
                from .tasks_system import submit_task_to_dispatcher

                try:
                    submit_task_to_dispatcher(execution_task)
                except Exception as e:
                    # The execution_task stays "pending" and was never tracked in _db_task_jobs,
                    # so the next _periodic_database_sync immediate_tasks scan re-submits it.
                    # (Not the _retry_failed_tasks path — that only handles status="failed".)
                    logger.warning(f"Failed to submit execution of recurring task {task.name}: {e}")

                # Keep the original recurring task unchanged (it stays as template)
                logger.info(f"Recurring task {task.name} (ID: {task_id}) remains as template for future executions")
                return

            # Check if task is ready to run
            if task.status not in ["pending"]:
                logger.warning(f"Task {task_id} is not in pending status (current: {task.status})")
                self._remove_database_task(task_id)
                return

            logger.info(f"Executing database task: {task.name} (ID: {task_id})")

            from .tasks_system import submit_task_to_dispatcher

            try:
                submit_task_to_dispatcher(task)
            except Exception as e:
                logger.warning(f"Failed to submit task {task.name}: {e}")
            finally:
                self._remove_database_task(task_id)

        except Exception as e:
            logger.exception(f"Failed to execute database task {task_id}: {e}")

    def _remove_database_task(self, task_id: int):
        """Remove a database task from the scheduler."""
        if task_id in self._db_task_jobs:
            job_id = self._db_task_jobs[task_id]
            try:
                self.scheduler.remove_job(job_id)
            except Exception as e:
                logger.debug(f"Job {job_id} not found in scheduler: {e}")
            del self._db_task_jobs[task_id]


# Global scheduler instance
_scheduler_instance: UnifiedTaskScheduler | None = None


def get_scheduler() -> UnifiedTaskScheduler:
    """Get the global scheduler instance."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = UnifiedTaskScheduler()
    return _scheduler_instance


def start_scheduler():
    """Start the global scheduler."""
    scheduler = get_scheduler()
    scheduler.start()
    return scheduler


def stop_scheduler():
    """Stop the global scheduler."""
    global _scheduler_instance
    if _scheduler_instance:
        _scheduler_instance.stop()
        _scheduler_instance = None
