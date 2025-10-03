"""
Base Django settings for metrics_service following AAP standards.

These are default values that can be overridden by:
1. config/settings.yaml (Dynaconf)
2. Environment variables with METRICS_SERVICE_ prefix
3. /etc/ansible-automation-platform/settings.yaml (production)

All environment variable overrides are handled by Dynaconf in settings/__init__.py
"""

from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
# Override with METRICS_SERVICE_SECRET_KEY environment variable
SECRET_KEY = "dev-secret-key-change-in-production"

# SECURITY WARNING: don't run with debug turned on in production!
# Override with METRICS_SERVICE_DEBUG environment variable
DEBUG = True

ALLOWED_HOSTS = ["*"]

# Service identification for AAP
# Override with METRICS_SERVICE_SERVICE_TYPE and METRICS_SERVICE_SERVICE_ID
SERVICE_TYPE = "metrics-service"
SERVICE_ID = "generated-uuid"

# Application definition
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "corsheaders",
    "oauth2_provider",
    "social_django",
]

# DAB apps - For immediate setup without complex dependencies
# Full AAP features available when installing with: pip install -e ".[dev]"
DAB_APPS = [
    "ansible_base",
    "ansible_base.rest_filters",
    "ansible_base.rest_pagination",
    "ansible_base.rbac",
    "ansible_base.authentication",
    # "ansible_base.oauth2_provider",  # Temporarily disabled due to conflicts
    "ansible_base.activitystream",
    "ansible_base.jwt_consumer",
    "ansible_base.resource_registry",
]


LOCAL_APPS = [
    "apps.core",
    "apps.tasks",
    "apps.api",
    "apps.dashboard",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + DAB_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "ansible_base.authentication.middleware.AuthenticatorBackendMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "ansible_base.lib.middleware.logging.LogRequestMiddleware",
    "ansible_base.lib.middleware.logging.LogTracebackMiddleware",
]

ROOT_URLCONF = "metrics_service.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "metrics_service.wsgi.application"
ASGI_APPLICATION = "metrics_service.asgi.application"

# Database
# PostgreSQL as default database
# Override with METRICS_SERVICE_DATABASES__default__HOST, etc.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        "USER": "metrics_service",
        "PASSWORD": "metrics_service",
        "NAME": "metrics_service",
        "OPTIONS": {
            "sslmode": "prefer",
        },
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]

# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom User Model
AUTH_USER_MODEL = "core.User"
# Redirect unauthenticated users to our custom login page
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/api/v1/"
LOGOUT_REDIRECT_URL = "/login/"
# Django REST Framework Configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
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
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI/Swagger Documentation
SPECTACULAR_SETTINGS = {
    "TITLE": "Metrics Service API",
    "DESCRIPTION": "API documentation for Metrics Service",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]",
    "SCHEMA_PATH_PREFIX_TRIM": True,
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,
    },
}

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = True  # Only for development
CORS_ALLOWED_ORIGINS: list[str] = []  # Set in production

# Django-Ansible-Base Configuration
ANSIBLE_BASE_TEAM_MODEL = "core.Team"
ANSIBLE_BASE_ORGANIZATION_MODEL = "core.Organization"
ANSIBLE_BASE_RESOURCE_CONFIG_MODULE = "apps.core.resource_api"
ANSIBLE_BASE_USER_VIEWSET = "apps.api.v1.views.UserViewSet"

# RBAC Configuration
ANSIBLE_BASE_ALLOW_SINGLETON_USER_ROLES = True
ANSIBLE_BASE_ALLOW_SINGLETON_TEAM_ROLES = True
ALLOW_SHARED_RESOURCE_CUSTOM_ROLES = True
ALLOW_LOCAL_ASSIGNING_JWT_ROLES = True  # Set to False with resource server
ANSIBLE_BASE_RBAC_MODEL_REGISTRY: dict[str, str] = {}
ANSIBLE_BASE_MANAGED_ROLE_REGISTRY: dict[str, str] = {}

# Authentication Backends
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",  # Default Django auth
    "ansible_base.authentication.backend.AnsibleBaseAuth",
]

# JWT Consumer Configuration
JWT_CONSUMER_ENABLED = True
JWT_CONSUMER_ALGORITHM = "HS256"

# OAuth2 Provider Configuration
OAUTH2_PROVIDER = {
    "SCOPES": {
        "read": "Read scope",
        "write": "Write scope",
    },
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 3600 * 24,
    "APPLICATION_MODEL": "oauth2_provider.Application",
}

# OAuth2 Provider Application Model - required by ansible_base
OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth2_provider.Application"

# Resource Server Configuration
RESOURCE_SERVER: dict[str, str | bool | None] = {
    # 'URL': 'https://aap-gw-proxy-1:9080',
    # 'SECRET_KEY': '<service key>',
    # 'VALIDATE_HTTPS': False,
}
RESOURCE_SERVER_SYNC_ENABLED = False

# Background Task Configuration (Dispatcherd)
# Dispatcherd is always enabled in this service (can be disabled in tests)
# Override with METRICS_SERVICE_DISPATCHERD_ENABLED environment variable
DISPATCHERD_ENABLED = True

# Feature Flags
# Override with METRICS_SERVICE_FEATURE_FLAGS__ANONYMIZED_DATA_COLLECTION, etc.
FEATURE_FLAGS = {
    "ANONYMIZED_DATA_COLLECTION": True,
    "METRICS_COLLECTION_ENABLED": False,
}

# Cache Configuration
# Use local memory cache
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default",
    }
}

# Session Configuration
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Logging Configuration (will be enhanced in post_load.py)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "ansible_base.lib.logging.filters.RequestIdFilter",
        },
    },
    "formatters": {
        "simple": {
            "format": "{asctime} {levelname:<8} {name} {message}",
            "style": "{",
        },
        "verbose": {
            "format": "{asctime} {levelname:<8} [{request_id}] {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id"],
        },
    },
    "loggers": {
        "ansible_base": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "metrics_service": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": True,
        },
    },
}

# Security Settings
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
