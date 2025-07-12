from django import template

register = template.Library()

@register.filter
def payment_percent(paid, total):
    try:
        paid = float(paid)
        total = float(total)
        if total == 0:
            return 0
        percent = int(round((paid / total) * 100))
        return percent
    except Exception:
        return 0

@register.filter
def sum(queryset, field):
    from decimal import Decimal
    total = Decimal('0')
    for obj in queryset:
        value = getattr(obj, field, 0)
        if callable(value):
            value = value()
        if value is None:
            value = 0
        total += value
    return total
