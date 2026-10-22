"""ViewSet for retrieving and updating AWX job template metadata (time estimates)."""

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.mixins import RetrieveModelMixin, UpdateModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.dashboard_reports.models import TemplateMetadata
from apps.dashboard_reports.serializers import TemplateMetadataBulkUpdateSerializer, TemplateMetadataSerializer


@extend_schema_view(
    retrieve=extend_schema(
        summary="Get specific template metadata ID.",
        description="Returns a specific template metadata by ID.",
    ),
    update=extend_schema(
        summary="Update a specific template metadata by ID.",
        description="Update a specific template metadata by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific template metadata by ID.",
        description="Partially update a specific template metadata by ID.",
    ),
)
class TemplateMetadataViewSet(RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """
    ViewSet for retrieving template metadata from metrics service database.

    Endpoints:
        GET    api/v1/dashboard_reports/template_metadata/{id}/ - Get template metadata
        PUT    api/v1/dashboard_reports/template_metadata/{id}/ - Update template metadata
        PATCH  api/v1/dashboard_reports/template_metadata/{id}/ - Partially update template metadata
        PATCH  api/v1/dashboard_reports/template_metadata/ - Partially update ALL template metadata
    """

    permission_classes = [IsSystemAdminOrAuditor]
    versioning_class = None
    pagination_class = None
    serializer_class = TemplateMetadataSerializer

    def get_queryset(self) -> QuerySet[TemplateMetadata]:
        """Return all TemplateMetadata records."""
        return TemplateMetadata.objects.all()

    @extend_schema(
        operation_id="dashboard_reports_template_metadata_partial_update_all",
        summary="Partially update all template metadata.",
        description=(
            "Applies the given time estimates (minutes) to every template metadata record, overwriting existing "
            "values. Only the provided fields are changed. Null values are not allowed."
        ),
        extensions={"x-ai-description": "Partially update the time estimates of all template metadata records"},
        request=TemplateMetadataBulkUpdateSerializer,
        responses=inline_serializer(
            "TemplateMetadataUpdateAllResponse",
            {"updated": serializers.IntegerField(help_text="Number of template metadata records updated")},
        ),
    )
    def partial_update_all(self, request: Request) -> Response:
        """Apply the given time estimates to all template metadata records."""
        serializer = TemplateMetadataBulkUpdateSerializer(
            data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if not values:
            return Response({"detail": "Provide at least one time estimate field."}, status=status.HTTP_400_BAD_REQUEST)
        updated = self.get_queryset().update(**values, modified=timezone.now(), modified_by=request.user)
        return Response({"updated": updated})
