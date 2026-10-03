"""Give every existing sale the commission it should have.

Sales synced up from a salesperson laptop, and sales made while their
employee's percentage was 0, were saved without one, so the employee pages
showed their sales but no commission. Each such sale gets one at its
employee's current percentage, dated with the sale so it lands in the sale's
month (and in FIFO payment order). Unpaid commissions are also recomputed
from the current total, as saving the employee would do; anything already
(partly) paid is left as it is.
"""

from decimal import Decimal

from django.conf import settings
from django.db import migrations


def backfill_commissions(apps, schema_editor):
    # A salesperson laptop holds no commission percentages; the server
    # computes theirs.
    if getattr(settings, 'SYNC_ROLE', 'standalone') == 'salesperson':
        return
    Sale = apps.get_model('panel', 'Sale')
    Commission = apps.get_model('panel', 'Commission')

    commissions = {(c.employee_id, c.sale_id): c for c in Commission.objects.all()}
    sales = Sale.objects.filter(employee__isnull=False).select_related('employee')
    for sale in sales.iterator():
        amount = (Decimal(sale.total or 0)
                  * Decimal(sale.employee.commission_percentage or 0)
                  / 100).quantize(Decimal('0.01'))
        commission = commissions.get((sale.employee_id, sale.pk))
        if commission is None:
            Commission.objects.create(employee_id=sale.employee_id, sale=sale,
                                      amount=amount, created_at=sale.created_at)
        elif commission.paid_amount == 0 and commission.amount != amount:
            commission.amount = amount
            commission.save()


class Migration(migrations.Migration):

    dependencies = [
        ('panel', '0009_alter_invoice_number'),
    ]

    operations = [
        migrations.RunPython(backfill_commissions, migrations.RunPython.noop),
    ]
