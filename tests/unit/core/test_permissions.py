"""Unit tests for apps.core.permissions.IsSystemAdminOrAuditorReadQuery."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIRequestFactory

from apps.core.permissions import IsSystemAdminOrAuditorReadQuery
from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

QUERY_ACTIONS = frozenset({"query", "details_query", "export_query"})
WRITE_METHODS = ("post", "put", "patch", "delete")


def _has_permission(user, method: str, action: str | None, read_only_query_actions=QUERY_ACTIONS) -> bool:
    """Evaluate the permission for ``user`` doing ``method`` on a view running ``action``."""
    request = getattr(APIRequestFactory(), method)("/")
    request.user = user
    view = SimpleNamespace(action=action)
    if read_only_query_actions is not None:
        view.read_only_query_actions = read_only_query_actions
    return IsSystemAdminOrAuditorReadQuery().has_permission(request, view)


@pytest.mark.unit
@pytest.mark.django_db
class TestIsSystemAdminOrAuditorReadQuery:
    """Read-only query actions are treated as reads; every other action keeps IsSystemAdminOrAuditor behaviour."""

    @pytest.mark.parametrize("action", sorted(QUERY_ACTIONS))
    def test_platform_auditor_allowed_on_query_actions(self, platform_auditor, action):
        assert _has_permission(platform_auditor, "post", action) is True

    @pytest.mark.parametrize("method", WRITE_METHODS)
    def test_platform_auditor_forbidden_on_other_unsafe_actions(self, platform_auditor, method):
        assert _has_permission(platform_auditor, method, "create") is False

    def test_platform_auditor_allowed_on_safe_methods(self, platform_auditor):
        assert _has_permission(platform_auditor, "get", "list") is True

    @pytest.mark.parametrize("action", sorted(QUERY_ACTIONS))
    def test_admin_allowed_on_query_actions(self, admin_user, action):
        assert _has_permission(admin_user, "post", action) is True

    @pytest.mark.parametrize("method", WRITE_METHODS)
    def test_admin_allowed_on_other_unsafe_actions(self, admin_user, method):
        assert _has_permission(admin_user, method, "create") is True

    @pytest.mark.parametrize("action", sorted(QUERY_ACTIONS))
    def test_regular_user_forbidden_on_query_actions(self, rando, action):
        assert _has_permission(rando, "post", action) is False

    @pytest.mark.parametrize("method", ("get", *WRITE_METHODS))
    def test_regular_user_forbidden_on_other_actions(self, rando, method):
        assert _has_permission(rando, method, "list") is False

    @pytest.mark.parametrize("action", ["query", "list"])
    def test_unauthenticated_forbidden(self, action):
        assert _has_permission(AnonymousUser(), "post", action) is False
        assert _has_permission(AnonymousUser(), "get", action) is False

    def test_view_without_query_actions_keeps_parent_behaviour(self, platform_auditor):
        """A view that does not declare read_only_query_actions gets plain IsSystemAdminOrAuditor."""
        assert _has_permission(platform_auditor, "post", "query", read_only_query_actions=None) is False
        assert _has_permission(platform_auditor, "get", "list", read_only_query_actions=None) is True

    def test_view_without_action_keeps_parent_behaviour(self, platform_auditor):
        """A POST without a resolved action (e.g. an unrouted method) is not treated as a read."""
        assert _has_permission(platform_auditor, "post", None) is False


@pytest.mark.unit
class TestDashboardReportViewSetPermissionConfig:
    """DashboardReportViewSet uses the read-query permission for exactly its POST query actions."""

    def test_permission_classes(self):
        assert DashboardReportViewSet.permission_classes == [IsSystemAdminOrAuditorReadQuery]

    def test_read_only_query_actions(self):
        assert DashboardReportViewSet.read_only_query_actions == QUERY_ACTIONS
