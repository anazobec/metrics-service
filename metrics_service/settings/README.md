# Metrics Service Settings

This document describes the settings configuration system for Metrics Service, which follows the AAP (Ansible Automation Platform) Phase 1 standards from [handbook proposal 0014-Django-Settings](https://handbook.eng.ansible.com/proposals/0014-Django-Settings).

## Overview

Metrics Service uses **Dynaconf** for settings management, providing a flexible, secure, and standardized approach to configuration across development and production environments.

## Architecture

### Settings Files

```
metrics_service/settings/
├── __init__.py          # Main settings module - Dynaconf factory integration
├── defaults.py          # Django defaults (base configuration)
├── development.py       # Development-specific overrides (legacy)
├── test.py             # Test environment settings
└── README.md           # This file

config/
├── settings.yaml        # Environment-specific configuration (Dynaconf)
└── settings.local.yaml  # Local overrides (git-ignored)
```

### Key Components

1. **`defaults.py`**: Base Django settings with sensible defaults for all environments
2. **`config/settings.yaml`**: Dynaconf configuration with environment-specific sections
3. **Environment variables**: Highest priority overrides using `METRICS_SERVICE_` prefix
4. **Validators**: Enforce security requirements in production

## Configuration Precedence

Settings are loaded in the following order (lowest to highest priority):

1. **defaults.py** - Base Django constants
2. **config/settings.yaml** (`default:` section) - Baseline for all environments
3. **config/settings.yaml** (environment section) - Environment-specific overrides
4. **/etc/ansible-automation-platform/settings.yaml** - System-wide AAP settings
5. **/etc/ansible-automation-platform/metrics-service/settings.yaml** - Service-specific AAP settings
6. **Environment variables** with `METRICS_SERVICE_` prefix - **Highest priority**

This precedence is implemented using DAB's (Django Ansible Base) manual loaders:
```python
load_standard_settings_files(DYNACONF)  # Load system settings
load_envvars(DYNACONF)                  # Load environment variables (highest priority)
```

## Environment Modes

The environment mode is controlled by the `METRICS_SERVICE_MODE` environment variable.

### Development Mode

**Environment Variable:**
```bash
export METRICS_SERVICE_MODE=development
```

**Behavior:**
- Uses defaults from `config/settings.yaml`
- Validation **disabled** - allows default values for easy setup
- DEBUG enabled
- CORS allows all origins
- No environment variables required

**Use Case:** Local development and testing

### Production Mode

**Environment Variable:**
```bash
export METRICS_SERVICE_MODE=production
```

**Behavior:**
- Validation **enforced** - requires explicit configuration
- SECRET_KEY **must** be set via environment variable
- Database credentials **must** be configured
- DEBUG disabled by default
- Empty ALLOWED_HOSTS (must be explicitly set)

**Use Case:** Production deployments

**Example Production Configuration:**
```bash
export METRICS_SERVICE_MODE=production
export METRICS_SERVICE_SECRET_KEY="your-secure-random-key-here"
export METRICS_SERVICE_DATABASES__default__HOST="prod-db.example.com"
export METRICS_SERVICE_DATABASES__default__PASSWORD="secure-db-password"
export METRICS_SERVICE_ALLOWED_HOSTS="app.example.com,metrics.example.com"
```

## Environment Variables

All settings can be overridden using environment variables with the `METRICS_SERVICE_` prefix.

### Flat Settings

```bash
export METRICS_SERVICE_DEBUG=true
export METRICS_SERVICE_SECRET_KEY="my-secret-key"
```

### Nested Settings (Dunder Notation)

For nested dictionaries, use double underscores (`__`):

```bash
# Database configuration
export METRICS_SERVICE_DATABASES__default__HOST=localhost
export METRICS_SERVICE_DATABASES__default__PORT=5432
export METRICS_SERVICE_DATABASES__default__NAME=metrics_service
export METRICS_SERVICE_DATABASES__default__USER=metrics_user
export METRICS_SERVICE_DATABASES__default__PASSWORD=secure_password
```

This is equivalent to:
```python
DATABASES = {
    "default": {
        "HOST": "localhost",
        "PORT": "5432",
        "NAME": "metrics_service",
        "USER": "metrics_user",
        "PASSWORD": "secure_password"
    }
}
```

### Common Environment Variables

| Variable | Description | Required in Production |
|----------|-------------|----------------------|
| `METRICS_SERVICE_MODE` | Environment mode (development/production) | No (defaults to development) |
| `METRICS_SERVICE_SECRET_KEY` | Django secret key | **Yes** |
| `METRICS_SERVICE_DEBUG` | Enable debug mode | No |
| `METRICS_SERVICE_DATABASES__default__HOST` | Database host | No (has default) |
| `METRICS_SERVICE_DATABASES__default__NAME` | Database name | No (has default) |
| `METRICS_SERVICE_DATABASES__default__USER` | Database user | No (has default) |
| `METRICS_SERVICE_DATABASES__default__PASSWORD` | Database password | No (has default) |
| `METRICS_SERVICE_ALLOWED_HOSTS` | Allowed hosts (comma-separated) | **Yes** (production) |

## Validators

Dynaconf validators enforce configuration requirements in production mode.

### SECRET_KEY Validators

Prevents using default SECRET_KEY values:
```python
Validator("SECRET_KEY", ne="dev-secret-key-change-in-production")
Validator("SECRET_KEY", ne="your-secret-key-here-change-in-production")
Validator("SECRET_KEY", ne="PRODUCTION-SECRET-KEY-NOT-SET")
```

**Error Message:**
```
dynaconf.validator.ValidationError: SECRET_KEY must be set in production.
Set METRICS_SERVICE_SECRET_KEY environment variable.
```

### Database Validators

Ensures critical database settings exist:
```python
Validator("DATABASES.default.NAME", must_exist=True)
Validator("DATABASES.default.HOST", must_exist=True)
Validator("DATABASES.default.USER", must_exist=True)
Validator("DATABASES.default.PASSWORD", must_exist=True)
```

**Note:** Validators only run when `validation=True` in the export call, which happens in production mode.

## Configuration Files

### config/settings.yaml

The main Dynaconf configuration file with environment-specific sections:

```yaml
# Base configuration - inherited by all environments
default:
  SECRET_KEY: 'your-secret-key-here-change-in-production'
  DEBUG: false
  DATABASES:
    default:
      ENGINE: django.db.backends.postgresql
      HOST: localhost
      PORT: 5432
      USER: metrics_service
      PASSWORD: metrics_service
      NAME: metrics_service

# Development overrides
development:
  DEBUG: true
  CORS_ALLOW_ALL_ORIGINS: true

# Production - forces explicit configuration
production:
  DEBUG: false
  SECRET_KEY: 'PRODUCTION-SECRET-KEY-NOT-SET'  # Will fail validation
  ALLOWED_HOSTS: []
```

### config/settings.local.yaml (Optional)

Create this file for personal local overrides. It's git-ignored and loads last (before environment variables):

```yaml
default:
  DATABASES:
    default:
      PORT: 55432  # Local PostgreSQL on different port
```

### .env File (Optional)

Dynaconf can load from `.env` files automatically. Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your local settings:
```bash
METRICS_SERVICE_MODE=development
METRICS_SERVICE_DATABASES__default__HOST=127.0.0.1
METRICS_SERVICE_DATABASES__default__PORT=5432
```

## Testing

### Running the Application

**Development:**
```bash
# Uses defaults - no env vars required
python manage.py runserver
```

**Production:**
```bash
# Requires explicit configuration
export METRICS_SERVICE_MODE=production
export METRICS_SERVICE_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"
export METRICS_SERVICE_ALLOWED_HOSTS="localhost,127.0.0.1"
python manage.py runserver
```

### Checking Configuration

Verify settings are loaded correctly:
```bash
python manage.py check
```

View current settings:
```bash
python manage.py shell
>>> from django.conf import settings
>>> settings.SECRET_KEY
>>> settings.DATABASES
```

### Unit Tests

Comprehensive unit tests for the Dynaconf configuration are located at:

**`tests/unit/test_dynaconf_settings.py`**

**Test Coverage:**

1. **TestDynaconfPrecedence** - Verifies settings precedence order
   - Environment variables are loaded correctly
   - Dynaconf can read environment variables
   - Database defaults load properly
   - CORS settings load correctly

2. **TestDynaconfValidators** - Tests validator configuration
   - Validators are registered
   - SECRET_KEY validators configured correctly
   - Database validators configured
   - Settings pass validation

3. **TestEnvironmentSwitching** - Tests environment mode switching
   - Current environment is set correctly
   - Development mode flag works

4. **TestDynaconfFactory** - Tests DAB factory integration
   - Factory creates Dynaconf instance
   - Factory registers validators
   - Environment variable prefix configured

5. **TestSettingsFileLoading** - Tests file loading order
   - defaults.py values loaded
   - config/settings.yaml loaded

**Running the Tests:**

```bash
# Run all Dynaconf settings tests
export METRICS_SERVICE_SECRET_KEY="test-key"
export DJANGO_SETTINGS_MODULE="metrics_service.settings"
pytest tests/unit/test_dynaconf_settings.py -v

# Run with coverage
pytest tests/unit/test_dynaconf_settings.py --cov=metrics_service.settings

# Run specific test class
pytest tests/unit/test_dynaconf_settings.py::TestDynaconfValidators -v
```

**Note:** Tests require `METRICS_SERVICE_SECRET_KEY` to be set since they use the main settings module (not test settings).

## Troubleshooting

### Common Issues

**1. ValidationError: SECRET_KEY must be set in production**

**Solution:** Set the SECRET_KEY environment variable:
```bash
export METRICS_SERVICE_SECRET_KEY="your-secure-key-here"
```

**2. Application uses wrong environment**

**Problem:** Set `METRICS_SERVICE_ENV` but it has no effect

**Solution:** Use `METRICS_SERVICE_MODE` (not `_ENV`):
```bash
export METRICS_SERVICE_MODE=production  # ✓ Correct
export METRICS_SERVICE_ENV=production   # ✗ Wrong variable name
```

**3. Environment variable not being read**

**Problem:** Set `SOME_VARIABLE=value` but Dynaconf doesn't see it

**Solution:** Use the correct prefix:
```bash
export METRICS_SERVICE_DEBUG=true  # ✓ Correct (has prefix)
export DEBUG=true                  # ✗ Wrong (no prefix)
```

**4. Nested settings not working**

**Problem:** `METRICS_SERVICE_DATABASES.default.HOST=localhost` doesn't work

**Solution:** Use double underscores for nesting:
```bash
export METRICS_SERVICE_DATABASES__default__HOST=localhost  # ✓ Correct (double underscore)
export METRICS_SERVICE_DATABASES.default.HOST=localhost    # ✗ Wrong (dots)
```

### Debugging

**Check what Dynaconf loaded:**
```python
from metrics_service.settings import DYNACONF

# See current environment
print(DYNACONF.current_env)  # Should be DEVELOPMENT or PRODUCTION

# See all loaded settings
print(DYNACONF.as_dict())

# Check specific setting
print(DYNACONF.get('SECRET_KEY'))
print(DYNACONF.get('DATABASES'))
```

**Check environment variable precedence:**
```bash
# 1. Check if env var is set
echo $METRICS_SERVICE_DEBUG

# 2. Run Python to see what Django sees
python -c "from django.conf import settings; print(settings.DEBUG)"

# 3. Compare with Dynaconf
python -c "from metrics_service.settings import DYNACONF; print(DYNACONF.get('DEBUG'))"
```

## Migration from Old Settings

If migrating from the old split-settings approach:

**Old way (settings/development.py):**
```python
from .defaults import *
DEBUG = True
```

**New way (config/settings.yaml):**
```yaml
development:
  DEBUG: true
```

**Or via environment variable:**
```bash
export METRICS_SERVICE_DEBUG=true
```

The old `development.py` and `test.py` files still exist for backward compatibility but are not used when loading via the main settings module.

## Additional Resources

- [Dynaconf Documentation](https://www.dynaconf.com/)
- [AAP Handbook Proposal 0014](https://handbook.eng.ansible.com/proposals/0014-Django-Settings)
- [Django Settings Documentation](https://docs.djangoproject.com/en/stable/topics/settings/)
- [Django Ansible Base](https://github.com/ansible/django-ansible-base)

## Support

For issues or questions about settings configuration:
1. Check this README
2. Review the unit tests in `tests/unit/test_dynaconf_settings.py`
3. Check the Django check command output: `python manage.py check`
4. File an issue in the metrics-service repository
