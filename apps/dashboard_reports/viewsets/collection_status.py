"""ViewSet for dashboard collection status."""

import json

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from apps.dashboard_reports.models import JobData
from apps.dynamic_settings.models import Setting
from apps.tasks.models import Task
from apps.tasks.task_groups import get_feature_enabled_from_db


@extend_schema_view(
    create=extend_schema(
        summary="Toggle the show_gamification feature flag.",
        description="Sets the runtime-toggleable show_gamification setting. Requires system admin or auditor "
        "permissions. Takes effect immediately without a service restart.",
        request=inline_serializer(
            name="DashboardCollectionPostRequest",
            fields={
                "show_gamification": serializers.BooleanField(default=False),
            },
        ),
        responses={
            200: inline_serializer(
                name="DashboardCollectionPostResponse",
                fields={
                    "show_gamification": serializers.BooleanField(default=False),
                },
            ),
        },
    ),
    list=extend_schema(
        summary="Return dashboard collection feature flag status and task state.",
        description="When enabled is false, next_run, initial_collection_status and min_collection_timestamp are null. initial_collection_status reflects the status of the one-shot initial collection task: 'pending', 'running', 'completed', 'failed' or 'canceled'.",
        responses={
            200: inline_serializer(
                name="DashboardCollectionListResponse",
                fields={
                    "enabled": serializers.BooleanField(),
                    "next_run": serializers.CharField(allow_null=True),
                    "initial_collection_status": serializers.CharField(allow_null=True),
                    "min_collection_timestamp": serializers.DateTimeField(allow_null=True),
                    "show_gamification": serializers.BooleanField(),
                    "show_dashboard": serializers.BooleanField(),
                },
            ),
        },
    ),
)
class DashboardCollectionStatusViewSet(ViewSet):
    """Returns the enabled state and task status for the dashboard reports collection pipeline."""

    # User must be authenticated
    permission_classes = [IsAuthenticated]

    def create(self, request: Request, *args, **kwargs) -> Response:
        is_system_admin_or_auditor = IsSystemAdminOrAuditor().has_permission(request, self)
        if not is_system_admin_or_auditor:
            raise PermissionDenied

        new_show_gamification = request.data.get("show_gamification")
        if not isinstance(new_show_gamification, bool):
            raise ValidationError({"show_gamification": "Value must be a boolean: true/false"})

        Setting.objects.update_or_create(
            setting_key="SHOW_GAMIFICATION",
            defaults={"current_value": json.dumps(new_show_gamification), "last_modified_by": request.user},
        )

        return Response(
            {
                "show_gamification": new_show_gamification,
            }
        )

    def list(self, request: Request, *args, **kwargs) -> Response:
        """Return dashboard collection feature flag status and task state.

        When enabled is false, next_run and initial_collection_status are null.
        initial_collection_status reflects the status of the one-shot initial collection task:
        "pending", "running", "completed", "failed", or "cancelled".
        """
        is_system_admin_or_auditor = IsSystemAdminOrAuditor().has_permission(request, self)
        enabled = get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=True)
        show_gamification = get_feature_enabled_from_db("SHOW_GAMIFICATION", default=False)
        show_dashboard = get_feature_enabled_from_db("SHOW_DASHBOARD", default=True) and is_system_admin_or_auditor

        next_run = None
        initial_collection_status = None
        min_collection_timestamp = None

        if enabled:
            min_collection_timestamp = JobData.min_timestamp()

            # Incremental dashboard sync is driven by the hourly_unified_jobs hook,
            # so next_run reflects when that collector will next fire.
            hourly_task = Task.objects.filter(
                name="hourly_unified_jobs",
                is_system_task=True,
            ).first()
            if hourly_task:
                next_run = hourly_task.get_next_run_time()

            initial_task = Task.objects.filter(
                function_name="collect_dashboard_reports_initial_data",
                is_system_task=True,
            ).first()
            if initial_task:
                initial_collection_status = initial_task.status

        return Response(
            {
                "enabled": enabled,
                "next_run": next_run,
                "initial_collection_status": initial_collection_status,
                "min_collection_timestamp": min_collection_timestamp,
                "show_gamification": show_gamification,  # toggle-able by admins/system-auditors
                "show_dashboard": show_dashboard,  # only if user == admin
            }
        )
