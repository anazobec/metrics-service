"""
Test settings for metrics_service.

This module provides test-specific Django settings that override
the default settings for running tests with pytest.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Basic required settings
SECRET_KEY = "test-secret-key-for-testing-only-not-secure"
DEBUG = True
TESTING = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

# Application definition - simplified for testing
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
    "oauth2_provider",
    "ansible_base",
    "ansible_base.activitystream",
    "ansible_base.rest_filters",
    "ansible_base.rest_pagination",
    "ansible_base.rbac",
    "ansible_base.oauth2_provider",
]

LOCAL_APPS = [
    "apps.core",
    "apps.tasks",
    "apps.dashboard",
    "apps.api",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# Django ansible-base settings for testing
ANSIBLE_BASE_ORGANIZATION_MODEL = "core.Organization"
ANSIBLE_BASE_TEAM_MODEL = "core.Team"
ANSIBLE_BASE_USER_MODEL = "core.User"
AUTH_USER_MODEL = "core.User"

# RBAC settings
ANSIBLE_BASE_RBAC_MODEL_REGISTRY: dict[str, str] = {}
ANSIBLE_BASE_MANAGED_ROLE_REGISTRY: dict[str, str] = {}
ANSIBLE_BASE_BYPASS_SUPERUSER_FLAGS = ["is_superuser"]
ANSIBLE_BASE_BYPASS_ACTION_FLAGS = {
    "create": "is_superuser",
    "read": "is_superuser",
    "update": "is_superuser",
    "delete": "is_superuser",
}
ANSIBLE_BASE_ALLOW_SINGLETON_USER_ROLES = True
ANSIBLE_BASE_ALLOW_SINGLETON_TEAM_ROLES = True

# Service identification
SERVICE_TYPE = "metrics-service"
SERVICE_ID = "test-service-id"

# Middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# URLs - simplified for testing to avoid oauth2 provider conflicts
ROOT_URLCONF = "metrics_service.test_urls"

# Templates
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

# Use SQLite for faster tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {
            "timeout": 20,
        },
    }
}


# Disable migrations during tests for faster execution
class DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()

# Security settings for testing
SECRET_KEY = "test-secret-key-for-testing-only-not-secure"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

# Disable caching during tests
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    }
}

# Use faster password hashing for tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Email settings for testing
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Disable logging during tests
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "null": {
            "class": "logging.NullHandler",
        },
    },
    "root": {
        "handlers": ["null"],
    },
}

# Disable dispatcherd during tests to avoid background processes
DISPATCHERD_ENABLED = False

# Disable feature flags during tests
FEATURE_FLAGS = {
    "ANONYMIZED_DATA_COLLECTION": False,
    "METRICS_COLLECTION_ENABLED": False,
}

# Test-specific apps - INSTALLED_APPS is already defined in defaults.py

# Static files settings for tests
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Media files settings for tests
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# OAuth2 Provider Configuration for testing
OAUTH2_PROVIDER = {
    "SCOPES": {
        "read": "Read scope",
        "write": "Write scope",
    },
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 3600 * 24,
}
OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth2_provider.Application"
OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL = "oauth2_provider.AccessToken"
OAUTH2_PROVIDER_REFRESH_TOKEN_MODEL = "oauth2_provider.RefreshToken"
OAUTH2_PROVIDER_ID_TOKEN_MODEL = "oauth2_provider.IDToken"
OAUTH2_PROVIDER_GRANT_MODEL = "oauth2_provider.Grant"

# REST Framework settings for tests
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# Test database settings
TEST_DATABASE_PREFIX = "test_"
