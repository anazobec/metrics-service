"""Top-level URL wiring for the analytics app.

Auto-discovered by the framework (via ``project_applications`` / ``LOADED_APPS``); no edit to
``metrics_service/urls.py`` is needed.
"""

from django.urls import include, path

app_name = "analytics"

urlpatterns = [
    path("api/v1/analytics/", include("apps.analytics.v1.urls", namespace="v1")),
]
