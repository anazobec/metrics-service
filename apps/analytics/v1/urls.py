"""v1 URL routes for the analytics API."""

from django.urls import path

from apps.analytics.v1 import views

app_name = "analytics_v1"

urlpatterns = [
    path("", views.AnalyticsRootView.as_view(), name="root"),
    path("<str:collector>/collect/", views.CollectorCollectView.as_view(), name="collect"),
    path("<str:collector>/", views.CollectorRowsView.as_view(), name="rows"),
]
