"""
Management command to reload dynaconf configuration.

This command triggers a reload of the dynaconf settings, allowing configuration
changes to be applied without restarting the entire application.
"""

import logging

from django.core.management.base import BaseCommand

from metrics_service.settings import DYNACONF

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Management command to reload dynaconf configuration."""

    help = "Reload dynaconf configuration from files and environment variables"
    dynaconf = DYNACONF

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed information about the reload process",
        )

    def handle(self, *args, **options):
        """Handle the command execution."""
        verbose = options.get("verbose", False)

        try:
            if verbose:
                self.stdout.write("Reloading dynaconf configuration...")

            # Call the existing reload function
            self.dynaconf.reload()

            # Log the successful reload
            logger.info("Dynaconf configuration reloaded successfully")
            self.stdout.write(self.style.SUCCESS("✅ Configuration reloaded successfully"))

            if verbose:
                from metrics_service.settings import DYNACONF

                self.stdout.write(f"Current environment: {DYNACONF.current_env}")
                self.stdout.write(f"Loaded settings files: {DYNACONF.settings_files}")

        except Exception as e:
            error_msg = f"Failed to reload configuration: {str(e)}"
            logger.error(error_msg)
            self.stdout.write(self.style.ERROR(f"❌ {error_msg}"))
            raise
