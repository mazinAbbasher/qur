from django.urls import path
from . import views

urlpatterns = [
    # Financial Dashboard and Company Balances
    path('dashboard/', views.financial_dashboard, name='financial_dashboard'),
    path('balances/', views.company_balances, name='company_balances'),

    # Partner Management
    path('partners/', views.partners_list, name='partners_list'),
    path('partners/add/', views.partner_add, name='partner_add'),
    path('partners/<int:partner_id>/edit/', views.partner_edit, name='partner_edit'),
    path('partners/<int:partner_id>/delete/', views.partner_delete, name='partner_delete'),
    path('partners/<int:partner_id>/transactions/', views.partner_transactions, name='partner_transactions'),
    path('partners/<int:partner_id>/transactions/add/', views.transaction_add, name='transaction_add'),
    path('transactions/<int:tx_id>/edit/', views.transaction_edit, name='transaction_edit'),
    path('transactions/<int:tx_id>/delete/', views.transaction_delete, name='transaction_delete'),

    # Currency Purchases
    path('currency-purchases/', views.currency_purchases_list, name='currency_purchases_list'),
    path('currency-purchases/add/', views.currency_purchase_add, name='currency_purchase_add'),
    path('currency-purchases/<int:purchase_id>/edit/', views.currency_purchase_edit, name='currency_purchase_edit'),
    path('currency-purchases/<int:purchase_id>/delete/', views.currency_purchase_delete, name='currency_purchase_delete'),
]
