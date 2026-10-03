"""Stop counting a reassigned sale's commission twice.

A sale moved to another employee (or to none) kept the old employee's
commission alongside the new one's. The old one is cut down to what was
actually paid on it, so only the paid part still counts for them.
"""

from django.conf import settings
from django.db import migrations


def shrink_reassigned_commissions(apps, schema_editor):
    # A salesperson laptop holds no commissions; the server fixes them.
    if getattr(settings, 'SYNC_ROLE', 'standalone') == 'salesperson':
        return
    Commission = apps.get_model('panel', 'Commission')
    for commission in Commission.objects.select_related('sale').iterator():
        if (commission.employee_id != commission.sale.employee_id
                and commission.amount != commission.paid_amount):
            commission.amount = commission.paid_amount
            commission.save()


class Migration(migrations.Migration):

    dependencies = [
        ('panel', '0010_backfill_commissions'),
    ]

    operations = [
        migrations.RunPython(shrink_reassigned_commissions, migrations.RunPython.noop),
    ]
