"""Single definition of which Controller jobs the dashboard stores and how raw rows are serialized.

The hourly sync hook, the initial backfill and the job definition update task all read
``unified_jobs_dashboard`` rows and must select and serialize them identically. Keep that
logic here so those paths cannot drift. Report visibility (hiding ``sync`` and ``workflow``
launch types) is a read-time concern handled in ``apps.dashboard_reports.filters``.

This module must not import Django models so the hourly collector can import it without
circular imports.
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Only finished jobs with these statuses belong in the dashboard.
DASHBOARD_JOB_STATUSES = ("failed", "successful")

# Launch types that are stored but hidden from reports by default.
HIDDEN_LAUNCH_TYPES = ("sync", "workflow")

DASHBOARD_COLUMNS = [
    "id",
    "name",
    "model",
    "unified_job_template_id",
    "organization_id",
    "organization_name",
    "started",
    "finished",
    "status",
    "launch_type",
    "elapsed",
    "launched_by_id",
    "launched_by_username",
    "project_id",
    "project_name",
    "created",
    "modified",
    "label_ids",
    "num_hosts",
]

INT_FIELDS = ("id", "organization_id", "unified_job_template_id", "launched_by_id", "project_id", "num_hosts")
STRING_FIELDS = (
    "name",
    "model",
    "organization_name",
    "status",
    "launch_type",
    "launched_by_username",
    "project_name",
    "label_ids",
)


def is_nan(value: Any) -> bool:
    """Return whether a value is a native float NaN requiring JSON normalization."""
    return isinstance(value, float) and math.isnan(value)


def select_dashboard_jobs(raw_data):
    """Return the dashboard jobs of a ``unified_jobs_dashboard`` DataFrame, or None when there are none."""
    if raw_data is None or raw_data.empty:
        return None

    filtered = raw_data[raw_data["status"].isin(DASHBOARD_JOB_STATUSES)]
    return None if filtered.empty else filtered


def has_usable_model(value: Any) -> bool:
    """Return whether a collected dashboard model value can be persisted."""
    return isinstance(value, str) and bool(value.strip())


def _log_model_gaps(filtered) -> None:
    """Warn when dashboard rows need Controller model recovery during sync."""
    if "model" not in filtered.columns:
        return

    invalid_models = ~filtered["model"].map(has_usable_model)
    if invalid_models.any():
        logger.warning(
            "unified_jobs_dashboard returned %s rows without a usable model; the sync task will resolve them",
            int(invalid_models.sum()),
        )


def _serialize_datetime_fields(row: dict) -> None:
    for field in ("started", "finished", "created", "modified"):
        val = row.get(field)
        if val is not None and hasattr(val, "isoformat"):
            row[field] = val.isoformat()


def _serialize_integer_fields(row: dict) -> None:
    # Pandas upcasts nullable integer columns to float64 when NaN is present.
    # .where(notna(), other=None) can leave float NaN, so guard before int(nan).
    for field in INT_FIELDS:
        val = row.get(field)
        if val is None or is_nan(val):
            row[field] = None
        else:
            row[field] = int(val)


def _serialize_string_fields(row: dict) -> None:
    # Entirely missing string columns can also arrive as float NaN from pandas.
    for field in STRING_FIELDS:
        val = row.get(field)
        if is_nan(val):
            row[field] = None


def _serialize_elapsed(row: dict) -> None:
    val = row.get("elapsed")
    if val is None or is_nan(val):
        row["elapsed"] = None
    else:
        row["elapsed"] = float(val)


def serialize_dashboard_record(row: dict) -> None:
    """Coerce all non-serializable numpy/pandas types to Python natives in-place.

    pandas .to_dict("records") preserves numpy dtypes (numpy.int64, numpy.float64) for
    columns with no NaN values. DjangoJSONEncoder does not handle these, so storing raw
    records in Task.task_data would raise TypeError. Nullable numeric and string fields
    are normalized to None when pandas represents missing values as float NaN.
    """
    _serialize_datetime_fields(row)
    _serialize_integer_fields(row)
    _serialize_string_fields(row)
    _serialize_elapsed(row)


def prepare_dashboard_records(filtered) -> list[dict]:
    """Select and serialize the dashboard columns from collected job rows."""
    _log_model_gaps(filtered)

    # unified_jobs_dashboard guarantees these columns; guard with available
    # in case a future schema change temporarily drops one.
    available = [column for column in DASHBOARD_COLUMNS if column in filtered.columns]
    missing = set(DASHBOARD_COLUMNS) - set(available)
    if missing:
        logger.warning(
            "unified_jobs_dashboard is missing expected columns: %s",
            sorted(missing),
        )
        if "model" in missing:
            logger.warning("Dashboard job types will be resolved from Controller by job ID where possible")

    subset = filtered[available]
    records = subset.where(subset.notna(), other=None).to_dict("records")
    for row in records:
        serialize_dashboard_record(row)
    return records
