"""Queue every existing record for upload.

Use this ONCE on the manager laptop that holds the current data, to seed a
fresh central server:

    python manage.py sync_seed
    python manage.py sync          # uploads everything to the server

It simply fills the outbox with all current rows; because the server upserts by
sync_id, running it more than once is harmless (no duplicates).
"""

from django.core.management.base import BaseCommand

from sync.models import SyncOutbox
from sync.registry import SYNC_ORDER
from sync.serializers import get_model


class Command(BaseCommand):
    help = "Queue every existing record for upload (initial server seeding)."

    def handle(self, *args, **options):
        total = 0
        for label in SYNC_ORDER:
            model = get_model(label)
            for sync_id in model.objects.values_list('sync_id', flat=True):
                SyncOutbox.objects.update_or_create(
                    model_label=label, sync_id=sync_id, defaults={'deleted': False})
                total += 1
            self.stdout.write(f"  queued {label}")
        self.stdout.write(self.style.SUCCESS(
            f"Queued {total} records. Now run: python manage.py sync"))
