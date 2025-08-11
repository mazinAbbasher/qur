from django import forms
from .models import Partner, PartnerTransaction, CurrencyExchange

class PartnerForm(forms.ModelForm):
    class Meta:
        model = Partner
        fields = ['full_name']
        widgets = {
            'currency': forms.Select(attrs={'class': 'form-select'}),
        }

class PartnerTransactionForm(forms.ModelForm):
    class Meta:
        model = PartnerTransaction
        fields = ['transaction_type','currency', 'amount', 'date', 'note']

class CurrencyPurchaseForm(forms.ModelForm):
    class Meta:
        model = CurrencyExchange
        fields = ['bought_currency', 'bought_amount','sold_currency',  'sold_amount', 'date', 'note']
        widgets = {
            'currency': forms.Select(attrs={'class': 'form-select'}),
            'date': forms.DateInput(attrs={'type': 'date'}),
        }
        labels = {
            'sold_currency': 'العملة_المصدرة',
            'bought_currency': 'العملة_الواردة',
            'sold_amount': 'المبلغ_المصدر بالكامل',
            'bought_amount': 'المبلغ_الوارد بالكامل',
            'date': 'التاريخ',
            'note': 'ملاحظة',
        }
