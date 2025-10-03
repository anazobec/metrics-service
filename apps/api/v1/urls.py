"""
URL configuration for API v1.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ConfigView, OrganizationViewSet, UserViewSet

app_name = "v1"

# Create DRF router for non-nested endpoints
router = DefaultRouter()
router.register(r"users", UserViewSet, basename="user")
router.register(r"organizations", OrganizationViewSet, basename="organization")
router.register(r"config", ConfigView, basename="config")

urlpatterns = [
    # Nested API endpoints
    path("tasks/", include("apps.api.v1.tasks.urls", namespace="tasks")),
    # Include router URLs for other endpoints
    path("", include(router.urls)),
]
