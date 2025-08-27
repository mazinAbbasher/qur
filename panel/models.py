from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
import random

USD_TO_SDG_RATE = Decimal('600')  # Example conversion rate

class Area(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

class Client(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    area = models.ForeignKey(Area, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.name

class Employee(models.Model):
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

    def delete(self, *args, **kwargs):
        # Cascade delete commissions
        Commission.objects.filter(employee=self).delete()
        super().delete(*args, **kwargs)

    def clean(self):
        if self.commission_percentage < 0 or self.commission_percentage > 100:
            from django.core.exceptions import ValidationError
            raise ValidationError("نسبة العمولة يجب أن تكون بين 0 و 100.")

class ExchangeRate(models.Model):
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Rate: {self.rate} at {self.updated_at}"

class Product(models.Model):
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

class Supplier(models.Model):
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

class Shipment(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    shipment_cost = models.DecimalField(max_digits=12, decimal_places=2)
    received_at = models.DateTimeField(default=timezone.now)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost_sdg = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    sale_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True)
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

    def __str__(self):
        return f"Shipment of {self.product.name} ({self.quantity})"

class SupplierPayment(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Optionally, update supplier's balance or trigger any hooks
        self.supplier.update_balance()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        self.supplier.update_balance()

    def __str__(self):
        return f"Payment {self.amount} to {self.supplier.name} at {self.paid_at}"

def update_supplier_on_payment_delete(sender, instance, **kwargs):
    supplier = instance.supplier
    supplier.update_balance()

post_delete.connect(update_supplier_on_payment_delete, sender=SupplierPayment)

class Inventory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='inventories')
    shipment = models.OneToOneField(Shipment, on_delete=models.CASCADE, related_name='inventory')
    quantity = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.product.name} - Batch {self.shipment.batch_number} (Exp: {self.shipment.expiry_date})"

class LostProduct(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='lost_products')
    inventory = models.ForeignKey(Inventory, on_delete=models.CASCADE, related_name='lost_products')
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=255, blank=True, null=True)
    lost_at = models.DateTimeField(default=timezone.now)

    def save(self, *args, **kwargs):
        # Deduct from inventory only on creation
        if not self.pk:
            if self.quantity > self.inventory.quantity:
                raise ValueError("Lost quantity exceeds available inventory.")
            self.inventory.quantity -= self.quantity
            self.inventory.save()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Lost {self.quantity} of {self.product.name} (Batch {self.inventory.shipment.batch_number})"

class Sale(models.Model):
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
        return self.total

    def __str__(self):
        return f"Sale #{self.pk}"

class SaleItem(models.Model):
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
    def total_units(self):
        # Total units delivered (paid + free)
        return self.quantity + self.free_units

    def __str__(self):
        return f"{self.quantity} x {self.inventory.product.name} (Batch {self.inventory.shipment.batch_number})"

class ReturnedProduct(models.Model):
    sale = models.ForeignKey('Sale', on_delete=models.CASCADE, related_name='returned_products')
    sale_item = models.ForeignKey('SaleItem', on_delete=models.CASCADE, related_name='returns')
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        # On creation, increase inventory and decrease sale total
        if not self.pk:
            # Increase inventory
            self.sale_item.inventory.quantity += self.quantity
            self.sale_item.inventory.save()
        super().save(*args, **kwargs)
        # Recalculate sale total
        self.sale.calculate_total()

    def delete(self, *args, **kwargs):
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

class Invoice(models.Model):
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
    number = models.CharField(max_length=6, unique=True, blank=True, null=True)  # <-- new field

    def save(self, *args, **kwargs):
        if not self.number:
            # Generate a unique 6-digit number
            while True:
                num = f"{random.randint(100000, 999999)}"
                if not Invoice.objects.filter(number=num).exists():
                    self.number = num
                    break
        super().save(*args, **kwargs)

    def update_status(self):
        # Subtract returned products value from sale total
        from decimal import Decimal
        # returned_total = sum(Decimal(str(r.value)) for r in self.sale.returned_products.all())
        # print(returned_total)
        net_total = Decimal(str(self.sale.total))
        paid = self.paid_amount
        print(paid, net_total)
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

class InvoicePayment(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.invoice.update_status()

    def __str__(self):
        return f"Payment {self.amount} for Invoice #{self.invoice.pk}"

def update_invoice_on_payment_delete(sender, instance, **kwargs):
    invoice = instance.invoice
    invoice.update_status()

post_delete.connect(update_invoice_on_payment_delete, sender=InvoicePayment)

class Expense(models.Model):
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

class Commission(models.Model):
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

class CommissionPayment(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='commission_payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)
    # Optionally, link to commissions paid in this payment (for audit)
    commissions = models.ManyToManyField(Commission, blank=True, related_name='payments')

    def save(self, *args, **kwargs):
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
    from .models import Sale, Commission
    sales = Sale.objects.filter(employee=instance)
    for sale in sales:
        for commission_obj in Commission.objects.filter(employee=instance, sale=sale):
            paid = float(commission_obj.paid_amount)
            print(paid, commission_obj.amount, instance.commission_percentage)
            # Only update unpaid commissions
            if paid < commission_obj.amount:
                if paid == 0:
                    new_total = float(sale.total or 0) * float(instance.commission_percentage or 0) / 100
                    commission_obj.amount = new_total
                    commission_obj.save(update_fields=['amount'])
                else:
                    old_percentage = float(commission_obj.amount) / float(sale.total or 0) * 100 if sale.total else 0
                    new_percentage = float(instance.commission_percentage)
                    exchange = new_percentage / old_percentage if old_percentage else 0
                    # apply new percentage to just the remaining unpaid amount
                    commission_obj.amount = paid + (float(commission_obj.unpaid_amount) * float(exchange))
                    commission_obj.save()

