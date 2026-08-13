import csv
import io

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path

from .models import (
    Product, Shipment, Client, Area, Employee, Sale, SaleItem,
    Invoice, Expense, Commission, ExchangeRate
)


class ClientCSVImportForm(forms.Form):
    csv_file = forms.FileField(
        label='ملف CSV',
        help_text='الأعمدة: name, phone, address, area — الاسم مطلوب فقط. '
                  'المناطق تُنشأ تلقائياً بالاسم إن لم تكن موجودة.',
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
    # Adds an "Import CSV" button to the client list page.
    change_list_template = 'admin/panel/client/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('import-csv/', self.admin_site.admin_view(self.import_csv),
                 name='panel_client_import_csv'),
        ]
        return custom + urls

    def import_csv(self, request):
        """Bulk-create clients from an uploaded CSV.

        Idempotent: a client is matched by (name, area) so re-uploading the same
        file adds nothing new. Areas are created on demand by name, so importing
        into a freshly-zeroed database rebuilds both clients and their areas.
        """
        if request.method == 'POST':
            form = ClientCSVImportForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    created, skipped, areas_made = self._do_import(request.FILES['csv_file'])
                except Exception as exc:  # malformed file, bad encoding, …
                    self.message_user(request, f'تعذّر قراءة الملف: {exc}', level=messages.ERROR)
                    return redirect('..')
                self.message_user(
                    request,
                    f'تم استيراد {created} عميل جديد، وتخطّي {skipped} موجود مسبقاً، '
                    f'وإنشاء {areas_made} منطقة جديدة.',
                    level=messages.SUCCESS,
                )
                return redirect('..')
        else:
            form = ClientCSVImportForm()

        context = {
            **self.admin_site.each_context(request),
            'title': 'استيراد العملاء من CSV',
            'form': form,
            'opts': self.model._meta,
        }
        return render(request, 'admin/panel/client/import_csv.html', context)

    @staticmethod
    def _do_import(file_obj):
        # utf-8-sig strips the BOM Excel adds, so Arabic headers/values decode.
        text = io.TextIOWrapper(file_obj.file, encoding='utf-8-sig', newline='')
        reader = csv.DictReader(text)
        # Normalise headers ("Name" -> "name") so the file is forgiving.
        reader.fieldnames = [(h or '').strip().lower() for h in (reader.fieldnames or [])]
        if 'name' not in reader.fieldnames:
            raise ValueError("العمود 'name' مفقود في الملف.")

        created = skipped = 0
        area_cache = {}
        areas_made = 0
        for row in reader:
            name = (row.get('name') or '').strip()
            if not name:
                continue
            area_name = (row.get('area') or '').strip()
            area = None
            if area_name:
                area = area_cache.get(area_name)
                if area is None:
                    area, made = Area.objects.get_or_create(name=area_name)
                    area_cache[area_name] = area
                    areas_made += 1 if made else 0
            _, was_created = Client.objects.get_or_create(
                name=name, area=area,
                defaults={
                    'phone': (row.get('phone') or '').strip() or None,
                    'address': (row.get('address') or '').strip() or None,
                },
            )
            created += 1 if was_created else 0
            skipped += 0 if was_created else 1
        return created, skipped, areas_made

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
