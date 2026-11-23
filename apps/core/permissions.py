"""Project-level DRF permission classes built on top of DAB's RBAC permissions."""

from typing import Any

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from ansible_base.rbac.evaluations import has_super_permission
from rest_framework.request import Request


class IsSystemAdminOrAuditorReadQuery(IsSystemAdminOrAuditor):
    """
    Treats the view's read-only query actions (POST with a filter body) as reads.

    DAB's IsSystemAdminOrAuditor decides by HTTP method: platform auditors are allowed only on
    safe methods, so they get 403 on any POST. Some read endpoints accept their filters in a
    POST body (to avoid URL length limits), which would lock auditors out of them.

    A view opts in by listing such actions in ``read_only_query_actions``. For those actions
    the view permission is required (system admin or platform auditor); every other action
    keeps the IsSystemAdminOrAuditor behaviour, so writes stay admin-only.

    Actions listed in ``read_only_query_actions`` MUST be free of side effects.
    """

    def has_permission(self, request: Request, view: Any) -> bool:
        """Grant view permission for the view's read-only query actions, defer to the parent otherwise."""
        if not (request.user and request.user.is_authenticated):
            return False
        if getattr(view, "action", None) in getattr(view, "read_only_query_actions", ()):
            return has_super_permission(request.user, "view")
        return super().has_permission(request, view)
