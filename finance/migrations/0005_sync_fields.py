"""Add sync identity to finance models (same safe 3-step pattern as panel)."""

import uuid

import django.utils.timezone
from django.db import migrations, models


FINANCE_MODELS = [
    'currency', 'financiallog', 'currencyexchange', 'partner', 'partnertransaction',
]


def backfill_sync_ids(apps, schema_editor):
    for model_name in FINANCE_MODELS:
        Model = apps.get_model('finance', model_name)
        for pk in Model.objects.filter(sync_id__isnull=True).values_list('pk', flat=True):
            Model.objects.filter(pk=pk).update(sync_id=uuid.uuid4())


def _add_ops():
    ops = []
    for m in FINANCE_MODELS:
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
    ops.append(migrations.RunPython(backfill_sync_ids, migrations.RunPython.noop))
    for m in FINANCE_MODELS:
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
        ('finance', '0004_currencyexchange_delete_currencypurchase'),
    ]

    operations = _add_ops()
