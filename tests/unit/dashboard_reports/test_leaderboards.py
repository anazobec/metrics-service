"""Unit tests for the dashboard leaderboards endpoint.

The endpoint aggregates successful ``JobData`` over the trailing 30 UTC calendar
days into counts, streaks, per-org / per-user leaderboards and achievements. All
tests pin "now" so the 30-day window is deterministic.
"""

import datetime
import itertools
import json

import pytest
from django.urls import reverse

from apps.dashboard_reports.models import JobData, JobStatusChoices
from apps.dynamic_settings.models import Setting

pytestmark = [pytest.mark.unit, pytest.mark.django_db]

# Pinned clock. Window = window_start (FIXED_NOW.date() - 29d) . FIXED_NOW.date().
FIXED_NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
WINDOW_START = FIXED_NOW.date() - datetime.timedelta(days=29)  # 2026-05-17
URL = reverse("v1:leaderboard-list")

_job_ids = itertools.count(1)


class _FixedDatetime(datetime.datetime):
    """``datetime`` subclass with a frozen ``now`` (everything else inherited)."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    monkeypatch.setattr("apps.dashboard_reports.viewsets.dashboard_leaderboards.datetime", _FixedDatetime)


def day(offset: int, hour: int = 12) -> datetime.datetime:
    """A UTC timestamp ``offset`` days after the window start."""
    d = WINDOW_START + datetime.timedelta(days=offset)
    return datetime.datetime(d.year, d.month, d.day, hour, 0, tzinfo=datetime.UTC)


def make_job(
    finished: datetime.datetime,
    *,
    status: str = JobStatusChoices.SUCCESSFUL,
    org_id: int | None = 1,
    org_name: str | None = "Org One",
    template_id: int | None = 10,
    template_name: str = "Template",
    launched_by_id: int | None = 1,
    launched_by_username: str | None = None,
) -> JobData:
    """Create one JobData row with sensible defaults."""
    return JobData.objects.create(
        job_id=next(_job_ids),
        template_name=template_name,
        template_id=template_id,
        organization_id=org_id,
        organization_name=org_name,
        status=status,
        started=finished - datetime.timedelta(minutes=1),
        finished=finished,
        elapsed=60,
        num_hosts=1,
        launched_by_id=launched_by_id,
        launched_by_username=launched_by_username,
    )


def get(client):
    """GET the endpoint, asserting 200, and return the parsed body."""
    response = client.get(URL)
    assert response.status_code == 200, response.content
    return response.data


def _me(user, **kwargs):
    """make_job kwargs for a job launched by the currently authenticated user.

    The endpoint resolves the current user's AWX id by matching
    ``launched_by_username`` against the authenticated username, so "my" jobs
    must carry both.
    """
    kwargs.setdefault("launched_by_id", user.id)
    kwargs.setdefault("launched_by_username", user.username)
    return kwargs


class TestAuth:
    def test_unauthenticated_is_rejected(self, api_client):
        assert api_client.get(URL).status_code in (401, 403)

    def test_authenticated_user_allowed(self, authenticated_client):
        make_job(day(1))
        assert authenticated_client.get(URL).status_code == 200

    def test_detail_route_not_supported(self, authenticated_client):
        response = authenticated_client.get(reverse("v1:leaderboard-detail", args=[1]))
        assert response.status_code == 405


class TestLeaderboardFlag:
    def test_returns_404_when_show_leaderboard_disabled(self, authenticated_client):
        """When SHOW_LEADERBOARD is explicitly False, the endpoint returns 404."""
        Setting.objects.update_or_create(setting_key="SHOW_LEADERBOARD", defaults={"current_value": json.dumps(False)})

        response = authenticated_client.get(URL)

        assert response.status_code == 404
        assert response.data == {"detail": "Leaderboard not enabled"}

    def test_returns_200_when_show_leaderboard_setting_missing(self, authenticated_client):
        """With no Setting row at all, the endpoint defaults to enabled and returns 200."""
        Setting.objects.filter(setting_key="SHOW_LEADERBOARD").delete()

        response = authenticated_client.get(URL)

        assert response.status_code == 200

    def test_returns_200_when_show_leaderboard_enabled(self, authenticated_client):
        """When SHOW_LEADERBOARD is explicitly True, the endpoint behaves normally."""
        Setting.objects.update_or_create(setting_key="SHOW_LEADERBOARD", defaults={"current_value": json.dumps(True)})

        assert authenticated_client.get(URL).status_code == 200


class TestCounts:
    def test_job_runs_counts_only_successful_in_window(self, authenticated_client):
        make_job(day(1))
        make_job(day(2))
        make_job(day(3), status=JobStatusChoices.FAILED)  # wrong status
        make_job(day(-1))  # before the window
        make_job(FIXED_NOW + datetime.timedelta(days=400))  # far future -> excluded (after "now")

        data = get(authenticated_client)
        assert data["job_runs"] == 2  # only the 2 in-window runs

    def test_active_organizations_is_distinct_non_null(self, authenticated_client):
        make_job(day(1), org_id=1)
        make_job(day(2), org_id=1)
        make_job(day(3), org_id=2)
        make_job(day(4), org_id=None, org_name=None)

        data = get(authenticated_client)
        assert data["active_organizations"] == 2

    def test_featured_template_is_most_run_ties_broken_by_id(self, authenticated_client):
        for _ in range(3):
            make_job(day(1), template_id=2, template_name="Bravo")
        for _ in range(3):
            make_job(day(2), template_id=1, template_name="Alpha")
        make_job(day(3), template_id=3, template_name="Charlie")

        featured = get(authenticated_client)["featured_template"]
        assert featured == {"id": 1, "name": "Alpha", "run_count": 3}

    def test_featured_template_null_when_no_runs(self, authenticated_client):
        assert get(authenticated_client)["featured_template"] is None

    def test_featured_template_ignores_runs_with_no_template(self, authenticated_client):
        for _ in range(5):
            make_job(day(1), template_id=None, template_name="")
        make_job(day(2), template_id=7, template_name="Deploy")

        featured = get(authenticated_client)["featured_template"]
        assert featured == {"id": 7, "name": "Deploy", "run_count": 1}

    def test_featured_template_survives_rename_mid_window(self, authenticated_client):
        for _ in range(2):
            make_job(day(1), template_id=5, template_name="Deploy")
        for _ in range(2):
            make_job(day(10), template_id=5, template_name="Deploy v2")
        for _ in range(3):
            make_job(day(2), template_id=6, template_name="Other")

        featured = get(authenticated_client)["featured_template"]
        # 4 runs on id 5 beat 3 on id 6 even though id 5's name changed; the
        # latest known name is reported.
        assert featured == {"id": 5, "name": "Deploy v2", "run_count": 4}


class TestEnterpriseStreak:
    def test_daily_series_covers_30_days_zero_filled(self, authenticated_client):
        make_job(day(0))
        make_job(day(15))

        streak = get(authenticated_client)["enterprise_streak"]
        assert len(streak["daily"]) == 30
        assert streak["daily"][0] == {"date": str(WINDOW_START), "successful_runs": 1}
        assert streak["daily"][1]["successful_runs"] == 0
        assert streak["daily"][15]["successful_runs"] == 1

    def test_streak_counts_consecutive_days_ending_today(self, authenticated_client):
        for offset in (27, 28, 29):  # days 28, 29, 30 (== today)
            make_job(day(offset))

        assert get(authenticated_client)["enterprise_streak"]["streak"] == 3

    def test_streak_is_zero_when_today_has_no_runs(self, authenticated_client):
        for offset in range(29):  # every day except today
            make_job(day(offset))

        assert get(authenticated_client)["enterprise_streak"]["streak"] == 0

    def test_failed_runs_do_not_extend_streak(self, authenticated_client):
        make_job(day(29), status=JobStatusChoices.FAILED)
        assert get(authenticated_client)["enterprise_streak"]["streak"] == 0


class TestOrganizationLeaderboard:
    def test_org_streak_scoped_to_busiest_org(self, authenticated_client):
        for _ in range(5):
            make_job(day(29), org_id=2, org_name="Busy")
        make_job(day(29), org_id=3, org_name="Quiet")

        org_streak = get(authenticated_client)["org_streak"]
        assert org_streak["organization"] == {"id": 2, "name": "Busy", "run_count": 5}
        assert org_streak["streak"] == 1
        assert sum(d["successful_runs"] for d in org_streak["daily"]) == 5

    def test_org_streak_null_without_org_data(self, authenticated_client):
        make_job(day(1), org_id=None, org_name=None)
        assert get(authenticated_client)["org_streak"] is None

    def test_leaderboard_ranks_and_totals(self, authenticated_client):
        make_job(day(1), org_id=1, org_name="Org A")
        make_job(day(1), org_id=1, org_name="Org A")
        make_job(day(1), org_id=2, org_name="Org B")

        board = get(authenticated_client)["organization_leaderboard"]
        assert board["total_organizations"] == 2
        assert board["user_organization_rank"] == 1
        assert board["leaderboard"] == [
            {"rank": 1, "name": "Org A", "runs": 2},
            {"rank": 2, "name": "Org B", "runs": 1},
        ]

    def test_leaderboard_ties_broken_alphabetically(self, authenticated_client):
        make_job(day(1), org_id=10, org_name="Zeta")
        make_job(day(1), org_id=11, org_name="Alpha")

        names = [row["name"] for row in get(authenticated_client)["organization_leaderboard"]["leaderboard"]]
        assert names == ["Alpha", "Zeta"]

    def test_leaderboard_places_unnamed_organizations_after_named_ones(self, authenticated_client):
        make_job(day(1), org_id=10, org_name=None)
        make_job(day(1), org_id=11, org_name="Alpha")

        names = [row["name"] for row in get(authenticated_client)["organization_leaderboard"]["leaderboard"]]
        assert names == ["Alpha", None]

    def test_leaderboard_capped_at_10(self, authenticated_client):
        for org_id in range(1, 16):
            make_job(day(1), org_id=org_id, org_name=f"Org {org_id:02d}")

        board = get(authenticated_client)["organization_leaderboard"]
        assert len(board["leaderboard"]) == 10
        assert board["total_organizations"] == 15


class TestActivityLevels:
    def test_shape_and_ids(self, authenticated_client, user):
        make_job(day(1), **_me(user))
        dims = get(authenticated_client)["activity_levels"]
        assert [d["id"] for d in dims] == ["volume", "breadth", "consistency"]

    def test_volume_ranks_by_successful_run_count(self, authenticated_client, user):
        for _ in range(3):
            make_job(day(1), **_me(user))
        for _ in range(5):
            make_job(day(1), launched_by_id=2, launched_by_username="other")

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["total_users"] == 2
        assert volume["current_user_rank"] == 2
        assert volume["leaderboard"][0] == {"rank": 1, "username": "OT", "runs": 5}
        assert volume["leaderboard"][1] == {
            "rank": 2,
            "username": user.username,
            "runs": 3,
            "is_current_user": True,
        }

    def test_breadth_counts_distinct_templates(self, authenticated_client, user):
        for template_id in (1, 2, 3):
            make_job(day(1), template_id=template_id, **_me(user))
        make_job(day(2), template_id=1, **_me(user))

        breadth = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "breadth")
        assert breadth["leaderboard"][0]["runs"] == 3

    def test_consistency_counts_distinct_active_days(self, authenticated_client, user):
        for offset in (1, 1, 2, 5):
            make_job(day(offset), **_me(user))

        consistency = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "consistency")
        assert consistency["leaderboard"][0]["runs"] == 3

    def test_grouped_by_launched_by_id_not_username(self, authenticated_client):
        # Same user id, username changed mid-window -> one leaderboard row.
        make_job(day(1), launched_by_id=7, launched_by_username="old_name")
        make_job(day(2), launched_by_id=7, launched_by_username="new_name")

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["total_users"] == 1
        assert volume["leaderboard"][0]["runs"] == 2

    def test_masked_initials_use_latest_username_not_lexicographic_max(self, authenticated_client):
        make_job(day(1), launched_by_id=5, launched_by_username="Zoe")
        make_job(day(2), launched_by_id=5, launched_by_username="Adam")  # renamed later

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["leaderboard"][0]["username"] == "AD"  # from the most recent run, not max("Zoe","Adam")

    def test_jobs_without_launched_by_id_are_excluded(self, authenticated_client, user):
        make_job(day(1), **_me(user))
        make_job(day(1), launched_by_id=None, launched_by_username=None)

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["total_users"] == 1

    def test_current_user_rank_none_when_user_has_no_runs(self, authenticated_client):
        make_job(day(1), launched_by_id=2, launched_by_username="other")

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["current_user_rank"] is None

    def test_other_usernames_masked_to_initials(self, authenticated_client):
        make_job(day(1), launched_by_id=2, launched_by_username="alice")

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["leaderboard"][0]["username"] == "AL"
        assert "is_current_user" not in volume["leaderboard"][0]


class TestUserAchievements:
    def test_empty_without_successful_runs(self, authenticated_client, user):
        make_job(day(1), status=JobStatusChoices.FAILED, **_me(user))
        assert get(authenticated_client)["user_achievements"] == []

    def test_ignition_on_first_successful_run(self, authenticated_client, user):
        make_job(day(1), **_me(user))
        assert "ignition" in get(authenticated_client)["user_achievements"]

    def test_achievements_follow_launched_by_id_across_rename(self, authenticated_client, user):
        # Username changed mid-window; the whole history must still count because
        # the current user is matched by id, not by the current username.
        for offset in range(20):
            name = "old_name" if offset < 10 else user.username
            make_job(day(offset), launched_by_id=user.id, launched_by_username=name)

        assert "reliable" in get(authenticated_client)["user_achievements"]  # needs 20 successful runs

    def test_week_warrior_needs_7_consecutive_days(self, authenticated_client, user):
        for offset in range(6):
            make_job(day(offset), **_me(user))
        assert "week_warrior" not in get(authenticated_client)["user_achievements"]

        make_job(day(6), **_me(user))
        assert "week_warrior" in get(authenticated_client)["user_achievements"]

    def test_month_warrior_needs_all_30_days(self, authenticated_client, user):
        for offset in range(29):
            make_job(day(offset), **_me(user))
        assert "month_warrior" not in get(authenticated_client)["user_achievements"]

        make_job(day(29), **_me(user))
        assert "month_warrior" in get(authenticated_client)["user_achievements"]

    def test_explorer_needs_5_distinct_templates(self, authenticated_client, user):
        for template_id in range(1, 5):
            make_job(day(1), template_id=template_id, **_me(user))
        assert "explorer" not in get(authenticated_client)["user_achievements"]

        make_job(day(1), template_id=5, **_me(user))
        assert "explorer" in get(authenticated_client)["user_achievements"]

    def test_centurion_needs_100_successful_runs(self, authenticated_client, user):
        for _ in range(99):
            make_job(day(1), **_me(user))
        assert "centurion" not in get(authenticated_client)["user_achievements"]

        make_job(day(1), **_me(user))
        assert "centurion" in get(authenticated_client)["user_achievements"]

    def test_reliable_needs_20_successful_runs(self, authenticated_client, user):
        for _ in range(19):
            make_job(day(1), **_me(user))
        assert "reliable" not in get(authenticated_client)["user_achievements"]

        make_job(day(1), **_me(user))
        assert "reliable" in get(authenticated_client)["user_achievements"]

    def test_reliable_needs_the_20_successes_to_be_consecutive(self, authenticated_client, user):
        base = day(10)
        # 15 successes, a failure, then 15 more successes: the longest unbroken
        # run of successes is 15, so the badge is not earned despite 30 successes.
        for i in range(15):
            make_job(base + datetime.timedelta(minutes=i), **_me(user))
        make_job(base + datetime.timedelta(minutes=15), status=JobStatusChoices.FAILED, **_me(user))
        for i in range(16, 31):
            make_job(base + datetime.timedelta(minutes=i), **_me(user))
        assert "reliable" not in get(authenticated_client)["user_achievements"]

        # Five more back-to-back successes take the trailing run to 20.
        for i in range(31, 36):
            make_job(base + datetime.timedelta(minutes=i), **_me(user))
        assert "reliable" in get(authenticated_client)["user_achievements"]

    @pytest.mark.parametrize(
        "breaker",
        [JobStatusChoices.FAILED, JobStatusChoices.ERROR, JobStatusChoices.CANCELED],
    )
    def test_reliable_streak_reset_by_any_non_successful_run(self, authenticated_client, user, breaker):
        base = day(10)
        for i in range(19):
            make_job(base + datetime.timedelta(minutes=i), **_me(user))
        make_job(base + datetime.timedelta(minutes=19), status=breaker, **_me(user))
        for i in range(20, 39):
            make_job(base + datetime.timedelta(minutes=i), **_me(user))
        # 19 successes on either side of the interruption - never 20 in a row.
        assert "reliable" not in get(authenticated_client)["user_achievements"]

    def test_accelerator_needs_more_in_second_half(self, authenticated_client, user):
        make_job(day(1), **_me(user))  # first half (days 1-15)
        make_job(day(20), **_me(user))  # second half (days 16-30)
        make_job(day(21), **_me(user))
        assert "accelerator" in get(authenticated_client)["user_achievements"]

    def test_no_accelerator_when_front_loaded(self, authenticated_client, user):
        make_job(day(1), **_me(user))
        make_job(day(2), **_me(user))
        make_job(day(20), **_me(user))
        assert "accelerator" not in get(authenticated_client)["user_achievements"]

    def test_returned_in_canonical_order(self, authenticated_client, user):
        for offset in range(30):  # month_warrior + week_warrior + ignition
            make_job(day(offset), template_id=(offset % 6) + 1, **_me(user))  # explorer
        for _ in range(100):  # centurion + reliable
            make_job(day(29), **_me(user))

        achievements = get(authenticated_client)["user_achievements"]
        assert achievements == sorted(
            achievements,
            key=["ignition", "week_warrior", "month_warrior", "explorer", "centurion", "reliable", "accelerator"].index,
        )


class TestOrgAchievements:
    def test_empty_without_org_data(self, authenticated_client):
        make_job(day(1), org_id=None, org_name=None)
        assert get(authenticated_client)["org_achievements"] == []

    def test_top_tier_when_org_ranked_top_3(self, authenticated_client):
        make_job(day(1), org_id=1, org_name="Org One")
        assert "top_tier" in get(authenticated_client)["org_achievements"]

    def test_sustained_needs_14_consecutive_days(self, authenticated_client):
        for offset in range(13):
            make_job(day(offset), org_id=1, org_name="Org One")
        assert "sustained" not in get(authenticated_client)["org_achievements"]

        make_job(day(13), org_id=1, org_name="Org One")
        assert "sustained" in get(authenticated_client)["org_achievements"]

    def test_rising_when_second_half_busier(self, authenticated_client):
        make_job(day(1), org_id=1, org_name="Org One")
        make_job(day(20), org_id=1, org_name="Org One")
        make_job(day(21), org_id=1, org_name="Org One")
        assert "rising" in get(authenticated_client)["org_achievements"]

    def test_no_rising_when_first_half_busier(self, authenticated_client):
        make_job(day(1), org_id=1, org_name="Org One")
        make_job(day(2), org_id=1, org_name="Org One")
        make_job(day(20), org_id=1, org_name="Org One")
        assert "rising" not in get(authenticated_client)["org_achievements"]


class TestEdgeCases:
    @pytest.mark.parametrize(
        "status",
        [JobStatusChoices.ERROR, JobStatusChoices.CANCELED, JobStatusChoices.RUNNING, JobStatusChoices.PENDING],
    )
    def test_non_successful_statuses_are_ignored_everywhere(self, authenticated_client, user, status):
        make_job(day(5), status=status, **_me(user))

        data = get(authenticated_client)
        assert data["job_runs"] == 0
        assert data["org_streak"] is None
        assert data["user_achievements"] == []
        assert all(d["total_users"] == 0 for d in data["activity_levels"])

    def test_runs_bucket_by_utc_calendar_day(self, authenticated_client):
        # 23:00 and 00:00 of the same UTC day -> one day; 00:00 next day -> separate.
        make_job(day(5, hour=0))
        make_job(day(5, hour=23))
        make_job(day(6, hour=0))

        daily = get(authenticated_client)["enterprise_streak"]["daily"]
        assert daily[5]["successful_runs"] == 2
        assert daily[6]["successful_runs"] == 1

    def test_window_lower_bound_is_inclusive_midnight(self, authenticated_client):
        make_job(day(0, hour=0))  # exactly window_start 00:00 UTC -> in
        one_sec_before = day(0, hour=0) - datetime.timedelta(seconds=1)
        make_job(one_sec_before)  # 23:59:59 the day before -> out

        data = get(authenticated_client)
        assert data["job_runs"] == 1
        assert data["enterprise_streak"]["daily"][0]["successful_runs"] == 1

    def test_window_upper_bound_excludes_future_finished(self, authenticated_client):
        make_job(day(29))  # today -> in
        make_job(FIXED_NOW + datetime.timedelta(hours=1))  # later today, not yet happened -> out
        make_job(FIXED_NOW + datetime.timedelta(days=5))  # future -> out

        data = get(authenticated_client)
        assert data["job_runs"] == 1
        assert data["featured_template"]["run_count"] == 1
        assert sum(d["successful_runs"] for d in data["enterprise_streak"]["daily"]) == 1

    def test_streak_only_counts_the_run_ending_today(self, authenticated_client):
        for offset in (10, 11, 12):  # earlier island, broken by a gap
            make_job(day(offset))
        for offset in (27, 28, 29):  # run ending today
            make_job(day(offset))

        assert get(authenticated_client)["enterprise_streak"]["streak"] == 3

    def test_streak_broken_by_a_single_missing_day(self, authenticated_client):
        for offset in (25, 26, 28, 29):  # day 27 missing
            make_job(day(offset))

        assert get(authenticated_client)["enterprise_streak"]["streak"] == 2

    def test_week_warrior_requires_consecutive_not_just_seven_active_days(self, authenticated_client, user):
        for offset in (0, 2, 4, 6, 8, 10, 12):  # 7 active days, none adjacent
            make_job(day(offset), **_me(user))

        assert "week_warrior" not in get(authenticated_client)["user_achievements"]

    def test_accelerator_not_earned_on_exact_tie(self, authenticated_client, user):
        make_job(day(1), **_me(user))
        make_job(day(2), **_me(user))
        make_job(day(20), **_me(user))
        make_job(day(21), **_me(user))

        assert "accelerator" not in get(authenticated_client)["user_achievements"]

    def test_current_user_rank_reported_when_outside_top_10(self, authenticated_client, user):
        for uid in range(2, 13):  # 11 other users, each busier than the current user
            for _ in range(20 - uid):
                make_job(day(1), launched_by_id=1000 + uid, launched_by_username=f"user{uid}")
        make_job(day(1), **_me(user))  # 1 run -> last

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["total_users"] == 12
        assert volume["current_user_rank"] == 12
        assert len(volume["leaderboard"]) == 10
        assert all(row["username"] != user.username for row in volume["leaderboard"])

    def test_activity_row_with_id_but_blank_username_does_not_crash(self, authenticated_client, user):
        make_job(day(1), launched_by_id=9, launched_by_username="")
        make_job(day(1), launched_by_id=8, launched_by_username=None)
        make_job(day(1), **_me(user))

        volume = next(d for d in get(authenticated_client)["activity_levels"] if d["id"] == "volume")
        assert volume["total_users"] == 3
        assert {row["username"] for row in volume["leaderboard"]} == {"", "testuser"}

    def test_org_sustained_requires_consecutive_days(self, authenticated_client):
        for offset in range(0, 28, 2):  # 14 active days, every other day
            make_job(day(offset), org_id=1, org_name="Org One")

        assert "sustained" not in get(authenticated_client)["org_achievements"]


class TestResponseShape:
    def test_all_top_level_keys_present(self, authenticated_client):
        make_job(day(1), launched_by_id=1, launched_by_username="testuser")

        data = get(authenticated_client)
        assert set(data) == {
            "job_runs",
            "active_organizations",
            "featured_template",
            "enterprise_streak",
            "org_streak",
            "organization_leaderboard",
            "org_achievements",
            "activity_levels",
            "user_achievements",
        }

    def test_empty_database_is_a_valid_response(self, authenticated_client):
        data = get(authenticated_client)
        assert data["job_runs"] == 0
        assert data["active_organizations"] == 0
        assert data["featured_template"] is None
        assert data["org_streak"] is None
        assert data["organization_leaderboard"]["leaderboard"] == []
        assert data["user_achievements"] == []
        assert data["org_achievements"] == []
        assert len(data["enterprise_streak"]["daily"]) == 30
        assert all(len(d["leaderboard"]) == 0 for d in data["activity_levels"])

    def test_query_count_is_bounded_regardless_of_data_volume(
        self, authenticated_client, user, django_assert_max_num_queries
    ):
        # Every metric is a rollup of a handful of window-wide GROUP BYs; the
        # query count must not grow with the number of jobs, users or orgs.
        for offset in range(0, 30, 2):
            for uid in range(1, 6):
                for org in range(1, 4):
                    make_job(
                        day(offset),
                        org_id=org,
                        org_name=f"Org {org}",
                        template_id=uid,
                        launched_by_id=uid,
                        launched_by_username=f"user{uid}",
                    )
            # One run as the authenticated user so the per-user achievements path
            # (including the "reliable" streak scan) is exercised too.
            make_job(day(offset), org_id=1, org_name="Org 1", template_id=offset % 5 + 1, **_me(user))
        with django_assert_max_num_queries(13):  # 12 data queries today (incl. SHOW_LEADERBOARD flag), 1 spare
            assert authenticated_client.get(URL).status_code == 200
