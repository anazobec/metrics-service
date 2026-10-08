# Analytics Retention

Analytics payload cleanup follows the Controller `cleanup_jobs` schedule. The
retention value is read from the Controller database each time the cleanup task
runs, so changing the Controller schedule takes effect on the next cleanup run.

The value is read from `main_schedule.extra_data.days` for enabled schedules
whose system job template has `job_type = 'cleanup_jobs'`.

## Resolution Rules

- Only enabled schedules are considered.
- A schedule with a missing, non-integer, zero, or negative `days` value is
  skipped.
- If multiple valid schedules remain, the lowest `days` value wins.
- Equal `days` values are tied by the soonest `next_run`.
- If the Controller cannot be queried, cleanup logs the failure and uses 90
  days.
- If the query succeeds but no valid schedules remain, cleanup logs that no
  valid retention was found and uses 90 days.

Invalid schedules do not generate individual warnings when another valid
schedule exists. This avoids noisy logs for unused or malformed schedules;
the no-valid-schedules fallback is logged instead.

## Examples

| Controller schedules | Effective retention |
| --- | --- |
| Enabled: `days=30` | 30 days |
| Enabled: `days=90`, enabled: `days=30` | 30 days |
| Enabled: `days=0`, enabled: `days=45` | 45 days; the zero-day schedule is skipped |
| Disabled: `days=10`, enabled: `days=60` | 60 days |
| Enabled: `days="bad"` only | 90-day fallback, with a logged no-valid-schedules warning |
| Controller database unavailable | 90-day fallback, with the query failure logged |

An explicit `analytics_retention_days` task argument remains available for
manual runs and overrides the resolved Controller value. The scheduled
`cleanup_metrics_data` task does not pass that argument, so it follows the
Controller schedule dynamically.

The cleanup cutoff uses the `AnalyticsPayload.created` timestamp. Existing
retention policies for other metrics tables are unchanged.
