"""Unit tests for Dashboard Reports model."""

import datetime
import decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.dashboard_reports.models import (
    DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES,
    FilterSet,
    JobData,
    JobHostSummary,
    JobLabel,
    JobStatusChoices,
    SubscriptionCost,
    TemplateMetadata,
)

User = get_user_model()


@pytest.fixture
def user():
    """Fixture to create a test user."""
    return User.objects.create_user(username="testuser", email="test@example.com", password="testpassword123")  # noqa: S105


@pytest.fixture
def template_metadata():
    """Fixture to create a TemplateMetadata instance."""
    return TemplateMetadata.objects.create(
        template_id=1,
        template_name="Template 1",
        time_taken_manually_execute_minutes=60,
        time_taken_create_automation_minutes=20,
    )


@pytest.fixture
def job_data(template_metadata):
    """Fixture to create a JobData instance."""
    return JobData.objects.create(
        job_id=1,
        template_name="Template 1",
        template_id=1,
        project_id=1,
        project_name="Project 1",
        organization_id=1,
        status="successful",
        started=datetime.datetime.fromisoformat("2024-01-01T00:00:00Z"),
        finished=datetime.datetime.fromisoformat("2024-01-01T00:02:00Z"),
        elapsed=120.5,
        num_hosts=5,
        launched_by_id=1,
        launched_by_username="testuser",
        template_metadata=template_metadata,
    )


@pytest.mark.unit
@pytest.mark.django_db
class TestSubscriptionCost:
    """Test cases for SubscriptionCost model."""

    def test_subscription_cost_get_cost(self):
        """Test get_cost method of SubscriptionCost."""
        subscription_cost = SubscriptionCost.get()
        assert subscription_cost.monthly_subscription_cost == decimal.Decimal("5000.00")
        assert subscription_cost.engineer_avg_hourly_rate == decimal.Decimal("60.00")
        assert subscription_cost.include_template_creation_time_in_costs is True

    def test_subscription_cost_creation(self):
        """Test subscription cost can be created successfully."""
        subscription_cost = SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=15.00,
            include_template_creation_time_in_costs=False,
        )
        assert subscription_cost.monthly_subscription_cost == decimal.Decimal("100.00")
        assert subscription_cost.engineer_avg_hourly_rate == decimal.Decimal("15.00")
        assert subscription_cost.include_template_creation_time_in_costs is False

    def test_subscription_cost_string_representation(self):
        """Test subscription cost string representation."""
        subscription_cost = SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=15.00,
            include_template_creation_time_in_costs=False,
        )
        expected_str = f"SubscriptionCost: Monthly={subscription_cost.monthly_subscription_cost}, Engineer Hourly Rate={subscription_cost.engineer_avg_hourly_rate}"
        assert str(subscription_cost) == expected_str

    def test_subscription_cost_singleton_behavior(self):
        """Test that SubscriptionCost behaves like a singleton."""
        subscription_cost1 = SubscriptionCost.get()
        subscription_cost2 = SubscriptionCost.get()
        assert subscription_cost1 == subscription_cost2
        assert SubscriptionCost.objects.count() == 1
        SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=15.00,
            include_template_creation_time_in_costs=False,
        )
        assert SubscriptionCost.objects.count() == 1
        subscription_cost3 = SubscriptionCost.get()
        assert subscription_cost3.monthly_subscription_cost == decimal.Decimal("100.00")
        assert subscription_cost3.engineer_avg_hourly_rate == decimal.Decimal("15.00")
        assert subscription_cost3.include_template_creation_time_in_costs is False

    def test_cost_employee_per_minute_standard(self):
        """Test cost_employee_per_minute property with standard hourly rate."""
        subscription_cost = SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=60.00,
            include_template_creation_time_in_costs=True,
        )
        result = subscription_cost.cost_employee_per_minute
        assert isinstance(result, decimal.Decimal)
        assert result == decimal.Decimal("1.00")

    def test_cost_employee_per_minute_zero(self):
        """Test cost_employee_per_minute property with zero hourly rate."""
        subscription_cost = SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=0.00,
            include_template_creation_time_in_costs=True,
        )
        result = subscription_cost.cost_employee_per_minute
        assert result == decimal.Decimal("0.00")

    def test_cost_employee_per_minute_decimal_precision(self):
        """Test cost_employee_per_minute property with decimal precision."""
        subscription_cost = SubscriptionCost.objects.create(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=61.23,
            include_template_creation_time_in_costs=True,
        )
        result = subscription_cost.cost_employee_per_minute
        # The model stores engineer_avg_hourly_rate rounded to two decimal places
        assert result == decimal.Decimal("1.0205")

    def test_cost_employee_per_minute_validator(self):
        """Test cost_employee_per_minute property does not allow negative hourly rate."""
        subscription_cost = SubscriptionCost(
            monthly_subscription_cost=100.00,
            engineer_avg_hourly_rate=-10.00,
            include_template_creation_time_in_costs=True,
        )
        with pytest.raises(ValidationError):
            subscription_cost.full_clean()


@pytest.mark.unit
@pytest.mark.django_db
class TestFilterSet:
    """Test cases for FilterSet model."""

    def test_filter_set_creation(self, user):
        """Test filter set can be created successfully."""
        filter_set = FilterSet.objects.create(
            name="Test Filter Set", filters={"key": "value"}, user=user, is_default=True
        )
        assert filter_set.name == "Test Filter Set"
        assert filter_set.filters == {"key": "value"}
        assert filter_set.user == user
        assert filter_set.is_default is True

    def test_filter_set_string_representation(self, user):
        """Test filter set string representation."""
        filter_set = FilterSet.objects.create(
            name="Test Filter Set", filters={"key": "value"}, user=user, is_default=True
        )
        assert str(filter_set) == "Test Filter Set"

    def test_filter_set_default_behavior(self, user):
        """Test that only one default FilterSet exists per user."""
        FilterSet.objects.create(name="Default Filter Set", filters={"key": "value"}, user=user, is_default=True)
        with pytest.raises(IntegrityError):
            FilterSet.objects.create(
                name="Another Default Filter Set", filters={"key": "value"}, user=user, is_default=True
            )


@pytest.mark.unit
@pytest.mark.django_db
class TestTemplateMetadata:
    """Test cases for TemplateMetadata model."""

    def test_template_metadata_creation(self, template_metadata):
        """Test template metadata can be created successfully."""
        assert template_metadata.template_id == 1
        assert template_metadata.template_name == "Template 1"
        assert template_metadata.time_taken_manually_execute_minutes == 60
        assert template_metadata.time_taken_create_automation_minutes == 20

    def test_template_metadata_string_representation(self, template_metadata):
        """Test template metadata string representation."""
        expected_str = f"Metadata for {template_metadata.template_name} (ID: {template_metadata.template_id})"
        assert str(template_metadata) == expected_str

    def test_template_metadata_unique_template_id(self, template_metadata):
        """Test that template_id is unique."""
        with pytest.raises(IntegrityError):
            TemplateMetadata.objects.create(
                template_id=1,
                template_name="Duplicate Template",
                time_taken_manually_execute_minutes=30,
                time_taken_create_automation_minutes=10,
            )

    def test_template_metadata_update_time_fields(self, template_metadata):
        """Test updating time fields of TemplateMetadata."""
        template_metadata.time_taken_manually_execute_minutes = 45
        template_metadata.time_taken_create_automation_minutes = 15
        template_metadata.save()
        template_metadata.refresh_from_db()
        assert template_metadata.time_taken_manually_execute_minutes == 45
        assert template_metadata.time_taken_create_automation_minutes == 15

    def test_min_awx_id_minus_one(self):
        """Test TemplateMetadata.get_min_awx_id returns -1 when no records exist."""
        assert TemplateMetadata.get_min_awx_id() == -1

    def test_min_awx_id_positive_ids(self, template_metadata):
        """Test TemplateMetadata.get_min_awx_id returns -1 when only positive template_ids exist."""
        TemplateMetadata.objects.create(
            template_id=10,
            template_name="Test Positive",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        TemplateMetadata.objects.create(
            template_id=20,
            template_name="Test Positive 2",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        assert TemplateMetadata.get_min_awx_id() == -1

    def test_min_awx_id_negative_ids(self, template_metadata):
        """Test TemplateMetadata.get_min_awx_id returns min template_id minus one when negative template_ids exist."""
        TemplateMetadata.objects.create(
            template_id=-5,
            template_name="Test Negative",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        TemplateMetadata.objects.create(
            template_id=-10,
            template_name="Test Negative 2",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        assert TemplateMetadata.get_min_awx_id() == -11

    def test_min_awx_id_mixed_ids(self, template_metadata):
        """Test TemplateMetadata.get_min_awx_id returns min template_id minus one if negative template_ids exist, even if positive template_ids also exist."""
        TemplateMetadata.objects.create(
            template_id=10,
            template_name="Test Positive",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        TemplateMetadata.objects.create(
            template_id=-3,
            template_name="Test Negative",
            time_taken_manually_execute_minutes=30,
            time_taken_create_automation_minutes=10,
        )
        assert TemplateMetadata.get_min_awx_id() == -4

    def test_retrieve_by_awx_id(self):
        obj = TemplateMetadata.objects.create(template_id=42, template_name="Test")
        result = TemplateMetadata.get_by_awx_id_or_name(name="Other", awx_id=42)
        assert result.pk == obj.pk

    def test_retrieve_by_name(self):
        obj = TemplateMetadata.objects.create(template_id=99, template_name="TestName")
        result = TemplateMetadata.get_by_awx_id_or_name(name="TestName")
        assert result.pk == obj.pk

    def test_create_new_when_none_exist(self):
        result = TemplateMetadata.get_by_awx_id_or_name(name="NewTemplate", awx_id=123)
        assert result.template_name == "NewTemplate"
        assert result.template_id == 123

    def test_awx_id_and_name_both_provided_awx_id_exists(self):
        obj = TemplateMetadata.objects.create(template_id=7, template_name="AWXTemplate")
        result = TemplateMetadata.get_by_awx_id_or_name(name="AWXTemplate", awx_id=7)
        assert result.pk == obj.pk

    def test_awx_id_and_name_both_provided_name_exists(self):
        obj = TemplateMetadata.objects.create(template_id=8, template_name="NameTemplate")
        result = TemplateMetadata.get_by_awx_id_or_name(name="NameTemplate", awx_id=999)
        assert result.pk == obj.pk
        assert result.template_id == 8

    def test_elapsed_sets_manual_time_estimate(self):
        result = TemplateMetadata.get_by_awx_id_or_name(
            name="ElapsedTemplate", awx_id=55, elapsed=decimal.Decimal(3600)
        )
        # 2x elapsed (3600s = 60min), so 2x60 = 120min, min 30
        assert result.time_taken_manually_execute_minutes == 120

    def test_automation_time_estimate_default(self):
        result = TemplateMetadata.get_by_awx_id_or_name(name="AutoTemplate", awx_id=66)
        assert result.time_taken_create_automation_minutes == DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES


@pytest.mark.unit
@pytest.mark.django_db
class TestJobData:
    """Test cases for JobData model."""

    def test_job_data_creation(self, job_data, template_metadata):
        """Test job data can be created successfully."""
        job_data.refresh_from_db()
        assert job_data.job_id == 1
        assert job_data.template_name == "Template 1"
        assert job_data.template_id == 1
        assert job_data.project_id == 1
        assert job_data.project_name == "Project 1"
        assert job_data.organization_id == 1
        assert job_data.status == JobStatusChoices.SUCCESSFUL
        assert job_data.started == datetime.datetime.fromisoformat("2024-01-01T00:00:00Z")
        assert job_data.finished == datetime.datetime.fromisoformat("2024-01-01T00:02:00Z")
        assert job_data.elapsed == decimal.Decimal("120.5")
        assert job_data.num_hosts == 5
        assert job_data.launched_by_id == 1
        assert job_data.launched_by_username == "testuser"
        assert job_data.template_metadata == template_metadata

    def test_job_data_string_representation(self, job_data):
        """Test job data string representation."""
        expected_str = f"Job {job_data.job_id} - Template: {job_data.template_name} - Status: {job_data.status}"
        assert str(job_data) == expected_str

    def test_job_data_update_status(self, job_data):
        """Test updating the status of JobData."""
        job_data.status = JobStatusChoices.FAILED
        job_data.save()
        job_data.refresh_from_db()
        assert job_data.status == JobStatusChoices.FAILED

    def test_job_data_unique_job_id(self, job_data):
        """Test that job_id is unique."""
        with pytest.raises(IntegrityError):
            JobData.objects.create(
                job_id=1,
                template_name="Another Template",
                template_id=2,
                project_id=2,
                project_name="Project 2",
                organization_id=2,
                status="failed",
                started=datetime.datetime.fromisoformat("2024-01-02T00:00:00Z"),
                finished=datetime.datetime.fromisoformat("2024-01-02T00:05:00Z"),
                elapsed=300.0,
                num_hosts=10,
                launched_by_id=1,
                launched_by_username="testuser",
                template_metadata=None,
            )


@pytest.mark.unit
@pytest.mark.django_db
class TestMinTimestamp:
    """Tests for JobData.min_timestamp() classmethod."""

    def test_returns_none_when_no_records(self):
        """min_timestamp returns None when JobData table is empty."""
        assert JobData.min_timestamp() is None

    def test_returns_none_when_all_finished_null(self):
        """min_timestamp returns None when all records have finished=None."""
        JobData.objects.create(job_id=100, template_name="T", elapsed=1, finished=None)
        assert JobData.min_timestamp() is None

    def test_returns_earliest_finished(self):
        """min_timestamp returns the earliest finished timestamp."""
        now = datetime.datetime.now().astimezone(datetime.UTC)
        earlier = (now - datetime.timedelta(days=5)).astimezone(datetime.UTC)
        oldest = (now - datetime.timedelta(days=10)).astimezone(datetime.UTC)
        JobData.objects.create(job_id=101, template_name="T1", elapsed=1, finished=now)
        JobData.objects.create(job_id=102, template_name="T2", elapsed=1, finished=earlier)
        JobData.objects.create(job_id=103, template_name="T3", elapsed=1, finished=oldest)
        assert JobData.min_timestamp() == oldest

    def test_returns_single_record_finished(self):
        """min_timestamp returns the only available finished timestamp."""
        now = datetime.datetime.now().astimezone(datetime.UTC)
        JobData.objects.create(job_id=104, template_name="T", elapsed=1, finished=now)
        assert JobData.min_timestamp() == now

    def test_ignores_null_finished_among_valid(self):
        """min_timestamp ignores records with finished=None and returns min of non-null."""
        now = datetime.datetime.now().astimezone(datetime.UTC)
        earlier = (now - datetime.timedelta(days=3)).astimezone(datetime.UTC)
        JobData.objects.create(job_id=105, template_name="T1", elapsed=1, finished=None)
        JobData.objects.create(job_id=106, template_name="T2", elapsed=1, finished=now)
        JobData.objects.create(job_id=107, template_name="T3", elapsed=1, finished=earlier)
        assert JobData.min_timestamp() == earlier


@pytest.mark.unit
@pytest.mark.django_db
class TestJobStatusChoices:
    """Test cases for JobStatusChoices."""

    def test_job_status_choices(self):
        """Test that JobStatusChoices contains expected values."""
        assert JobStatusChoices.NEW == "new"
        assert JobStatusChoices.PENDING == "pending"
        assert JobStatusChoices.WAITING == "waiting"
        assert JobStatusChoices.RUNNING == "running"
        assert JobStatusChoices.SUCCESSFUL == "successful"
        assert JobStatusChoices.FAILED == "failed"
        assert JobStatusChoices.ERROR == "error"
        assert JobStatusChoices.CANCELED == "canceled"


@pytest.mark.unit
@pytest.mark.django_db
class TestJobLabel:
    def test_job_label_creation(self, job_data):
        """Test JobLabel can be created successfully."""
        label = JobLabel.objects.create(
            job_data=job_data,
            label_id=1,
        )
        label.refresh_from_db()
        assert label.job_data == job_data
        assert label.label_id == 1

    def test_job_label_string_representation(self, job_data):
        """Test JobLabel string representation."""
        label = JobLabel.objects.create(
            job_data=job_data,
            label_id=1,
        )
        label.refresh_from_db()
        expected_str = f"{label.job_data.template_name}: {label.label_id}"
        assert str(label) == expected_str


@pytest.mark.unit
@pytest.mark.django_db
class TestJobHostSummary:
    def test_job_host_summary_creation(self, job_data):
        """Test JobHostSummary can be created successfully."""
        host_summary = JobHostSummary.objects.create(
            job_data=job_data,
            host_summary_id=1,
            host_id=2,
            host_name="host2",
        )
        host_summary.refresh_from_db()
        assert host_summary.job_data == job_data
        assert host_summary.host_summary_id == 1
        assert host_summary.host_id == 2
        assert host_summary.host_name == "host2"

    def test_job_host_summary_string_representation(self, job_data):
        """Test JobHostSummary string representation."""
        host_summary = JobHostSummary.objects.create(
            job_data=job_data,
            host_summary_id=1,
            host_id=2,
            host_name="host2",
        )
        host_summary.refresh_from_db()
        expected_str = f"{host_summary.host_name}: {host_summary.job_data.template_name}"
        assert str(host_summary) == expected_str

    def test_job_host_summary_unique_host_summary_id(self, job_data):
        """Test that host_summary_id is unique."""
        JobHostSummary.objects.create(
            job_data=job_data,
            host_summary_id=1,
            host_id=2,
            host_name="host2",
        )
        with pytest.raises(IntegrityError):
            JobHostSummary.objects.create(
                job_data=job_data,
                host_summary_id=1,
                host_id=3,
                host_name="host3",
            )


@pytest.mark.unit
@pytest.mark.django_db
class TestJobDataFilterMethods:
    """
    Unit tests for JobDataFilterMethods filter methods.
    Tests filtering by date, organizations, templates, projects, labels.
    """

    @pytest.fixture
    def job_data_batch(self, template_metadata):
        objs = [
            JobData.objects.create(
                job_id=10,
                template_name="T1",
                template_id=101,
                project_id=201,
                project_name="P1",
                organization_id=301,
                status="successful",
                started=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
                finished=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
                elapsed=100,
                num_hosts=2,
                launched_by_id=1,
                launched_by_username="user1",
                template_metadata=template_metadata,
            ),
            JobData.objects.create(
                job_id=11,
                template_name="T2",
                template_id=102,
                project_id=202,
                project_name="P2",
                organization_id=302,
                status="failed",
                started=datetime.datetime(2024, 2, 1, tzinfo=datetime.UTC),
                finished=datetime.datetime(2024, 2, 2, tzinfo=datetime.UTC),
                elapsed=200,
                num_hosts=3,
                launched_by_id=2,
                launched_by_username="user2",
                template_metadata=template_metadata,
            ),
        ]
        # Add labels
        JobLabel.objects.create(job_data=objs[0], label_id=501)
        JobLabel.objects.create(job_data=objs[1], label_id=502)
        return objs

    def test_before_date(self, job_data_batch):
        dt = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
        qs = JobData.objects.before_date(dt)
        assert qs.count() == 1
        assert qs.first().job_id == 10

    def test_after_date(self, job_data_batch):
        dt = datetime.datetime(2024, 2, 1, tzinfo=datetime.UTC)
        qs = JobData.objects.after_date(dt)
        assert qs.count() == 1
        assert qs.first().job_id == 11

    def test_organizations(self, job_data_batch):
        qs = JobData.objects.organizations([301])
        assert qs.count() == 1
        assert qs.first().organization_id == 301
        # None and empty list returns all
        assert JobData.objects.organizations(None).count() == 2
        assert JobData.objects.organizations([]).count() == 2

    def test_templates(self, job_data_batch):
        qs = JobData.objects.templates([102])
        assert qs.count() == 1
        assert qs.first().template_id == 102
        assert JobData.objects.templates(None).count() == 2

    def test_projects(self, job_data_batch):
        qs = JobData.objects.projects([201])
        assert qs.count() == 1
        assert qs.first().project_id == 201
        assert JobData.objects.projects([]).count() == 2

    def test_labels(self, job_data_batch):
        qs = JobData.objects.labels([501])
        assert qs.count() == 1
        assert qs.first().job_id == 10
        # None returns all
        assert JobData.objects.labels(None).count() == 2


@pytest.mark.unit
class TestCreateOrUpdateFromAwxHostSummaries:
    """Tests for the host_summaries=None guard in create_or_update_from_awx."""

    _AWX_JOB = {
        "id": 9001,
        "name": "Test Job",
        "unified_job_template_id": 1,
        "project_id": 1,
        "project_name": "Proj",
        "organization_id": 1,
        "organization_name": "Org",
        "status": "successful",
        "started": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "finished": datetime.datetime(2024, 1, 1, 1, tzinfo=datetime.UTC),
        "elapsed": 60.0,
        "launched_by_id": 1,
        "launched_by_username": "user",
        "created": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "modified": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "num_hosts": 0,
        "labels": [],
    }

    @patch("apps.dashboard_reports.models.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.models.TemplateMetadata.get_by_awx_id_or_name", return_value=None)
    @pytest.mark.django_db
    def test_host_summaries_none_skips_sync(self, _mock_template_metadata, mock_sync_host_summaries):
        """When host_summaries=None, _sync_host_summaries is never called."""
        job = {**self._AWX_JOB, "host_summaries": None}
        JobData.create_or_update_from_awx(job)
        mock_sync_host_summaries.assert_not_called()

    @patch("apps.dashboard_reports.models.JobData._sync_host_summaries")
    @patch("apps.dashboard_reports.models.TemplateMetadata.get_by_awx_id_or_name", return_value=None)
    @pytest.mark.django_db
    def test_host_summaries_list_calls_sync(self, _mock_template_metadata, mock_sync_host_summaries):
        """When host_summaries is a non-None list, _sync_host_summaries is called."""
        job = {**self._AWX_JOB, "id": 9002, "host_summaries": []}
        JobData.create_or_update_from_awx(job)
        mock_sync_host_summaries.assert_called_once()


@pytest.mark.unit
class TestCreateOrUpdateFromAwxLabels:
    """Tests for the labels=None guard in create_or_update_from_awx (AAP-79243)."""

    _AWX_JOB = {
        "id": 9100,
        "name": "Label Guard Job",
        "unified_job_template_id": 1,
        "project_id": 1,
        "project_name": "Proj",
        "organization_id": 1,
        "organization_name": "Org",
        "status": "successful",
        "started": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "finished": datetime.datetime(2024, 1, 1, 1, tzinfo=datetime.UTC),
        "elapsed": 60.0,
        "launched_by_id": 1,
        "launched_by_username": "user",
        "created": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "modified": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "num_hosts": 0,
        "host_summaries": None,
    }

    @patch("apps.dashboard_reports.models.JobData._sync_labels")
    @patch("apps.dashboard_reports.models.TemplateMetadata.get_by_awx_id_or_name", return_value=None)
    @pytest.mark.django_db
    def test_labels_none_skips_sync(self, _mock_template_metadata, mock_sync_labels):
        """When labels=None (column absent in collected data), _sync_labels is never called."""
        job = {**self._AWX_JOB, "labels": None}
        JobData.create_or_update_from_awx(job)
        mock_sync_labels.assert_not_called()

    @patch("apps.dashboard_reports.models.JobData._sync_labels")
    @patch("apps.dashboard_reports.models.TemplateMetadata.get_by_awx_id_or_name", return_value=None)
    @pytest.mark.django_db
    def test_labels_empty_list_calls_sync(self, _mock_template_metadata, mock_sync_labels):
        """When labels=[] (job has no labels in AWX), _sync_labels is called to clear stale records."""
        job = {**self._AWX_JOB, "id": 9101, "labels": []}
        JobData.create_or_update_from_awx(job)
        mock_sync_labels.assert_called_once()

    @patch("apps.dashboard_reports.models.JobData._sync_labels")
    @patch("apps.dashboard_reports.models.TemplateMetadata.get_by_awx_id_or_name", return_value=None)
    @pytest.mark.django_db
    def test_labels_list_calls_sync(self, _mock_template_metadata, mock_sync_labels):
        """When labels=[3, 7], _sync_labels is called with the provided IDs."""
        job = {**self._AWX_JOB, "id": 9102, "labels": [3, 7]}
        JobData.create_or_update_from_awx(job)
        mock_sync_labels.assert_called_once()
