"""
Production environment settings.

This file is loaded when METRICS_SERVICE_MODE=production and serves three purposes:

1. ZERO OUT SENSITIVE INFORMATION
   All sensitive settings (passwords, keys, secrets) are explicitly set to empty
   strings here, even if they already default to empty. This ensures production
   never accidentally inherits insecure defaults from development or defaults.py.
   These values MUST be provided via environment variables or external config.

2. SET PRODUCTION-APPROPRIATE DEFAULTS
   Override settings with values that are secure and appropriate for production:
   - DEBUG = False (never run debug mode in production)
   - Disable permissive RBAC settings
   - Configure gateway integration URLs
   - Require JWT authentication

3. VALIDATE IMPORTANT SETTINGS
   Each critical setting has a corresponding Dynaconf Validator that runs at
   startup. If any required setting is missing or invalid, the application
   will fail to start with a clear error message. This prevents misconfigured
   deployments from running.

Usage:
   export METRICS_SERVICE_MODE=production
   export METRICS_SERVICE_SECRET_KEY=your-secret-key
   export METRICS_SERVICE_DATABASES__default__PASSWORD=your-db-password
   # ... set all other required environment variables
   python manage.py metrics_service run --workers 4
   # Or set Gunicorn and dispatcher workers separately:
   # python manage.py metrics_service run --gunicorn-workers 4 --dispatcher-workers 4

Validators are registered in metrics_service/settings.py and run during export().
"""

from dynaconf import Validator

validators = []

# =============================================================================
# Security Settings
# =============================================================================

DEBUG = False
ALLOW_SHARED_RESOURCE_CUSTOM_ROLES = False
validators.append(
    Validator(
        "ALLOW_SHARED_RESOURCE_CUSTOM_ROLES",
        eq=False,
        messages={"operations": "ALLOW_SHARED_RESOURCE_CUSTOM_ROLES must be False."},
    ),
)

ALLOW_LOCAL_ASSIGNING_JWT_ROLES = False
validators.append(
    Validator(
        "ALLOW_LOCAL_ASSIGNING_JWT_ROLES",
        eq=False,
        messages={"operations": "ALLOW_LOCAL_ASSIGNING_JWT_ROLES must be False."},
    ),
)

# =============================================================================
# Resource Server / Gateway Integration
# =============================================================================

RESOURCE_SERVER__URL = ""
validators.append(
    Validator(
        "RESOURCE_SERVER__URL",
        must_exist=True,
        ne="",
        messages={"operations": "RESOURCE_SERVER__URL must be set."},
    ),
)

RESOURCE_SERVER__SECRET_KEY = ""
validators.append(
    Validator(
        "RESOURCE_SERVER__SECRET_KEY",
        must_exist=True,
        ne="",
        messages={
            "operations": (
                "RESOURCE_SERVER__SECRET_KEY must be set. "
                "Set METRICS_SERVICE_RESOURCE_SERVER__SECRET_KEY environment variable."
            )
        },
    ),
)

# =============================================================================
# Authentication
# =============================================================================

ANSIBLE_BASE_JWT_KEY = ""
validators.append(
    Validator(
        "ANSIBLE_BASE_JWT_KEY",
        must_exist=True,
        ne="",
        messages={
            "operations": (
                "ANSIBLE_BASE_JWT_KEY must be set. Set METRICS_SERVICE_ANSIBLE_BASE_JWT_KEY environment variable."
            )
        },
    ),
)

REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES = [
    "apps.core.authentication.ServiceJWTAuthentication",
]
validators.append(
    Validator(
        "REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES",
        must_exist=True,
        condition=lambda v: "apps.core.authentication.ServiceJWTAuthentication" in v,
        messages={
            "condition": (
                "REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES must contain "
                "'apps.core.authentication.ServiceJWTAuthentication'."
            ),
        },
    ),
)

# =============================================================================
# Django Core
# =============================================================================

SECRET_KEY = ""
validators.append(
    Validator(
        "SECRET_KEY",
        must_exist=True,
        ne="",
        messages={"operations": "SECRET_KEY must be set and not empty."},
    ),
)

# =============================================================================
# Database Credentials
# =============================================================================

DATABASES__default__HOST = ""
validators.append(
    Validator(
        "DATABASES__default__HOST",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__default__HOST must be set."},
    ),
)

DATABASES__default__USER = ""
validators.append(
    Validator(
        "DATABASES__default__USER",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__default__USER must be set."},
    ),
)

DATABASES__default__PASSWORD = ""
validators.append(
    Validator(
        "DATABASES__default__PASSWORD",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__default__PASSWORD must be set."},
    ),
)

DATABASES__awx__HOST = ""
validators.append(
    Validator(
        "DATABASES__awx__HOST",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__awx__HOST must be set."},
    ),
)

DATABASES__awx__USER = ""
validators.append(
    Validator(
        "DATABASES__awx__USER",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__awx__USER must be set."},
    ),
)

DATABASES__awx__PASSWORD = ""
validators.append(
    Validator(
        "DATABASES__awx__PASSWORD",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__awx__PASSWORD must be set."},
    ),
)

DATABASES__gateway__HOST = ""
validators.append(
    Validator(
        "DATABASES__gateway__HOST",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__gateway__HOST must be set."},
    ),
)

DATABASES__gateway__USER = ""
validators.append(
    Validator(
        "DATABASES__gateway__USER",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__gateway__USER must be set."},
    ),
)

DATABASES__gateway__PASSWORD = ""
validators.append(
    Validator(
        "DATABASES__gateway__PASSWORD",
        must_exist=True,
        ne="",
        messages={"operations": "DATABASES__gateway__PASSWORD must be set."},
    ),
)

# =============================================================================
# External Services
# =============================================================================

# Loaded from file at startup via _load_segment_write_key_from_file().
# Default path: /etc/ansible-automation-platform/metrics/segment-write-key
# Override path via METRICS_SERVICE_SEGMENT_WRITE_KEY_FILE
# Key is baked into container image at build time - always present even in air-gapped environments.
SEGMENT_WRITE_KEY = ""
validators.append(
    Validator(
        "SEGMENT_WRITE_KEY",
        must_exist=True,
        ne="",
        messages={
            "operations": (
                "SEGMENT_WRITE_KEY must be set. "
                "Either set METRICS_SERVICE_SEGMENT_WRITE_KEY environment variable "
                "or provide the key file at /etc/ansible-automation-platform/metrics/segment-write-key "
                "(or path specified in METRICS_SERVICE_SEGMENT_WRITE_KEY_FILE)."
            )
        },
    ),
)

# =============================================================================
# Static Files
# =============================================================================

# Override BASE_DIR-relative default so static files land in a known writable
# location regardless of where the package is installed (e.g. site-packages).
STATIC_ROOT = "/var/lib/ansible-automation-platform/metrics/staticfiles"

# =============================================================================
# URL Configuration
# =============================================================================

# Production login/logout URLs for gateway integration
LOGIN_URL = "/api/gateway/v1/login/"
LOGOUT_URL = "/api/gateway/v1/logout/"

# =============================================================================
# Allowed Hosts
# =============================================================================
# Set via METRICS_SERVICE_ALLOWED_HOSTS (comma-separated or JSON array).
# Example: METRICS_SERVICE_ALLOWED_HOSTS=metrics.example.com,api.example.com
# Or:      METRICS_SERVICE_ALLOWED_HOSTS='["metrics.example.com","api.example.com"]'
ALLOWED_HOSTS = []
validators.append(
    Validator(
        "ALLOWED_HOSTS",
        must_exist=True,
        condition=lambda v: isinstance(v, list) and len(v) > 0,
        messages={
            "condition": (
                "ALLOWED_HOSTS must be set via METRICS_SERVICE_ALLOWED_HOSTS in production. "
                "Django will reject all requests with an empty ALLOWED_HOSTS list. "
                "Set METRICS_SERVICE_ALLOWED_HOSTS to a comma-separated list or JSON array of hostnames."
            )
        },
    ),
)
