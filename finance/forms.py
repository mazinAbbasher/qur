from django import forms
from .models import Partner, PartnerTransaction, CurrencyPurchase

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
        model = CurrencyPurchase
        fields = ['currency', 'amount', 'exchange_rate', 'date', 'note']
        widgets = {
            'currency': forms.Select(attrs={'class': 'form-select'}),
            'date': forms.DateInput(attrs={'type': 'date'}),
        }
