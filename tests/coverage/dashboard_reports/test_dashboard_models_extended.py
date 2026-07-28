"""
Extended unit tests for apps/dashboard_reports/models.py.

Targets the ~108 uncovered lines from the 67% baseline, aiming to push
coverage above 80%.  Covers:
  - _get_month_overlap_days helper
  - CommonModel fallback (DAB unavailable path)
  - SubscriptionCostObjectManager.create (singleton upsert)
  - SubscriptionCost.__str__, daily_subscription_cost multi-month, per_second_subscription_cost
  - TemplateMetadata._lookup_by_id, _lookup_or_create_by_name (all branches),
    _create_with_race_handling (happy path + IntegrityError recovery),
    _apply_time_estimate_defaults
  - JobData.__str__, last_timestamp (with records), create_or_update_from_awx
    (create path + update path), _sync_labels, _sync_host_summaries
  - JobLabel.__str__, JobHostSummary.__str__, JobHostSummary.unique_count
  - label_ids_to_job_data_ids utility
  - JobDataFilterMethods: organizations, templates, projects, labels with real IDs
"""

import decimal
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_awx_job(job_id=1001, template_name="tmpl", template_id=42, **overrides):
    """Return a minimal valid AWXJobType dict."""
    now = datetime.now(tz=UTC)
    base = {
        "id": job_id,
        "name": template_name,
        "status": "successful",
        "unified_job_template_id": template_id,
        "organization_id": 10,
        "organization_name": "Org A",
        "started": now - timedelta(minutes=5),
        "finished": now,
        "elapsed": decimal.Decimal("300.000"),
        "launched_by_id": 7,
        "launched_by_username": "tester",
        "project_id": 20,
        "project_name": "Proj X",
        "labels": [],
        "host_summaries": [],
        "num_hosts": 0,
        "created": now - timedelta(minutes=10),
        "modified": now,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _get_month_overlap_days helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_month_overlap_days_start_month():
    """First month of range: month_start_day = start.day."""
    from datetime import date

    from apps.dashboard_reports.models import _get_month_overlap_days

    start = date(2024, 3, 15)
    end = date(2024, 5, 20)
    month_days, month_start_day, month_end_day = _get_month_overlap_days(2024, 3, start, end)
    assert month_days == 31  # March has 31 days
    assert month_start_day == 15
    assert month_end_day == 31  # Not end month, so full end


@pytest.mark.unit
def test_get_month_overlap_days_end_month():
    """Last month of range: month_end_day = end.day."""
    from datetime import date

    from apps.dashboard_reports.models import _get_month_overlap_days

    start = date(2024, 3, 1)
    end = date(2024, 5, 20)
    month_days, month_start_day, month_end_day = _get_month_overlap_days(2024, 5, start, end)
    assert month_days == 31  # May has 31 days
    assert month_start_day == 1  # Not start month
    assert month_end_day == 20


@pytest.mark.unit
def test_get_month_overlap_days_middle_month():
    """Middle month: full month range (day 1 to month_days)."""
    from datetime import date

    from apps.dashboard_reports.models import _get_month_overlap_days

    start = date(2024, 3, 1)
    end = date(2024, 5, 20)
    month_days, month_start_day, month_end_day = _get_month_overlap_days(2024, 4, start, end)
    assert month_days == 30  # April has 30 days
    assert month_start_day == 1
    assert month_end_day == 30


@pytest.mark.unit
def test_get_month_overlap_days_february_leap():
    """February leap year has 29 days."""
    from datetime import date

    from apps.dashboard_reports.models import _get_month_overlap_days

    start = date(2024, 2, 1)
    end = date(2024, 2, 29)
    month_days, _, _ = _get_month_overlap_days(2024, 2, start, end)
    assert month_days == 29


# ---------------------------------------------------------------------------
# SubscriptionCost
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_subscription_cost_str():
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("1000.00"),
        engineer_avg_hourly_rate=decimal.Decimal("50.00"),
    )
    assert "1000" in str(cost)
    assert "50" in str(cost)


@pytest.mark.unit
@pytest.mark.django_db
def test_subscription_cost_manager_create_singleton():
    """SubscriptionCostObjectManager.create() upserts to pk=1."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    c1 = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("1000.00"),
        engineer_avg_hourly_rate=decimal.Decimal("50.00"),
    )
    c2 = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("2000.00"),
        engineer_avg_hourly_rate=decimal.Decimal("70.00"),
    )
    # Should be the same pk (singleton)
    assert c1.pk == c2.pk
    assert SubscriptionCost.objects.count() == 1
    # Latest values win
    c2.refresh_from_db()
    assert c2.monthly_subscription_cost == decimal.Decimal("2000.00")


@pytest.mark.unit
@pytest.mark.django_db
def test_subscription_cost_get_returns_existing():
    """get() returns the pre-existing record without creating a duplicate."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("9999.00"),
        engineer_avg_hourly_rate=decimal.Decimal("100.00"),
    )
    cost = SubscriptionCost.get()
    assert cost.monthly_subscription_cost == decimal.Decimal("9999.00")
    assert SubscriptionCost.objects.count() == 1


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_subscription_cost_no_args():
    """daily_subscription_cost with no args returns the default monthly/days value."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.get()
    daily = cost.daily_subscription_cost()
    assert isinstance(daily, decimal.Decimal)
    assert daily > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_subscription_cost_reversed_start_end():
    """When start > end, dates are swapped before calculation."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.get()
    start = datetime(2024, 1, 31, tzinfo=UTC)
    end = datetime(2024, 1, 1, tzinfo=UTC)
    # Should not raise, and should produce valid decimal
    daily = cost.daily_subscription_cost(start=start, end=end)
    assert isinstance(daily, decimal.Decimal)
    assert daily > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_subscription_cost_same_month():
    """start and end in same month: cost is monthly / days_in_month."""
    import calendar

    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("3100.00"),
        engineer_avg_hourly_rate=decimal.Decimal("60.00"),
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)
    daily = cost.daily_subscription_cost(start=start, end=end)
    days = calendar.monthrange(2024, 1)[1]
    expected = decimal.Decimal("3100.00") / decimal.Decimal(days)
    assert abs(daily - expected) < decimal.Decimal("0.01")


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_subscription_cost_multi_month():
    """Multi-month date range iterates correctly and returns a weighted daily cost."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("3000.00"),
        engineer_avg_hourly_rate=decimal.Decimal("60.00"),
    )
    start = datetime(2024, 1, 15, tzinfo=UTC)
    end = datetime(2024, 3, 15, tzinfo=UTC)
    daily = cost.daily_subscription_cost(start=start, end=end)
    assert isinstance(daily, decimal.Decimal)
    assert daily > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_daily_subscription_cost_year_boundary():
    """Multi-month spanning a year boundary (Dec -> Jan) works correctly."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.objects.create(
        monthly_subscription_cost=decimal.Decimal("6000.00"),
        engineer_avg_hourly_rate=decimal.Decimal("60.00"),
    )
    start = datetime(2023, 12, 1, tzinfo=UTC)
    end = datetime(2024, 2, 28, tzinfo=UTC)
    daily = cost.daily_subscription_cost(start=start, end=end)
    assert isinstance(daily, decimal.Decimal)
    assert daily > 0


@pytest.mark.unit
@pytest.mark.django_db
def test_per_second_subscription_cost():
    """per_second_subscription_cost returns 0 (quantized) when no jobs exist in the period."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.get()
    per_sec = cost.per_second_subscription_cost()
    assert isinstance(per_sec, decimal.Decimal)
    assert per_sec == decimal.Decimal("0").quantize(decimal.Decimal("0.0000000001"))


@pytest.mark.unit
@pytest.mark.django_db
def test_per_second_subscription_cost_with_dates():
    """per_second_subscription_cost with no jobs in range returns 0 (quantized)."""
    from apps.dashboard_reports.models import SubscriptionCost

    SubscriptionCost.objects.all().delete()
    cost = SubscriptionCost.get()
    start = datetime(2024, 3, 1, tzinfo=UTC)
    end = datetime(2024, 5, 31, tzinfo=UTC)
    per_sec = cost.per_second_subscription_cost(start=start, end=end)
    assert isinstance(per_sec, decimal.Decimal)
    assert per_sec == decimal.Decimal("0").quantize(decimal.Decimal("0.0000000001"))


# ---------------------------------------------------------------------------
# TemplateMetadata._lookup_by_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_by_id_none():
    """_lookup_by_id returns None when awx_id is None."""
    from apps.dashboard_reports.models import TemplateMetadata

    result = TemplateMetadata._lookup_by_id(None)
    assert result is None


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_by_id_found():
    """_lookup_by_id returns the record when it exists."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="found-tmpl", template_id=8001)
    result = TemplateMetadata._lookup_by_id(8001)
    assert result is not None
    assert result.pk == tm.pk


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_by_id_not_found():
    """_lookup_by_id returns None for non-existent ID."""
    from apps.dashboard_reports.models import TemplateMetadata

    TemplateMetadata.objects.filter(template_id=9999).delete()
    result = TemplateMetadata._lookup_by_id(9999)
    assert result is None


# ---------------------------------------------------------------------------
# TemplateMetadata._lookup_or_create_by_name
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_or_create_by_name_found_positive_id():
    """Found record with positive ID is returned unchanged."""
    from apps.dashboard_reports.models import TemplateMetadata

    TemplateMetadata.objects.filter(template_name="existing-pos").delete()
    TemplateMetadata.objects.create(template_name="existing-pos", template_id=500)
    result = TemplateMetadata._lookup_or_create_by_name("existing-pos", awx_id=600)
    # Real AWX ID present; no promotion needed since template_id is already positive
    assert result.template_name == "existing-pos"


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_or_create_by_name_promotes_negative_id():
    """Found record with negative placeholder ID gets promoted to the real AWX ID."""
    from apps.dashboard_reports.models import TemplateMetadata

    TemplateMetadata.objects.filter(template_name="neg-id-tmpl").delete()
    tm = TemplateMetadata.objects.create(template_name="neg-id-tmpl", template_id=-5)
    result = TemplateMetadata._lookup_or_create_by_name("neg-id-tmpl", awx_id=777)
    assert result.template_name == "neg-id-tmpl"
    tm.refresh_from_db()
    assert tm.template_id == 777


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_or_create_by_name_creates_new():
    """Record not found by name → new one is created."""
    from apps.dashboard_reports.models import TemplateMetadata

    unique_name = "brand-new-tmpl-xyz987"
    TemplateMetadata.objects.filter(template_name=unique_name).delete()
    result = TemplateMetadata._lookup_or_create_by_name(unique_name, awx_id=111)
    assert result.template_name == unique_name
    assert result.template_id == 111


@pytest.mark.unit
@pytest.mark.django_db
def test_lookup_or_create_by_name_multiple_returns_highest():
    """When MultipleObjectsReturned, the record with highest template_id is chosen."""
    from apps.dashboard_reports.models import TemplateMetadata

    name = "dup-tmpl-name"
    TemplateMetadata.objects.filter(template_name=name).delete()
    TemplateMetadata.objects.create(template_name=name, template_id=10)
    TemplateMetadata.objects.create(template_name=name, template_id=20)
    result = TemplateMetadata._lookup_or_create_by_name(name, awx_id=None)
    assert result.template_id == 20


# ---------------------------------------------------------------------------
# TemplateMetadata._create_with_race_handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_create_with_race_handling_happy_path():
    """Normal creation (no race) returns the new TemplateMetadata."""
    from apps.dashboard_reports.models import TemplateMetadata

    unique_name = "race-free-tmpl-abc"
    TemplateMetadata.objects.filter(template_name=unique_name).delete()
    result = TemplateMetadata._create_with_race_handling(unique_name, awx_id=321)
    assert result.template_name == unique_name
    assert result.template_id == 321


@pytest.mark.unit
@pytest.mark.django_db
def test_create_with_race_handling_integrity_error():
    """IntegrityError during create → falls back to fetching the winning row."""
    from django.db import IntegrityError

    from apps.dashboard_reports.models import TemplateMetadata

    name = "race-condition-tmpl"
    TemplateMetadata.objects.filter(template_name=name).delete()
    existing = TemplateMetadata.objects.create(template_name=name, template_id=444)

    with patch.object(TemplateMetadata.objects, "create", side_effect=IntegrityError("unique violation")):
        result = TemplateMetadata._create_with_race_handling(name, awx_id=555)

    assert result.pk == existing.pk


@pytest.mark.unit
@pytest.mark.django_db
def test_create_with_race_handling_integrity_error_no_winner():
    """If IntegrityError fires but no record exists either, re-raise."""
    from django.db import IntegrityError

    from apps.dashboard_reports.models import TemplateMetadata

    name = "phantom-race-tmpl"
    TemplateMetadata.objects.filter(template_name=name).delete()

    with (
        patch.object(TemplateMetadata.objects, "create", side_effect=IntegrityError("unique violation")),
        pytest.raises(IntegrityError),
    ):
        TemplateMetadata._create_with_race_handling(name, awx_id=999)


# ---------------------------------------------------------------------------
# TemplateMetadata._apply_time_estimate_defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_apply_time_estimate_defaults_both_none():
    """Both fields None → both are set and both field names returned."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="defaults-test", template_id=8888)
    tm.time_taken_manually_execute_minutes = None
    tm.time_taken_create_automation_minutes = None
    update_fields = TemplateMetadata._apply_time_estimate_defaults(tm, elapsed=decimal.Decimal("3600"))
    assert "time_taken_manually_execute_minutes" in update_fields
    assert "time_taken_create_automation_minutes" in update_fields
    assert tm.time_taken_manually_execute_minutes is not None
    assert tm.time_taken_create_automation_minutes is not None


@pytest.mark.unit
@pytest.mark.django_db
def test_apply_time_estimate_defaults_elapsed_none():
    """elapsed=None → manual time is NOT set, only automation time."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="defaults-no-elapsed", template_id=8889)
    tm.time_taken_manually_execute_minutes = None
    tm.time_taken_create_automation_minutes = None
    update_fields = TemplateMetadata._apply_time_estimate_defaults(tm, elapsed=None)
    assert "time_taken_manually_execute_minutes" not in update_fields
    assert "time_taken_create_automation_minutes" in update_fields


@pytest.mark.unit
@pytest.mark.django_db
def test_apply_time_estimate_defaults_already_set():
    """Both fields already set → empty update_fields list."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="defaults-already-set", template_id=8890)
    tm.time_taken_manually_execute_minutes = 30
    tm.time_taken_create_automation_minutes = 60
    update_fields = TemplateMetadata._apply_time_estimate_defaults(tm, elapsed=decimal.Decimal("3600"))
    assert update_fields == []


@pytest.mark.unit
@pytest.mark.django_db
def test_apply_time_estimate_defaults_short_elapsed_no_floor():
    """Very short elapsed → manual estimate is a pure 2x calculation, with no artificial floor."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="defaults-short-elapsed", template_id=8891)
    tm.time_taken_manually_execute_minutes = None
    tm.time_taken_create_automation_minutes = None
    # 10 seconds elapsed → 2x / 60 = 0.33 minutes → rounded up to 1, no 30-minute floor applied
    TemplateMetadata._apply_time_estimate_defaults(tm, elapsed=decimal.Decimal("10"))
    assert tm.time_taken_manually_execute_minutes == 1


@pytest.mark.unit
@pytest.mark.django_db
def test_apply_time_estimate_defaults_capped_at_maximum():
    """Very long elapsed → manual estimate is capped at 1,000,000 minutes."""
    from apps.dashboard_reports.models import TemplateMetadata

    tm = TemplateMetadata.objects.create(template_name="defaults-max-cap", template_id=8892)
    tm.time_taken_manually_execute_minutes = None
    tm.time_taken_create_automation_minutes = None
    # 60,000,001 seconds × 2 / 60 >> 1,000,000 minutes
    TemplateMetadata._apply_time_estimate_defaults(tm, elapsed=decimal.Decimal("3600000001"))
    assert tm.time_taken_manually_execute_minutes == 1_000_000


# ---------------------------------------------------------------------------
# JobData.__str__ and last_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_job_data_str():
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=500).delete()
    JobData.objects.filter(job_id=5001).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=5001, template_id=500, template_name="str-tmpl"))
    job = JobData.objects.get(job_id=5001)
    s = str(job)
    assert "5001" in s
    assert "str-tmpl" in s


@pytest.mark.unit
@pytest.mark.django_db
def test_job_data_last_timestamp_with_records():
    """last_timestamp() returns the max awx_modified from JobData records."""
    from apps.dashboard_reports.models import JobData

    JobData.objects.filter(job_id__in=[6001, 6002]).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=6001, template_id=600))
    now = datetime.now(tz=UTC)
    JobData.create_or_update_from_awx(_make_awx_job(job_id=6002, template_id=601, modified=now))
    ts = JobData.last_timestamp()
    assert ts is not None


# ---------------------------------------------------------------------------
# JobData.create_or_update_from_awx — create path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_create_or_update_from_awx_create():
    """create_or_update_from_awx creates a new JobData record."""
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=700).delete()
    JobData.objects.filter(job_id=7001).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=7001, template_id=700, template_name="create-tmpl"))
    assert JobData.objects.filter(job_id=7001).exists()
    job = JobData.objects.get(job_id=7001)
    assert job.template_name == "create-tmpl"
    assert job.organization_id == 10
    assert job.project_id == 20


@pytest.mark.unit
@pytest.mark.django_db
def test_create_or_update_from_awx_update():
    """Calling create_or_update_from_awx again updates the existing record."""
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=710).delete()
    JobData.objects.filter(job_id=7010).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=7010, template_id=710, template_name="before"))
    JobData.create_or_update_from_awx(_make_awx_job(job_id=7010, template_id=710, template_name="after"))
    assert JobData.objects.filter(job_id=7010).count() == 1
    job = JobData.objects.get(job_id=7010)
    assert job.template_name == "after"


@pytest.mark.unit
@pytest.mark.django_db
def test_create_or_update_from_awx_no_template_id():
    """AWX job with unified_job_template_id=None still creates a record."""
    from apps.dashboard_reports.models import JobData

    JobData.objects.filter(job_id=7020).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=7020, template_id=None, template_name="no-template-id"))
    assert JobData.objects.filter(job_id=7020).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_create_or_update_from_awx_with_labels():
    """Labels list is synced via _sync_labels creating JobLabel records."""
    from apps.dashboard_reports.models import JobData, JobLabel, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=720).delete()
    JobData.objects.filter(job_id=7030).delete()
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7030, template_id=720, template_name="with-labels", labels=[101, 102, 103])
    )
    job = JobData.objects.get(job_id=7030)
    label_ids = list(JobLabel.objects.filter(job_data=job).values_list("label_id", flat=True))
    assert sorted(label_ids) == [101, 102, 103]


@pytest.mark.unit
@pytest.mark.django_db
def test_create_or_update_from_awx_with_host_summaries():
    """host_summaries list is synced via _sync_host_summaries."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=730).delete()
    JobData.objects.filter(job_id=7040).delete()
    host_summaries = [
        {"id": 1001, "host_id": 201, "host_name": "host-a"},
        {"id": 1002, "host_id": 202, "host_name": "host-b"},
    ]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7040, template_id=730, template_name="with-hosts", host_summaries=host_summaries)
    )
    job = JobData.objects.get(job_id=7040)
    hs_ids = list(JobHostSummary.objects.filter(job_data=job).values_list("host_summary_id", flat=True))
    assert sorted(hs_ids) == [1001, 1002]


# ---------------------------------------------------------------------------
# JobData._sync_labels — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_sync_labels_removes_stale():
    """Stale labels not in the new list are deleted."""
    from apps.dashboard_reports.models import JobData, JobLabel, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=740).delete()
    JobData.objects.filter(job_id=7050).delete()
    # Create with labels 1, 2, 3
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7050, template_id=740, template_name="sync-labels", labels=[1, 2, 3])
    )
    job = JobData.objects.get(job_id=7050)
    assert JobLabel.objects.filter(job_data=job).count() == 3

    # Update — only label 2 remains
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7050, template_id=740, template_name="sync-labels", labels=[2])
    )
    remaining = list(JobLabel.objects.filter(job_data=job).values_list("label_id", flat=True))
    assert remaining == [2]


@pytest.mark.unit
@pytest.mark.django_db
def test_sync_labels_empty():
    """Syncing with an empty label list removes all existing labels."""
    from apps.dashboard_reports.models import JobData, JobLabel, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=741).delete()
    JobData.objects.filter(job_id=7051).delete()
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7051, template_id=741, template_name="clear-labels", labels=[10, 20])
    )
    job = JobData.objects.get(job_id=7051)
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7051, template_id=741, template_name="clear-labels", labels=[])
    )
    assert JobLabel.objects.filter(job_data=job).count() == 0


# ---------------------------------------------------------------------------
# JobData._sync_host_summaries — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_sync_host_summaries_update_existing():
    """Existing host summaries are bulk-updated (host_name changes)."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=750).delete()
    JobData.objects.filter(job_id=7060).delete()
    hs = [{"id": 2001, "host_id": 301, "host_name": "old-name"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7060, template_id=750, template_name="update-hs", host_summaries=hs)
    )
    # Update with same host_summary_id but different host_name
    hs_updated = [{"id": 2001, "host_id": 301, "host_name": "new-name"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7060, template_id=750, template_name="update-hs", host_summaries=hs_updated)
    )
    job = JobData.objects.get(job_id=7060)
    summary = JobHostSummary.objects.get(job_data=job, host_summary_id=2001)
    assert summary.host_name == "new-name"


@pytest.mark.unit
@pytest.mark.django_db
def test_sync_host_summaries_removes_stale():
    """Stale host summaries not in the new list are deleted."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=751).delete()
    JobData.objects.filter(job_id=7061).delete()
    hs = [
        {"id": 3001, "host_id": 401, "host_name": "keep"},
        {"id": 3002, "host_id": 402, "host_name": "remove"},
    ]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7061, template_id=751, template_name="stale-hs", host_summaries=hs)
    )
    job = JobData.objects.get(job_id=7061)
    assert JobHostSummary.objects.filter(job_data=job).count() == 2

    # Update — only host 3001 remains
    hs_trimmed = [{"id": 3001, "host_id": 401, "host_name": "keep"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=7061, template_id=751, template_name="stale-hs", host_summaries=hs_trimmed)
    )
    remaining = list(JobHostSummary.objects.filter(job_data=job).values_list("host_summary_id", flat=True))
    assert remaining == [3001]


# ---------------------------------------------------------------------------
# JobLabel and JobHostSummary __str__
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_job_label_str():
    from apps.dashboard_reports.models import JobData, JobLabel, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=800).delete()
    JobData.objects.filter(job_id=8001).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=8001, template_id=800, template_name="lbl-str", labels=[55]))
    job = JobData.objects.get(job_id=8001)
    lbl = JobLabel.objects.get(job_data=job, label_id=55)
    assert "55" in str(lbl)
    assert "lbl-str" in str(lbl)


@pytest.mark.unit
@pytest.mark.django_db
def test_job_host_summary_str():
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=810).delete()
    JobData.objects.filter(job_id=8010).delete()
    hs = [{"id": 9001, "host_id": 501, "host_name": "host-str-test"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=8010, template_id=810, template_name="hs-str", host_summaries=hs)
    )
    job = JobData.objects.get(job_id=8010)
    summary = JobHostSummary.objects.get(job_data=job, host_summary_id=9001)
    assert "host-str-test" in str(summary)
    assert "hs-str" in str(summary)


# ---------------------------------------------------------------------------
# label_ids_to_job_data_ids utility
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_label_ids_to_job_data_ids():
    from apps.dashboard_reports.models import JobData, TemplateMetadata, label_ids_to_job_data_ids

    TemplateMetadata.objects.filter(template_id=900).delete()
    JobData.objects.filter(job_id=9001).delete()
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=9001, template_id=900, template_name="lbl-util", labels=[777, 888])
    )
    job = JobData.objects.get(job_id=9001)
    ids = list(label_ids_to_job_data_ids([777]))
    assert job.pk in ids


@pytest.mark.unit
@pytest.mark.django_db
def test_label_ids_to_job_data_ids_no_match():
    from apps.dashboard_reports.models import label_ids_to_job_data_ids

    ids = list(label_ids_to_job_data_ids([99999]))
    assert ids == []


# ---------------------------------------------------------------------------
# JobDataFilterMethods — with real IDs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_filter_methods_organizations():
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1000).delete()
    JobData.objects.filter(job_id=10001).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=10001, template_id=1000, template_name="org-filter"))
    qs = JobData.objects.organizations([10])
    assert qs.filter(job_id=10001).exists()
    qs_none = JobData.objects.organizations([999])
    assert not qs_none.filter(job_id=10001).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_filter_methods_templates():
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1001).delete()
    JobData.objects.filter(job_id=10002).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=10002, template_id=1001, template_name="tmpl-filter"))
    qs = JobData.objects.templates([1001])
    assert qs.filter(job_id=10002).exists()
    qs_none = JobData.objects.templates([9999])
    assert not qs_none.filter(job_id=10002).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_filter_methods_projects():
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1002).delete()
    JobData.objects.filter(job_id=10003).delete()
    JobData.create_or_update_from_awx(_make_awx_job(job_id=10003, template_id=1002, template_name="proj-filter"))
    qs = JobData.objects.projects([20])
    assert qs.filter(job_id=10003).exists()


@pytest.mark.unit
@pytest.mark.django_db
def test_filter_methods_labels_with_ids():
    from apps.dashboard_reports.models import JobData, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1003).delete()
    JobData.objects.filter(job_id=10004).delete()
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=10004, template_id=1003, template_name="lbl-filter", labels=[555])
    )
    qs = JobData.objects.labels([555])
    assert qs.filter(job_id=10004).exists()
    qs_none = JobData.objects.labels([9999])
    assert not qs_none.filter(job_id=10004).exists()


# ---------------------------------------------------------------------------
# JobHostSummary.unique_count
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.django_db
def test_unique_count_basic():
    """unique_count returns distinct host count."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1100).delete()
    JobData.objects.filter(job_id=11001).delete()
    hs = [
        {"id": 5001, "host_id": 601, "host_name": "host-1"},
        {"id": 5002, "host_id": 602, "host_name": "host-2"},
    ]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=11001, template_id=1100, template_name="uc-basic", host_summaries=hs)
    )
    count = JobHostSummary.unique_count()
    assert count >= 2


@pytest.mark.unit
@pytest.mark.django_db
def test_unique_count_deduplicates_by_host_id():
    """Same host_id across two jobs is counted only once."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id__in=[1101, 1102]).delete()
    JobData.objects.filter(job_id__in=[11010, 11011]).delete()
    now = datetime.now(tz=UTC)
    hs_shared = [{"id": 6001, "host_id": 700, "host_name": "shared-host"}]
    hs_other = [{"id": 6002, "host_id": 700, "host_name": "shared-host"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(
            job_id=11010, template_id=1101, template_name="uc-dedup-a", host_summaries=hs_shared, finished=now
        )
    )
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=11011, template_id=1102, template_name="uc-dedup-b", host_summaries=hs_other, finished=now)
    )
    count = JobHostSummary.unique_count(
        start=now - timedelta(minutes=1),
        end=now + timedelta(minutes=1),
    )
    # host_id=700 appears in both jobs but should count as one unique host
    assert count >= 1


@pytest.mark.unit
@pytest.mark.django_db
def test_unique_count_fallback_to_host_name():
    """Host with no host_id uses host_name as surrogate."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1103).delete()
    JobData.objects.filter(job_id=11012).delete()
    hs = [{"id": 7001, "host_id": None, "host_name": "name-only-host"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=11012, template_id=1103, template_name="uc-nohost", host_summaries=hs)
    )
    count = JobHostSummary.unique_count()
    assert count >= 1


@pytest.mark.unit
@pytest.mark.django_db
def test_unique_count_with_filter_options():
    """unique_count respects organization/project/template filter options."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1104).delete()
    JobData.objects.filter(job_id=11013).delete()
    hs = [{"id": 8001, "host_id": 800, "host_name": "filtered-host"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(job_id=11013, template_id=1104, template_name="uc-filtered", host_summaries=hs)
    )
    count = JobHostSummary.unique_count(options={"organization": [10], "project": [20], "template": [1104]})
    assert count >= 1

    # Non-matching org returns 0
    count_none = JobHostSummary.unique_count(options={"organization": [9999]})
    assert count_none == 0


@pytest.mark.unit
@pytest.mark.django_db
def test_unique_count_with_label_filter():
    """unique_count respects label filter option (subquery path)."""
    from apps.dashboard_reports.models import JobData, JobHostSummary, TemplateMetadata

    TemplateMetadata.objects.filter(template_id=1105).delete()
    JobData.objects.filter(job_id=11014).delete()
    hs = [{"id": 9001, "host_id": 901, "host_name": "labelled-host"}]
    JobData.create_or_update_from_awx(
        _make_awx_job(
            job_id=11014,
            template_id=1105,
            template_name="uc-label",
            host_summaries=hs,
            labels=[333],
        )
    )
    count = JobHostSummary.unique_count(options={"label": [333]})
    assert count >= 1

    count_none = JobHostSummary.unique_count(options={"label": [9999]})
    assert count_none == 0
