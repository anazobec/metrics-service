"""Tests for the analytics app configuration."""

import pytest

from apps.analytics.apps import AnalyticsConfig


@pytest.mark.unit
def test_analytics_app_config():
    assert AnalyticsConfig.name == "apps.analytics"
    assert AnalyticsConfig.default_auto_field == "django.db.models.BigAutoField"
    assert AnalyticsConfig.verbose_name == "Analytics"
