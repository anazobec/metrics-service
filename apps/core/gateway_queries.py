"""
Gateway database query helpers.

Reads organization-membership RBAC data directly from the gateway database
instead of relying on metrics-service's local gateway-resource-sync copy
(``sync_resources_from_gateway`` / ``docs/core-rbac.md``), which is only
refreshed periodically. Direct reads avoid that sync lag for data that needs
to be current, e.g. "am I a member of this organization right now" (AAP-88670).

The gateway is itself built on django-ansible-base, so membership is stored the
same way as metrics-service's local RBAC tables: there is no direct user->org
column, only role assignments (``dab_rbac_roleuserassignment`` ->
``dab_rbac_roledefinition`` name "Organization Member"/"Organization Admin" ->
``object_id`` referencing the organization). Joined by ``username`` since that's
the shared identity key across gateway/AWX/metrics-service (same convention
``apps.dashboard_reports`` uses for AWX joins, e.g. ``launched_by_username``).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Role definitions that confer organization membership on the gateway.
_MEMBER_ROLE_NAMES = ("Organization Member", "Organization Admin")

_MEMBER_ORGANIZATIONS_QUERY = """
    SELECT o.id, o.name
    FROM dab_rbac_roleuserassignment rua
    JOIN aap_gateway_api_user u ON u.id = rua.user_id
    JOIN dab_rbac_roledefinition rd ON rd.id = rua.role_definition_id
    JOIN aap_gateway_api_organization o ON o.id::text = rua.object_id
    WHERE u.username = %s AND rd.name = ANY(%s)
    ORDER BY o.name
"""  # noqa: S608 static literal, only %s placeholders are parameterized


def fetch_member_organizations(username: str, db_connection: Any) -> list[dict[str, Any]]:
    """
    Return organizations (``[{"id": ..., "name": ...}]``) the given username is a member
    or admin of, read live from the gateway database.

    Raises the underlying exception after logging on failure (e.g. gateway DB unreachable).
    """
    try:
        with db_connection.cursor() as cursor:
            cursor.execute(_MEMBER_ORGANIZATIONS_QUERY, [username, list(_MEMBER_ROLE_NAMES)])
            rows = cursor.fetchall()
    except Exception:
        logger.exception("Error fetching organization membership from gateway database")
        raise
    return [{"id": row[0], "name": row[1]} for row in rows]
