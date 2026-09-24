import uuid
from unittest.mock import patch

import pytest
from ansible_base.rbac.models import DABContentType, RoleDefinition
from django.test.utils import override_settings
from rest_framework.test import APIClient

from apps.core.models import Organization


def make_user_data():
    unique_id = uuid.uuid4().hex[:8]
    return {"username": f"newuser-{unique_id}", "password": f"password-{unique_id}"}


@pytest.mark.django_db
class TestSuperuserAccess:
    """Superuser should have full CRUD on all resources."""

    def test_superuser_can_create_user(self, admin_api_client):
        """Superusers can create users."""
        data = make_user_data()
        r = admin_api_client.post("/api/v1/users/", data)
        assert r.status_code == 201

    def test_superuser_cannot_create_organization(self, admin_api_client):
        """POST to /organizations/ returns 405 — orgs are synced from the gateway, not created here.

        Organizations are managed by the AAP resource server (is_provider=False in resource_api.py).
        Creating them directly causes a DAB signal crash when a resource server is configured (AAP-74775).
        """
        r = admin_api_client.post("/api/v1/organizations/", {"name": "New Org"})
        assert r.status_code == 405

    def test_superuser_can_create_team(self, admin_api_client, organization):
        r = admin_api_client.post("/api/v1/teams/", {"name": "New Team", "organization": organization.id})
        assert r.status_code == 201

    def test_superuser_can_delete_user(self, admin_api_client, rando):
        r = admin_api_client.delete(f"/api/v1/users/{rando.id}/")
        assert r.status_code == 204

    def test_superuser_me_does_not_report_implicit_org_memberships(self, admin_api_client, organization):
        r = admin_api_client.get("/api/v1/users/me/")
        assert r.status_code == 200
        assert r.data["member_of_organizations"] == []


@pytest.mark.django_db
class TestNormalUserAccess:
    """Normal users without roles should have no access."""

    def test_cannot_create_user(self, user_api_client):
        """Normal users cannot create users (403 Forbidden)."""
        data = make_user_data()
        r = user_api_client.post("/api/v1/users/", data)
        assert r.status_code == 403

    def test_cannot_create_organization(self, user_api_client):
        """Normal users are denied before reaching the 405 method guard (RBAC fires first)."""
        r = user_api_client.post("/api/v1/organizations/", {"name": "New Org"})
        assert r.status_code == 403

    def test_cannot_create_team(self, user_api_client, organization):
        r = user_api_client.post("/api/v1/teams/", {"name": "Team", "organization": organization.id})
        assert r.status_code == 403

    @pytest.mark.parametrize("endpoint", ["/api/v1/organizations/", "/api/v1/teams/"])
    def test_sees_empty_list(self, user_api_client, organization, team, endpoint):
        r = user_api_client.get(endpoint)
        assert r.status_code == 200
        results = r.data["results"] if isinstance(r.data, dict) else r.data
        assert results == []

    def test_can_access_me_endpoint(self, user_api_client, rando):
        with patch("ansible_base.resource_registry.rest_client.get_resource_server_client") as get_client:
            r = user_api_client.get("/api/v1/users/me/")
        assert r.status_code == 200
        assert r.data["username"] == rando.username
        assert r.data["member_of_organizations"] == []
        get_client.assert_not_called()


@pytest.mark.django_db
class TestOrgAdminAccess:
    """Org Admin can manage their org and create teams in it."""

    def test_can_see_organization(self, user_api_client, rando, organization, org_admin_rd):
        org_admin_rd.give_permission(rando, organization)
        r = user_api_client.get("/api/v1/organizations/")
        assert r.status_code == 200
        results = r.data["results"] if isinstance(r.data, dict) else r.data
        assert len(results) == 1

    def test_me_returns_locally_assigned_member_and_admin_organizations(
        self, user_api_client, rando, organization, org_admin_rd, org_member_rd
    ):
        second_organization = Organization.objects.create(name="Second Org")
        third_organization = Organization.objects.create(name="Third Org")
        org_admin_rd.give_permission(rando, organization)
        org_member_rd.give_permission(rando, second_organization)
        with override_settings(ALLOW_SHARED_RESOURCE_CUSTOM_ROLES=True):
            custom_member_rd = RoleDefinition.objects.create_from_permissions(
                name="Custom Organization Membership",
                permissions=["member_organization", "view_organization"],
                content_type=DABContentType.objects.get_for_model(Organization),
            )
            custom_member_rd.give_permission(rando, third_organization)

        r = user_api_client.get("/api/v1/users/me/")

        assert r.status_code == 200
        assert r.data["member_of_organizations"] == [
            {"id": organization.id, "name": organization.name},
            {"id": second_organization.id, "name": second_organization.name},
            {"id": third_organization.id, "name": third_organization.name},
        ]

    def test_can_update_organization(self, user_api_client, rando, organization, org_admin_rd):
        org_admin_rd.give_permission(rando, organization)
        r = user_api_client.patch(f"/api/v1/organizations/{organization.id}/", {"description": "Updated"})
        assert r.status_code == 200

    def test_cannot_create_organization(self, user_api_client, rando, organization, org_admin_rd):
        """Org admins cannot create organizations — RBAC denies (403) before the method guard fires."""
        org_admin_rd.give_permission(rando, organization)
        r = user_api_client.post("/api/v1/organizations/", {"name": "Another Org"})
        assert r.status_code == 403

    def test_can_create_team_in_org(self, user_api_client, rando, organization, org_admin_rd):
        org_admin_rd.give_permission(rando, organization)
        r = user_api_client.post("/api/v1/teams/", {"name": "New Team", "organization": organization.id})
        assert r.status_code == 201


@pytest.mark.django_db
class TestTeamRoleAccess:
    """Team Admin vs Team Member permissions."""

    def test_team_admin_can_update_team(self, user_api_client, rando, team, team_admin_rd):
        team_admin_rd.give_permission(rando, team)
        r = user_api_client.patch(f"/api/v1/teams/{team.id}/", {"description": "Updated"})
        assert r.status_code == 200

    def test_team_member_cannot_update_team(self, user_api_client, rando, team, team_member_rd):
        team_member_rd.give_permission(rando, team)
        r = user_api_client.patch(f"/api/v1/teams/{team.id}/", {"description": "Fail"})
        assert r.status_code == 403

    def test_team_member_can_see_team(self, user_api_client, rando, team, team_member_rd):
        team_member_rd.give_permission(rando, team)
        r = user_api_client.get("/api/v1/teams/")
        assert r.status_code == 200
        results = r.data["results"] if isinstance(r.data, dict) else r.data
        assert len(results) == 1


# ---------------------------------------------------------------------------
# AAP-74790: activitystream, feature_flags_state, and users RBAC enforcement
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_auditor(db, rando):
    """Assign the Platform Auditor global role to rando and return the user."""
    auditor_rd = RoleDefinition.objects.get(name="Platform Auditor")
    auditor_rd.give_global_permission(rando)
    return rando


@pytest.fixture
def auditor_api_client(platform_auditor):
    """API client authenticated as a Platform Auditor."""
    client = APIClient()
    client.force_authenticate(user=platform_auditor)
    return client


@pytest.mark.django_db
class TestUsersEndpointRBAC:
    """Users list/retrieve must require at least Platform Auditor (AAP-74790)."""

    def test_basic_user_cannot_list_users(self, user_api_client):
        r = user_api_client.get("/api/v1/users/")
        assert r.status_code == 403

    def test_platform_auditor_can_list_users(self, auditor_api_client):
        r = auditor_api_client.get("/api/v1/users/")
        assert r.status_code == 200

    def test_platform_auditor_global_role_does_not_report_org_memberships(
        self, user_api_client, platform_auditor, organization
    ):
        r = user_api_client.get("/api/v1/users/me/")
        assert r.status_code == 200
        assert r.data["member_of_organizations"] == []

    def test_superuser_can_list_users(self, admin_api_client):
        r = admin_api_client.get("/api/v1/users/")
        assert r.status_code == 200

    def test_basic_user_can_access_me_endpoint(self, user_api_client, rando):
        """The /me action must remain accessible to any authenticated user."""
        r = user_api_client.get("/api/v1/users/me/")
        assert r.status_code == 200
        assert r.data["username"] == rando.username

    def test_unauthenticated_user_cannot_list_users(self):
        client = APIClient()
        r = client.get("/api/v1/users/")
        assert r.status_code in (401, 403)


@pytest.mark.django_db
class TestActivityStreamRBAC:
    """Activity stream must require at least Platform Auditor (AAP-74790)."""

    def test_basic_user_cannot_access_activitystream(self, user_api_client):
        r = user_api_client.get("/api/v1/activitystream/")
        assert r.status_code == 403

    def test_platform_auditor_can_access_activitystream(self, auditor_api_client):
        r = auditor_api_client.get("/api/v1/activitystream/")
        assert r.status_code == 200

    def test_superuser_can_access_activitystream(self, admin_api_client):
        r = admin_api_client.get("/api/v1/activitystream/")
        assert r.status_code == 200

    def test_unauthenticated_user_cannot_access_activitystream(self):
        client = APIClient()
        r = client.get("/api/v1/activitystream/")
        assert r.status_code in (401, 403)


@pytest.mark.django_db
class TestFeatureFlagsStateRBAC:
    """Old feature_flags_state endpoint must require at least Platform Auditor (AAP-74790)."""

    def test_basic_user_cannot_access_feature_flags_state(self, user_api_client):
        r = user_api_client.get("/api/v1/feature_flags_state/")
        assert r.status_code == 403

    def test_platform_auditor_can_access_feature_flags_state(self, auditor_api_client):
        r = auditor_api_client.get("/api/v1/feature_flags_state/")
        assert r.status_code == 200

    def test_superuser_can_access_feature_flags_state(self, admin_api_client):
        r = admin_api_client.get("/api/v1/feature_flags_state/")
        assert r.status_code == 200

    def test_unauthenticated_user_cannot_access_feature_flags_state(self):
        client = APIClient()
        r = client.get("/api/v1/feature_flags_state/")
        assert r.status_code in (401, 403)
