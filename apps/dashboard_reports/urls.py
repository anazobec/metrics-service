from ansible_base.lib.routers import AssociationResourceRouter
from django.urls import include, path

from apps.dashboard_reports.viewsets import (
    DashboardReportViewSet,
    JobTemplatesViewSet,
    LabelsViewSet,
    OrganizationsViewSet,
    ProjectsViewSet,
)

app_name = "dashboard_reports"

router = AssociationResourceRouter()
router.register(r"organizations", OrganizationsViewSet, basename="organizations")
router.register(r"templates", JobTemplatesViewSet, basename="templates")
router.register(r"projects", ProjectsViewSet, basename="projects")
router.register(r"labels", LabelsViewSet, basename="labels")
router.register(r"report", DashboardReportViewSet, basename="report")
router.register(r"export", DashboardReportExportViewSet, basename="export")

urlpatterns = [
    # Dashboard report endpoints at /api/metrics/v1/
    path("api/metrics/v1/dashboard_reports/", include(router.urls)),
]
