"""Move each commission payment onto the period it was recorded for.

A payment is recorded against a month (the employee page's month), but it was
spread over the employee's oldest unpaid commissions instead, so paying a
month's commission left that month showing unpaid — notably once older
months' missing commissions were filled in (0010).

Each employee's payments are replayed in order: a payment with a period pays
that period's commissions first; one without (made before periods existed)
pays the commissions it was linked to first, as it did originally. Anything
left over goes to the oldest others, as before. Only where the money sits
changes: an employee whose replayed total wouldn't match what is recorded as
paid (e.g. a payment deleted by hand) is left untouched.
"""

from decimal import Decimal

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def _month(dt):
    # The employee page groups sales by month in the local timezone.
    local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local.year, local.month


def reallocate(apps, schema_editor):
    # A salesperson laptop holds no commissions; the server fixes them.
    if getattr(settings, 'SYNC_ROLE', 'standalone') == 'salesperson':
        return
    Commission = apps.get_model('panel', 'Commission')
    CommissionPayment = apps.get_model('panel', 'CommissionPayment')

    employee_ids = CommissionPayment.objects.values_list('employee_id', flat=True).distinct()
    for employee_id in employee_ids:
        commissions = list(Commission.objects.filter(employee_id=employee_id)
                           .select_related('sale').order_by('created_at', 'pk'))
        paid = {c.pk: Decimal('0') for c in commissions}
        links = {}
        payments = CommissionPayment.objects.filter(employee_id=employee_id).order_by('paid_at', 'pk')
        for payment in payments:
            linked = set(payment.commissions.values_list('pk', flat=True))
            if payment.period_month and payment.period_year:
                period = (payment.period_year, payment.period_month)
                first = {c.pk for c in commissions if _month(c.sale.created_at) == period}
            else:
                first = linked
            remaining = payment.amount
            touched = set()
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
                touched.add(c.pk)
            links[payment.pk] = (payment, linked, touched)

        # Payments used to be split in floats, so allow a cent per commission.
        drift = abs(sum(paid.values()) - sum(c.paid_amount for c in commissions))
        if drift > Decimal('0.01') * len(commissions):
            continue
        for c in commissions:
            if c.paid_amount != paid[c.pk]:
                c.paid_amount = paid[c.pk]
                c.save()
        for payment, linked, touched in links.values():
            if linked != touched:
                payment.commissions.set(touched)
                # So the new links reach the laptops; update() rather than
                # save() so nothing can re-distribute the payment.
                CommissionPayment.objects.filter(pk=payment.pk).update(
                    sync_updated_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ('panel', '0011_reassigned_sale_commissions'),
    ]

    operations = [
        migrations.RunPython(reallocate, migrations.RunPython.noop),
    ]
