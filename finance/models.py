from django.db import models, transaction
from django.utils import timezone
from decimal import Decimal
from django.contrib import admin
from django.core.exceptions import ValidationError

class Currency(models.Model):
    """
    Supported currencies for the company.
    """
    CODE_CHOICES = [
        ('USD', 'US Dollar'),
        ('AED', 'UAE Dirham'),
        ('SDG', 'Sudanese Pound'),
    ]
    code = models.CharField(max_length=3, choices=CODE_CHOICES, unique=True)
    name = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.code} - {self.name}"

@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ['code', 'name']
    search_fields = ['code', 'name']

class FinancialLog(models.Model):
    """
    Logs every financial operation for auditing and accountability.
    """
    OPERATION_CHOICES = [
        ('currency_purchase', 'Currency Purchase'),
        ('partner_deposit', 'Partner Deposit'),
        ('partner_withdrawal', 'Partner Withdrawal'),
        ('balance_adjustment', 'Balance Adjustment'),
    ]
    operation_type = models.CharField(max_length=32, choices=OPERATION_CHOICES)
    related_id = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField()
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.ForeignKey('Currency', on_delete=models.SET_NULL, null=True)
    sdg_equivalent = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    timestamp = models.DateTimeField(auto_now_add=True)
    extra_data = models.JSONField(blank=True, null=True)

    def __str__(self):
        return f"{self.get_operation_type_display()} {self.amount} {self.currency} at {self.timestamp}"

class CurrencyPurchase(models.Model):
    """
    Records a currency purchase operation.
    """
    currency = models.ForeignKey(Currency, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    exchange_rate = models.DecimalField(max_digits=16, decimal_places=4)  # against SDG
    date = models.DateField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def clean(self):
        if self.amount < 0:
            raise ValidationError("Amount cannot be negative.")
        if self.exchange_rate <= 0:
            raise ValidationError("Exchange rate must be positive.")

    def __str__(self):
        return f"Purchase {self.amount} {self.currency.code} @ {self.exchange_rate} SDG"

class CompanyBalance(models.Model):
    """
    Tracks the company's balance per currency.
    """
    currency = models.OneToOneField(Currency, on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.currency.code} Balance: {self.balance}"

    def update_balance(self, amount):
        self.balance = max((self.balance or Decimal('0')) + Decimal(amount), Decimal('0'))
        self.save(update_fields=['balance'])

class Partner(models.Model):
    """
    Represents a company partner/funder.
    """
    full_name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.full_name}"

    def get_balance(self, currency):
        pb = self.balances.filter(currency=currency).first()
        return pb.balance if pb else Decimal('0')

    def get_all_balances(self):
        return {pb.currency.code: pb.balance for pb in self.balances.all()}

class PartnerBalance(models.Model):
    """
    Tracks the partner's balance per currency.
    """
    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name='balances')
    currency = models.ForeignKey(Currency, on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        unique_together = ('partner', 'currency')

    def __str__(self):
        return f"{self.partner.full_name} - {self.currency.code}: {self.balance}"

    def update_balance(self, amount):
        self.balance = max((self.balance or Decimal('0')) + Decimal(amount), Decimal('0'))
        self.save(update_fields=['balance'])

class PartnerTransaction(models.Model):
    """
    Records a deposit or withdrawal for a partner.
    """
    TRANSACTION_TYPE_CHOICES = [
        ('deposit', 'Deposit'),
        ('withdrawal', 'Withdrawal'),
    ]
    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.ForeignKey(Currency, on_delete=models.CASCADE)
    date = models.DateField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def clean(self):
        if self.amount < 0:
            raise ValidationError("Transaction amount cannot be negative.")

    def save(self, *args, **kwargs):
        # Withdrawal reliability: check balance at save time
        if self.transaction_type == 'withdrawal':
            pb = PartnerBalance.objects.filter(partner=self.partner, currency=self.currency).first()
            current_balance = pb.balance if pb else Decimal('0')
            if self.amount > current_balance:
                raise ValidationError(f"Withdrawal amount ({self.amount}) exceeds available balance ({current_balance}) for {self.currency.code}.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction_type.title()} {self.amount} {self.currency.code} for {self.partner.full_name}"

def get_latest_exchange_rate(currency_code):
    """
    Returns the latest exchange rate for a currency from CurrencyPurchase records.
    """
    latest = CurrencyPurchase.objects.filter(currency__code=currency_code).order_by('-date').first()
    if latest and latest.exchange_rate > 0:
        return float(latest.exchange_rate)
    # Fallback rates
    if currency_code == 'USD':
        return 2550
    elif currency_code == 'AED':
        return 700
    return 1

def convert_to_sdg(amount, currency_code):
    rate = get_latest_exchange_rate(currency_code)
    try:
        return float(amount) * rate if amount is not None else 0
    except Exception:
        return 0

def smart_alert(message):
    # Placeholder for real alerting (email, SMS, etc.)
    print(f"[ALERT] {message}")

# Signals to update balances atomically and log every operation
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

@receiver(post_save, sender=CurrencyPurchase)
def update_balances_on_purchase(sender, instance, created, **kwargs):
    with transaction.atomic():
        cb, _ = CompanyBalance.objects.get_or_create(currency=instance.currency)
        cb.update_balance(instance.amount)
        # Deduct SDG equivalent from SDG balance if not SDG
        if instance.currency.code != 'SDG':
            sdg_currency = Currency.objects.get(code='SDG')
            sdg_cb, _ = CompanyBalance.objects.get_or_create(currency=sdg_currency)
            sdg_equiv = Decimal(instance.amount) * Decimal(instance.exchange_rate)
            sdg_cb.update_balance(-sdg_equiv)
        # Log operation
        FinancialLog.objects.create(
            operation_type='currency_purchase',
            related_id=instance.id,
            description=f"Purchased {instance.amount} {instance.currency.code} @ {instance.exchange_rate} SDG",
            amount=instance.amount,
            currency=instance.currency,
            sdg_equivalent=Decimal(instance.amount) * Decimal(instance.exchange_rate),
            extra_data={'note': instance.note}
        )
        smart_alert(f"Currency purchase: {instance.amount} {instance.currency.code} @ {instance.exchange_rate} SDG")

@receiver(post_delete, sender=CurrencyPurchase)
def revert_balances_on_purchase_delete(sender, instance, **kwargs):
    with transaction.atomic():
        cb = CompanyBalance.objects.filter(currency=instance.currency).first()
        if cb:
            cb.update_balance(-instance.amount)
        if instance.currency.code != 'SDG':
            sdg_currency = Currency.objects.get(code='SDG')
            sdg_cb = CompanyBalance.objects.filter(currency=sdg_currency).first()
            if sdg_cb:
                sdg_equiv = Decimal(instance.amount) * Decimal(instance.exchange_rate)
                sdg_cb.update_balance(sdg_equiv)
        FinancialLog.objects.create(
            operation_type='balance_adjustment',
            related_id=instance.id,
            description=f"Reverted purchase of {instance.amount} {instance.currency.code}",
            amount=-instance.amount,
            currency=instance.currency,
            sdg_equivalent=-(Decimal(instance.amount) * Decimal(instance.exchange_rate)),
            extra_data={'note': instance.note}
        )
        smart_alert(f"Currency purchase deleted: {instance.amount} {instance.currency.code}")

@receiver(post_save, sender=PartnerTransaction)
def update_partner_and_account_on_transaction(sender, instance, created, **kwargs):
    if created:
        with transaction.atomic():
            pb, _ = PartnerBalance.objects.get_or_create(partner=instance.partner, currency=instance.currency)
            if instance.transaction_type == 'deposit':
                pb.update_balance(instance.amount)
                FinancialLog.objects.create(
                    operation_type='partner_deposit',
                    related_id=instance.id,
                    description=f"Partner {instance.partner.full_name} deposit {instance.amount} {instance.currency.code}",
                    amount=instance.amount,
                    currency=instance.currency,
                    sdg_equivalent=Decimal(convert_to_sdg(instance.amount, instance.currency.code)),
                    extra_data={'note': instance.note, 'partner_id': instance.partner.id}
                )
                smart_alert(f"Partner deposit: {instance.partner.full_name} {instance.amount} {instance.currency.code}")
            elif instance.transaction_type == 'withdrawal':
                pb.update_balance(-instance.amount)
                FinancialLog.objects.create(
                    operation_type='partner_withdrawal',
                    related_id=instance.id,
                    description=f"Partner {instance.partner.full_name} withdrawal {instance.amount} {instance.currency.code}",
                    amount=-instance.amount,
                    currency=instance.currency,
                    sdg_equivalent=-Decimal(convert_to_sdg(instance.amount, instance.currency.code)),
                    extra_data={'note': instance.note, 'partner_id': instance.partner.id}
                )
                smart_alert(f"Partner withdrawal: {instance.partner.full_name} {instance.amount} {instance.currency.code}")

@receiver(post_delete, sender=PartnerTransaction)
def revert_partner_and_account_on_transaction_delete(sender, instance, **kwargs):
    with transaction.atomic():
        pb, _ = PartnerBalance.objects.get_or_create(partner=instance.partner, currency=instance.currency)
        if instance.transaction_type == 'deposit':
            pb.update_balance(-instance.amount)
            FinancialLog.objects.create(
                operation_type='balance_adjustment',
                related_id=instance.id,
                description=f"Reverted partner deposit {instance.amount} {instance.currency.code}",
                amount=-instance.amount,
                currency=instance.currency,
                sdg_equivalent=-Decimal(convert_to_sdg(instance.amount, instance.currency.code)),
                extra_data={'note': instance.note, 'partner_id': instance.partner.id}
            )
            smart_alert(f"Partner deposit deleted: {instance.partner.full_name} {instance.amount} {instance.currency.code}")
        elif instance.transaction_type == 'withdrawal':
            pb.update_balance(instance.amount)
            FinancialLog.objects.create(
                operation_type='balance_adjustment',
                related_id=instance.id,
                description=f"Reverted partner withdrawal {instance.amount} {instance.currency.code}",
                amount=instance.amount,
                currency=instance.currency,
                sdg_equivalent=Decimal(convert_to_sdg(instance.amount, instance.currency.code)),
                extra_data={'note': instance.note, 'partner_id': instance.partner.id}
            )
            smart_alert(f"Partner withdrawal deleted: {instance.partner.full_name} {instance.amount} {instance.currency.code}")
