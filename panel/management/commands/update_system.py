"""Pull the latest code from GitHub and apply it (requirements, migrations).

    python manage.py update_system

This is what the "تحديث النظام" button runs in the background. Run it by hand
on the server, or on a laptop whose update page can't be opened. See
``panel/updater.py`` for the steps and how failures are rolled back.

Exits non-zero on failure.
"""

import sys

from django.core.management.base import BaseCommand

from panel import updater


class Command(BaseCommand):
    help = "Update the system from GitHub: pull, install requirements, migrate."
    # Must run even when the installed version fails its checks: that's
    # exactly when an update with the fix is needed.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            '--reload', choices=['touch', 'none'], default='none',
            help="touch: make a running `runserver` restart when done.",
        )
        parser.add_argument(
            '--skip-sync', action='store_true',
            help="Don't upload pending sync data first.",
        )

    def handle(self, *args, **options):
        ok = updater.run_update(reload_mode=options['reload'], skip_sync=options['skip_sync'])
        status = updater.read_status()
        style = self.style.SUCCESS if ok else self.style.ERROR
        self.stdout.write(style(status.get('message') or ('done' if ok else 'failed')))
        if not ok:
            sys.exit(1)
