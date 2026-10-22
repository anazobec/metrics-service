"""URL configuration for dashboard reports API endpoints."""

from ansible_base.lib.routers import AssociationResourceRouter
from django.urls import include, path

from apps.dashboard_reports.viewsets import (
    DashboardCollectionStatusViewSet,
    DashboardReportViewSet,
    DashboardTelemetryViewSet,
    FilterSetsViewSet,
    JobTemplatesViewSet,
    LabelsViewSet,
    OrganizationsViewSet,
    ProjectsViewSet,
    SubscriptionCostViewSet,
    TemplateMetadataViewSet,
)
from apps.dashboard_reports.viewsets.dashboard_leaderboards import DashboardLeaderboardsViewSet

router = AssociationResourceRouter()
router.register(r"organizations", OrganizationsViewSet, basename="organizations")
router.register(r"templates", JobTemplatesViewSet, basename="templates")
router.register(r"projects", ProjectsViewSet, basename="projects")
router.register(r"labels", LabelsViewSet, basename="labels")
router.register(r"report", DashboardReportViewSet, basename="report")
router.register(r"subscription_costs", SubscriptionCostViewSet, basename="subscription_costs")
router.register(r"template_metadata", TemplateMetadataViewSet, basename="template_metadata")
router.register(r"filter_sets", FilterSetsViewSet, basename="filter_sets")
router.register(r"collection_status", DashboardCollectionStatusViewSet, basename="collection_status")
router.register(r"collection_telemetry", DashboardTelemetryViewSet, basename="collection_telemetry")
router.register(r"leaderboard", DashboardLeaderboardsViewSet, basename="leaderboard")

urlpatterns = [
    # The router has no list route for template_metadata (no list/create), so PATCH-all is mapped explicitly.
    path(
        "api/v1/dashboard_reports/template_metadata/",
        TemplateMetadataViewSet.as_view({"patch": "partial_update_all"}),
        name="template_metadata-partial-update-all",
    ),
    path(
        "api/v1/dashboard_reports/",
        include((router.urls, "dashboard_reports"), namespace="v1"),
    ),
]
