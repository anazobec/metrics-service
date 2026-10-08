"""
Unit tests for database-driven feature enabled functionality.

Tests the new feature enabled system that stores settings in the database
with fallback to Django settings and defaults.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.tasks.task_groups import (
    get_feature_enabled_from_db,
)
from tests.test_utils import get_test_password

pytestmark = pytest.mark.unit

User = get_user_model()


class TestFeatureEnabledDB(TestCase):
    """Test database-driven feature enabled functionality."""

    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password=get_test_password()
        )

    def test_get_feature_enabled_from_db_no_setting(self):
        """Test getting feature enabled when no database setting exists."""
        # Should return default when no setting in DB or Django settings
        result = get_feature_enabled_from_db("NONEXISTENT_SETTING", default=True)
        assert result is True

        result = get_feature_enabled_from_db("NONEXISTENT_SETTING", default=False)
        assert result is False

    @override_settings(FEATURE={"TEST_SETTING": True})
    def test_get_feature_enabled_from_django_settings(self):
        """Test getting feature enabled from Django settings when no DB setting."""
        result = get_feature_enabled_from_db("TEST_SETTING", default=False)
        assert result is True

    def test_get_feature_enabled_from_db_json_value(self):
        """Test getting feature enabled from database with JSON value."""
        from apps.dynamic_settings.models import Setting

        # Create a setting with JSON boolean value
        Setting.objects.create(
            setting_key="JSON_TEST_SETTING", current_value=json.dumps(True), last_modified_by=self.user
        )

        result = get_feature_enabled_from_db("JSON_TEST_SETTING", default=False)
        assert result is True

        # Test with False JSON value
        Setting.objects.filter(setting_key="JSON_TEST_SETTING").update(current_value=json.dumps(False))

        result = get_feature_enabled_from_db("JSON_TEST_SETTING", default=True)
        assert result is False

    def test_get_feature_enabled_from_db_string_value(self):
        """Test getting feature enabled from database with string boolean value."""
        from apps.dynamic_settings.models import Setting

        # Test various string representations of True
        for true_value in ["true", "True", "TRUE", "1", "yes", "YES", "on", "ON"]:
            Setting.objects.update_or_create(
                setting_key="STRING_TEST_SETTING", defaults={"current_value": true_value, "last_modified_by": self.user}
            )

            result = get_feature_enabled_from_db("STRING_TEST_SETTING", default=False)
            assert result is True, f"Failed for value: {true_value}"

        # Test various string representations of False
        for false_value in ["false", "False", "FALSE", "0", "no", "NO", "off", "OFF"]:
            Setting.objects.update_or_create(
                setting_key="STRING_TEST_SETTING",
                defaults={"current_value": false_value, "last_modified_by": self.user},
            )

            result = get_feature_enabled_from_db("STRING_TEST_SETTING", default=True)
            assert result is False, f"Failed for value: {false_value}"

    def test_get_feature_enabled_from_db_invalid_json(self):
        """Test getting feature enabled when database has invalid JSON."""
        from apps.dynamic_settings.models import Setting

        # Create setting with invalid JSON
        Setting.objects.create(
            setting_key="INVALID_JSON_SETTING", current_value="invalid json {", last_modified_by=self.user
        )

        # Should treat as string boolean and return False (invalid string)
        result = get_feature_enabled_from_db("INVALID_JSON_SETTING", default=True)
        assert result is False

    @patch("apps.tasks.task_groups.logger")
    def test_get_feature_enabled_from_db_exception(self, mock_logger):
        """Test getting feature enabled when database query raises exception."""
        with patch("apps.dynamic_settings.models.Setting.objects.filter", side_effect=Exception("DB Error")):
            result = get_feature_enabled_from_db("ERROR_SETTING", default=True)
            assert result is True
            mock_logger.warning.assert_called_once()

    def test_read_errors_can_be_propagated(self):
        with (
            patch("apps.dynamic_settings.models.Setting.objects.filter", side_effect=RuntimeError("DB Error")),
            pytest.raises(RuntimeError, match="DB Error"),
        ):
            get_feature_enabled_from_db("SHOW_LEADERBOARD", default=True, raise_errors=True)


class TestFeatureEnabledAAPFlagFallback(TestCase):
    """Test AAPFlag fallback in get_feature_enabled_from_db.

    Priority chain:
      1. dynamic_settings.Setting         (runtime DB override)
      2. settings.FEATURE         (when the key is present, e.g. env overrides)
      3. settings.FEATURE_<name>_ENABLED  (top-level attr, set directly by installer settings.yaml)
      4. AAPFlag.value                    (YAML-seeded platform default)
      5. default argument
    """

    def _make_mock_flag(self, value: str) -> MagicMock:
        flag = MagicMock()
        flag.value = value
        return flag

    @override_settings(FEATURE={})
    def test_platform_read_errors_can_be_propagated(self):
        with (
            patch("ansible_base.feature_flags.models.AAPFlag.objects.filter", side_effect=RuntimeError("DB Error")),
            pytest.raises(RuntimeError, match="DB Error"),
        ):
            get_feature_enabled_from_db("SHOW_LEADERBOARD", default=True, raise_errors=True)

    def _patch_aap_flag(self, flag_instance):
        """Patch AAPFlag at its source module so the local import inside the function picks it up."""
        return patch(
            "ansible_base.feature_flags.models.AAPFlag",
            **{"objects.filter.return_value.first.return_value": flag_instance},
        )

    # ------------------------------------------------------------------
    # AAPFlag fallback — basic true / false
    # ------------------------------------------------------------------

    def test_aap_flag_true_returns_true(self):
        """Returns True when AAPFlag.value is 'True' and no Setting exists."""
        with self._patch_aap_flag(self._make_mock_flag("True")):
            assert get_feature_enabled_from_db("MY_FLAG", default=False) is True

    def test_aap_flag_false_returns_false(self):
        """Returns False when AAPFlag.value is 'False' and no Setting exists."""
        with self._patch_aap_flag(self._make_mock_flag("False")):
            assert get_feature_enabled_from_db("MY_FLAG", default=True) is False

    def test_aap_flag_string_variants(self):
        """AAPFlag.value truthy strings are all accepted."""
        for truthy in ("true", "True", "TRUE", "1", "yes", "on"):
            with self._patch_aap_flag(self._make_mock_flag(truthy)):
                assert get_feature_enabled_from_db("MY_FLAG", default=False) is True, truthy

        for falsy in ("false", "False", "0", "no", "off"):
            with self._patch_aap_flag(self._make_mock_flag(falsy)):
                assert get_feature_enabled_from_db("MY_FLAG", default=True) is False, falsy

    # ------------------------------------------------------------------
    # Name mapping: setting_name → FEATURE_<setting_name>_ENABLED
    # ------------------------------------------------------------------

    @override_settings(FEATURE={})
    def test_aap_flag_queried_with_correct_name(self):
        """AAPFlag is looked up using the FEATURE_<setting_name>_ENABLED convention."""
        mock_aap_flag_class = MagicMock()
        mock_aap_flag_class.objects.filter.return_value.first.return_value = None

        with patch("ansible_base.feature_flags.models.AAPFlag", mock_aap_flag_class):
            get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=False)

        mock_aap_flag_class.objects.filter.assert_called_once_with(
            name="FEATURE_DASHBOARD_COLLECTION_ENABLED", condition="boolean"
        )

    # ------------------------------------------------------------------
    # Priority: Setting > FEATURE (if key present) > AAPFlag > default
    # ------------------------------------------------------------------

    def test_dynamic_setting_takes_precedence_over_aap_flag(self):
        """A dynamic_settings.Setting overrides the AAPFlag value."""
        from django.contrib.auth import get_user_model

        from apps.dynamic_settings.models import Setting

        user = get_user_model().objects.create_user(username="prio_test", password="x")  # noqa: S106
        Setting.objects.create(setting_key="PRIO_FLAG", current_value=json.dumps(True), last_modified_by=user)

        # AAPFlag disagrees — says False
        with self._patch_aap_flag(self._make_mock_flag("False")):
            result = get_feature_enabled_from_db("PRIO_FLAG", default=False)

        assert result is True  # Setting wins

    @override_settings(FEATURE={"FALLBACK_FLAG": True})
    def test_feature_enabled_takes_precedence_over_aap_flag(self):
        """settings.FEATURE wins when the key is present (e.g. deployment env)."""
        # AAPFlag says False; settings says True — settings wins
        with self._patch_aap_flag(self._make_mock_flag("False")):
            result = get_feature_enabled_from_db("FALLBACK_FLAG", default=True)

        assert result is True

    @override_settings(FEATURE={"DASHBOARD_COLLECTION": True})
    def test_dashboard_collection_feature_enabled_overrides_false_aap_flag(self):
        """Env-style FEATURE must win over a false AAPFlag for dashboard collection."""
        with self._patch_aap_flag(self._make_mock_flag("False")):
            assert get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=False) is True

    def test_dashboard_collection_uses_aap_flag_when_omitted_from_feature_enabled(self):
        """When the key is absent from FEATURE, the platform AAPFlag applies."""
        with self._patch_aap_flag(self._make_mock_flag("True")), override_settings(FEATURE={}):
            assert get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=False) is True

    @override_settings(FEATURE={}, FEATURE_DASHBOARD_COLLECTION_ENABLED=True)
    def test_direct_top_level_attr_overrides_false_aap_flag(self):
        """FEATURE_<name>_ENABLED top-level attr (installer convention) wins over a false AAPFlag."""
        with self._patch_aap_flag(self._make_mock_flag("False")):
            assert get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=False) is True

    @override_settings(FEATURE={}, FEATURE_DASHBOARD_COLLECTION_ENABLED=False)
    def test_direct_top_level_attr_false_overrides_true_aap_flag(self):
        """FEATURE_<name>_ENABLED=False suppresses a true AAPFlag."""
        with self._patch_aap_flag(self._make_mock_flag("True")):
            assert get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=True) is False

    @override_settings(FEATURE={"DASHBOARD_COLLECTION": True}, FEATURE_DASHBOARD_COLLECTION_ENABLED=False)
    def test_feature_enabled_dict_takes_precedence_over_direct_attr(self):
        """FEATURE dict (tier 2) beats the top-level attr (tier 3)."""
        with self._patch_aap_flag(self._make_mock_flag("False")):
            assert get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=False) is True

    @override_settings(FEATURE={"SETTINGS_FLAG": True})
    def test_feature_enabled_settings_used_when_no_aap_flag(self):
        """settings.FEATURE is used when AAPFlag returns None."""
        with self._patch_aap_flag(None):
            result = get_feature_enabled_from_db("SETTINGS_FLAG", default=False)

        assert result is True

    def test_default_used_when_nothing_found(self):
        """default argument is returned when Setting, FEATURE key, and AAPFlag all miss."""
        with self._patch_aap_flag(None), override_settings(FEATURE={}):
            assert get_feature_enabled_from_db("NONEXISTENT", default=True) is True
            assert get_feature_enabled_from_db("NONEXISTENT", default=False) is False

    # ------------------------------------------------------------------
    # Exception resilience
    # ------------------------------------------------------------------

    def test_aap_flag_exception_falls_through_to_default(self):
        """An exception inside the AAPFlag block falls through to the default argument."""
        mock_aap_flag_class = MagicMock()
        mock_aap_flag_class.objects.filter.side_effect = Exception("registry unavailable")

        with (
            patch("ansible_base.feature_flags.models.AAPFlag", mock_aap_flag_class),
            override_settings(FEATURE={}),
        ):
            result = get_feature_enabled_from_db("ERR_FLAG", default=True)

        assert result is True
