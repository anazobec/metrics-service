import decimal
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.dashboard_reports.filters import DateFilter
from apps.dashboard_reports.models import JobData

# Provisional lookback window until AAP-88669's gateway-backed membership lands.
MEMBERSHIP_LOOKBACK_DAYS = DateFilter.get_num_last_days(DateFilter.LAST_30_DAYS.value)


def get_member_organizations(launched_by_id: int, days: int = MEMBERSHIP_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """
    Return organizations the given user has launched jobs in within the lookback window.

    This is a provisional stand-in for authoritative gateway-sourced organization
    membership (AAP-88669): the gateway service (see ansible/aap-dev) is the real
    source of truth for user<->organization membership, but isn't available in every
    dev/deployment setup yet. Derived instead from ``JobData.launched_by_id`` /
    ``organization_id``, which AWX only populates for jobs that carry launch info
    (e.g. relaunched/scheduled jobs may not), and ``JobData`` rows are periodically
    purged by the retention cleanup task - so this can under-report membership for
    infrequent or inactive users. Only reliable enough for self-service ("me") use;
    do not use this for other users until gateway integration lands.
    """
    since = timezone.now() - timedelta(days=days)
    rows = (
        JobData.objects.filter(launched_by_id=launched_by_id)
        .after_date(since)
        .exclude(organization_id__isnull=True)
        .values("organization_id", "organization_name")
        .distinct()
        .order_by("organization_name")
    )
    return [{"id": row["organization_id"], "name": row["organization_name"]} for row in rows]


def sec2time(sec: decimal.Decimal | int | float) -> str:
    """
    Convert a number of seconds into a human-readable string (e.g. "2h 5min 30sec").
    Rounds to whole seconds before splitting to avoid rollover like "59min 60sec".
    """
    total_seconds = int(decimal.Decimal(str(sec)).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}min {seconds}sec" if hours > 0 else f"{minutes}min {seconds}sec"


__all__ = ["sec2time", "get_member_organizations"]
