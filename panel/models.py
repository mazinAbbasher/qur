from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from decimal import Decimal
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
import random

# Every business model inherits sync identity (sync_id / sync_updated_at /
# is_deleted). See sync/mixins.py. The existing integer PKs are kept as-is.
from sync.mixins import SyncModel

USD_TO_SDG_RATE = Decimal('600')  # Example conversion rate

class Area(SyncModel):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

class Client(SyncModel):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    area = models.ForeignKey(Area, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.name

class Employee(SyncModel):
    name = models.CharField(max_length=100)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # %
    sales_target = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="الهدف الشهري")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def get_monthly_sales(self, month=None, year=None):
        from .models import Sale
        from datetime import date
        today = date.today()
        if not month:
            month = today.month
        if not year:
            year = today.year
        return Sale.objects.filter(employee=self, created_at__year=year, created_at__month=month).aggregate(total=models.Sum('total'))['total'] or 0

    def get_monthly_commission(self, month=None, year=None):
        # check for all commision in the time period
        from .models import Commission
        from datetime import date
        today = date.today()
        if not month:
            month = today.month
        if not year:
            year = today.year
        commissions = Commission.objects.filter(employee=self, sale__created_at__year=year, sale__created_at__month=month)
        return sum([c.amount for c in commissions])


        # sales = self.get_monthly_sales(month, year)
        # return float(sales) * float(self.commission_percentage or 0) / 100

    def get_unpaid_commission(self, month=None, year=None):
        from .models import Commission, Sale
        sales = Sale.objects.filter(employee=self)
        payments = self.commission_payments.all()
        if month and year:
            sales = sales.filter(created_at__year=year, created_at__month=month)
            payments = payments.filter(
                models.Q(period_month=month, period_year=year)
                | models.Q(period_month__isnull=True, paid_at__year=year, paid_at__month=month))
        commissions = Commission.objects.filter(employee=self, sale__in=sales)
        # What a payment paid toward commissions since cancelled (a deleted
        # sale, a return) is owed back, so it comes off what is unpaid, below
        # zero when nothing is left to come off.
        owed_back = sum(p.owed_back for p in payments)
        return sum([c.unpaid_amount for c in commissions]) - owed_back

    def get_outstanding_commissions(self, first=None):
        """[((month, year), unpaid)] for each month with commission still to
        pay (what get_unpaid_commission has unpaid for it, when above zero):
        the ``first`` (month, year) before the others, which go oldest first.
        A month owed back more than it has unpaid isn't listed."""
        from django.db.models.functions import ExtractMonth, ExtractYear
        months = (Commission.objects.filter(employee=self, amount__gt=models.F('paid_amount'))
                  .annotate(year=ExtractYear('sale__created_at'), month=ExtractMonth('sale__created_at'))
                  .values_list('month', 'year').distinct().order_by('year', 'month'))
        outstanding = []
        for month, year in months:
            unpaid = self.get_unpaid_commission(month=month, year=year)
            if unpaid > 0:
                outstanding.append(((month, year), unpaid))
        outstanding.sort(key=lambda o: o[0] != first)  # stable
        return outstanding

    def recalculate_commissions(self, rerate_partly_paid=True):
        """Bring this employee's commissions in line with their current percentage.

        A sale without a commission gets one (e.g. one made while the percentage
        was 0). An unpaid commission is recomputed in full; a partly paid one
        keeps its paid part and re-rates only the rest, unless
        ``rerate_partly_paid`` is False; a fully paid one is left alone.
        """
        if not computes_commissions():
            return
        commissions = {c.sale_id: c for c in Commission.objects.filter(employee=self)}
        for sale in Sale.objects.filter(employee=self):
            amount = commission_for(sale.total, self.commission_percentage)
            commission = commissions.get(sale.pk)
            if commission is None:
                Commission.objects.create(employee=self, sale=sale, amount=amount,
                                          created_at=sale.created_at)
                continue
            paid = float(commission.paid_amount)
            if paid == 0:
                pass  # recomputed in full: ``amount`` above
            elif rerate_partly_paid and paid < commission.amount:
                old_percentage = float(commission.amount) / float(sale.total or 0) * 100 if sale.total else 0
                exchange = float(self.commission_percentage) / old_percentage if old_percentage else 0
                # apply new percentage to just the remaining unpaid amount
                amount = _cents(paid + float(commission.unpaid_amount) * exchange)
            else:
                continue
            if commission.amount != amount:
                commission.amount = amount
                commission.save()

    def delete(self, *args, **kwargs):
        # Commission has on_delete=CASCADE, so the database removes them together
        # with the employee. Skip the manual pre-delete during sync apply (no
        # side effects mid-batch); super().delete() still cascades.
        from sync.tracking import sync_apply_active
        if not sync_apply_active():
            Commission.objects.filter(employee=self).delete()
        super().delete(*args, **kwargs)

    def clean(self):
        if self.commission_percentage < 0 or self.commission_percentage > 100:
            from django.core.exceptions import ValidationError
            raise ValidationError("نسبة العمولة يجب أن تكون بين 0 و 100.")

class ExchangeRate(SyncModel):
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Rate: {self.rate} at {self.updated_at}"

class Product(SyncModel):
    CATEGORY_CHOICES = [
        ('med', 'Medicine'),
        ('sup', 'Supplement'),
        ('oth', 'Other'),
    ]
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, null=True)  
    unit = models.CharField(max_length=50, null=True)  
    # cost_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    # cost_sdg = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    # sale_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    exchange_rate = models.IntegerField(null=True)
   

    # def save(self, *args, **kwargs):
    #     latest_rate = ExchangeRate.objects.order_by('-updated_at').first()
    #     if latest_rate:
    #         self.cost_sdg = self.cost_usd * latest_rate.rate
    #     else:
    #         self.cost_sdg = Decimal('0')
    #     super().save(*args, **kwargs)

    def get_category_display(self):
        return dict(self.CATEGORY_CHOICES).get(self.category, self.category)

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('panel:product_edit', args=[self.pk])

    def __str__(self):
        return self.name

class Supplier(SyncModel):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=30, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    note = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name

    @property
    def total_shipments_amount(self):
        # Total owed to supplier (sum of shipment cost + purchase cost for all shipments)
        return sum(
            (s.cost_usd or 0) * (s.quantity or 0)
            for s in self.shipments.all()
        )

    @property
    def total_paid(self):
        from decimal import Decimal
        return self.payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0')

    @property
    def balance(self):
        return self.total_shipments_amount - self.total_paid

    @property
    def remaining_amount(self):
        # For consistency with Invoice
        from decimal import Decimal
        return max(self.total_shipments_amount - self.total_paid, Decimal('0'))

    def update_balance(self):
        # For future extensibility, not strictly needed as balance is property
        pass

class Shipment(SyncModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    shipment_cost = models.DecimalField(max_digits=12, decimal_places=2)
    received_at = models.DateTimeField(default=timezone.now)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost_sdg = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    # 4 decimals so the USD value derived from a clean SDG price stores precisely.
    sale_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    # Sale price in SDG. This is the source of truth for the selling price so the
    # user can set a clean SDG amount without being limited by USD's 2 decimals.
    sale_sdg = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    batch_number = models.CharField(max_length=100)  # <-- moved here
    expiry_date = models.DateField() 
    exchange_rate = models.IntegerField(null=True)
                    # <-- moved here
    supplier = models.ForeignKey('Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='shipments')

    # @property
    # def profit(self):
    #     # Example: profit = (selling price - cost_sdg) * quantity - shipment_cost
    #     # Assume selling price is cost_sdg * 1.2 for demonstration
    #     selling_price = self.product.cost_sdg * Decimal('1.2')
    #     return (selling_price - self.product.cost_sdg) * self.quantity - self.shipment_cost

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('panel:shipment_edit', args=[self.pk])

    @property
    def sale_price_sdg(self):
        """Effective unit selling price in SDG.

        Prefers the explicit ``sale_sdg`` so a clean SDG price is preserved
        exactly; falls back to the legacy ``sale_usd * product.exchange_rate``
        for older shipments that predate the ``sale_sdg`` field.
        """
        if self.sale_sdg is not None:
            return self.sale_sdg
        rate = getattr(self.product, 'exchange_rate', None) or 0
        return Decimal(str(self.sale_usd or 0)) * Decimal(str(rate))

    def __str__(self):
        return f"Shipment of {self.product.name} ({self.quantity})"

class SupplierPayment(SyncModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # update_balance() is a no-op today, but guard for parity with the other
        # payment models so a future implementation can't run its side effects
        # mid-apply (the bug class that bit InvoicePayment).
        from sync.tracking import sync_apply_active
        if not sync_apply_active():
            self.supplier.update_balance()

    def delete(self, *args, **kwargs):
        supplier = self.supplier
        super().delete(*args, **kwargs)
        from sync.tracking import sync_apply_active
        if not sync_apply_active():
            supplier.update_balance()

    def __str__(self):
        return f"Payment {self.amount} to {self.supplier.name} at {self.paid_at}"

def update_supplier_on_payment_delete(sender, instance, **kwargs):
    from sync.tracking import sync_apply_active
    if sync_apply_active():
        return
    instance.supplier.update_balance()

post_delete.connect(update_supplier_on_payment_delete, sender=SupplierPayment)

class Inventory(SyncModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='inventories')
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name='inventory')
    quantity = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.product.name} - Batch {self.shipment.batch_number} (Exp: {self.shipment.expiry_date})"

class LostProduct(SyncModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='lost_products')
    inventory = models.ForeignKey(Inventory, on_delete=models.CASCADE, related_name='lost_products')
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=255, blank=True, null=True)
    lost_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        # During sync apply, just persist the row; stock is recomputed
        # authoritatively on the server (avoids double-deducting).
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().save(*args, **kwargs)
        # Deduct from inventory only on creation
        if not self.pk:
            if self.quantity > self.inventory.quantity:
                raise ValueError("Lost quantity exceeds available inventory.")
            self.inventory.quantity -= self.quantity
            self.inventory.save()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Lost {self.quantity} of {self.product.name} (Batch {self.inventory.shipment.batch_number})"

class Sale(SyncModel):
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True)
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def calculate_total(self):
        total = sum(item.get_total for item in self.items.all())
        # Subtract returned products value
        returned_total = sum(r.value for r in self.returned_products.all())
        self.total = total - returned_total
        self.save()
        # The commission is a share of the total, so it follows every change
        # (creating, editing, and adding or removing a return).
        update_sale_commission(self)
        return self.total

    def delete(self, *args, **kwargs):
        # Its commissions go with it, but what they were paid stays paid out:
        # the employee owes it back (see owe_back_commission). During sync
        # apply the payments arrive already updated; the server does it itself
        # for a deletion pushed to it (see sync.engine), which is how a
        # salesperson laptop's deletion gets it (see computes_commissions).
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().delete(*args, **kwargs)
        with transaction.atomic():
            if computes_commissions():
                owe_back_sale_commissions(self)
            return super().delete(*args, **kwargs)

    def __str__(self):
        return f"Sale #{self.pk}"

class SaleItem(SyncModel):
    sale = models.ForeignKey(Sale, related_name='items', on_delete=models.CASCADE)
    inventory = models.ForeignKey('Inventory', on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    free_goods_discount = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # percent
    price_discount = models.DecimalField(max_digits=5, decimal_places=2, default=0)      # percent

    @property
    def free_units(self):
        # Number of free units based on free_goods_discount
        from math import floor
        return floor(self.quantity * float(self.free_goods_discount) / 100)

    @property
    def discounted_unit_price(self):
        # Price after price_discount
        if self.price_discount > 0:
            return float(self.price) / (1 + float(self.price_discount) / 100)
        return float(self.price)

    @property
    def get_total(self):
        # Total price after price discount, only for paid units (not free)
        return self.discounted_unit_price * self.quantity

    @property
    def total_before_discount(self):
        # Total at the original price, before price discount (paid units only)
        return self.price * self.quantity

    @property
    def total_units(self):
        # Total units delivered (paid + free)
        return self.quantity + self.free_units

    def __str__(self):
        return f"{self.quantity} x {self.inventory.product.name} (Batch {self.inventory.shipment.batch_number})"

class ReturnedProduct(SyncModel):
    sale = models.ForeignKey('Sale', on_delete=models.CASCADE, related_name='returned_products')
    sale_item = models.ForeignKey('SaleItem', on_delete=models.CASCADE, related_name='returns')
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        # During sync apply, persist only; stock/totals are recomputed
        # authoritatively on the server.
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().save(*args, **kwargs)
        # On creation, increase inventory and decrease sale total
        if not self.pk:
            # Increase inventory
            self.sale_item.inventory.quantity += self.quantity
            self.sale_item.inventory.save()
        super().save(*args, **kwargs)
        # Recalculate sale total
        self.sale.calculate_total()

    def delete(self, *args, **kwargs):
        # Mirror save(): during sync apply just remove the row. Stock and the
        # sale total are recomputed authoritatively on the server and pulled
        # down; adjusting them here would corrupt those synced values (on a
        # laptop pull there is no recompute to correct it afterwards).
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().delete(*args, **kwargs)
        # On delete, decrease inventory and restore sale total
        self.sale_item.inventory.quantity -= self.quantity
        self.sale_item.inventory.save()
        super().delete(*args, **kwargs)
        self.sale.calculate_total()

    @property
    def value(self):
        # Value of returned items (use discounted price)
        return self.quantity * self.sale_item.discounted_unit_price

    def __str__(self):
        return f"Returned {self.quantity} of {self.sale_item.inventory.product.name} (Sale #{self.sale.pk})"

class Invoice(SyncModel):
    sale = models.OneToOneField(Sale, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    file_path = models.CharField(max_length=255, blank=True, null=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    STATUS_CHOICES = [
        ('paid', 'Paid'),
        ('unpaid', 'Unpaid'),
        ('partial', 'Partial'),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unpaid')
    number = models.CharField(max_length=7, unique=True, blank=True, null=True)  # <-- new field

    def save(self, *args, **kwargs):
        if not self.number:
            # A 7-digit number: THIS node's SYNC_NODE_NUMBER as the leading digit
            # followed by 6 random digits, giving each node a disjoint pool of
            # 1,000,000 numbers so invoices minted on different laptops can never
            # collide on the globally-unique `number` when they sync.
            from django.conf import settings
            prefix = str(int(getattr(settings, 'SYNC_NODE_NUMBER', 0)) % 10)
            while True:
                num = f"{prefix}{random.randint(0, 999999):06d}"
                if not Invoice.objects.filter(number=num).exists():
                    self.number = num
                    break
        super().save(*args, **kwargs)

    def computed_status(self):
        """'paid', 'partial' or 'unpaid', from its payments against the sale's (net) total."""
        net_total = Decimal(str(self.sale.total))
        paid = self.paid_amount
        if paid >= net_total and net_total > 0:
            return 'paid'
        if paid > 0:
            return 'partial'
        return 'unpaid'

    def update_status(self):
        self.status = self.computed_status()
        self.save(update_fields=['status'])

    @property
    def paid_amount(self):
        from decimal import Decimal
        return self.payments.aggregate(total=models.Sum('amount'))['total'] or Decimal('0')

    @property
    def remaining_amount(self):
        from decimal import Decimal
        return self.sale.total - self.paid_amount

    def __str__(self):
        return f"Invoice #{self.number or self.pk} for Sale #{self.sale.pk}"

class InvoicePayment(SyncModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        # During sync apply, persist only; the invoice's status is carried by the
        # pushed Invoice row (and recomputed authoritatively), so running the
        # side-effecting update_status here would double-write and, on a server
        # whose stdout is closed, its debug output used to crash the whole push.
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().save(*args, **kwargs)
        super().save(*args, **kwargs)
        self.invoice.update_status()

    def __str__(self):
        return f"Payment {self.amount} for Invoice #{self.invoice.pk}"

def update_invoice_on_payment_delete(sender, instance, **kwargs):
    # A payment removed through sync apply must not re-derive status locally —
    # the Invoice row propagates its own status (same contract as save()).
    from sync.tracking import sync_apply_active
    if sync_apply_active():
        return
    invoice = instance.invoice
    invoice.update_status()

post_delete.connect(update_invoice_on_payment_delete, sender=InvoicePayment)

class Expense(SyncModel):
    # CATEGORY_CHOICES = [
    #     ('rent', 'إيجار'),
    #     ('salary', 'رواتب'),
    #     ('utility', 'خدمات'),
    #     ('other', 'أخرى'),
    # ]
    description = models.CharField(max_length=255)
    # category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='other')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)

    def get_category_display(self):
        return dict(self.CATEGORY_CHOICES).get(self.category, self.category)

    def __str__(self):
        return f"Expense: {self.description} ({self.amount})"

class Commission(SyncModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # total commission for this sale
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # new: how much has been paid
    created_at = models.DateTimeField(default=timezone.now)
    # paid = models.BooleanField(default=False)  # remove this, use paid_amount instead

    class Meta:
        unique_together = ('employee', 'sale')

    @property
    def unpaid_amount(self):
        return max(self.amount - self.paid_amount, 0)

    @property
    def is_paid(self):
        return self.unpaid_amount == 0

    def __str__(self):
        return f"Commission for {self.employee.name} on Sale #{self.sale.pk}"

CENT = Decimal('0.01')

def _cents(value):
    return Decimal(str(value or 0)).quantize(CENT)

def commission_for(total, percentage):
    """``percentage`` % of ``total``, rounded like ``Commission.amount``."""
    return _cents(Decimal(str(total or 0)) * Decimal(str(percentage or 0)) / 100)

def computes_commissions():
    """A salesperson laptop leaves commissions to the server: it shows the
    ones it pulls, but works none out itself. Commission rows aren't pushed
    from it, so the server's own figures would differ from the laptop's
    anyway; and a laptop that hasn't synced since commissions were opened to
    reps holds no commissions or percentages to work from."""
    return getattr(settings, 'SYNC_ROLE', 'standalone') != 'salesperson'

def update_sale_commission(sale):
    """Create or recompute ``sale``'s commission at its employee's current percentage.

    Runs whenever a sale's total is calculated and, on the server, for every
    sale pushed up from a laptop (see sync.engine) — a salesperson's sales
    arrive without one.
    """
    if not computes_commissions():
        return
    # A sale moved to another employee (or to none) no longer earns the old
    # one a commission; only what was already paid to them stays theirs. The
    # row is shrunk rather than deleted so the change syncs like any edit.
    for stale in Commission.objects.filter(sale=sale).exclude(employee_id=sale.employee_id):
        if stale.amount != stale.paid_amount:
            stale.amount = stale.paid_amount
            stale.save()
    if not sale.employee_id:
        return
    employee = sale.employee
    amount = commission_for(sale.total, employee.commission_percentage)
    commission = Commission.objects.filter(employee=employee, sale=sale).first()
    if commission is None:
        Commission.objects.create(employee=employee, sale=sale, amount=amount,
                                  created_at=sale.created_at)
    elif commission.amount != amount:
        _settle_paid_amount(commission, amount)
        commission.amount = amount
        commission.save()

class CommissionPayment(SyncModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='commission_payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)
    # The commission period (month/year) this payment is recorded against.
    # Kept separate from ``paid_at`` so a payment for a past month is reported
    # against that month regardless of when it was actually entered.
    period_month = models.PositiveSmallIntegerField(null=True, blank=True)
    period_year = models.PositiveSmallIntegerField(null=True, blank=True)
    # Optionally, link to commissions paid in this payment (for audit)
    commissions = models.ManyToManyField(Commission, blank=True, related_name='payments')
    # How much of the payment went to each commission, keyed by that
    # commission's sale sync_id: the same on every node, unlike the
    # commission's own, which sync may replace. It is what lets a payment be
    # edited or deleted exactly. Empty for one recorded before splits were
    # kept (see _release).
    allocations = models.JSONField(default=dict, blank=True)
    # What of it paid commissions since cancelled, keyed the same way: all it
    # paid toward one whose sale was deleted, or the part of one a return took
    # below what it was paid. The employee keeps that money with no commission
    # behind it, so it is owed back: it comes off the unpaid commission of the
    # payment's period (see owe_back_commission).
    cancelled = models.JSONField(default=dict, blank=True)

    @property
    def owed_back(self):
        """What the employee owes back of this payment."""
        return sum((Decimal(v) for v in self.cancelled.values()), Decimal('0'))

    def save(self, *args, **kwargs):
        # During sync apply, persist only. Re-running the distribution would
        # double-pay commissions; the Commission rows are synced separately.
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().save(*args, **kwargs)
        with transaction.atomic():
            previous = None
            if not self._state.adding:
                previous = CommissionPayment.objects.filter(pk=self.pk).first()
            # A new payment is distributed; an edited one again only when what
            # it pays changed (editing just the note leaves its split alone).
            redistribute = previous is None or (
                (previous.employee_id, previous.amount, previous.period_month, previous.period_year)
                != (self.employee_id, Decimal(str(self.amount)), self.period_month, self.period_year))
            paid = None
            if redistribute:
                if previous is not None:
                    previous._release()
                paid = self._allocate()
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = {*kwargs['update_fields'], 'allocations',
                                               'cancelled', 'sync_updated_at'}
            super().save(*args, **kwargs)
            if paid is not None:
                self.commissions.set(paid)

    def delete(self, *args, **kwargs):
        # Mirror save(): during sync apply just remove the row; the Commission
        # rows it paid arrive through sync already released.
        from sync.tracking import record_server_deletion, sync_apply_active
        if sync_apply_active():
            return super().delete(*args, **kwargs)
        with transaction.atomic():
            stored = CommissionPayment.objects.filter(pk=self.pk).first()
            if stored is not None:
                stored._release()
            result = super().delete(*args, **kwargs)
            record_server_deletion(self)
        return result

    def _allocate(self):
        """Pay ``amount`` into the employee's unpaid commissions, oldest first
        but the payment's own period before any other: paying a month's
        commission must clear that month, not an older one. Records the split
        in ``allocations`` and returns the commissions paid."""
        commissions = list(Commission.objects.filter(employee_id=self.employee_id)
                           .select_related('sale').order_by('created_at', 'pk'))
        if self.period_month and self.period_year:
            in_period = set(Commission.objects.filter(
                employee_id=self.employee_id, sale__created_at__year=self.period_year,
                sale__created_at__month=self.period_month,
            ).values_list('pk', flat=True))
            commissions.sort(key=lambda c: c.pk not in in_period)  # stable
        remaining = Decimal(str(self.amount))
        allocations, paid = {}, []
        for commission in commissions:
            if remaining <= 0:
                break
            pay = min(commission.unpaid_amount, remaining)
            if pay <= 0:
                continue
            commission.paid_amount += pay
            # sync_updated_at too, or a payment made on the server never
            # reaches the laptops (they pull rows by that timestamp).
            commission.save(update_fields=['paid_amount', 'sync_updated_at'])
            allocations[str(commission.sale.sync_id)] = str(_cents(pay))
            paid.append(commission)
            remaining -= pay
        self.allocations = allocations
        # Distributed afresh, it pays only commissions that still exist.
        self.cancelled = {}
        return paid

    def _release(self):
        """Take this payment back out of the commissions it paid, and so out
        of what is owed back of it."""
        def take_back(commission, amount):
            amount = min(amount, commission.paid_amount)
            if amount > 0:
                commission.paid_amount -= amount
                commission.save(update_fields=['paid_amount', 'sync_updated_at'])
            return amount

        cancelled, owed_back = self.cancelled, self.owed_back
        if cancelled:
            CommissionPayment.objects.filter(pk=self.pk).update(cancelled={})
            self.cancelled = {}
        if self.allocations:
            commissions = {str(c.sale.sync_id): c for c in Commission.objects.filter(
                employee_id=self.employee_id, sale__sync_id__in=list(self.allocations),
            ).select_related('sale')}
            for sale_sync_id, amount in self.allocations.items():
                commission = commissions.get(sale_sync_id)
                if commission is not None:  # else its sale is gone, and it with it
                    # Not what of it is owed back: that no longer pays it.
                    take_back(commission, Decimal(amount) - Decimal(cancelled.get(sale_sync_id, '0')))
            return
        # Recorded before splits were kept: take it back from the commissions
        # it is linked to, newest first, then from the newest others. Not what
        # is owed back of it, though: that no longer pays any.
        linked = list(self.commissions.order_by('-created_at', '-pk'))
        others = (Commission.objects.filter(employee_id=self.employee_id)
                  .exclude(pk__in=[c.pk for c in linked]).order_by('-created_at', '-pk'))
        remaining = Decimal(str(self.amount)) - owed_back
        for commission in [*linked, *others]:
            if remaining <= 0:
                break
            remaining -= take_back(commission, remaining)

    def __str__(self):
        return f"Commission Payment {self.amount} to {self.employee.name} at {self.paid_at}"

def commission_owed_back(commission, amount=None):
    """What the employee would owe back of each payment were ``amount`` of what
    ``commission`` was paid cancelled (all of it when None, as when its sale is
    deleted), as (payment, share) pairs: newest payment first, each for what it
    still pays toward the commission."""
    key = str(commission.sale.sync_id)
    payments = list(CommissionPayment.objects.filter(employee_id=commission.employee_id)
                    .order_by('-paid_at', '-pk'))
    owed, left = [], amount

    def owe(payment, available):
        nonlocal left
        share = available if left is None else min(available, left)
        if share <= 0:
            return Decimal('0')
        owed.append((payment, share))
        if left is not None:
            left -= share
        return share

    # One that recorded its split still pays its share, less what of it is
    # owed back already.
    recorded = Decimal('0')
    for payment in payments:
        if key in payment.allocations:
            live = max(Decimal(payment.allocations[key])
                       - Decimal(payment.cancelled.get(key, '0')), Decimal('0'))
            recorded += live
            owe(payment, live)
    # One recorded before splits were kept is linked to the commissions it paid
    # instead: what of the paid amount no split accounts for came from those.
    # Paid by no recorded payment at all, it can't be owed back.
    unrecorded = commission.paid_amount - recorded
    linked = set(commission.payments.values_list('pk', flat=True))
    for payment in payments:
        if unrecorded <= 0 or (left is not None and left <= 0):
            break
        if payment.pk in linked and not payment.allocations:
            unrecorded -= owe(payment, min(unrecorded, payment.amount - payment.owed_back))
    return owed

def _add_owed_back(payment, key, amount):
    """Add ``amount`` (take it off, when negative) to what ``payment`` owes back
    of the commission on the sale ``key``."""
    owed = Decimal(payment.cancelled.get(key, '0')) + amount
    cancelled = {k: v for k, v in payment.cancelled.items() if k != key}
    if owed > 0:
        cancelled[key] = str(_cents(owed))
    payment.cancelled = cancelled
    # sync_updated_at too, so the laptops pull it (see _allocate).
    payment.save(update_fields=['cancelled', 'sync_updated_at'])

def owe_back_commission(commission, amount=None):
    """Cancel ``amount`` of what ``commission`` was paid (all of it when None,
    before it is deleted with its sale): the money stays paid out, so the
    employee owes it back. Each payment records its share in ``cancelled``,
    which takes it off the unpaid commission of the payment's period, below
    zero when nothing there is unpaid. Returns how much was cancelled."""
    key = str(commission.sale.sync_id)
    owed = commission_owed_back(commission, amount)
    for payment, share in owed:
        _add_owed_back(payment, key, share)
    return sum((share for _, share in owed), Decimal('0'))

def _restore_owed_back(commission, amount):
    """Undo up to ``amount`` of what was cancelled of ``commission``'s paid
    amount, oldest payment first (the reverse of how it was cancelled), so it
    pays the commission again. Returns how much."""
    key = str(commission.sale.sync_id)
    left = amount
    for payment in (CommissionPayment.objects.filter(employee_id=commission.employee_id)
                    .order_by('paid_at', 'pk')):
        if left <= 0:
            break
        share = min(Decimal(payment.cancelled.get(key, '0')), left)
        if share > 0:
            _add_owed_back(payment, key, -share)
            left -= share
    return amount - left

def _settle_paid_amount(commission, amount):
    """Before ``commission`` changes to ``amount`` (a return on its sale, say):
    what it was paid beyond a lower amount is owed back, and a higher one
    first takes back what was owed back of it. Only the change counts: one
    paid beyond its amount before (by an older return) stays so."""
    old, paid = commission.amount, commission.paid_amount
    if amount < old:
        beyond = min(paid, old) - amount
        if beyond > 0:
            commission.paid_amount -= owe_back_commission(commission, beyond)
    elif amount > old:
        commission.paid_amount += _restore_owed_back(commission, amount - old)

def owe_back_sale_commissions(sale):
    """``owe_back_commission`` for each paid commission of ``sale``: any
    employee's, as a sale moved to another one keeps the old one's paid part."""
    for commission in Commission.objects.filter(sale=sale, paid_amount__gt=0).select_related('sale'):
        owe_back_commission(commission)

@receiver(pre_save, sender=Employee)
def note_saved_commission_percentage(sender, instance, **kwargs):
    """Note the percentage this save replaces, for update_employee_commissions."""
    from sync.tracking import sync_apply_active
    if sync_apply_active() or not instance.pk:
        instance._replaced_percentage = None
        return
    instance._replaced_percentage = (Employee.objects.filter(pk=instance.pk)
                                     .values_list('commission_percentage', flat=True).first())

@receiver(post_save, sender=Employee)
def update_employee_commissions(sender, instance, **kwargs):
    """
    Recalculate only the unpaid portion of commissions for this employee whenever their commission_percentage changes.
    Paid portions remain unchanged and are not recalculated.
    """
    # Applying a synced Employee row must not recompute commissions locally —
    # Commission rows arrive through sync with their authoritative amounts
    # (the server catches up after a push, see sync.engine).
    from sync.tracking import sync_apply_active
    if sync_apply_active():
        return
    # Every save fills in missing commissions and recomputes unpaid ones, but
    # only a new percentage re-rates the unpaid part of a partly paid one: it
    # works out the rate the commission was at from its amount, which an
    # earlier re-rating left apart from any percentage, so saving the employee
    # unchanged (a new name) would re-rate it again.
    replaced = getattr(instance, '_replaced_percentage', None)
    instance.recalculate_commissions(rerate_partly_paid=(
        replaced is not None
        and Decimal(str(instance.commission_percentage)) != replaced))

class Manager(SyncModel):
    name = models.CharField(max_length=100, blank = False, null = False)
    employees = models.ManyToManyField(Employee, related_name='managers', blank = False, null = False)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # %


    def __str__(self):
        return self.name

class ManagerCommissionPayment(SyncModel):
    manager = models.ForeignKey('Manager', on_delete=models.CASCADE, related_name='commission_payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)
    # The commission period (month/year) this payment is recorded against.
    period_month = models.PositiveSmallIntegerField(null=True, blank=True)
    period_year = models.PositiveSmallIntegerField(null=True, blank=True)

    def delete(self, *args, **kwargs):
        from sync.tracking import record_server_deletion
        result = super().delete(*args, **kwargs)
        record_server_deletion(self)
        return result

    def __str__(self):
        return f"Commission Payment {self.amount} to Manager {self.manager.name} at {self.paid_at}"