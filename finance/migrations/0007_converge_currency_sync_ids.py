"""Converge existing currency sync_ids to their deterministic value.

Databases seeded before the ids were made deterministic (0006 originally used a
random ``uuid4``) carry a *different* ``Currency.sync_id`` per machine for the
same code. Pulling then tried to INSERT the server's currency as a brand-new row
and hit the unique ``code`` constraint, which — because a pull applies in one
transaction — aborted the entire sync.

Rewriting each row's ``sync_id`` to ``currency_sync_id(code)`` makes every node
agree on one identity, so the engine matches the row instead of duplicating it.
The rewrite uses ``.update()`` so it does not bump ``sync_updated_at`` (no
needless re-propagation); the engine also self-heals this collision by natural
key, which covers the window before a laptop has run this migration.

Idempotent: run it on the server and every laptop. Reverse is a no-op — the
original random ids are gone and there is nothing to restore.
"""

from django.db import migrations

from finance.sync_keys import currency_sync_id


def converge_sync_ids(apps, schema_editor):
    Currency = apps.get_model('finance', 'Currency')
    for pk, code, sync_id in Currency.objects.values_list('pk', 'code', 'sync_id'):
        target = currency_sync_id(code)
        if str(sync_id) != str(target):
            Currency.objects.filter(pk=pk).update(sync_id=target)


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0006_seed_currencies'),
    ]

    operations = [
        migrations.RunPython(converge_sync_ids, migrations.RunPython.noop),
    ]
