from django.db import models, transaction
from django.utils import timezone
from decimal import Decimal
from django.contrib import admin
from django.core.exceptions import ValidationError
 
from django.db.models.functions import Coalesce
from django.db.models import Sum, Q, F, ExpressionWrapper, DecimalField


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

class CurrencyExchange(models.Model):
    sold_currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name='exchanges_as_sold')
    bought_currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name='exchanges_as_bought')
    sold_amount = models.DecimalField(max_digits=16, decimal_places=2)
    bought_amount = models.DecimalField(max_digits=16, decimal_places=2)
    exchange_rate = models.DecimalField(max_digits=16, decimal_places=4)  # Optional: bought / sold
    date = models.DateField(default=timezone.now)
    note = models.CharField(max_length=255, blank=True, null=True)

    def clean(self):
        if self.sold_amount <= 0 or self.bought_amount <= 0:
            raise ValidationError("Amounts must be positive.")
        if self.sold_currency == self.bought_currency:
            raise ValidationError("Currencies must be different.")
    
    # def save(self, *args, **kwargs):
    #     # if not self.exchange_rate:
    #     self.exchange_rate = self.bought_amount / self.sold_amount
    #     super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sold_amount} {self.sold_currency.code} → {self.bought_amount} {self.bought_currency.code} @ {self.exchange_rate:.4f}"

class Partner(models.Model):
    """
    Represents a company partner/funder.
    """
    full_name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.full_name}"

    # def get_balance(self, currency):
    #     pb = self.balances.filter(currency=currency).first()
    #     return pb.balance if pb else Decimal('0')

       
    def get_all_balances(self):
        """
        Returns the partner's balances in all currencies, including SDG equivalent.
        """
        balances = self.transactions.values('currency__code', 'currency__name').annotate(
            total_deposit=Coalesce(Sum('amount', filter=Q(transaction_type='deposit')), Decimal('0')),
            total_withdrawal=Coalesce(Sum('amount', filter=Q(transaction_type='withdrawal')), Decimal('0')),
        ).annotate(
            balance=ExpressionWrapper(
                F('total_deposit') - F('total_withdrawal'),
                output_field=DecimalField(max_digits=16, decimal_places=2)
            )
        ).order_by('currency__code')

        # Add SDG equivalent
        for b in balances:
            code = b['currency__code']
            b['sdg_equivalent'] = Decimal(convert_to_sdg(b['balance'], code))

        return balances


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
  

    # def save(self, *args, **kwargs):
    #     # Withdrawal reliability: check balance at save time
    #     if self.transaction_type == 'withdrawal':
    #         pb = PartnerBalance.objects.filter(partner=self.partner, currency=self.currency).first()
    #         current_balance = pb.balance if pb else Decimal('0')
    #         if self.amount > current_balance:
    #             raise ValidationError(f"Withdrawal amount ({self.amount}) exceeds available balance ({current_balance}) for {self.currency.code}.")
    #     super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction_type.title()} {self.amount} {self.currency.code} for {self.partner.full_name}"

def get_latest_exchange_rate(to_currency):
    latest = CurrencyExchange.objects.filter(
        sold_currency__code="SDG",
        bought_currency__code=to_currency
    ).order_by('-date').first()

    if latest:
        return float(latest.exchange_rate)

    # fallback rates
    if to_currency == 'USD':
        return 2550
    elif to_currency == 'AED':
        return 700
    return 1


def convert_to_sdg(amount, currency_code):
    rate = get_latest_exchange_rate(currency_code)
    try:
        return float(amount) * rate if amount is not None else 0
    except Exception:
        return 0
