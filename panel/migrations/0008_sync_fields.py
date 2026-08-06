"""Add sync identity (sync_id / sync_updated_at / is_deleted) to panel models.

Adding a *unique* UUID column to tables that already contain rows is done in
three safe steps so existing data is never violated:

1. Add ``sync_id`` as a nullable, non-unique column.
2. Backfill every existing row with its own fresh UUID (RunPython).
3. Tighten the column to ``unique=True`` with the uuid4 default for new rows.

``sync_updated_at`` is added with a concrete default first (to fill existing
rows) then switched to ``auto_now``. This migration is idempotent-safe and was
verified against a copy of the production database before shipping.
"""

import uuid

import django.utils.timezone
from django.db import migrations, models


# Lowercased model names in this app that inherit SyncModel.
PANEL_MODELS = [
    'area', 'client', 'employee', 'exchangerate', 'product', 'supplier',
    'shipment', 'supplierpayment', 'inventory', 'lostproduct', 'sale',
    'saleitem', 'returnedproduct', 'invoice', 'invoicepayment', 'expense',
    'commission', 'commissionpayment', 'manager', 'managercommissionpayment',
]


def backfill_sync_ids(apps, schema_editor):
    for model_name in PANEL_MODELS:
        Model = apps.get_model('panel', model_name)
        for pk in Model.objects.filter(sync_id__isnull=True).values_list('pk', flat=True):
            Model.objects.filter(pk=pk).update(sync_id=uuid.uuid4())


def _add_ops():
    ops = []
    # Step 1: nullable columns.
    for m in PANEL_MODELS:
        ops.append(migrations.AddField(
            model_name=m, name='sync_id',
            field=models.UUIDField(null=True, editable=False),
        ))
        ops.append(migrations.AddField(
            model_name=m, name='sync_updated_at',
            field=models.DateTimeField(default=django.utils.timezone.now, db_index=True),
        ))
        ops.append(migrations.AddField(
            model_name=m, name='is_deleted',
            field=models.BooleanField(default=False, db_index=True),
        ))
    # Step 2: backfill unique UUIDs for existing rows.
    ops.append(migrations.RunPython(backfill_sync_ids, migrations.RunPython.noop))
    # Step 3: tighten to the final model state.
    for m in PANEL_MODELS:
        ops.append(migrations.AlterField(
            model_name=m, name='sync_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ))
        ops.append(migrations.AlterField(
            model_name=m, name='sync_updated_at',
            field=models.DateTimeField(auto_now=True, db_index=True),
        ))
    return ops


class Migration(migrations.Migration):

    dependencies = [
        ('panel', '0007_alter_shipment_sale_usd'),
    ]

    operations = _add_ops()
