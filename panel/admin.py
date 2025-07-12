from django.contrib import admin
from .models import (
    Product, Shipment, Client, Area, Employee, Sale, SaleItem,
    Invoice, Expense, Commission, ExchangeRate
)

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity', 'shipment_cost', 'received_at')
    search_fields = ('product__name',)
    list_filter = ('received_at',)

@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'address', 'area')
    search_fields = ('name', 'phone')
    list_filter = ('area',)

@admin.register(Area)
class AreaAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'employee', 'created_at', 'total')
    search_fields = ('client__name', 'employee__name')
    list_filter = ('created_at', 'employee')
    inlines = [SaleItemInline]

@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ('sale', 'quantity', 'price')
    search_fields = ('sale__id', 'product__name')

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'sale', 'created_at', 'file_path')
    search_fields = ('sale__id',)

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('description', 'amount', 'date')
    search_fields = ('description',)
    list_filter = ('date',)

@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = ('employee', 'sale', 'amount', 'created_at')
    search_fields = ('employee__name', 'sale__id')
    list_filter = ('created_at',)

@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ('rate', 'updated_at')
    list_filter = ('updated_at',)
