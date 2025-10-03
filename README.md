# Metrics Service

A modern Django-based service built for the Ansible Automation Platform (AAP) ecosystem, featuring comprehensive task management, REST APIs, and automated background job processing.

## Features

- **🚀 Modern Django Architecture** - Django 4.2+ with clean app-based structure
- **📊 Automated Task Management** - Feature-flag controlled task groups with automatic routing
- **⚡ Smart Task Routing** - Automatic submission to dispatcherd with no manual intervention
- **🔌 REST API** - Versioned RESTful APIs with OpenAPI documentation
- **🔐 Authentication & Authorization** - Django-Ansible-Base integration with RBAC
- **📈 Real-time Dashboard** - Web-based task monitoring and management interface
- **🐳 Docker Ready** - Simplified single-container deployment with PostgreSQL
- **🧪 Comprehensive Testing** - Unit and integration tests with coverage reporting
- **📝 API Documentation** - Interactive Swagger/OpenAPI documentation
- **🔧 Metrics Collection** - Integrated metrics-utility for data collection

## Quick Start

### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone <repository-url>
cd metrics-service

# Start all services
docker-compose up -d

# Create a superuser (optional)
docker-compose exec metrics-service python manage.py createsuperuser
```

Your service will be available at:

- **Application**: http://localhost:8000
- **API Documentation**: http://localhost:8000/api/docs/
- **Admin Interface**: http://localhost:8000/admin/
- **Task Dashboard**: http://localhost:8000/dashboard/

### Option 2: Local Development

```bash
# Prerequisites: Python 3.11+, PostgreSQL 13+

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Configure database (update as needed)
cp .env.example .env

# Set up database
python manage.py migrate
python manage.py metrics_service init-service-id
python manage.py metrics_service init-system-tasks
python manage.py createsuperuser

# Start complete service (Django + dispatcher + scheduler)
python manage.py metrics_service run
```

## Architecture

### Project Structure

```
metrics-service/
├── apps/
│   ├── api/v1/              # REST API endpoints
│   ├── core/                # Core models and business logic
│   ├── dashboard/           # Web dashboard interface
│   └── tasks/               # Background task system
├── metrics_service/
│   ├── settings/            # Environment-specific settings
│   └── urls.py              # URL configuration
├── tests/                   # Test suite
├── config/                  # Configuration files
└── docker-compose.yml       # Container orchestration
```

### Key Components

**Core Models** (`apps/core/models.py`)

- User management with Django-Ansible-Base
- Organization and team hierarchy
- RBAC permissions and roles

**Task System** (`apps/tasks/`)

- Feature-flag controlled task groups (System, Anonymized Data, Metrics Collection)
- Automatic task routing with Django signals
- APScheduler integration for cron-based scheduling
- Dispatcherd background task execution
- Task execution tracking and monitoring
- Built-in task functions and metrics collection

**API Layer** (`apps/api/v1/`)

- RESTful endpoints with filtering and pagination
- OpenAPI/Swagger documentation
- Authentication and permission controls

**Dashboard** (`apps/dashboard/`)

- Real-time task monitoring
- Task creation and management interface
- Live status updates every 5 seconds

## API Usage

### Authentication

The API supports multiple authentication methods:

- Session authentication (for web interface)
- Token authentication
- OAuth2 tokens (for third-party integrations)

### Core Endpoints

```bash
# List all tasks
GET /api/v1/tasks/

# Create a new task
POST /api/v1/tasks/
{
  "name": "Data Cleanup",
  "function_name": "cleanup_old_data",
  "task_data": {"days_old": 30}
}

# Get running tasks
GET /api/v1/tasks/running/

# Retry a failed task
POST /api/v1/tasks/{id}/retry/

# Available task functions
GET /api/v1/tasks/available_functions/
```

### Built-in Task Functions

**System Tasks** (always enabled):

- `cleanup_old_data` - Clean up old system data
- `cleanup_old_tasks` - Clean up completed/failed tasks
- `send_notification_email` - Send notification emails
- `process_user_data` - Process user data in background

**Metrics Collection Tasks** (feature flag controlled):

- `collect_anonymous_metrics` - Collect anonymous system metrics
- `collect_config_metrics` - Collect configuration information
- `collect_job_host_summary` - Collect job execution statistics
- `collect_host_metrics` - Collect host performance data
- `collect_all_metrics` - Run multiple collectors in sequence

## Background Tasks

The service includes an automated background task system with intelligent routing:

### Unified Service Management

```bash
# Start complete service (Django + dispatcher + scheduler)
python manage.py metrics_service run

# Start with custom configuration
python manage.py metrics_service run --workers 4 --log-level DEBUG

# Individual components (for development)
python manage.py run_dispatcherd --workers 2
python manage.py metrics_service cron start
```

### Automatic Task Routing

Tasks are automatically routed based on their properties:

- **Immediate tasks** → Direct to dispatcherd
- **Scheduled tasks** → APScheduler with DateTrigger
- **Recurring tasks** → APScheduler with CronTrigger

No manual intervention required - create a task and it's automatically processed!

### Task Groups & Feature Flags

Control task execution with environment variables:

```bash
# Enable/disable anonymized data collection
METRICS_SERVICE_ANONYMIZED_DATA=true

# Enable/disable metrics collection
METRICS_SERVICE_METRICS_COLLECTION=false
```

## Development

### Code Quality Tools

```bash
# Format code
black .

# Lint code
ruff check .

# Type checking
mypy .

# Sort imports
isort .
```

### Pre-commit Hooks

This project uses pre-commit hooks to ensure code quality and automatically sync requirements files:

```bash
# Install pre-commit hooks
pre-commit install

# Run hooks on all files
pre-commit run --all-files

# Run hooks manually
pre-commit run
```

The pre-commit configuration automatically:

- Syncs requirements files when `pyproject.toml` or `uv.lock` changes
- Ensures requirements files are always up-to-date before commits

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=apps --cov=metrics_service --cov-report=html

# Run specific test categories
pytest -m unit          # Unit tests only
pytest -m integration   # Integration tests only
```

### Database Operations

```bash
# Create migrations
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Initialize DAB ServiceID (required after first migration)
python manage.py metrics_service init-service-id

# Initialize system tasks
python manage.py metrics_service init-system-tasks
```

## Configuration

Metrics Service uses [Dynaconf](https://www.dynaconf.com/) for settings management, following the [AAP Phase 1 standards](https://handbook.eng.ansible.com/proposals/0014-Django-Settings).

### Quick Start

**Development Mode** (default):

```bash
# Database
METRICS_SERVICE_DB_HOST=localhost
METRICS_SERVICE_DB_PORT=55432
METRICS_SERVICE_DB_USER=metrics_service
METRICS_SERVICE_DB_PASSWORD=metrics_service
METRICS_SERVICE_DB_NAME=metrics_service

# Django
METRICS_SERVICE_SECRET_KEY=your-secret-key
METRICS_SERVICE_DEBUG=false
METRICS_SERVICE_ALLOWED_HOSTS=localhost,yourdomain.com

# Task Feature Flags
METRICS_SERVICE_ANONYMIZED_DATA=true
METRICS_SERVICE_METRICS_COLLECTION=false
# Just run - no configuration required
python manage.py runserver
```

**Production Mode**:

```bash
# Set environment mode and required secrets
export METRICS_SERVICE_MODE=production
export METRICS_SERVICE_SECRET_KEY="your-secure-random-key"
export METRICS_SERVICE_ALLOWED_HOSTS="yourdomain.com,api.yourdomain.com"

# Override defaults as needed
export METRICS_SERVICE_DATABASES__default__HOST=prod-db.example.com
export METRICS_SERVICE_DATABASES__default__PASSWORD=secure-password

python manage.py runserver
```

### Configuration Methods

Settings are loaded in order of precedence (lowest to highest):

1. **`metrics_service/settings/defaults.py`** - Base Django defaults
2. **`config/settings.yaml`** - Environment-specific configuration
3. **`/etc/ansible-automation-platform/settings.yaml`** - System-wide AAP settings
4. **Environment variables** with `METRICS_SERVICE_` prefix - **Highest priority**

### Common Environment Variables

| Variable                                       | Description                               | Required in Production       |
| ---------------------------------------------- | ----------------------------------------- | ---------------------------- |
| `METRICS_SERVICE_MODE`                         | Environment mode (development/production) | No (defaults to development) |
| `METRICS_SERVICE_SECRET_KEY`                   | Django secret key                         | **Yes**                      |
| `METRICS_SERVICE_DEBUG`                        | Enable debug mode                         | No                           |
| `METRICS_SERVICE_DATABASES__default__HOST`     | Database host                             | No (has default)             |
| `METRICS_SERVICE_DATABASES__default__PASSWORD` | Database password                         | No (has default)             |
| `METRICS_SERVICE_ALLOWED_HOSTS`                | Allowed hosts (comma-separated)           | **Yes** (production)         |

**Note:** Use double underscores (`__`) for nested settings:

```bash
# Nested database configuration
export METRICS_SERVICE_DATABASES__default__HOST=localhost
export METRICS_SERVICE_DATABASES__default__PORT=5432
```

For comprehensive configuration documentation, validators, troubleshooting, and testing information, see **[metrics_service/settings/README.md](metrics_service/settings/README.md)**.

## Deployment

### Docker Production

```bash
# Build production image
docker build -t metrics-service .

# Run with production settings
docker run -p 8000:8000 \
  -e METRICS_SERVICE_ENV=production \
  -e METRICS_SERVICE_SECRET_KEY=your-secret-key \
  -e METRICS_SERVICE_DB_HOST=your-db-host \
  metrics-service


## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes with tests
4. Run the test suite: `pytest`
5. Run code quality checks: `ruff check . && black . && mypy .`
6. Submit a pull request

### Development Standards

- **Code Style**: Black formatting, 120 character line length
- **Type Hints**: Required for all new code
- **Documentation**: Docstrings for public APIs
- **Testing**: Test coverage for new features
- **Commits**: Conventional commit messages

## License

This project is licensed under the Apache License - see the [LICENSE](LICENSE) file for details.

## Support

- **Documentation**: Check the [CLAUDE.md](CLAUDE.md) file for detailed development guidance
- **Issues**: Report bugs and feature requests via GitHub issues
- **API Documentation**: Interactive docs available at `/api/docs/` when running
```
