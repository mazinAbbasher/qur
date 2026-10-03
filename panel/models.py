from django.conf import settings
from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.db.models.signals import post_save, post_delete
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
        if month and year:
            sales = sales.filter(created_at__year=year, created_at__month=month)
        commissions = Commission.objects.filter(employee=self, sale__in=sales)
        return sum([c.unpaid_amount for c in commissions])

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

    def update_status(self):
        from decimal import Decimal
        net_total = Decimal(str(self.sale.total))
        paid = self.paid_amount
        if paid >= net_total and net_total > 0:
            self.status = 'paid'
        elif paid > 0:
            self.status = 'partial'
        else:
            self.status = 'unpaid'
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
    """A salesperson laptop never holds commission percentages (they're
    stripped when syncing), so it leaves commissions to the server."""
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

    def save(self, *args, **kwargs):
        # During sync apply, persist only. Re-running FIFO distribution would
        # double-pay commissions; the Commission rows are synced separately.
        from sync.tracking import sync_apply_active
        if sync_apply_active():
            return super().save(*args, **kwargs)
        super().save(*args, **kwargs)
        # Distribute payment to unpaid commissions (FIFO)
        commissions = Commission.objects.filter(employee=self.employee).order_by('created_at')
        remaining = float(self.amount)
        for commission in commissions:
            unpaid = float(commission.unpaid_amount)
            if unpaid <= 0:
                continue
            pay = min(unpaid, remaining)
            commission.paid_amount = float(commission.paid_amount) + pay
            commission.save(update_fields=['paid_amount'])
            self.commissions.add(commission)
            remaining -= pay
            if remaining <= 0:
                break

    def __str__(self):
        return f"Commission Payment {self.amount} to {self.employee.name} at {self.paid_at}"

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
    instance.recalculate_commissions()

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

    def __str__(self):
        return f"Commission Payment {self.amount} to Manager {self.manager.name} at {self.paid_at}"