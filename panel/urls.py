from django.urls import path
from . import views
app_name = 'panel'
urlpatterns = [
    path('', views.index, name='index'),

    # Product URLs
    path('products/', views.product_list, name='product_list'),
    path('products/add/', views.product_add, name='product_add'),
    path('products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('products/<int:pk>/delete/', views.product_delete, name='product_delete'),
    path('products/<int:pk>/', views.product_detail, name='product_detail'),

    # Sales URLs
    path('sales/', views.sale_list, name='sale_list'),
    path('sales/add/', views.sale_create, name='sale_create'),
    path('sales/<int:pk>/', views.sale_detail, name='sale_detail'),
    path('sales/<int:pk>/return_product/', views.sale_return_product, name='sale_return_product'),
    path('sales/export/pdf/', views.sale_list_pdf, name='sale_list_pdf'),

    # Shipment URLs
    path('shipments/', views.shipment_list, name='shipment_list'),
    path('shipments/add/', views.shipment_create, name='shipment_create'),
    path('shipments/<int:pk>/edit/', views.shipment_edit, name='shipment_edit'),
    path('shipments/<int:pk>/delete/', views.shipment_delete, name='shipment_delete'),
    path('shipments/profit/', views.shipment_profit_report, name='shipment_profit_report'),
    path('shipments/export/pdf/', views.shipment_list_pdf, name='shipment_list_pdf'),

    # Employee URLs
    path('employees/', views.employee_list, name='employee_list'),
    path('employees/add/', views.employee_add, name='employee_add'),
    path('employees/<int:pk>/edit/', views.employee_edit, name='employee_edit'),
    path('employees/<int:pk>/delete/', views.employee_delete, name='employee_delete'),
    path('employees/<int:pk>/detail/', views.employee_detail, name='employee_detail'),

    # Client URLs
    path('clients/', views.client_list, name='client_list'),
    path('clients/add/', views.client_add, name='client_add'),
    path('clients/<int:pk>/edit/', views.client_edit, name='client_edit'),
    path('clients/<int:pk>/delete/', views.client_delete, name='client_delete'),
    path('clients/<int:pk>/detail/', views.client_detail, name='client_detail'),
    path('clients/export/pdf/', views.client_list_pdf, name='client_list_pdf'),

    # Area URLs
    path('areas/', views.area_list, name='area_list'),
    path('areas/add/', views.area_add, name='area_add'),
    path('areas/<int:pk>/edit/', views.area_edit, name='area_edit'),
    path('areas/<int:pk>/delete/', views.area_delete, name='area_delete'),
    path('areas/export/pdf/', views.area_list_pdf, name='area_list_pdf'),

    # Commissions
    path('commissions/', views.sale_commissions, name='sale_commissions'),
    path('ajax/get-employee-commission/', views.get_employee_commission, name='get_employee_commission'),
    path('employee/<int:employee_id>/commission_pay/', views.commission_pay, name='commission_pay'),

    # Reports
    path('reports/area-sales/', views.area_sales_report, name='area_sales_report'),
    path('clients/by-area/', views.clients_by_area, name='clients_by_area'),

    # Net Profit Dashboard
    path('net-profit/', views.net_profit_dashboard, name='net_profit_dashboard'),

    # Invoice URLs

    # Expense URLs
    path('expenses/', views.expense_list, name='expense_list'),
    path('expenses/add/', views.expense_add, name='expense_add'),
    path('expenses/<int:pk>/edit/', views.expense_edit, name='expense_edit'),
    path('expenses/<int:pk>/delete/', views.expense_delete, name='expense_delete'),
    path('expenses/export/pdf/', views.expense_list_pdf, name='expense_list_pdf'),

    # Inventory URLs
    path('inventory/', views.inventory_list, name='inventory_list'),
    # Removed add/edit/delete inventory URLs
    path('inventory/export/pdf/', views.inventory_list_pdf, name='inventory_list_pdf'),

    path('lost-products/', views.lost_product_list, name='lost_product_list'),
    path('lost-products/add/', views.lost_product_add, name='lost_product_add'),

    path('invoices/', views.invoice_list, name='invoice_list'),

    #  path('', views.invoice_list, name='invoice_list'),
    path('invoices/<int:pk>/', views.invoice_detail, name='invoice_detail'),
    path('invoices/<int:pk>/mark_paid/', views.invoice_mark_paid, name='invoice_mark_paid'),
    path('invoices/<int:pk>/mark_unpaid/', views.invoice_mark_unpaid, name='invoice_mark_unpaid'),
    path('invoices/<int:pk>/pdf/', views.invoice_pdf, name='invoice_pdf'),
    path('invoices/<int:pk>/add_payment/', views.invoice_add_payment, name='invoice_add_payment'),

    path('debts/', views.debts_view, name='debts'),

    # Supplier URLs
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('suppliers/add/', views.supplier_add, name='supplier_add'),
    path('suppliers/<int:pk>/edit/', views.supplier_edit, name='supplier_edit'),
    path('suppliers/<int:pk>/', views.supplier_detail, name='supplier_detail'),
    path('suppliers/<int:pk>/delete/', views.supplier_delete, name='supplier_delete'),
    path('suppliers/<int:pk>/add_payment/', views.supplier_add_payment, name='supplier_add_payment'),
    path('suppliers/export/pdf/', views.supplier_list_pdf, name='supplier_list_pdf'),
]
