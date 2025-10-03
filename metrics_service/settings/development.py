"""
Development settings for metrics_service.
These settings are optimized for local development with Docker.
"""


from . import defaults

# Import all settings from defaults module
ALLOWED_HOSTS = defaults.ALLOWED_HOSTS
INSTALLED_APPS = defaults.INSTALLED_APPS
MIDDLEWARE = defaults.MIDDLEWARE
TEMPLATES = defaults.TEMPLATES
DATABASES = defaults.DATABASES
LOGGING = defaults.LOGGING
STATIC_URL = defaults.STATIC_URL
STATICFILES_DIRS = defaults.STATICFILES_DIRS
AUTH_USER_MODEL = defaults.AUTH_USER_MODEL
REST_FRAMEWORK = defaults.REST_FRAMEWORK
SPECTACULAR_SETTINGS = defaults.SPECTACULAR_SETTINGS

# Import other commonly used settings
SECRET_KEY = defaults.SECRET_KEY
ROOT_URLCONF = defaults.ROOT_URLCONF
WSGI_APPLICATION = defaults.WSGI_APPLICATION
LANGUAGE_CODE = defaults.LANGUAGE_CODE
TIME_ZONE = defaults.TIME_ZONE
USE_I18N = defaults.USE_I18N
USE_TZ = defaults.USE_TZ
DEFAULT_AUTO_FIELD = defaults.DEFAULT_AUTO_FIELD

# Override DEBUG setting
DEBUG = True

# More permissive ALLOWED_HOSTS for development
ALLOWED_HOSTS = ["*"]

# Development-specific logging with more verbose output
LOGGING["loggers"]["django"]["level"] = "DEBUG"  # noqa: F405
LOGGING["loggers"]["metrics_service"]["level"] = "DEBUG"  # noqa: F405
LOGGING["loggers"]["ansible_base"]["level"] = "DEBUG"  # noqa: F405

# Suppress DAB authentication plugin errors
LOGGING["loggers"]["ansible_base.authentication.authenticator_plugins.utils"] = {  # noqa: F405
    "handlers": [],
    "level": "CRITICAL",
    "propagate": False,
}
LOGGING["loggers"]["apps.core.apps"] = {  # noqa: F405
    "handlers": [],
    "level": "CRITICAL",
    "propagate": False,
}

# Disable CSRF for easier API testing in development
# Note: Only use in development!
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://metrics-service:8000",
]

# CORS settings for development
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# Cache settings - use dummy cache for development to avoid caching issues
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.dummy.DummyCache",
    }
}

# Email backend for development (console output)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


# Static files serving in development
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
