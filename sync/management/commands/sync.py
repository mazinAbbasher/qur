"""Run one push+pull sync from a laptop (manual or via cron/Task Scheduler).

    python manage.py sync

Exits non-zero on failure so schedulers can detect problems.
"""

import sys

from django.core.management.base import BaseCommand

from sync.client import run_sync


class Command(BaseCommand):
    help = "Push local changes to the server and pull the latest data."

    def handle(self, *args, **options):
        summary = run_sync()
        style = self.style.SUCCESS if summary['ok'] else self.style.ERROR
        self.stdout.write(style(summary['message'] or 'done'))
        if not summary['ok']:
            sys.exit(1)
