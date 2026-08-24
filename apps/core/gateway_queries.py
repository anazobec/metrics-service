"""
Gateway API query helpers.

Reads organization-membership RBAC data live from the gateway's cross-service
``/api/gateway/v1/service-index/role-user-assignments/`` API instead of relying
on metrics-service's local gateway-resource-sync copy (``sync_resources_from_gateway``
/ ``docs/core-rbac.md``), which is only refreshed periodically. Reading the
gateway's own RBAC assignments directly avoids that sync lag for data that needs
to be current, e.g. "am I a member of this organization right now" (AAP-88670).

We authenticate to that API the same way DAB's own resource-sync task does: via
``ResourceAPIClient`` / ``RESOURCE_SERVER`` service-to-service JWT auth, not the
gateway database (metrics-service has no direct DB access to the gateway).

Role assignments come back referencing organizations by ``object_ansible_id``
(the org's shared-resource ID, common across gateway/AWX/metrics-service), which
is resolved to a locally-synced ``core.Organization`` for its name/id, since the
(relatively static) set of organizations themselves is not the data at risk of
sync lag -- only "is this user currently a member" is.
"""

import logging
from typing import Any

from ansible_base.rbac.models import RoleDefinition
from ansible_base.resource_registry.rest_client import get_resource_server_client
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

logger = logging.getLogger(__name__)


def _get_resource(model_cls, pk) -> Any:
    """Look up the ``Resource`` row (with its ``ansible_id``) for a given model instance's pk."""
    from ansible_base.resource_registry.models import Resource

    content_type = ContentType.objects.get_for_model(model_cls)
    return Resource.objects.get(content_type=content_type, object_id=str(pk))


def _get_member_role_names() -> set[str]:
    """
    Role definition names that confer organization membership on the gateway.

    Looked up by the ``member_organization`` permission (rather than hardcoded
    managed-role names) so custom roles granting that permission are also
    honored. Queried lazily (not at import time) so a DB connection isn't
    required on module import, and so newly-added custom roles are picked up
    without a service restart.
    """
    return set(
        RoleDefinition.objects.filter(permissions__codename="member_organization").values_list("name", flat=True)
    )


# api_slug of the shared Organization content type, as returned in the
# role-user-assignments response's `content_type` field (see DABContentType.api_slug).
_ORGANIZATION_CONTENT_TYPE = "shared.organization"


def _list_all_user_assignments(client, user_ansible_id: str) -> list[dict[str, Any]]:
    """Page through every role-user-assignment for the given gateway user ansible_id."""
    assignments: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.list_user_assignments(user_ansible_id=user_ansible_id, filters={"page": page})
        response.raise_for_status()
        body = response.json()
        assignments.extend(body.get("results", []))
        if not body.get("next"):
            break
        page += 1
    return assignments


def fetch_member_organizations(username: str) -> list[dict[str, Any]]:
    """
    Return organizations (``[{"id": ..., "name": ...}]``) the given username is a member
    or admin of, read live from the gateway's role-user-assignments API.

    Raises the underlying exception after logging on failure (e.g. gateway API
    unreachable, or the user has no synced ``resource`` record yet).
    """
    from apps.core.models import Organization, User

    try:
        user = User.objects.get(username=username)
        user_ansible_id = str(_get_resource(User, user.pk).ansible_id)

        client = get_resource_server_client(
            service_path=settings.RESOURCE_SERVICE_PATH,
            raise_if_bad_request=False,
        )
        assignments = _list_all_user_assignments(client, user_ansible_id)
    except Exception:
        logger.exception("Error fetching organization membership from gateway API")
        raise

    member_role_names = _get_member_role_names()
    org_ansible_ids = {
        assignment["object_ansible_id"]
        for assignment in assignments
        if assignment.get("content_type") == _ORGANIZATION_CONTENT_TYPE
        and assignment.get("role_definition") in member_role_names
    }
    if not org_ansible_ids:
        return []

    from ansible_base.resource_registry.models import Resource

    content_type = ContentType.objects.get_for_model(Organization)
    org_pks = [
        int(object_id)
        for object_id in Resource.objects.filter(content_type=content_type, ansible_id__in=org_ansible_ids).values_list(
            "object_id", flat=True
        )
    ]
    organizations = Organization.objects.filter(pk__in=org_pks).values("id", "name")
    return list(organizations)
