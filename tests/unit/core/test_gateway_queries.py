from unittest.mock import MagicMock, patch

import pytest
from django.contrib.contenttypes.models import ContentType

from apps.core.gateway_queries import _ORGANIZATION_CONTENT_TYPE, fetch_member_organizations
from apps.core.models import Organization, User

_MEMBER_ROLE_NAMES = ("Organization Member", "Organization Admin")


def _assignment(content_type, role_definition, object_ansible_id):
    return {
        "content_type": content_type,
        "role_definition": role_definition,
        "object_ansible_id": object_ansible_id,
    }


def _mock_response(results, next_page=None):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"results": results, "next": next_page}
    return response


def _make_resource(instance, ansible_id):
    from ansible_base.resource_registry.models import Resource

    content_type = ContentType.objects.get_for_model(type(instance))
    resource, _created = Resource.objects.get_or_create(content_type=content_type, object_id=str(instance.pk))
    resource.ansible_id = ansible_id
    resource.save()
    return resource


@pytest.mark.unit
@pytest.mark.django_db
class TestFetchMemberOrganizations:
    """Unit tests for fetch_member_organizations (gateway API query helper)."""

    def test_returns_orgs_for_member_and_admin_roles(self):
        user = User.objects.create(username="testuser")
        _make_resource(user, "11111111-1111-1111-1111-111111111111")
        org_a = Organization.objects.create(name="Org A")
        _make_resource(org_a, "aaaaaaaa-0000-0000-0000-000000000001")
        org_b = Organization.objects.create(name="Org B")
        _make_resource(org_b, "bbbbbbbb-0000-0000-0000-000000000002")

        assignments = [
            _assignment(_ORGANIZATION_CONTENT_TYPE, _MEMBER_ROLE_NAMES[0], "aaaaaaaa-0000-0000-0000-000000000001"),
            _assignment(_ORGANIZATION_CONTENT_TYPE, _MEMBER_ROLE_NAMES[1], "bbbbbbbb-0000-0000-0000-000000000002"),
            _assignment("shared.team", "Team Member", "cccccccc-0000-0000-0000-000000000003"),  # not an org assignment
        ]

        with (
            patch("apps.core.gateway_queries.get_resource_server_client") as mock_get_client,
            patch("apps.core.gateway_queries._get_member_role_names", return_value=set(_MEMBER_ROLE_NAMES)),
        ):
            mock_client = MagicMock()
            mock_client.list_user_assignments.return_value = _mock_response(assignments)
            mock_get_client.return_value = mock_client

            result = fetch_member_organizations("testuser")

        assert result == [{"id": org_a.id, "name": "Org A"}, {"id": org_b.id, "name": "Org B"}]

    def test_paginates_through_all_assignments(self):
        user = User.objects.create(username="testuser")
        _make_resource(user, "11111111-1111-1111-1111-111111111111")
        org_a = Organization.objects.create(name="Org A")
        _make_resource(org_a, "aaaaaaaa-0000-0000-0000-000000000001")
        org_b = Organization.objects.create(name="Org B")
        _make_resource(org_b, "bbbbbbbb-0000-0000-0000-000000000002")

        page1 = _mock_response(
            [_assignment(_ORGANIZATION_CONTENT_TYPE, _MEMBER_ROLE_NAMES[0], "aaaaaaaa-0000-0000-0000-000000000001")],
            next_page="page2",
        )
        page2 = _mock_response(
            [_assignment(_ORGANIZATION_CONTENT_TYPE, _MEMBER_ROLE_NAMES[0], "bbbbbbbb-0000-0000-0000-000000000002")]
        )

        with (
            patch("apps.core.gateway_queries.get_resource_server_client") as mock_get_client,
            patch("apps.core.gateway_queries._get_member_role_names", return_value=set(_MEMBER_ROLE_NAMES)),
        ):
            mock_client = MagicMock()
            mock_client.list_user_assignments.side_effect = [page1, page2]
            mock_get_client.return_value = mock_client

            result = fetch_member_organizations("testuser")

        assert mock_client.list_user_assignments.call_count == 2
        assert result == [{"id": org_a.id, "name": "Org A"}, {"id": org_b.id, "name": "Org B"}]

    def test_returns_empty_list_when_no_assignments(self):
        user = User.objects.create(username="testuser")
        _make_resource(user, "11111111-1111-1111-1111-111111111111")

        with patch("apps.core.gateway_queries.get_resource_server_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_user_assignments.return_value = _mock_response([])
            mock_get_client.return_value = mock_client

            assert fetch_member_organizations("testuser") == []

    def test_logs_and_reraises_on_api_error(self):
        user = User.objects.create(username="testuser")
        _make_resource(user, "11111111-1111-1111-1111-111111111111")

        with (
            patch("apps.core.gateway_queries.get_resource_server_client", side_effect=ConnectionError("boom")),
            patch("apps.core.gateway_queries.logger") as mock_logger,
            pytest.raises(ConnectionError),
        ):
            fetch_member_organizations("testuser")

        mock_logger.exception.assert_called_once()
