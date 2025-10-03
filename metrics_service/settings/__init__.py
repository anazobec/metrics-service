import os
from pathlib import Path

from ansible_base.lib.dynamic_config import export, factory, load_envvars, load_standard_settings_files
from dynaconf import Validator

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Set environment before including any settings
# Note: Factory uses METRICS_SERVICE_MODE as the env_switcher (not METRICS_SERVICE_ENV)
os.environ.setdefault("METRICS_SERVICE_MODE", "development")
environment = os.environ.get("METRICS_SERVICE_MODE", "development")

# Initialize Dynaconf following AAP Phase 1 standards using DAB factory
# This follows the pattern from handbook proposal 0014-Django-Settings
# The factory will load settings in this order:
# 1. defaults.py (loaded as a settings_file)
# 2. /etc/ansible-automation-platform/settings.yaml (auto-loaded by factory)
# 3. config/settings.yaml (local overrides)
# 4. Environment variables with METRICS_SERVICE_ prefix
DYNACONF = factory(
    module_name=__name__,
    app_name="METRICS_SERVICE",
    validators=[
        # Security: Require SECRET_KEY to be explicitly set (not defaults)
        # Note: Validators only run in production (validation=False in development at export time)
        Validator(
            "SECRET_KEY",
            must_exist=True,
            ne="dev-secret-key-change-in-production",  # From defaults.py
            messages={
                "operations": "SECRET_KEY must not use default value. Set METRICS_SERVICE_SECRET_KEY environment variable.",
            },
        ),
        Validator(
            "SECRET_KEY",
            must_exist=True,
            ne="your-secret-key-here-change-in-production",  # From config/settings.yaml default section
            messages={
                "operations": "SECRET_KEY must not use default value. Set METRICS_SERVICE_SECRET_KEY environment variable.",
            },
        ),
        Validator(
            "SECRET_KEY",
            must_exist=True,
            ne="PRODUCTION-SECRET-KEY-NOT-SET",  # From config/settings.yaml production section
            messages={
                "operations": "SECRET_KEY must be set in production. Set METRICS_SERVICE_SECRET_KEY environment variable.",
            },
        ),
        # Database: Ensure critical database settings exist
        Validator("DATABASES.default.NAME", must_exist=True),
        Validator("DATABASES.default.HOST", must_exist=True),
        Validator("DATABASES.default.USER", must_exist=True),
        Validator("DATABASES.default.PASSWORD", must_exist=True),
    ],
    # Settings files to load (in order)
    # Factory loads in this precedence (lowest to highest):
    # 1. defaults.py
    # 2. /etc/ansible-automation-platform/settings.yaml (from factory)
    # 3. config/settings.yaml
    # 4. config/settings.local.yaml
    # 5. Environment variables (METRICS_SERVICE_*) - handled by Dynaconf loaders
    settings_files=[
        "defaults.py",
        str(BASE_DIR / "config" / "settings.yaml"),
        str(BASE_DIR / "config" / "settings.local.yaml"),
    ],
    environments=True,  # Enable environment-specific sections in YAML
    merge_enabled=True,
    load_dotenv=True,
)

# Load settings following AAP precedence (lowest to highest priority):
# 1. settings_files (defaults.py, config/settings.yaml) - already loaded by factory
# 2. Standard AAP settings from /etc/ansible-automation-platform/
load_standard_settings_files(DYNACONF)
# 3. Environment variables (METRICS_SERVICE_*) - highest priority
load_envvars(DYNACONF)

# Export Dynaconf settings back to Django settings module
# In development mode, skip validation to allow defaults
# In production/other modes, require all settings to be explicitly set
is_development = "development" in DYNACONF.current_env.lower()
export(__name__, DYNACONF, validation=not is_development)
