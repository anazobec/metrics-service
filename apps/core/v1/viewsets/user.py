from ansible_base.rbac.api.permissions import AnsibleBaseUserPermissions, IsSystemAdminOrAuditor
from ansible_base.rbac.policies import visible_users
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import User
from apps.core.v1.serializers import UserSerializer
from apps.dashboard_reports.utils import get_member_organizations

from .base import BaseViewSet


@extend_schema_view(
    list=extend_schema(
        summary="Get a list of users.",
        description="Returns a list of users.",
    ),
    retrieve=extend_schema(
        summary="Get a specific user by ID.",
        description="Returns a specific user by ID.",
    ),
    create=extend_schema(
        summary="Create a user.",
        description="Create a new user object",
        request=UserSerializer,
    ),
    update=extend_schema(
        summary="Update a specific user by ID.",
        description="Update a specific user by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific user by ID.",
        description="Partially update a specific user by ID.",
    ),
    destroy=extend_schema(
        summary="Delete a specific user by ID.",
        description="Delete a specific user by ID.",
    ),
)
class UserViewSet(BaseViewSet):
    """CRUD viewset for User resources.

    Read access (list, retrieve) is restricted to Platform Auditor role or higher.
    Write access (create, update, destroy) is restricted to system administrators.
    The /me action is accessible to any authenticated user.
    """

    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_permissions(self):
        """
        Return permission instances based on the current action.

        - me: any authenticated user may read their own profile.
        - list / retrieve: Platform Auditor or system admin required.
        - create / update / partial_update / destroy: system admin only
          (IsSystemAdminOrAuditor denies unsafe methods for non-admins, while
          AnsibleBaseUserPermissions enforces user-management business rules such
          as preventing self-deletion).
        """
        if self.action == "me":
            return [IsAuthenticated()]
        if self.action in ("list", "retrieve"):
            return [IsSystemAdminOrAuditor()]
        # Write operations: require system admin AND user-management business rules.
        return [IsSystemAdminOrAuditor(), AnsibleBaseUserPermissions()]

    def filter_queryset(self, queryset):
        """Restrict queryset to users visible to the requesting user."""
        queryset = visible_users(self.request.user, queryset=queryset)
        return super(BaseViewSet, self).filter_queryset(queryset)

    @extend_schema(
        summary="Get current user details",
        description=(
            "Get currently logged in user's details, including a provisional "
            "`member_of_organizations` list derived from recent job data (see AAP-88669). "
            "This is a stand-in until gateway-backed organization membership is available "
            "and is only computed for the requesting user."
        ),
        responses={200: UserSerializer},
    )
    @action(detail=False, methods=["get"])
    def me(self, request):
        """Return the profile of the currently authenticated user, plus their organization membership.

        `member_of_organizations` is derived from JobData (see
        apps.dashboard_reports.utils.get_member_organizations) as a provisional
        replacement for gateway-backed membership data (AAP-88669), so it is only
        populated for the requesting user, not for list/retrieve of other users.
        """
        serializer = self.get_serializer(request.user)
        data = serializer.data
        data["member_of_organizations"] = get_member_organizations(request.user.pk)
        return Response(data)
