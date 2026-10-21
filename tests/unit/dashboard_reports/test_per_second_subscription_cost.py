import datetime
import decimal
from datetime import UTC
from unittest.mock import patch

import pytest

from apps.dashboard_reports.models import (
    JobData,
    JobStatusChoices,
    SubscriptionCost,
    TemplateMetadata,
    _get_month_overlap_days,
    _month_range_iter,
)


@pytest.mark.unit
@pytest.mark.django_db
class TestMonthHelpers:
    def test_month_range_iter_same_month(self):
        start = datetime.datetime(2023, 5, 1)
        end = datetime.datetime(2023, 5, 31)
        result = list(_month_range_iter(start, end))
        assert result == [(2023, 5)]

    def test_month_range_iter_multiple_months_same_year(self):
        start = datetime.datetime(2023, 3, 1)
        end = datetime.datetime(2023, 5, 31)
        result = list(_month_range_iter(start, end))
        assert result == [(2023, 3), (2023, 4), (2023, 5)]

    def test_month_range_iter_spanning_years(self):
        start = datetime.datetime(2022, 11, 1)
        end = datetime.datetime(2023, 2, 28)
        result = list(_month_range_iter(start, end))
        assert result == [(2022, 11), (2022, 12), (2023, 1), (2023, 2)]

    def test_month_range_iter_december_to_january(self):
        start = datetime.datetime(2023, 12, 1)
        end = datetime.datetime(2024, 1, 31)
        result = list(_month_range_iter(start, end))
        assert result == [(2023, 12), (2024, 1)]

    def test_month_range_iter_single_day(self):
        start = datetime.datetime(2023, 7, 15)
        end = datetime.datetime(2023, 7, 15)
        result = list(_month_range_iter(start, end))
        assert result == [(2023, 7)]

    def test_full_month_overlap(self):
        # Full month: March 2023
        start = datetime.datetime(2023, 3, 1)
        end = datetime.datetime(2023, 3, 31)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2023, 3, start, end)
        assert month_days == 31
        assert month_start_day == 1
        assert month_end_day == 31

    def test_partial_first_month(self):
        # Overlap starts on 15th
        start = datetime.datetime(2023, 3, 15)
        end = datetime.datetime(2023, 3, 31)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2023, 3, start, end)
        assert month_days == 31
        assert month_start_day == 15
        assert month_end_day == 31

    def test_partial_last_month(self):
        # Overlap ends on 10th
        start = datetime.datetime(2023, 3, 1)
        end = datetime.datetime(2023, 3, 10)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2023, 3, start, end)
        assert month_days == 31
        assert month_start_day == 1
        assert month_end_day == 10

    def test_single_day_overlap(self):
        start = datetime.datetime(2023, 3, 20)
        end = datetime.datetime(2023, 3, 20)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2023, 3, start, end)
        assert month_days == 31
        assert month_start_day == 20
        assert month_end_day == 20

    def test_february_non_leap_year(self):
        start = datetime.datetime(2023, 2, 1)
        end = datetime.datetime(2023, 2, 28)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2023, 2, start, end)
        assert month_days == 28
        assert month_start_day == 1
        assert month_end_day == 28

    def test_february_leap_year(self):
        start = datetime.datetime(2024, 2, 1)
        end = datetime.datetime(2024, 2, 29)
        month_days, month_start_day, month_end_day = _get_month_overlap_days(2024, 2, start, end)
        assert month_days == 29
        assert month_start_day == 1
        assert month_end_day == 29


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestPerSecondSubscriptionCost:
    """
    Tests for SubscriptionCost.per_second_subscription_cost().

    The method returns cost_per_elapsed_second (currency / second) where:
      cost_per_elapsed_second * total_elapsed_seconds_in_period == total_period_cost

    This distributes the proportional monthly subscription cost across all
    successful/failed job elapsed seconds in the period.
    """

    @pytest.fixture()
    def subscription_cost(self) -> "SubscriptionCost":
        db = SubscriptionCost.get()
        db.monthly_subscription_cost = decimal.Decimal("2000.00")
        db.save()
        return db

    @pytest.fixture()
    def jobs_in_march(self, subscription_cost: "SubscriptionCost"):
        """Two successful jobs in March 2025, each with elapsed=500 seconds."""

        TemplateMetadata.objects.create(template_id=101, template_name="Test Template")
        JobData.objects.create(
            job_id=1001,
            template_name="Test Template",
            template_id=101,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 3, 1, 10, 0, 0, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 3, 1, 10, 8, 20, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("500"),
        )
        JobData.objects.create(
            job_id=1002,
            template_name="Test Template",
            template_id=101,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 3, 15, 12, 0, 0, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 3, 15, 12, 8, 20, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("500"),
        )

    def test_full_month_weighted_cost(self, subscription_cost: "SubscriptionCost", jobs_in_march: None) -> None:
        """Full March: period_cost == monthly_cost; total_elapsed == 1000s → rate = 2000/1000 = 2."""
        start = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        expected = (decimal.Decimal("2000") / decimal.Decimal("1000")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected

    def test_partial_month_weighted_cost(self, subscription_cost, jobs_in_march):
        start = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 15, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        period_cost = decimal.Decimal("2000") * decimal.Decimal("15") / decimal.Decimal("31")
        expected = (period_cost / decimal.Decimal("1000")).quantize(
            decimal.Decimal("0.0000000001")
        )  # both jobs are in range
        assert result == expected

    def test_multi_month_weighted_cost(self, subscription_cost: "SubscriptionCost") -> None:
        """Feb + March full months: period_cost = 4000, total_elapsed = 200+300 = 500 → rate = 8."""

        TemplateMetadata.objects.create(template_id=202, template_name="Multi Month Template")
        JobData.objects.create(
            job_id=2001,
            template_name="Multi Month Template",
            template_id=202,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 2, 10, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 2, 10, 1, 0, 0, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("200"),
        )
        JobData.objects.create(
            job_id=2002,
            template_name="Multi Month Template",
            template_id=202,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 3, 10, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 3, 10, 1, 0, 0, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("300"),
        )

        start = datetime.datetime(2025, 2, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        # Feb (28/28) + Mar (31/31) = 2000 + 2000 = 4000; total_elapsed = 500
        expected = (decimal.Decimal("4000") / decimal.Decimal("500")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected

    def test_no_jobs_returns_fallback(self, subscription_cost):
        start = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 1, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        assert result == decimal.Decimal("0").quantize(decimal.Decimal("0.0000000001"))

    def test_zero_monthly_cost_returns_zero_rate(
        self, subscription_cost: "SubscriptionCost", jobs_in_march: None
    ) -> None:
        """Zero monthly cost → period_cost = 0 → rate = 0."""
        subscription_cost.monthly_subscription_cost = decimal.Decimal("0.00")
        subscription_cost.save()

        start = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        assert result == decimal.Decimal("0").quantize(decimal.Decimal("0.0000000001"))

    def test_failed_jobs_are_included(self, subscription_cost: "SubscriptionCost") -> None:
        """Failed jobs must be included in the elapsed total."""

        TemplateMetadata.objects.create(template_id=303, template_name="Failed Template")
        JobData.objects.create(
            job_id=3001,
            template_name="Failed Template",
            template_id=303,
            status=JobStatusChoices.FAILED,
            started=datetime.datetime(2025, 3, 5, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 3, 5, 1, 0, 0, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("1000"),
        )

        start = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        expected = (decimal.Decimal("2000") / decimal.Decimal("1000")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected

    def test_start_greater_than_end_is_swapped(
        self, subscription_cost: "SubscriptionCost", jobs_in_march: None
    ) -> None:
        """Reversed start/end should give same result as normal order."""
        result_normal = subscription_cost.per_second_subscription_cost(
            datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC),
            datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC),
        )
        result_reversed = subscription_cost.per_second_subscription_cost(
            datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC),
            datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC),
        )
        assert result_normal == result_reversed

    def test_cost_distribution_invariant(self, subscription_cost: "SubscriptionCost", jobs_in_march: None) -> None:
        """
        Core invariant: cost_per_second * total_elapsed ≈ period_cost.
        Tolerance is one quantization unit per elapsed second.
        """
        start = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 31, tzinfo=datetime.UTC)

        rate = subscription_cost.per_second_subscription_cost(start, end)
        total_elapsed = decimal.Decimal("1000")  # two jobs × 500s each
        reconstructed = rate * total_elapsed

        expected_period_cost = decimal.Decimal("2000")
        tolerance = decimal.Decimal("0.0000000001") * total_elapsed

        assert abs(reconstructed - expected_period_cost) <= tolerance, (
            f"Invariant broken: |{reconstructed} - {expected_period_cost}| > {tolerance}"
        )

    @patch("apps.dashboard_reports.models.datetime")
    def test_no_date_defaults_to_current_month(self, mock_datetime, subscription_cost: "SubscriptionCost") -> None:
        """
        When start/end are None the method defaults to the current full calendar month.
        A job finishing within that month must be included in the elapsed sum.
        """

        fixed_now = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        mock_datetime.now.return_value = fixed_now

        TemplateMetadata.objects.create(template_id=404, template_name="June Template")
        JobData.objects.create(
            job_id=4001,
            template_name="June Template",
            template_id=404,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 6, 10, 0, 0, 0, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 6, 10, 0, 16, 40, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("1000"),
        )

        result = subscription_cost.per_second_subscription_cost()

        # Full June (30 days) → period_cost = monthly_cost; elapsed = 1000s
        expected = (decimal.Decimal("2000") / decimal.Decimal("1000")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected

    def test_pending_and_cancelled_jobs_are_excluded(self, subscription_cost: "SubscriptionCost") -> None:
        """Jobs with status other than SUCCESSFUL or FAILED must not contribute to the elapsed sum."""

        TemplateMetadata.objects.create(template_id=505, template_name="Mixed Status Template")
        # This job should be counted
        JobData.objects.create(
            job_id=5001,
            template_name="Mixed Status Template",
            template_id=505,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 4, 1, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 4, 1, 0, 16, 40, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("1000"),
        )
        # These jobs must NOT be counted
        for job_id, status in [
            (5002, JobStatusChoices.PENDING),
            (5003, JobStatusChoices.RUNNING),
            (5004, JobStatusChoices.CANCELED),
        ]:
            JobData.objects.create(
                job_id=job_id,
                template_name="Mixed Status Template",
                template_id=505,
                status=status,
                started=datetime.datetime(2025, 4, 2, tzinfo=datetime.UTC),
                finished=datetime.datetime(2025, 4, 2, 1, 0, 0, tzinfo=datetime.UTC),
                elapsed=decimal.Decimal("9999"),
            )

        start = datetime.datetime(2025, 4, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 4, 30, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        # Only the SUCCESSFUL job's 1000s should count; period_cost = 2000 (full April)
        expected = (decimal.Decimal("2000") / decimal.Decimal("1000")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected

    def test_cross_year_boundary_cost(self, subscription_cost: "SubscriptionCost") -> None:
        """Full Dec + full Jan spanning a year boundary should sum two full monthly costs."""

        TemplateMetadata.objects.create(template_id=606, template_name="Year Boundary Template")
        JobData.objects.create(
            job_id=6001,
            template_name="Year Boundary Template",
            template_id=606,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2024, 12, 15, tzinfo=datetime.UTC),
            finished=datetime.datetime(2024, 12, 15, 1, 0, 0, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("400"),
        )
        JobData.objects.create(
            job_id=6002,
            template_name="Year Boundary Template",
            template_id=606,
            status=JobStatusChoices.SUCCESSFUL,
            started=datetime.datetime(2025, 1, 15, tzinfo=datetime.UTC),
            finished=datetime.datetime(2025, 1, 15, 1, 0, 0, tzinfo=datetime.UTC),
            elapsed=decimal.Decimal("600"),
        )

        start = datetime.datetime(2024, 12, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 1, 31, tzinfo=datetime.UTC)

        result = subscription_cost.per_second_subscription_cost(start, end)

        # Dec (31/31) + Jan (31/31) = 2000 + 2000 = 4000; total_elapsed = 1000s
        expected = (decimal.Decimal("4000") / decimal.Decimal("1000")).quantize(decimal.Decimal("0.0000000001"))
        assert result == expected
