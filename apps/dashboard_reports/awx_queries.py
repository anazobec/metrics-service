"""
AWX database query helpers for dashboard reports filter dropdowns.

Provides low-level SQL helpers and higher-level fetch functions for retrieving
organizations, job templates, projects, and labels directly from the AWX database.
"""

import enum
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Per-query GROUP BY clauses, kept separate from the AWXQuery SELECT literals so that
# fetch_data_from_db can compose WHERE / GROUP BY / ORDER BY in valid SQL order
# (WHERE and GROUP BY must precede ORDER BY, and WHERE must precede GROUP BY).
GROUP_BY_CLAUSES: dict["AWXQuery", str] = {}


class AWXQuery(enum.Enum):
    """Enumeration of allowed AWX read-only SELECT queries."""

    ORGANIZATIONS = "SELECT id, name FROM main_organization"
    TEMPLATES = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_jobtemplate jt on jt.unifiedjobtemplate_ptr_id = ujt.id"
    )
    PROJECTS = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_project pj on pj.unifiedjobtemplate_ptr_id = ujt.id"
    )
    # Labels are organization-scoped in AWX: the same label name can exist as multiple rows
    # with different ids across organizations. GROUP BY name collapses those into one row per
    # name, picking the smallest id as the canonical representative (see AAP-85133). The GROUP BY
    # is deliberately kept out of the SELECT literal itself (see GROUP_BY_CLAUSES below) so that
    # fetch_data_from_db can insert WHERE/GROUP BY/ORDER BY clauses in valid SQL order.
    LABELS = "SELECT MIN(id) as id, name FROM main_label"

    RETENTION_SETTINGS = (
        "SELECT "
        "sjt.job_type, "
        "ujt.name AS template_name, "
        "s.name AS schedule_name, "
        "s.enabled AS schedule_enabled, "
        "s.rrule, "
        "s.next_run, "
        "(s.extra_data::jsonb ->> 'days')::int AS retention_days "
        "FROM main_systemjobtemplate sjt "
        "JOIN main_unifiedjobtemplate ujt "
        "ON ujt.id = sjt.unifiedjobtemplate_ptr_id "
        "LEFT JOIN main_schedule s "
        "ON s.unified_job_template_id = ujt.id "
        "WHERE sjt.job_type IN ('cleanup_jobs', 'cleanup_activitystream')"
    )


# Registered after the enum body so members exist; maps queries needing GROUP BY to their clause.
GROUP_BY_CLAUSES[AWXQuery.LABELS] = " GROUP BY name"


def _build_where_clause(join_alias: str, search_str: str | None, pk: Any) -> tuple[str, list[Any]]:
    """Build WHERE clause and parameters for SQL query."""
    where_clauses = []
    params = []
    if search_str:
        # Escape backslash first, then ILIKE wildcards, so user-supplied % and _ are treated literally.
        escaped = search_str.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses.append(f"{join_alias}name ilike %s ESCAPE E'\\\\'")
        params.append("%" + escaped + "%")
    if pk is not None:
        where_clauses.append(f"{join_alias}id = %s")
        params.append(pk)
    clause = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return clause, params


def _execute_db_query(db_connection, query: str, params: list[Any]) -> tuple[list[str], list[Any]]:
    """Execute SQL query and return columns and data."""
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        data = cursor.fetchall()
    return columns, data


def _execute_count_query(db_connection, count_query: str, params: list[Any]) -> int:
    """Execute a COUNT query and return the integer result."""
    with db_connection.cursor() as cursor:
        cursor.execute(count_query, params)
        return cursor.fetchone()[0]


def fetch_data_from_db(awx_query: AWXQuery, join_alias: str = "", **kwargs: Any) -> tuple[list[Any], int]:
    """
    Execute a parameterized SQL query against the AWX database with optional search, pk, limit, and offset filters.

    When ``limit`` is provided, runs a COUNT(*) subquery first to obtain the total matching row count,
    then fetches only the requested page via LIMIT/OFFSET — keeping full table scans out of Python memory.
    When ``limit`` is omitted (e.g. single-row retrieve by pk), returns all matching rows and derives
    the total from the result length.

    Returns ``(rows, total_count)``.
    """
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str")
    pk = kwargs.get("pk")
    limit = kwargs.get("limit")
    offset = kwargs.get("offset", 0)

    base_query = awx_query.value
    where_clause, params = _build_where_clause(join_alias, search_str, pk)
    group_by_clause = GROUP_BY_CLAUSES.get(awx_query, "")
    order_clause = f" ORDER BY {join_alias}name"

    if limit is not None:
        # WHERE must precede GROUP BY, which must precede ORDER BY/LIMIT — see GROUP_BY_CLAUSES.
        count_query = f"SELECT COUNT(*) FROM ({base_query}{where_clause}{group_by_clause}) AS _count_subq"  # noqa: S608 base_query is an AWXQuery enum value (hardcoded literal); where_clause uses %s placeholders
        total = _execute_count_query(db_connection, count_query, params)
        query = base_query + where_clause + group_by_clause + order_clause + " LIMIT %s OFFSET %s"
        _, data = _execute_db_query(db_connection, query, params + [limit, offset])
    else:
        query = base_query + where_clause + group_by_clause + order_clause
        _, data = _execute_db_query(db_connection, query, params)
        total = len(data)

    return data, total


def format_id_name_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Format rows as list of dicts with 'id' and 'name' keys."""
    return [{"id": row[0], "name": row[1]} for row in rows]


def fetch_id_name(
    awx_query: AWXQuery, join_alias: str = "", error_msg: str = "", **kwargs
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch id/name pairs from the AWX database and return ``(items, total_count)``.

    ``total_count`` is the DB-level COUNT when pagination params (``limit``/``offset``) are supplied,
    or the length of the result set otherwise.  Raises the underlying exception after logging on failure.
    """
    try:
        rows, total = fetch_data_from_db(awx_query, join_alias=join_alias, **kwargs)
    except Exception:
        logger.exception(error_msg)
        raise
    return format_id_name_rows(rows), total


def fetch_organizations(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch organizations from DB, returning ``(items, total_count)``."""
    return fetch_id_name(AWXQuery.ORGANIZATIONS, error_msg="Error fetching organizations from AWX database", **kwargs)


def fetch_templates(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch job templates from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.TEMPLATES, join_alias="ujt.", error_msg="Error fetching job templates from AWX database", **kwargs
    )


def fetch_projects(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch projects from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.PROJECTS, join_alias="ujt.", error_msg="Error fetching projects from AWX database", **kwargs
    )


def fetch_labels(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch labels from DB, returning ``(items, total_count)``."""
    return fetch_id_name(AWXQuery.LABELS, error_msg="Error fetching labels from AWX database", **kwargs)


def fetch_retention_settings(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch retention settings from DB, returning ``(items, total_count)``."""
    rows, total = fetch_data_from_db(
        AWXQuery.RETENTION_SETTINGS,
        join_alias="s.",
        db_connection=kwargs.get("db_connection"),
    )
    items = sorted(
        [
            {
                "job_type": row[0],
                "template_name": row[1],
                "schedule_name": row[2],
                "schedule_enabled": row[3],
                "rrule": row[4],
                "next_run": row[5],
                "retention_days": row[6],
            }
            for row in rows
        ],
        key=lambda r: (r["job_type"] or "", r["schedule_name"] or ""),
    )
    return items, total
