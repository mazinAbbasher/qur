"""Seed the three supported currencies so a fresh database is usable.

The codes are a fixed enum (Currency.CODE_CHOICES). Several views look a
currency up by code with ``Currency.objects.get(code='SDG')`` (shipment/purchase
creation, balance checks, supplier payments, …). On a brand-new laptop whose
database has not been populated yet those rows don't exist and the lookup raises
Currency.DoesNotExist — surfacing as the "currency" error when adding a purchase.

Seeding them here guarantees they are always present. It is idempotent
(get_or_create by code) so it never clobbers an existing name.

The ``sync_id`` is derived from the code (``currency_sync_id``) rather than a
random uuid4, so every database seeds the *same* identity for a given currency.
Otherwise each node would invent its own sync_id for 'USD', and pulling the
server's copy would collide on the unique ``code`` (see migration 0007, which
converges databases seeded before this was made deterministic).
"""

from django.db import migrations

from finance.sync_keys import currency_sync_id


# code -> Arabic display name (matches names already in use in production data).
CURRENCIES = [
    ('USD', 'دولار'),
    ('AED', 'درهم'),
    ('SDG', 'جنيه'),
]


def seed_currencies(apps, schema_editor):
    Currency = apps.get_model('finance', 'Currency')
    for code, name in CURRENCIES:
        Currency.objects.get_or_create(
            code=code,
            defaults={'name': name, 'sync_id': currency_sync_id(code)},
        )


def unseed_currencies(apps, schema_editor):
    # Reverse is a no-op: removing reference rows other tables may point at
    # (CurrencyExchange, PartnerTransaction, …) would break FKs. Leave them.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0005_sync_fields'),
    ]

    operations = [
        migrations.RunPython(seed_currencies, unseed_currencies),
    ]
