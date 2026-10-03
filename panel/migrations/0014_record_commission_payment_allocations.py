"""Record how each existing commission payment was split over commissions.

Editing or deleting a payment takes exactly its own share back out of the
commissions it paid, so that split must be known. Payments made from now on
record it as they are distributed; for the existing ones it is rebuilt here by
replaying each employee's payments in order, the same way 0012 did: a payment
with a period pays that period's commissions first, one without pays the
commissions it is linked to first, and anything left over goes to the oldest
others. The commissions' paid amounts are set to the replayed ones, so the
recorded splits always add up to them.

An employee whose replayed total wouldn't match what is recorded as paid (e.g.
a payment deleted by hand) is left untouched; their payments keep an empty
split and are taken back from the commissions they are linked to instead.
"""

from decimal import Decimal

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def _month(dt):
    # The employee page groups sales by month in the local timezone.
    local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local.year, local.month


def record_allocations(apps, schema_editor):
    # A salesperson laptop holds no commissions; the server records theirs.
    if getattr(settings, 'SYNC_ROLE', 'standalone') == 'salesperson':
        return
    Commission = apps.get_model('panel', 'Commission')
    CommissionPayment = apps.get_model('panel', 'CommissionPayment')

    employee_ids = CommissionPayment.objects.values_list('employee_id', flat=True).distinct()
    for employee_id in employee_ids:
        commissions = list(Commission.objects.filter(employee_id=employee_id)
                           .select_related('sale').order_by('created_at', 'pk'))
        paid = {c.pk: Decimal('0') for c in commissions}
        replayed = []
        payments = CommissionPayment.objects.filter(employee_id=employee_id).order_by('paid_at', 'pk')
        for payment in payments:
            linked = set(payment.commissions.values_list('pk', flat=True))
            if payment.period_month and payment.period_year:
                period = (payment.period_year, payment.period_month)
                first = {c.pk for c in commissions if _month(c.sale.created_at) == period}
            else:
                first = linked
            remaining = payment.amount
            allocations = {}
            for c in sorted(commissions, key=lambda c: c.pk not in first):  # stable
                if remaining <= 0:
                    break
                # One overpaid before its sale shrank (a later return) still
                # holds what it was paid.
                pay = min(max(c.amount, c.paid_amount) - paid[c.pk], remaining)
                if pay <= 0:
                    continue
                paid[c.pk] += pay
                remaining -= pay
                allocations[c.pk] = pay
            replayed.append((payment, linked, allocations))

        # Payments used to be split in floats, so allow a cent per commission.
        drift = abs(sum(paid.values()) - sum(c.paid_amount for c in commissions))
        if drift > Decimal('0.01') * len(commissions):
            continue
        sale_sync_ids = {c.pk: str(c.sale.sync_id) for c in commissions}
        for c in commissions:
            if c.paid_amount != paid[c.pk]:
                c.paid_amount = paid[c.pk]
                c.save()
        for payment, linked, allocations in replayed:
            if linked != set(allocations):
                payment.commissions.set(list(allocations))
            # update() rather than save(): nothing may re-distribute the
            # payment. The new timestamp sends the split to the laptops.
            CommissionPayment.objects.filter(pk=payment.pk).update(
                allocations={sale_sync_ids[pk]: str(amount) for pk, amount in allocations.items()},
                sync_updated_at=timezone.now(),
            )


class Migration(migrations.Migration):

    dependencies = [
        ('panel', '0013_commissionpayment_allocations'),
    ]

    operations = [
        migrations.RunPython(record_allocations, migrations.RunPython.noop),
    ]
