"""
DB-backed tests for TemplateMetadataViewSet.

These tests verify the full round-trip: the viewset actually persists changes
and reverts to system defaults correctly. No mocking of ORM calls.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import User
from apps.dashboard_reports.models import TemplateMetadata
from tests.test_utils import get_test_password


def _create_metadata(
    template_id: int = 42,
    template_name: str = "Test Template",
    time_taken_manually_execute_minutes: int | None = 30,
    time_taken_create_automation_minutes: int | None = 60,
) -> TemplateMetadata:
    return TemplateMetadata.objects.create(
        template_id=template_id,
        template_name=template_name,
        time_taken_manually_execute_minutes=time_taken_manually_execute_minutes,
        time_taken_create_automation_minutes=time_taken_create_automation_minutes,
    )


def _url(pk: int) -> str:
    return f"/api/v1/dashboard_reports/template_metadata/{pk}/"


@pytest.mark.integration
@pytest.mark.django_db
class TestTemplateMetadataPutPatchDb(TestCase):
    """Verify PUT actually persists updated values to the DB."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="admin", email="admin@example.com", password=get_test_password(), is_superuser=True
        )
        self.client.force_authenticate(user=self.user)
        self.instance = _create_metadata()

    def test_put_updates_time_fields_in_db(self):
        response = self.client.put(
            _url(self.instance.pk),
            data={
                "time_taken_manually_execute_minutes": 99,
                "time_taken_create_automation_minutes": 199,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

        self.instance.refresh_from_db()
        assert self.instance.time_taken_manually_execute_minutes == 99
        assert self.instance.time_taken_create_automation_minutes == 199

    def test_put_response_reflects_updated_values(self):
        response = self.client.put(
            _url(self.instance.pk),
            data={
                "time_taken_manually_execute_minutes": 55,
                "time_taken_create_automation_minutes": 110,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["time_taken_manually_execute_minutes"] == 55
        assert response.data["time_taken_create_automation_minutes"] == 110

    def test_put_with_null_sets_fields_to_null_in_db(self):
        response = self.client.put(
            _url(self.instance.pk),
            data={
                "time_taken_manually_execute_minutes": None,
                "time_taken_create_automation_minutes": None,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

        self.instance.refresh_from_db()
        assert self.instance.time_taken_manually_execute_minutes is None
        assert self.instance.time_taken_create_automation_minutes is None

    def test_put_does_not_change_template_id(self):
        original_template_id = self.instance.template_id

        response = self.client.put(
            _url(self.instance.pk),
            data={
                "template_id": 999,
                "time_taken_manually_execute_minutes": 10,
                "time_taken_create_automation_minutes": 20,
            },
            format="json",
        )
        assert response.status_code == 200

        self.instance.refresh_from_db()
        assert self.instance.template_id == original_template_id

    def test_put_returns_404_for_nonexistent_pk(self):
        response = self.client.put(
            _url(99999),
            data={
                "time_taken_manually_execute_minutes": 10,
                "time_taken_create_automation_minutes": 20,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_patch_updates_only_provided_field(self):
        response = self.client.patch(
            _url(self.instance.pk),
            data={
                "time_taken_manually_execute_minutes": 77,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK

        self.instance.refresh_from_db()
        assert self.instance.time_taken_manually_execute_minutes == 77
        assert self.instance.time_taken_create_automation_minutes == 60  # unchanged


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_concurrent_template_metadata_creation_recovers_winner():
    """A concurrent insert with the same AWX ID returns the row created by the winner."""
    name = "Concurrent Template"
    awx_id = 901001
    TemplateMetadata.objects.filter(template_id=awx_id, template_name=name).delete()
    lookup_barrier = Barrier(2)
    create_barrier = Barrier(2)

    def synchronized_lookup(_awx_id: int) -> None:
        lookup_barrier.wait(timeout=10)

    real_create_with_race_handling = TemplateMetadata._create_with_race_handling

    def synchronized_create(name: str, awx_id: int | None) -> TemplateMetadata:
        create_barrier.wait(timeout=10)
        return real_create_with_race_handling(name, awx_id)

    def create_metadata() -> TemplateMetadata:
        close_old_connections()
        try:
            return TemplateMetadata.get_by_awx_id_or_name(name, awx_id=awx_id)
        finally:
            close_old_connections()

    with (
        patch.object(TemplateMetadata, "_lookup_by_id", side_effect=synchronized_lookup),
        patch.object(TemplateMetadata, "_create_with_race_handling", side_effect=synchronized_create),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        instances = list(executor.map(lambda _worker: create_metadata(), range(2)))

    assert instances[0].pk == instances[1].pk
    assert TemplateMetadata.objects.filter(template_id=awx_id).count() == 1
