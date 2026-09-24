import logging

from ansible_base.rbac.api.permissions import AnsibleBaseUserPermissions, IsSystemAdminOrAuditor
from ansible_base.rbac.policies import visible_users
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import User
from apps.core.v1.serializers import UserMeSerializer, UserSerializer

from .base import BaseViewSet

logger = logging.getLogger(__name__)


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
            "Get currently logged in user's details, including a `member_of_organizations` "
            "list from locally synced RBAC assignments (AAP-88670)."
        ),
        responses={200: UserMeSerializer},
    )
    @action(detail=False, methods=["get"])
    def me(self, request):
        """Return the profile of the currently authenticated user, plus their organization membership.

        `member_of_organizations` comes from the locally synced RBAC assignments and
        is only computed here for the requesting user, since `me` only ever returns
        the caller's own profile.
        """
        serializer = self.get_serializer(request.user)
        data = serializer.data
        try:
            data["member_of_organizations"] = request.user.get_member_organizations()
        except Exception:
            logger.exception(
                "Failed to read organization membership from local RBAC data for user %s",
                request.user.username,
            )
            data["member_of_organizations"] = []
        return Response(data)
