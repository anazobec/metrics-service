from ansible_base.lib.abstract_models.user import AbstractDABUser


class User(AbstractDABUser):
    """
    Custom User model extending DAB's AbstractDABUser.

    This model can be extended with additional fields as needed.
    """

    encrypted_fields = ["password"]

    @property
    def is_platform_auditor(self) -> bool:
        """True if the user holds the Platform Auditor global RBAC role.

        The gateway conveys auditor status via the JWT global_roles claim (not
        user_data), so this is an RBAC lookup rather than a stored flag.
        DAB's has_super_permission checks getattr(user, bypass_flag) for the
        'view' action; this property makes that work for auditors.
        """
        from ansible_base.rbac.models import RoleDefinition

        return RoleDefinition.objects.filter(
            name="Platform Auditor",
            user_assignments__user=self.pk,
            content_type=None,
        ).exists()

    def get_member_organizations(self) -> list[dict[str, int | str]]:
        """Return this user's explicitly assigned organization memberships from local RBAC data.

        DAB's resource sync keeps organization role assignments in metrics-service's
        RBAC tables. Query the local permission evaluation model directly so global
        superuser permissions do not make an unassigned user appear to belong to
        every organization.
        """
        from ansible_base.rbac.models import get_evaluation_model

        from apps.core.models import Organization

        evaluation_model = get_evaluation_model(Organization)
        organization_ids = evaluation_model.accessible_ids(Organization, self, "member_organization")
        organizations = Organization.objects.filter(pk__in=organization_ids).order_by("pk").values("id", "name")
        return list(organizations)

    def related_fields(self, request):
        return {}

    def get_summary_fields(self):
        return {}
