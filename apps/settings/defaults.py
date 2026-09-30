"""
Top level settings file for all apps.

The settings here overrides any setting previously loaded
from the `metrics_service.settings`.
"""

from pathlib import Path

from dynaconf import Dynaconf, post_hook

# Extra applications added after PSF templating
extra_applications = [
    "django_prometheus",
    "django_extensions",
]

# Default DAB applications layd out from PSF, add/remove according to the project needs,
# adjust `pyproject` dab extra dependencies acording to apps added/removed here
dab_applications = [
    "ansible_base.feature_flags",  # Must be first to ensure table exists before other apps' post_migrate signals
    "ansible_base.activitystream",
    "ansible_base.api_documentation",
    "ansible_base.jwt_consumer",
    "ansible_base.rbac",
    "ansible_base.resource_registry",
    "ansible_base.rest_filters",
    "ansible_base.rest_pagination",
]

# List of applications from the apps/ folder
project_applications = [
    "apps.core",
    "apps.dynamic_settings",
    "apps.tasks",
    "apps.dashboard_reports",  # Dashboard data for automation-reports integration
    "apps.analytics",  # Customer-facing BYO-BI analytics API (ANSTRAT-1587)
]

# Final state of the INSTALLED_APPS that will merge with the rest of the settings
INSTALLED_APPS = [
    "dynaconf_merge_unique",  # DO NOT REMOVE THIS
    *dab_applications,
    *project_applications,
    *extra_applications,
    "django_generate_series",  # Dashboard data for automation-reports integration
]

# Enable debug mode
DEBUG = False

# REST framework settings
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "UNAUTHENTICATED_USER": None,
    "UNAUTHENTICATED_TOKEN": None,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "apps.core.renderers.ServiceBrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
}

# Title of Swagger the API documentation
SPECTACULAR_SETTINGS__TITLE = "metrics_service API"
# Description of Swagger the API documentation
SPECTACULAR_SETTINGS__DESCRIPTION = "API documentation for the metrics_service"
# Version of Swagger the API documentation
SPECTACULAR_SETTINGS__VERSION = "v1"
# Split components into request and response for generating clients
SPECTACULAR_SETTINGS__COMPONENT_SPLIT_REQUEST = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default",
    },
}
CSRF_TRUSTED_ORIGINS = []

# Databases settings, using PostgreSQL by default
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "",  # require to be set at runtime
        "PORT": "5432",
        "USER": "metrics_service",
        "PASSWORD": "",  # require to be set at runtime
        "NAME": "metrics_service",
        "OPTIONS": {
            "sslmode": "prefer",
        },
    },
    # AWX database for metrics-utility collector integration
    # Override with METRICS_SERVICE_DATABASES__awx__HOST, etc.
    "awx": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "",  # require to be set at runtime
        "PORT": "5432",
        "USER": "awx",
        "PASSWORD": "",  # require to be set at runtime
        "NAME": "awx",
        "OPTIONS": {
            "sslmode": "prefer",
        },
    },
}

# Feature flag defaults — controlled at runtime via METRICS_SERVICE_FEATURE__<KEY>=value env vars
# (dynaconf nested-key syntax merges into this dict) or via the dynamic_settings DB API.
# Keys present here provide the static default used by get_feature_enabled_from_db when no DB row
# exists. Disable via METRICS_SERVICE_FEATURE__<KEY>=false env var.
FEATURE = {
    # Local hourly/daily collectors, rollup, cleanup_metrics_data — see METRICS_COLLECTION_GROUP.
    "METRICS_COLLECTION": True,
    # Anonymization and Segment transmission only — does not gate METRICS_COLLECTION_GROUP.
    "ANONYMIZED_DATA_COLLECTION": True,
    # Dashboard data collection for automation-reports — see DASHBOARD_COLLECTION_GROUP.
    "DASHBOARD_COLLECTION": True,
    # Indirect node collection, enabled by default, see INDIRECT_NODE_COLLECTION_GROUP.
    "INDIRECT_NODE_COLLECTION": True,
}

# Used when generating API URLs in views, example "/api/metrics/"; None means "/api/"
URL_PREFIX = None

# Prevent DAB from registering its default api_documentation URL patterns so that
# the service can substitute MetricsSpectacularSwaggerView in their place.  The
# app itself (and its ready() hooks) is still active — only URL registration is
# skipped.  See apps/core/urls.py for the replacement registrations.
ANSIBLE_BASE_APPS_EXCLUDE_VIEW_LIST = ["ansible_base.api_documentation"]

# Grant 'view' bypass to users carrying the Platform Auditor global RBAC role.
# The gateway conveys auditor status via JWT global_roles (not user_data), so
# is_platform_auditor is a property on User that queries RBAC assignments.
# Mirrors aap_gateway_api/defaults.py so IsSystemAdminOrAuditor works here too.
ANSIBLE_BASE_BYPASS_ACTION_FLAGS = {"view": "is_platform_auditor"}

# Task execution timeout in seconds (override via METRICS_SERVICE_TASK_TIMEOUT env var)
TASK_TIMEOUT = 3600

# Maximum number of job event rows fetched per hourly collection run.
# At ~700–900 bytes/row in memory, 2 000 000 rows ≈ 1.4–1.8 GB.  Raise for
# high-volume installations; lower for memory-constrained environments.
# Override via METRICS_SERVICE_JOBEVENT_ROW_LIMIT env var.
JOBEVENT_ROW_LIMIT = 200_000

# Maximum finished jobs processed per hourly window by main_jobevent_service.
# Keeps the SQL IN clause manageable; jobs are sorted by created time (oldest first).
# Override via METRICS_SERVICE_JOBEVENT_JOB_LIMIT env var.
JOBEVENT_JOB_LIMIT = 1_000

# Install type — how this metrics-service instance was deployed.
# The operator sets METRICS_SERVICE_INSTALL_TYPE=operator; the containerized installer
# leaves this at the default so no extra configuration is needed there.
INSTALL_TYPE = "containerized"


# Project-specific middleware additions
MIDDLEWARE = "@merge_unique whitenoise.middleware.WhiteNoiseMiddleware"

# Template directories for core app
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            "apps/core/templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Dashboard collection schedule configuration
DASHBOARD_COLLECTION = {
    "COLLECTION_SCHEDULE_CRON": "0 */6 * * *",
    # Set to True to derive the backfill/retention window from the Controller's active
    # cleanup_jobs schedules instead of the fixed 90-day default.
    "USE_CONTROLLER_RETENTION": True,
}

# Conditional static files directory (avoids staticfiles.W004 when absent)
_base_dir = Path(__file__).resolve().parent.parent.parent
STATICFILES_DIRS = [d for d in [_base_dir / "static"] if d.exists()]


@post_hook
def load_prometheus_middlewares(settings: Dynaconf) -> dict:
    """Defer to execute after all settings are loaded."""
    middleware = settings.get("MIDDLEWARE", [])
    if "django_prometheus" in " ".join(middleware):
        return {}
    new = [
        "django_prometheus.middleware.PrometheusBeforeMiddleware",
        *middleware,
        "django_prometheus.middleware.PrometheusAfterMiddleware",
    ]
    return {"MIDDLEWARE": new}


@post_hook
def load_segment_write_key(settings: Dynaconf) -> dict:
    """Load SEGMENT_WRITE_KEY from file if configured."""
    import os

    from apps.core.segment import read_segment_key_from_path

    # Respect env/settings precedence: do not overwrite if already set
    if os.environ.get("METRICS_SERVICE_SEGMENT_WRITE_KEY", "").strip():
        return {}
    if settings.get("SEGMENT_WRITE_KEY"):
        return {}

    # Get path from environment or use default
    segment_key_path = os.environ.get(
        "METRICS_SERVICE_SEGMENT_WRITE_KEY_FILE",
        "/etc/ansible-automation-platform/metrics/segment-write-key",
    )
    path = Path(segment_key_path)

    if not path.exists():
        return {}

    key = read_segment_key_from_path(path)
    if key:
        return {"SEGMENT_WRITE_KEY": key}
    return {}


@post_hook
def parse_allowed_hosts_env(settings: Dynaconf) -> dict:
    """Parse METRICS_SERVICE_ALLOWED_HOSTS from environment (CSV or JSON array)."""
    import json
    import logging
    import os

    if not os.environ.get("METRICS_SERVICE_ALLOWED_HOSTS"):
        return {}

    raw = os.environ["METRICS_SERVICE_ALLOWED_HOSTS"].strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            logging.getLogger(__name__).warning(
                "METRICS_SERVICE_ALLOWED_HOSTS: invalid JSON (%s), using empty list: %s",
                type(e).__name__,
                e,
            )
            parsed = []
        if not isinstance(parsed, list):
            logging.getLogger(__name__).warning(
                "METRICS_SERVICE_ALLOWED_HOSTS: expected JSON array, got %s, using empty list",
                type(parsed).__name__,
            )
            parsed = []
        allowed_hosts = [str(x).strip() for x in parsed if str(x).strip()]
    else:
        allowed_hosts = [str(x).strip() for x in raw.split(",") if x.strip()]

    return {"ALLOWED_HOSTS": allowed_hosts}


@post_hook
def setup_json_logging_for_production(settings: Dynaconf) -> dict:
    """Enable JSON logging when in production mode or when METRICS_SERVICE_LOG_FORMAT=json."""
    import copy
    import os

    environment = os.environ.get("METRICS_SERVICE_MODE", "development").lower()
    if environment == "production" or os.environ.get("METRICS_SERVICE_LOG_FORMAT", "").lower() == "json":
        log_cfg = copy.deepcopy(settings.get("LOGGING") or {})
        log_cfg.setdefault("formatters", {})["json"] = {"()": "apps.core.logging_config.JsonFormatter"}
        for h in log_cfg.get("handlers", {}).values():
            if isinstance(h, dict) and "StreamHandler" in str(h.get("class", "")):
                h["formatter"] = "json"
        return {"LOGGING": log_cfg}
    return {}
