"""Integration tests for the dashboard collection status endpoint
GET/POST /api/v1/dashboard_reports/collection_status/."""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve
from rest_framework.test import APIClient

from apps.dynamic_settings.models import Setting
from tests.test_utils import get_test_password

User = get_user_model()

COLLECTION_STATUS_ENDPOINT = "/api/v1/dashboard_reports/collection_status/"


@pytest.mark.integration
class TestCollectionStatusEndpoint(TestCase):
    """Integration tests for the /collection_status/ endpoint (GET + POST)."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            username="collection_status_admin",
            email="collection_status_admin@example.com",
            password=get_test_password(),
        )
        self.regular_user = User.objects.create_user(
            username="collection_status_user",
            email="collection_status_user@example.com",
            password=get_test_password(),
        )

    def tearDown(self):
        Setting.objects.filter(setting_key="SHOW_LEADERBOARD").delete()
        super().tearDown()

    def test_endpoint_resolves(self):
        """The URL /api/v1/dashboard_reports/collection_status/ resolves correctly."""
        match = resolve(COLLECTION_STATUS_ENDPOINT)
        assert match is not None

    def test_get_returns_200(self):
        """Authenticated request returns HTTP 200."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 200

    def test_get_unauthenticated_returns_403(self):
        """Unauthenticated GET requests are rejected."""
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.status_code == 403

    def test_get_default_show_leaderboard_true(self):
        """With no Setting row present, show_leaderboard defaults to True."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert response.json()["show_leaderboard"] is True

    def test_post_as_admin_sets_flag_true(self):
        """Admin POST with show_leaderboard=True persists a Setting row and is reflected in GET."""
        self.client.force_authenticate(user=self.admin)

        post_response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert post_response.status_code == 200
        assert post_response.json() == {"show_leaderboard": True}

        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is True
        assert setting.last_modified_by == self.admin

        get_response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert get_response.json()["show_leaderboard"] is True

    def test_post_as_admin_sets_flag_false(self):
        """Admin POST with show_leaderboard=False persists a Setting row and is reflected in GET."""
        self.client.force_authenticate(user=self.admin)
        Setting.objects.create(
            setting_key="SHOW_LEADERBOARD", current_value=json.dumps(True), last_modified_by=self.admin
        )

        post_response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": False}, format="json")
        assert post_response.status_code == 200
        assert post_response.json() == {"show_leaderboard": False}

        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is False

        get_response = self.client.get(COLLECTION_STATUS_ENDPOINT)
        assert get_response.json()["show_leaderboard"] is False

    def test_post_updates_existing_row_not_duplicated(self):
        """A second POST updates the same Setting row instead of creating a new one."""
        self.client.force_authenticate(user=self.admin)

        self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": False}, format="json")

        assert Setting.objects.filter(setting_key="SHOW_LEADERBOARD").count() == 1
        setting = Setting.objects.get(setting_key="SHOW_LEADERBOARD")
        assert json.loads(setting.current_value) is False

    def test_post_as_regular_user_forbidden(self):
        """Non-admin/auditor users cannot toggle show_leaderboard."""
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert response.status_code == 403
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()

    def test_post_unauthenticated_forbidden(self):
        """Unauthenticated POST requests are rejected."""
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": True}, format="json")
        assert response.status_code == 403
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()

    def test_post_non_boolean_value_returns_400(self):
        """A non-boolean show_leaderboard value is rejected with 400."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {"show_leaderboard": "yes"}, format="json")
        assert response.status_code == 400
        assert not Setting.objects.filter(setting_key="SHOW_LEADERBOARD").exists()

    def test_post_missing_value_returns_400(self):
        """A POST body without show_leaderboard is rejected with 400."""
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(COLLECTION_STATUS_ENDPOINT, {}, format="json")
        assert response.status_code == 400
