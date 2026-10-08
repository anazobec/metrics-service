"""Validate nullable organization responses against the generated OpenAPI schema."""

import pytest
from drf_spectacular.generators import SchemaGenerator
from jsonschema import RefResolver
from openapi_schema_validator import OAS30Validator


@pytest.mark.unit
@pytest.mark.parametrize(
    "organization",
    [None, {"id": 1, "name": "Org One", "run_count": 3}, {"id": 1, "name": None, "run_count": 3}],
)
def test_org_streak_response_validates(organization):
    schema = SchemaGenerator().get_schema(request=None, public=True)
    org_streak = schema["components"]["schemas"]["OrgStreak"]
    validator = OAS30Validator(org_streak, resolver=RefResolver.from_schema(schema))
    validator.validate(
        {"organization": organization, "streak": 0, "daily": [{"date": "2026-06-15", "successful_runs": 0}]}
    )


@pytest.mark.unit
@pytest.mark.parametrize("organization", [{}, {"id": "invalid", "name": "Org One", "run_count": 3}])
def test_org_streak_schema_rejects_invalid_organization(organization):
    schema = SchemaGenerator().get_schema(request=None, public=True)
    validator = OAS30Validator(schema["components"]["schemas"]["OrgStreak"], resolver=RefResolver.from_schema(schema))
    assert not validator.is_valid({"organization": organization, "streak": 0, "daily": []})
