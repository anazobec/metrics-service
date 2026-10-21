"""Analytics storage models (ANSTRAT-1587 / AAP-87799)."""

from django.db import models
from django.db.models import F, Sum

# Reuse the same DAB base classes / fallbacks as the tasks app.
try:
    from ansible_base.activitystream.models import AuditableModel
    from ansible_base.lib.abstract_models import CommonModel
except ImportError:  # pragma: no cover - simple fallback for setups without DAB

    class CommonModel(models.Model):
        created = models.DateTimeField(auto_now_add=True)
        modified = models.DateTimeField(auto_now=True)

        class Meta:
            abstract = True

    class AuditableModel(models.Model):
        class Meta:
            abstract = True


# Default origin for a local install.
LOCAL_SOURCE = "local"
HTTP_STATUS_CLASSES = (1, 2, 3, 4, 5)


class AnalyticsPayload(CommonModel, AuditableModel):
    """Raw pre-``prepare()`` collector payload for the analytics API.

    Every successful collection is retained, including repeated or unbounded windows. The
    ``payload`` holds the JSON-serialisable form of the collector's ``gather()`` output — a list
    of record dicts for DataFrame collectors, or a dict for the ``config`` snapshot collector.
    """

    class Meta:
        app_label = "analytics"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["created"]),
            models.Index(fields=["collector", "since"]),
            models.Index(fields=["collector", "until"]),
            models.Index(fields=["collector", "started_at"]),
        ]
        verbose_name = "Analytics Payload"
        verbose_name_plural = "Analytics Payloads"

    # Identification
    collector = models.CharField(max_length=128, help_text="Collector name (collector_type / API path segment)")

    source = models.CharField(
        max_length=64,
        default=LOCAL_SOURCE,
        help_text="Originating platform component, such as local, controller, eda, or hub.",
    )
    cluster_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Controller installation UUID (INSTALL_UUID) read from the AWX database, if available.",
    )

    # Collection window as passed to the collector. Both nullable — some collectors
    # (snapshots such as config) do not accept a since/until window.
    since = models.DateTimeField(null=True, blank=True, help_text="`since` passed to the collector (inclusive), if any")
    until = models.DateTimeField(null=True, blank=True, help_text="`until` passed to the collector (exclusive), if any")

    # When the gather() call ran.
    started_at = models.DateTimeField(help_text="When collection (gather) started")
    finished_at = models.DateTimeField(help_text="When collection (gather) finished")

    # Data
    payload = models.JSONField(default=dict, help_text="Raw pre-prepare() gather() output, JSON-serialisable")

    def __str__(self) -> str:
        """Return a readable representation: collector + window (or 'snapshot')."""
        window = f"{self.since} → {self.until}" if self.since or self.until else "snapshot"
        return f"{self.collector} [{self.source}] ({window})"


class APIUsageCounter(models.Model):
    """Daily request counts for API routes served by metrics-service."""

    STATUS_CLASSES = HTTP_STATUS_CLASSES

    class Meta:
        app_label = "analytics"
        constraints = [
            models.UniqueConstraint(
                fields=["usage_date", "endpoint_name", "status_class"],
                name="api_usage_counter_unique",
            )
        ]
        verbose_name = "API Usage Counter"
        verbose_name_plural = "API Usage Counters"

    usage_date = models.DateField(help_text="UTC date on which the API request completed")
    endpoint_name = models.CharField(
        max_length=512,
        help_text="Stable resolved view name or route template; contains no request parameters",
    )
    status_class = models.PositiveSmallIntegerField(
        choices=[(status_class, f"{status_class}xx") for status_class in HTTP_STATUS_CLASSES],
    )
    request_count = models.PositiveBigIntegerField(default=0)

    @classmethod
    def record_request(cls, usage_date, endpoint_name: str, status_class: int) -> None:
        """Atomically increment one daily endpoint/status counter."""
        row, _ = cls.objects.get_or_create(
            usage_date=usage_date,
            endpoint_name=endpoint_name,
            status_class=status_class,
            defaults={"request_count": 0},
        )
        cls.objects.filter(pk=row.pk).update(
            request_count=F("request_count") + 1,
        )

    @classmethod
    def empty_daily_summary(cls, available: bool = True) -> dict:
        """Return the stable, zero-filled shape used by the Segment payload."""
        return {
            "available": available,
            "total_requests": 0,
            "by_endpoint": {},
        }

    @classmethod
    def daily_summary(cls, usage_date) -> dict:
        """Aggregate request counts for one UTC date without exposing request details."""
        summary = cls.empty_daily_summary()
        rows = (
            cls.objects.filter(usage_date=usage_date)
            .values("endpoint_name", "status_class")
            .annotate(total=Sum("request_count"))
        )
        for row in rows:
            endpoint_name = row["endpoint_name"]
            status_key = f"{row['status_class']}xx"
            count = row["total"] or 0
            endpoint_counts = summary["by_endpoint"].setdefault(
                endpoint_name,
                {f"{status_class}xx": 0 for status_class in cls.STATUS_CLASSES},
            )
            endpoint_counts[status_key] = count
            summary["total_requests"] += count
        return summary
