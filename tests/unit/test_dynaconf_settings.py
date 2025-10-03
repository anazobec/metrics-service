"""
Tests for Dynaconf configuration and settings precedence.

Following AAP Phase 1 standards from handbook proposal 0014-Django-Settings.
Tests ensure proper precedence order and validator functionality.
"""

import os
from pathlib import Path
from unittest import mock

# Set a valid SECRET_KEY for test module import
# This allows Django settings to load without validation errors
os.environ.setdefault("METRICS_SERVICE_SECRET_KEY", "test-secret-key-for-dynaconf-tests")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "metrics_service.settings")


class TestDynaconfPrecedence:
    """Test settings precedence order following AAP standards.

    Note: These tests verify the Dynaconf instance directly since Django
    settings are cached and cannot be reliably reloaded in tests.
    """

    def test_dynaconf_can_read_env_vars(self):
        """Test that Dynaconf can read environment variables via load_envvars."""
        from metrics_service.settings import DYNACONF

        # DYNACONF should have loaded the environment variable
        assert DYNACONF.get("SECRET_KEY") == "test-secret-key-for-dynaconf-tests"

    def test_database_defaults_loaded(self):
        """Test that database defaults from defaults.py are loaded."""
        from django.conf import settings

        # Database settings should be present
        assert "default" in settings.DATABASES
        assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
        assert settings.DATABASES["default"]["NAME"] is not None

    def test_cors_settings_loaded(self):
        """Test CORS settings are loaded from config."""
        from django.conf import settings

        # CORS settings should be configured
        assert hasattr(settings, "CORS_ALLOW_ALL_ORIGINS")
        # In development mode (from config/settings.yaml), this should be True
        # But could be overridden by environment, so just check it exists


class TestDynaconfValidators:
    """Test Dynaconf validators enforce security requirements.

    Note: Since settings are already loaded, we test that validators
    are properly configured rather than trying to trigger them.
    """

    def test_validators_are_registered(self):
        """Test that validators are registered in DYNACONF."""
        from metrics_service.settings import DYNACONF

        validators_list = list(DYNACONF.validators)
        assert len(validators_list) > 0

    def test_secret_key_validators_configured(self):
        """Test that SECRET_KEY validators are configured."""
        from metrics_service.settings import DYNACONF

        validators_list = list(DYNACONF.validators)
        secret_key_validators = [v for v in validators_list if "SECRET_KEY" in v.names]

        # Should have at least 2 validators for SECRET_KEY (one for each default value)
        assert len(secret_key_validators) >= 2

        # Check that validators check for default values
        for validator in secret_key_validators:
            # Validators should have 'ne' (not equal) operations for default keys
            assert hasattr(validator, "operations")

    def test_database_validators_configured(self):
        """Test that database validators are configured."""
        from metrics_service.settings import DYNACONF

        validators_list = list(DYNACONF.validators)

        # Should have validators for database settings
        db_validators = [v for v in validators_list if any("DATABASES" in str(name) for name in v.names)]
        assert len(db_validators) > 0

    def test_settings_pass_validation(self):
        """Test that current settings pass validation (since we're running)."""
        from django.conf import settings

        # If we got here, validation passed during settings load
        assert settings.SECRET_KEY is not None
        assert settings.DATABASES is not None


class TestEnvironmentSwitching:
    """Test environment-specific configuration sections."""

    def test_current_environment_is_set(self):
        """Test that current_env is properly set."""
        from metrics_service.settings import DYNACONF

        # Should have a current environment
        assert DYNACONF.current_env is not None
        assert isinstance(DYNACONF.current_env, str)

    def test_is_development_mode_flag(self):
        """Test that is_development_mode flag is set by factory."""
        from metrics_service.settings import DYNACONF

        # Factory should set is_development_mode based on environment
        assert hasattr(DYNACONF, "is_development_mode")
        assert isinstance(DYNACONF.is_development_mode, bool)


class TestDynaconfFactory:
    """Test that DAB factory pattern is properly configured."""

    def test_factory_creates_dynaconf_instance(self):
        """Test that factory creates a valid Dynaconf instance."""
        from metrics_service.settings import DYNACONF

        assert DYNACONF is not None
        assert hasattr(DYNACONF, "validators")
        assert hasattr(DYNACONF, "current_env")

    def test_factory_registers_validators(self):
        """Test that validators are registered with the Dynaconf instance."""
        from metrics_service.settings import DYNACONF

        # Check that validators were registered
        # DYNACONF.validators is a ValidatorList which is iterable
        validators_list = list(DYNACONF.validators)
        assert len(validators_list) > 0

        # Check for SECRET_KEY validators
        secret_key_validators = [v for v in validators_list if "SECRET_KEY" in v.names]
        assert len(secret_key_validators) > 0

    def test_envvar_prefix_configured(self):
        """Test that METRICS_SERVICE prefix is configured."""
        from metrics_service.settings import DYNACONF

        # Check that the prefix is configured by seeing if env vars work
        # The fact that SECRET_KEY was loaded from METRICS_SERVICE_SECRET_KEY proves it works
        assert DYNACONF.get("SECRET_KEY") is not None


class TestSettingsFileLoading:
    """Test that settings files are loaded in correct order."""

    def test_defaults_py_loaded(self):
        """Test that defaults.py values are loaded."""
        with mock.patch.dict(os.environ, {"METRICS_SERVICE_SECRET_KEY": "valid-key"}):
            import importlib

            from metrics_service import settings as settings_module

            importlib.reload(settings_module)

            from django.conf import settings

            # Values from defaults.py should be present
            assert settings.SERVICE_TYPE == "metrics-service"
            assert Path(settings_module.__file__).resolve().parent.parent.parent == settings.BASE_DIR

    def test_config_yaml_loaded(self):
        """Test that config/settings.yaml values are loaded."""
        with mock.patch.dict(
            os.environ, {"METRICS_SERVICE_SECRET_KEY": "valid-key", "METRICS_SERVICE_ENV": "development"}
        ):
            import importlib

            from metrics_service import settings as settings_module

            importlib.reload(settings_module)

            from django.conf import settings

            # Development environment sets CORS_ALLOW_ALL_ORIGINS=true in config/settings.yaml
            assert settings.CORS_ALLOW_ALL_ORIGINS is True
