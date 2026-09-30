from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.db import models
from django.db.models import Count
from django.db.models.functions import TruncDate
from drf_spectacular.helpers import forced_singular_serializer
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.dashboard_reports.models import JobData, JobStatusChoices
from apps.dashboard_reports.serializers import DashboardLeaderboardsSerializer
from apps.tasks.task_groups import get_feature_enabled_from_db

logger = logging.getLogger(__name__)


# Order in which earned achievements are returned.
_ACHIEVEMENTS: tuple[str, ...] = (
    "ignition",
    "week_warrior",
    "month_warrior",
    "explorer",
    "centurion",
    "reliable",
    "accelerator",
)


def _user_achievements(
    successful_runs: models.QuerySet[JobData],
    current_user_id: int | None,
    today: date,
    window_start: date,
    now_utc: datetime,
) -> list[str]:
    """Return the achievement ids the current user has earned in the window.

    Counts and distinct tallies are aggregated in the database rather than
    materializing the user's (potentially large) run history in Python. The
    "reliable" streak is the one exception - it needs run order, so it scans the
    ordered, single-column status list of the user's finished runs (see
    ``_longest_successful_run_streak``). ``successful_runs`` is already bounded to
    the shared ``window_start``...now window (the leaderboard only ever considers
    successful runs).
    """
    if current_user_id is None:
        return []
    user_runs = successful_runs.filter(launched_by_id=current_user_id)
    total_runs = user_runs.count()
    if not total_runs:
        return []

    window_start_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)
    window_days = (today - window_start).days + 1
    midpoint = window_start_dt + timedelta(days=window_days / 2)

    # Distinct UTC calendar days with a successful run - bounded to at most
    # `window_days` (30) rows, unlike `total_runs` which can be arbitrarily large.
    active_days = set(
        user_runs.annotate(day=TruncDate("finished", tzinfo=UTC)).order_by().values_list("day", flat=True).distinct()
    )
    distinct_templates = user_runs.filter(template_id__isnull=False).values("template_id").distinct().count()
    first_half = user_runs.filter(finished__lt=midpoint).count()

    earned = {
        "ignition": True,
        "week_warrior": _max_consecutive_days(active_days) >= 7,
        "month_warrior": len(active_days) == window_days,
        "explorer": distinct_templates >= 5,
        "centurion": total_runs >= 100,
        # 20+ successful runs back-to-back with no failed/errored/canceled run
        # between them, anywhere in the window.
        "reliable": _longest_successful_run_streak(current_user_id, window_start_dt, now_utc) >= _RELIABLE_STREAK,
        "accelerator": (total_runs - first_half) > first_half,
    }
    return [achievement for achievement in _ACHIEVEMENTS if earned[achievement]]


# Order in which earned org achievements are returned.
_ORG_ACHIEVEMENTS: tuple[str, ...] = ("sustained", "rising", "top_tier")


def _org_achievements(org_streak: dict[str, Any] | None, org_rank: int | None) -> list[str]:
    """Return the achievement ids the org_streak organization has earned.

    Derived from the org's ``daily`` successful-run series (already computed
    for ``org_streak``) plus its current leaderboard rank.
    """
    if not org_streak:
        return []

    daily = org_streak["daily"]
    half = len(daily) // 2

    active_days = {entry["date"] for entry in daily if entry["successful_runs"] > 0}
    earned = {
        # 14+ consecutive UTC calendar days with a successful run in the window.
        "sustained": _max_consecutive_days(active_days) >= 14,
        # More successful runs in the second half of the window than the first.
        "rising": sum(e["successful_runs"] for e in daily[half:]) > sum(e["successful_runs"] for e in daily[:half]),
        # Sync-point rank history is not stored, so use the current standing:
        # ranked in the top 3 of the org leaderboard.
        "top_tier": org_rank is not None and org_rank <= 3,
    }
    return [achievement for achievement in _ORG_ACHIEVEMENTS if earned[achievement]]


# Job statuses that represent a finished run with a definitive outcome. Anything
# here that is not ``SUCCESSFUL`` (a failure, an error or a cancellation) breaks a
# user's run of consecutive successful jobs.
_OUTCOME_STATUSES: tuple[str, ...] = (
    JobStatusChoices.SUCCESSFUL,
    JobStatusChoices.FAILED,
    JobStatusChoices.ERROR,
    JobStatusChoices.CANCELED,
)

# Consecutive successful runs required for the "reliable" achievement.
_RELIABLE_STREAK = 20


def _longest_successful_run_streak(current_user_id: int, since: datetime, until: datetime) -> int:
    """Length of the longest unbroken run of successful jobs for the user.

    Consecutiveness is judged over the user's finished runs in the window ordered
    by ``finished``: any non-successful outcome (failed, errored or canceled)
    between two successes resets the count, so 10 successes, a failure, then 10
    successes is a streak of 10, not 20. Only the ordered status column is
    fetched - one short row per finished run for this single user - and the tally
    is one linear pass.
    """
    statuses = (
        JobData.objects.filter(
            launched_by_id=current_user_id,
            finished__gte=since,
            finished__lte=until,
            status__in=_OUTCOME_STATUSES,
        )
        .order_by("finished", "job_id")
        .values_list("status", flat=True)
    )
    longest = current = 0
    for run_status in statuses:
        current = current + 1 if run_status == JobStatusChoices.SUCCESSFUL else 0
        longest = max(longest, current)
    return longest


def _max_consecutive_days(days: set[date]) -> int:
    """Length of the longest run of consecutive calendar days in ``days``."""
    longest = run = 0
    previous: date | None = None
    for day in sorted(days):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    return longest


def _activity_level(
    metric: str,
    ranked: list[dict[str, Any]],
    usernames_by_id: dict[int, str | None],
    current_user_id: int | None,
) -> dict[str, Any]:
    """Build a per-user leaderboard for one activity level.

    ``ranked`` is the shared per-user aggregate (keyed by ``launched_by_id``)
    already sorted by this activity level's metric descending, ties broken by id.
    Returns the top 10, the current user's rank (``None`` when they have no
    successful runs in the window) and ``total_users`` (the pool the rank is
    out of, i.e. "rank N of X").
    """
    leaderboard: list[dict[str, Any]] = []
    current_user_rank: int | None = None
    for rank, row in enumerate(ranked, start=1):
        is_current_user = row["launched_by_id"] == current_user_id
        if is_current_user:
            current_user_rank = rank
        if rank <= 10:
            display_name = usernames_by_id.get(row["launched_by_id"]) or ""
            entry: dict[str, Any] = {
                "rank": rank,
                # Other users are shown as their initials only (first two
                # letters, upper-cased); the current user sees their username.
                "username": display_name if is_current_user else display_name[:2].upper(),
                "runs": row[metric],
            }
            if is_current_user:
                entry["is_current_user"] = True
            leaderboard.append(entry)

    return {
        "id": metric,
        "current_user_rank": current_user_rank,
        "total_users": len(ranked),
        "leaderboard": leaderboard,
    }


def _streak_series(counts_by_day: dict[date, int], window_dates: list[date]) -> dict[str, Any]:
    """Zero-fill per-day successful-run counts over the window and derive the streak.

    ``counts_by_day`` is an in-memory rollup of the shared ``(day, org)``
    GROUP BY. ``streak`` counts consecutive active days ending today — a day
    with no runs (today included) ends it.
    """
    daily = [{"date": day, "successful_runs": counts_by_day.get(day, 0)} for day in window_dates]

    streak = 0
    for entry in reversed(daily):
        if entry["successful_runs"] == 0:
            break
        streak += 1

    return {"streak": streak, "daily": daily}


def _featured_template_stats(successful_runs: models.QuerySet[JobData]) -> dict[str, Any] | None:
    """Return the most-used successful template and its latest display name."""
    # Exclude ad-hoc runs and group only by template_id so renames stay one row.
    # Break ties by id for a deterministic pick. Keep this query separate from
    # the day/organization rollup: grouping that by template would multiply rows.
    featured_template = (
        successful_runs.filter(template_id__isnull=False)
        .values("template_id")
        .annotate(run_count=Count("id"))
        .order_by("-run_count", "template_id")
        .first()
    )
    if not featured_template:
        return None

    # Names can change mid-window; use the latest name for display, the id for identity.
    featured_template_name = (
        successful_runs.filter(template_id=featured_template["template_id"])
        .order_by("-finished")
        .values_list("template_name", flat=True)
        .first()
    )
    return {
        "id": featured_template["template_id"],
        "name": featured_template_name,
        "run_count": featured_template["run_count"],
    }


def _build_organization_stats(
    day_org_rows: list[dict[str, Any]], window_dates: list[date]
) -> tuple[dict[str, Any] | None, dict[str, Any], int | None]:
    """Build the organization streak, leaderboard, and current-organization rank."""
    # Per-organization successful-run totals for the leaderboard and the
    # busiest org's streak — an in-memory rollup of day_org_rows by org id.
    # TODO: derive the user's own organization once membership data is
    # ingested; for now everything org-scoped uses the busiest org.
    org_totals: dict[int, dict[str, Any]] = {}
    for row in day_org_rows:
        org_id = row["organization_id"]
        if org_id is None:
            continue
        agg = org_totals.setdefault(
            org_id, {"organization_id": org_id, "organization_name": row["organization_name"], "runs": 0}
        )
        agg["runs"] += row["runs"]

    # -runs, then name (NULL last), then id — a deterministic total order.
    org_rows = sorted(
        org_totals.values(),
        key=lambda row: (
            -row["runs"],
            row["organization_name"] is None,
            row["organization_name"] or "",
            row["organization_id"],
        ),
    )
    top_org = org_rows[0] if org_rows else None

    if top_org:
        top_org_by_day: dict[date, int] = defaultdict(int)
        for row in day_org_rows:
            if row["organization_id"] == top_org["organization_id"]:
                top_org_by_day[row["day"]] += row["runs"]
        org_streak: dict[str, Any] | None = {
            "organization": {
                "id": top_org["organization_id"],
                "name": top_org["organization_name"],
                "run_count": top_org["runs"],
            },
            **_streak_series(top_org_by_day, window_dates),
        }
    else:
        org_streak = None

    # Rank of the user's org in the leaderboard. While ``top_org`` is the
    # busiest org this is always 1; the lookup stays generic so it keeps
    # working once ``top_org`` becomes the user's actual (owned) org.
    user_organization_rank = (
        next(rank for rank, row in enumerate(org_rows, start=1) if row["organization_id"] == top_org["organization_id"])
        if top_org
        else None
    )
    organization_leaderboard = {
        "user_organization_rank": user_organization_rank,
        "total_organizations": len(org_rows),
        "leaderboard": [
            {"rank": rank, "name": row["organization_name"], "runs": row["runs"]}
            for rank, row in enumerate(org_rows[:10], start=1)
        ],
    }
    return org_streak, organization_leaderboard, user_organization_rank


def _build_activity_levels(
    successful_runs: models.QuerySet[JobData], current_user_id: int | None
) -> list[dict[str, Any]]:
    """Build the volume, breadth, and consistency activity leaderboards."""
    per_user_rows = (
        successful_runs.filter(launched_by_id__isnull=False)
        .order_by()
        .values("launched_by_id")
        .annotate(
            volume=Count("id"),  # total successful runs
            breadth=Count("template_id", distinct=True),  # distinct templates
            consistency=Count(TruncDate("finished", tzinfo=UTC), distinct=True),  # active days
        )
    )
    ranked_by_metric = {
        metric: sorted(per_user_rows, key=lambda row, metric=metric: (-row[metric], row["launched_by_id"]))
        for metric in ("volume", "breadth", "consistency")
    }
    # Latest known username, fetched only for the ids that actually surface in
    # a top 10 (<= 30) — identity and ranking are by id, this is display only.
    # An empty ``visible_ids`` makes ``__in`` a no-op (no query).
    visible_ids = {row["launched_by_id"] for ranked in ranked_by_metric.values() for row in ranked[:10]}
    usernames_by_id: dict[int, str | None] = dict(
        successful_runs.filter(launched_by_id__in=visible_ids)
        .order_by("launched_by_id", "-finished")
        .distinct("launched_by_id")
        .values_list("launched_by_id", "launched_by_username")
    )
    return [
        _activity_level(metric, ranked_by_metric[metric], usernames_by_id, current_user_id)
        for metric in ("volume", "breadth", "consistency")
    ]


class DashboardLeaderboardsViewSet(ReadOnlyModelViewSet):
    """Aggregated engagement metrics (counts, streaks, leaderboards, achievements).

    Read-only ``list`` endpoint computed over the trailing 30 UTC calendar days.
    Visible to any authenticated user; other users' identities are shown only as
    initials. Everything is derived from successful ``JobData`` runs.
    """

    permission_classes = [IsAuthenticated]
    pagination_class = None
    serializer_class = DashboardLeaderboardsSerializer
    # Never used (list/retrieve are overridden) — satisfies schema tooling only.
    queryset = JobData.objects.none()
    # list() is fully overridden and ignores query params; drop the default
    # (DAB) filter/ordering/search backends so schema generation does not expose
    # unsupported JobData filter parameters on this endpoint.
    filter_backends = []

    @extend_schema(
        summary="Dashboard leaderboards, streaks and achievements (trailing 30 days)",
        description=(
            "Aggregated engagement metrics over the last 30 UTC calendar days: overall counts, "
            "featured template, enterprise/org automation streaks, the organization leaderboard, "
            "per-user activity levels (volume/breadth/consistency) and earned achievements."
        ),
        responses={
            200: forced_singular_serializer(DashboardLeaderboardsSerializer),
            404: inline_serializer(
                name="DashboardLeaderboardDisabledResponse",
                fields={"detail": serializers.CharField()},
            ),
        },
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Return leaderboard metrics for the trailing 30 days."""

        # Check if leaderboard is enabled at all
        is_leaderboard_enabled = get_feature_enabled_from_db("SHOW_LEADERBOARD", default=True)
        if not is_leaderboard_enabled:
            return Response(
                data={"detail": "Leaderboard not enabled"},
                status=status.HTTP_404_NOT_FOUND,
            )

        now_utc = datetime.now(UTC)
        today = now_utc.date()
        # 30 calendar days (UTC): from window_start 00:00 up to "now". Runs with a
        # future ``finished`` (clock skew, bad data) are excluded so they cannot
        # inflate counts/activity-levels/achievements while the streak series —
        # which only ever runs to today — ignores them. Every metric below shares
        # this window so the daily streak/achievements and the aggregate counts
        # (job_runs, top_org, leaderboards, activity levels) never disagree.
        window_start = today - timedelta(days=29)
        since = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)
        window_dates = [window_start + timedelta(days=offset) for offset in range((today - window_start).days + 1)]

        successful_runs = JobData.objects.filter(
            status=JobStatusChoices.SUCCESSFUL,
            finished__gte=since,
            finished__lte=now_utc,
        )

        # One GROUP BY (day, org) over the window. job_runs, active_organizations,
        # both streak series and the whole org leaderboard are in-memory rollups
        # of these rows — no further per-metric queries. order_by() drops
        # JobData's default -started ordering, which would otherwise leak into the
        # GROUP BY.
        day_org_rows = list(
            successful_runs.order_by()
            .annotate(day=TruncDate("finished", tzinfo=UTC))
            .values("day", "organization_id", "organization_name")
            .annotate(runs=Count("id"))
        )

        stats: dict[str, Any] = {
            "job_runs": sum(row["runs"] for row in day_org_rows),
            # Organizations with at least one successful job run in the window.
            "active_organizations": len(
                {row["organization_id"] for row in day_org_rows if row["organization_id"] is not None}
            ),
        }

        stats["featured_template"] = _featured_template_stats(successful_runs)

        # Enterprise automation streak: per-day totals across every org (runs with
        # no org included), platform-wide.
        enterprise_by_day: dict[date, int] = defaultdict(int)
        for row in day_org_rows:
            enterprise_by_day[row["day"]] += row["runs"]
        stats["enterprise_streak"] = _streak_series(enterprise_by_day, window_dates)

        org_streak, organization_leaderboard, user_organization_rank = _build_organization_stats(
            day_org_rows, window_dates
        )
        stats["org_streak"] = org_streak
        stats["organization_leaderboard"] = organization_leaderboard
        stats["org_achievements"] = _org_achievements(org_streak, user_organization_rank)

        # The local User pk is not the AWX user id (this deployment uses DAB's
        # resource registry, i.e. a separate User table per service), so it
        # cannot be compared to ``launched_by_id`` directly. Resolve the AWX id
        # from the authenticated user's most recent successful run in the
        # window instead - the only link ``JobData`` offers back to a platform
        # identity. ``usernames_by_id`` below is unrelated: display-only (other
        # users' initials).
        current_user_id = (
            successful_runs.filter(launched_by_username=request.user.get_username())
            .order_by("-finished")
            .values_list("launched_by_id", flat=True)
            .first()
        )

        stats["activity_levels"] = _build_activity_levels(successful_runs, current_user_id)

        stats["user_achievements"] = _user_achievements(successful_runs, current_user_id, today, window_start, now_utc)
        return Response(self.get_serializer(stats).data)

    @extend_schema(exclude=True)
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Not supported — this endpoint only exposes the aggregated ``list`` view."""
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
