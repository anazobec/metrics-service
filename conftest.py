"""Repo-wide pytest fixtures shared by tests/ and apps/*/tests/."""

import uuid

import pytest


@pytest.fixture
def rando(db):
    """Create a user with no permissions."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=f"rando-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def platform_auditor(db, rando):
    """Assign the Platform Auditor global role to rando and return the user."""
    from ansible_base.rbac.models import RoleDefinition

    RoleDefinition.objects.get(name="Platform Auditor").give_global_permission(rando)
    return rando
