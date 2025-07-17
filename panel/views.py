from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.http import HttpResponseRedirect, JsonResponse
from django.db.models import Sum, Count, Q
from django.db import models
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.core.paginator import Paginator
from datetime import timedelta  # <-- Add this import
from .models import (
    Expense, Product, Invoice, Sale, SaleItem, Client, Employee,
    ExchangeRate, Area, Shipment, Commission, Inventory, InvoicePayment,
    Supplier, SupplierPayment, LostProduct, ReturnedProduct  # <-- add ReturnedProduct
)
from django import forms
from django.forms import inlineformset_factory, ModelForm
from django.views.decorators.http import require_GET, require_POST
from collections import defaultdict
from django.template.defaulttags import register
from datetime import datetime, date
from django.http import FileResponse
import io
from calendar import monthrange

@register.filter
def get_item(dictionary, key):
    return dictionary.get(key, [])

def index(request):
    """
    Render the index page of the admin panel with dashboard metrics.
    """
    from datetime import timedelta
    from decimal import Decimal
    today = timezone.now().date()
    # Metrics for dashboard
    total_sales = Sale.objects.aggregate(total=Sum('total'))['total'] or Decimal('0')
    outstanding_invoices = Invoice.objects.filter(status='unpaid')
    net_profit = None
    # Calculate net profit (for all time)
    sales = Sale.objects.all()
    expenses = Expense.objects.all()
    commissions = Commission.objects.all()
    shipments = Shipment.objects.all()
    total_expenses = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_commissions = commissions.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_purchases = sum(
        (s.shipment_cost + (s.cost_sdg * s.quantity)) for s in shipments
    )
    net_profit = total_sales - (total_purchases + total_expenses + total_commissions)
    # Current month summary
    first_of_month = today.replace(day=1)
    month_sales = Sale.objects.filter(created_at__date__gte=first_of_month)
    month_expenses = Expense.objects.filter(date__gte=first_of_month)
    month_commissions = Commission.objects.filter(created_at__date__gte=first_of_month)
    month_shipments = Shipment.objects.filter(received_at__date__gte=first_of_month)
    month_total_sales = month_sales.aggregate(total=Sum('total'))['total'] or Decimal('0')
    month_total_expenses = month_expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    month_total_commissions = month_commissions.aggregate(total=Sum('amount'))['total'] or Decimal('0')  # <-- fix here
    month_total_purchases = sum(
        (s.shipment_cost + (s.cost_sdg * s.quantity)) for s in month_shipments
    )
    month_net_profit = month_total_sales - (month_total_purchases + month_total_expenses + month_total_commissions)
    # Recent sales and expenses
    recent_sales = Sale.objects.select_related('client').order_by('-created_at')[:5]
    recent_expenses = Expense.objects.order_by('-date')[:5]
    # Upcoming due invoices
    upcoming_due_invoices = Invoice.objects.filter(
        due_date__gte=today,
        due_date__lte=today + timedelta(days=7),
        status='unpaid'
    ).select_related('sale', 'sale__client')
    return render(request, 'panel/index.html', {
        "total_sales": total_sales,
        "net_profit": net_profit,
        "outstanding_invoices": outstanding_invoices,
        "upcoming_due_invoices": upcoming_due_invoices,
        "month_total_sales": month_total_sales,
        "month_net_profit": month_net_profit,
        "recent_sales": recent_sales,
        "recent_expenses": recent_expenses,
        "active_sidebar": "dashboard",
        # Add URLs for dashboard cards
        "shipment_profit_url": reverse('panel:shipment_profit_report'),
        "inventory_url": reverse('panel:inventory_list'),
        "net_profit_url": reverse('panel:net_profit_dashboard'),
    })

# Product Menu Views
def product_list(request):
    """
    List products with optional search/filter.
    """
    products = Product.objects.all()
    search = request.GET.get('search')
    category = request.GET.get('category')
    if search:
        products = products.filter(name__icontains=search)
    if category:
        products = products.filter(category=category)
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'products/product_list.html', {
        'products': page_obj,
        'category_choices': Product.CATEGORY_CHOICES,
        'search': search,
        'selected_category': category,
        "active_sidebar": "products"
    })

def product_add(request):
    from .models import Product
    category_choices = Product.CATEGORY_CHOICES
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تم إضافة المنتج بنجاح.")
            return redirect('panel:product_list')
        product = form.instance
    else:
        form = ProductForm()
        product = None
    return render(request, 'products/product_form.html', {
        "form": form,
        "product": product,
        "category_choices": category_choices,
        "active_sidebar": "products"
    })

def product_edit(request, pk):
    from .models import Product
    product = get_object_or_404(Product, pk=pk)
    category_choices = Product.CATEGORY_CHOICES
    if request.method == 'POST':
        form = ProductForm(request.POST, instance=product)
        if form.is_valid():
            # print(form.cleaned_data)
            form.save()
            messages.success(request, "تم تعديل المنتج بنجاح.")
            return redirect('panel:product_list')
    else:
        form = ProductForm(instance=product)
    return render(request, 'products/product_form.html', {
        "form": form,
        "product": product,
        "category_choices": category_choices,
        "active_sidebar": "products"
    })

def product_delete(request, pk):
    from .models import Product
    product = get_object_or_404(Product, pk=pk)
    # if request.method == 'POST':
    product.delete()
    messages.success(request, "تم حذف المنتج نهائيًا.")
    return redirect('panel:product_list')
    # return render(request, 'products/product_confirm_delete.html', {
    #     'product': product,
    #     "active_sidebar": "products"
    # })

def product_detail(request, pk):
    from .models import Product, Shipment, Inventory, SaleItem, Sale, Client
    product = get_object_or_404(Product, pk=pk)

    # Purchase history (shipments)
    shipments = (
        Shipment.objects
        .filter(product=product)
        .order_by('-received_at')
    )

    # Sales history (sale items)
    sale_items = (
        SaleItem.objects
        .filter(inventory__product=product)
        .select_related('sale', 'inventory', 'sale__client')
        .order_by('-sale__created_at')
    )

    # Stock movement timeline (inbound: shipments, outbound: sales)
    timeline = []
    for shipment in shipments:
        timeline.append({
            'type': 'in',
            'date': shipment.received_at,
            'quantity': shipment.quantity,
            'note': f"شراء {shipment.quantity} وحدة (تشغيلة {shipment.batch_number})",
            'related': shipment,
        })
    for item in sale_items:
        timeline.append({
            'type': 'out',
            'date': item.sale.created_at,
            'quantity': item.quantity,
            'note': f"بيع {item.quantity} وحدة للعميل {item.sale.client.name if item.sale.client else ''}",
            'related': item,
        })
    timeline.sort(key=lambda x: x['date'])

    # Current stock level (sum of all inventories for this product)
    from django.db.models import Sum
    current_stock = Inventory.objects.filter(product=product).aggregate(total=Sum('quantity'))['total'] or 0

    # Purchase and sale prices over time
    purchase_prices = [
        {'date': s.received_at, 'cost_usd': s.cost_usd, 'cost_sdg': s.cost_sdg, 'batch': s.batch_number}
        for s in shipments
    ]
    sale_prices = [
        {'date': si.sale.created_at, 'price': si.price, 'quantity': si.quantity, 'client': si.sale.client.name if si.sale.client else '', 'batch': si.inventory.shipment.batch_number}
        for si in sale_items
    ]

    # Suppliers: If you have a supplier model, link here. For now, just show "N/A".
    suppliers = ["N/A"]  # Placeholder

    # Customers associated with sales
    customers = (
        Client.objects
        .filter(sale__items__inventory__product=product)
        .distinct()
    )

    # Notes/activities: Use timeline for now
    notes = timeline  # Could be extended with a dedicated notes model

    return render(request, 'products/product_detail.html', {
        'product': product,
        'shipments': shipments,
        'sale_items': sale_items,
        'timeline': timeline,
        'current_stock': current_stock,
        'purchase_prices': purchase_prices,
        'sale_prices': sale_prices,
        'suppliers': suppliers,
        'customers': customers,
        'notes': notes,
        "active_sidebar": "products"
    })

def client_list(request):
    clients = Client.objects.select_related('area').all()
    search = request.GET.get('search')
    area_id = request.GET.get('area')
    if search:
        clients = clients.filter(name__icontains=search)
    if area_id:
        clients = clients.filter(area_id=area_id)
    # Annotate each client with total sales
    clients = clients.annotate(total_sales=Sum('sale__total'))
    paginator = Paginator(clients, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    areas = Area.objects.all()
    return render(request, 'panel/client_list.html', {
        'clients': page_obj,
        'areas': areas,
        'selected_area': int(area_id) if area_id else None,
        'search': search,
        "active_sidebar": "clients"
    })

def clients_by_area(request):
    area_id = request.GET.get('area')
    areas = Area.objects.all()
    clients = Client.objects.all()
    if area_id:
        clients = clients.filter(area_id=area_id)
    # Annotate each client with total sales
    clients = clients.annotate(total_sales=Sum('sale__total'))
    return render(request, 'panel/clients_by_area.html', {
        'areas': areas,
        'clients': clients,
        'selected_area': int(area_id) if area_id else None,
        "active_sidebar": "clients"
    })

def area_sales_report(request):
    # Revenue per area
    area_revenue = (
        Area.objects
        .annotate(total_sales=Sum('client__sale__total'))
        .order_by('-total_sales')
    )
    # Top clients
    top_clients = (
        Client.objects
        .annotate(total_sales=Sum('sale__total'))
        .order_by('-total_sales')[:10]
    )
    # Sales heatmap: sales count per day (for the last 30 days)
    from django.utils import timezone
    from datetime import timedelta
    today = timezone.now().date()
    last_30_days = today - timedelta(days=29)
    sales_heatmap = (
        Sale.objects
        .filter(created_at__date__gte=last_30_days)
        .values('created_at__date')
        .annotate(total=Sum('total'), count=Count('id'))
        .order_by('created_at__date')
    )
    return render(request, 'panel/area_sales_report.html', {
        'area_revenue': area_revenue,
        'top_clients': top_clients,
        'sales_heatmap': sales_heatmap,
        "active_sidebar": "reports"
    })

class SaleItemForm(forms.ModelForm):
    free_goods_discount = forms.DecimalField(
        label="خصم بضاعة مجانية (%)", required=False, min_value=0, max_value=100, initial=0,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )
    price_discount = forms.DecimalField(
        label="خصم سعر (%)", required=False, min_value=0, max_value=100, initial=0,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    )

    class Meta:
        model = SaleItem
        fields = ['inventory', 'quantity', 'price', 'free_goods_discount', 'price_discount']
        labels = {
            'inventory': 'الدفعة',
            'quantity': 'الكمية',
            'price': 'السعر',
            'free_goods_discount': 'خصم بضاعة مجانية (%)',
            'price_discount': 'خصم سعر (%)',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['price'].widget.attrs['readonly'] = True
        self.fields['price'].widget.attrs['tabindex'] = -1
        self.fields['price'].widget.attrs['class'] = self.fields['price'].widget.attrs.get('class', '') + ' bg-light'
        # Set default discount values if not set
        self.fields['free_goods_discount'].initial = self.instance.free_goods_discount or 0
        self.fields['price_discount'].initial = self.instance.price_discount or 0

SaleItemFormSet = inlineformset_factory(
    Sale, SaleItem, form=SaleItemForm, extra=0, can_delete=True
)

class SaleForm(forms.ModelForm):
    due_date = forms.DateField(
        label="تاريخ الاستحقاق",
        required=True,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )
    # Removed commission field
    class Meta:
        model = Sale
        fields = ['client', 'employee', 'due_date']
        labels = {
            'client': 'العميل',
            'employee': 'المندوب',
            'due_date': 'تاريخ الاستحقاق',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['client'].widget.attrs.update({'class': 'form-select'})
        self.fields['employee'].widget.attrs.update({'class': 'form-select'})
        self.fields['due_date'].widget.attrs.update({'required': True})
        # Set min attribute to today for due_date field
        today_str = date.today().isoformat()
        self.fields['due_date'].widget.attrs['min'] = today_str

def sale_create(request):
    latest_rate = ExchangeRate.objects.order_by('-updated_at').first()
    products = Product.objects.all()
    inventories = Inventory.objects.filter(quantity__gt=0).select_related('product', 'shipment')
    inventories_by_product = defaultdict(list)
    for inv in inventories:
        inventories_by_product[inv.product.pk].append(inv)
    products_with_batches = [p for p in products if inventories_by_product.get(p.pk)]
    if request.method == 'POST':
        sale_form = SaleForm(request.POST)
        post_data = request.POST.copy()
        total_forms = int(post_data.get('items-TOTAL_FORMS', 0))
        for i in range(total_forms):
            prefix = f'items-{i}'
            product_id = post_data.get(f'{prefix}-product')
            batch_id = post_data.get(f'{prefix}-batch')
            # --- Get discounts from POST ---
            free_goods_discount = post_data.get(f'{prefix}-free_goods_discount', 0)
            price_discount = post_data.get(f'{prefix}-price_discount', 0)
            post_data[f'{prefix}-free_goods_discount'] = free_goods_discount or 0
            post_data[f'{prefix}-price_discount'] = price_discount or 0
            # ...existing code for price...
            if batch_id:
                post_data[f'{prefix}-inventory'] = batch_id
            if product_id and batch_id:
                try:
                    inventory = Inventory.objects.select_related('shipment', 'product').get(pk=batch_id)
                    shipment = inventory.shipment
                    product = inventory.product
                    if shipment and shipment.sale_usd is not None and product.exchange_rate is not None:
                        base_price = float(shipment.sale_usd or 0) * float(product.exchange_rate or 0)
                        # --- FIX: Use correct discount formula and ensure string ---
                        if price_discount and float(price_discount) > 0:
                            price = base_price * (1 - float(price_discount) / 100)
                        else:
                            price = base_price
                        post_data[f'{prefix}-price'] = str(round(price, 2))
                    else:
                        post_data[f'{prefix}-price'] = "0"
                except (Inventory.DoesNotExist, Product.DoesNotExist, AttributeError):
                    post_data[f'{prefix}-price'] = "0"
            else:
                post_data[f'{prefix}-price'] = "0"
        formset = SaleItemFormSet(post_data)
        if sale_form.is_valid() and formset.is_valid():
            sale = sale_form.save(commit=False)
            sale.created_at = timezone.now()
            sale.save()
            formset.instance = sale
            sale_items = formset.save(commit=False)
            total = 0
            for i, form in enumerate(formset.forms):
                if form.cleaned_data.get('DELETE', False):
                    continue
                prefix = form.prefix
                batch_key = f"{prefix}-batch"
                batch_id = request.POST.get(batch_key)
                if not batch_id:
                    messages.error(request, "يجب اختيار دفعة لكل منتج.")
                    return render(request, 'sales/sale_form.html', {
                        'sale_form': sale_form,
                        'formset': formset,
                        'latest_rate': latest_rate,
                        'products': products,
                        'inventories': inventories,
                        'products_with_batches': products_with_batches,
                        'inventories_by_product': inventories_by_product,
                        'sale': None,
                        "active_sidebar": "sales"
                    })
                try:
                    inventory = Inventory.objects.select_related('shipment', 'product').get(pk=batch_id)
                except Inventory.DoesNotExist:
                    messages.error(request, "دفعة غير صالحة.")
                    return render(request, 'sales/sale_form.html', {
                        'sale_form': sale_form,
                        'formset': formset,
                        'latest_rate': latest_rate,
                        'products': products,
                        'inventories': inventories,
                        'products_with_batches': products_with_batches,
                        'inventories_by_product': inventories_by_product,
                        'sale': None,
                        "active_sidebar": "sales"
                    })
                form.instance.inventory = inventory
                # Set price from shipment.sale_usd * product.exchange_rate (enforce backend)
                shipment = inventory.shipment
                product = inventory.product
                if shipment and shipment.sale_usd is not None and product.exchange_rate is not None:
                    form.instance.price = float(shipment.sale_usd or 0) * float(product.exchange_rate or 0)
                else:
                    form.instance.price = 0
                # --- Set discounts from form data ---
                form.instance.free_goods_discount = float(form.cleaned_data.get('free_goods_discount') or 0)
                form.instance.price_discount = float(form.cleaned_data.get('price_discount') or 0)
            for obj in formset.deleted_objects:
                obj.delete()
            for item in sale_items:
                # Deduct both paid and free units from inventory
                total_units = item.quantity + item.free_units
                if total_units > item.inventory.quantity:
                    messages.error(request, f"الكمية المطلوبة (مع المجاني) غير متوفرة في الدفعة {item.inventory.shipment.batch_number} للمنتج {item.inventory.product.name}")
                    return render(request, 'sales/sale_form.html', {
                        'sale_form': sale_form,
                        'formset': formset,
                        'latest_rate': latest_rate,
                        'products': products,
                        'inventories': inventories,
                        'products_with_batches': products_with_batches,
                        'inventories_by_product': inventories_by_product,
                        'sale': None,
                        "active_sidebar": "sales"
                    })
                item.inventory.quantity -= total_units
                item.inventory.save()
                item.save()
                total += item.get_total
            sale.total = total
            sale.save()
            formset.save_m2m()
            sale.calculate_total()
            # --- Commission creation ---
            employee = sale.employee
            if employee and getattr(employee, 'commission_percentage', 0):
                commission_percentage = float(employee.commission_percentage)
                commission_amount = float(sale.total or 0) * (commission_percentage / 100)
                Commission.objects.update_or_create(
                    employee=employee, sale=sale,
                    defaults={'amount': commission_amount}
                )
            # --- End commission creation ---
            invoice = Invoice.objects.create(
                sale=sale,
                created_at=timezone.now(),
                file_path='',
            )
            invoice.total = sale.total
            invoice.due_date = sale_form.cleaned_data['due_date']
            invoice.status = 'unpaid'
            invoice.save()
            return redirect('panel:sale_detail', pk=sale.pk)
        else:
            # --- Add this block to print form errors for debugging ---
            print("SaleForm errors:", sale_form.errors)
            print("Formset errors:", formset.errors)
            messages.error(request, "حدث خطأ في البيانات المدخلة. يرجى مراجعة الحقول.")
    else:
        sale_form = SaleForm()
        formset = SaleItemFormSet()
    return render(request, 'sales/sale_form.html', {
        'sale_form': sale_form,
        'formset': formset,
        'latest_rate': latest_rate,
        'products': products,
        'inventories': inventories,
        'products_with_batches': products_with_batches,
        'inventories_by_product': inventories_by_product,
        'sale': None,
        "active_sidebar": "sales"
    })

# AJAX endpoint to get employee commission percentage
@require_GET
def get_employee_commission(request):
    emp_id = request.GET.get('id')
    from .models import Employee
    try:
        emp = Employee.objects.get(pk=emp_id)
        return JsonResponse({'commission_percentage': float(emp.commission_percentage)})
    except Employee.DoesNotExist:
        return JsonResponse({'commission_percentage': 0})

def sale_detail(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    invoice = Invoice.objects.filter(sale=sale).first()
    returned_products = sale.returned_products.select_related('sale_item', 'sale_item__inventory', 'sale_item__inventory__product').all()
    return_form = ReturnedProductForm(sale=sale)
    # Payment form logic is now handled inline in the template
    return render(request, 'panel/sale_detail.html', {
        'sale': sale,
        'invoice': invoice,
        'returned_products': returned_products,
        'return_form': return_form,
        "active_sidebar": "sales"
    })

@require_POST
def sale_return_product(request, pk):
    sale = get_object_or_404(Sale, pk=pk)
    form = ReturnedProductForm(sale=sale, data=request.POST)
    if form.is_valid():
        returned = form.save(commit=False)
        returned.sale = sale
        returned.save()
        messages.success(request, f"تم تسجيل إرجاع {returned.quantity} وحدة من {returned.sale_item.inventory.product.name}.")
    else:
        for error in form.errors.values():
            messages.error(request, error)
    return redirect('panel:sale_detail', pk=sale.pk)

def sale_list(request):
    sales = Sale.objects.select_related('client', 'employee').order_by('-created_at')
    # Prefetch invoice for each sale for payment info in the list
    from django.db.models import Prefetch
    invoices = Invoice.objects.all()
    sales = sales.prefetch_related(Prefetch('invoice', queryset=invoices))
    # --- Add search by invoice number ---
    invoice_number = request.GET.get('invoice_number')
    if invoice_number:
        sales = sales.filter(invoice__number__icontains=invoice_number)
    # --- End search ---

    # --- New: Filtering ---
    area_id = request.GET.get('area')
    status = request.GET.get('status')
    employee_id = request.GET.get('employee')
    client_id = request.GET.get('client')

    if area_id:
        sales = sales.filter(client__area_id=area_id)
    # --- handle new partial_or_unpaid status ---
    if status == "partial_or_unpaid":
        sales = sales.filter(invoice__status__in=["partial", "unpaid"])
    elif status:
        sales = sales.filter(invoice__status=status)
    # --- end handle ---
    if employee_id:
        sales = sales.filter(employee_id=employee_id)
    if client_id:
        sales = sales.filter(client_id=client_id)

    # For filter dropdowns
    areas = Area.objects.all()
    employees = Employee.objects.all()
    clients = Client.objects.all()
    status_choices = Invoice.STATUS_CHOICES

    return render(request, 'panel/sale_list.html', {
        'sales': sales,
        "active_sidebar": "sales",
        'invoice_number': invoice_number,  # Pass to template for form value
        # --- New context for filters ---
        'areas': areas,
        'employees': employees,
        'clients': clients,
        'status_choices': status_choices,
        'selected_area': int(area_id) if area_id else None,
        'selected_status': status,
        'selected_employee': int(employee_id) if employee_id else None,
        'selected_client': int(client_id) if client_id else None,
    })

def shipment_list(request):
    """
    List shipments with optional search/filter and pagination.
    """
    shipments = Shipment.objects.select_related('product').all().order_by('-received_at')
    search = request.GET.get('search')
    if search:
        shipments = shipments.filter(product__name__icontains=search)
    paginator = Paginator(shipments, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'shipments/shipment_list.html', {
        'shipments': page_obj,
        'search': search,
        "active_sidebar": "shipments"
    })

class ShipmentForm(forms.ModelForm):
    # batch_number = forms.CharField(label="رقم التشغيلة", required=True)
    expiry_date = forms.DateField(label="تاريخ الانتهاء", required=True, widget=forms.DateInput(attrs={'type': 'date'}))
    # cost_usd = forms.DecimalField(label="تكلفة الوحدة بالدولار", max_digits=10, decimal_places=2, required=True)  # <-- new field
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.all(),
        required=True,
        label="المورد",
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    class Meta:
        model = Shipment
        fields = ['product', 'quantity', 'cost_usd',"cost_sdg", "sale_usd", "shipment_cost", "batch_number", "expiry_date", "supplier"]
        labels = {
            'product': 'المنتج',
            'quantity': 'الكمية',
            'shipment_cost': 'تكاليف الشحن الاضافية بالجنيه (ترحيل, جمارك, تحميل و غيرها)',
            'batch_number': 'رقم التشغيلة',                 
            'cost_usd': 'تكلفة الوحدة بالدولار',
            "cost_sdg": "تكلفة الوحدة بالجنيه",
            "sale_usd": "سعر البيع بالدولار",
            'expiry_date': 'تاريخ الانتهاء',
            'supplier': 'المورد',

        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].widget.attrs.update({'class': 'form-select'})
        self.fields['quantity'].widget.attrs.update({'class': 'form-control', 'min': 1})
        self.fields['shipment_cost'].widget.attrs.update({'class': 'form-control', 'step': '1', 'min': 0})
        self.fields['batch_number'].widget.attrs.update({'class': 'form-control'})
        self.fields['expiry_date'].widget.attrs.update({'class': 'form-control'})
        self.fields['cost_usd'].widget.attrs.update({'class': 'form-control', 'step': '0.01', 'min': 0})
        self.fields['supplier'].queryset = Supplier.objects.all()
        # Only disable product field if editing (instance with pk)
        if self.instance and getattr(self.instance, 'pk', None):
            self.fields['product'].disabled = True
            self.fields['quantity'].disabled = True  # <-- make quantity uneditable when editing

def shipment_create(request):
    products = Product.objects.all()
    shipment = None
    if request.method == 'POST':
        form = ShipmentForm(request.POST)
        if form.is_valid():
            shipment = form.save(commit=False)
            shipment.received_at = timezone.now()
            shipment.save()
            # Create Inventory for this shipment
            from .models import Inventory
            Inventory.objects.create(
                product=shipment.product,
                shipment=shipment,
                quantity=shipment.quantity
            )
            messages.success(request, "تم تسجيل الشحنة بنجاح.")
            return redirect('panel:shipment_list')
    else:
        form = ShipmentForm()
    return render(request, 'shipments/shipment_form.html', {
        'form': form,
        'products': products,
        'shipment': shipment,
        "active_sidebar": "shipments"
    })

def shipment_edit(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    from .models import Inventory
    try:
        inventory = Inventory.objects.get(shipment=shipment)
    except Inventory.DoesNotExist:
        inventory = None
    orig_quantity = shipment.quantity
    if request.method == 'POST':
        form = ShipmentForm(request.POST, instance=shipment)
        if form.is_valid():
            new_shipment = form.save(commit=False)

            # Update inventory for this shipment
            if inventory:
                
                inventory_qty_diff = new_shipment.quantity - orig_quantity
                # print(inventory_qty_diff)
                # print(orig_quantity)
                # if orig_quantity < new_shipment.quantity:
                #     # If quantity increased, just update the inventory
                #     inventory.quantity += inventory_qty_diff
                # elif orig_quantity > new_shipment.quantity:
                #     # If quantity decreased, decrease the inventory
                #     inventory.quantity -= inventory_qty_diff
                # Ensure inventory quantity does not go negative
                inventory.quantity += inventory_qty_diff
                if inventory.quantity < 0:
                    messages.error(request, "الكمية الجديدة لا يمكن أن تكون أقل من الصفر.")
                    return render(request, 'shipments/shipment_form.html', {
                        'form': form,
                        'products': Product.objects.all(),
                        'shipment': shipment,
                        "active_sidebar": "shipments"
                    })
                inventory.product = new_shipment.product
                inventory.save()
            else:
                Inventory.objects.create(
                    product=new_shipment.product,
                    shipment=new_shipment,
                    quantity=new_shipment.quantity
                )
            new_shipment.save()
            messages.success(request, "تم تعديل الشحنة بنجاح.")
            return redirect('panel:shipment_list')
    else:
        initial = {
            'batch_number': shipment.batch_number,
            'expiry_date': shipment.expiry_date,
            'cost_usd': shipment.cost_usd,
            'product': shipment.product.pk if shipment.product else None,
            'supplier': shipment.supplier.pk if shipment.supplier else None,
        }
        form = ShipmentForm(instance=shipment, initial=initial)
    return render(request, 'shipments/shipment_form.html', {
        'form': form,
        'products': Product.objects.all(),
        'shipment': shipment,
        "active_sidebar": "shipments"
    })

def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    from .models import Inventory
    try:
        inv = Inventory.objects.get(shipment=shipment)
    except Inventory.DoesNotExist:
        inv = None
    if request.method == 'POST':
        if inv:
            inv.delete()
        shipment.delete()
        messages.success(request, "تم حذف الشحنة.")
        return redirect('panel:shipment_list')
    return render(request, 'shipments/shipment_confirm_delete.html', {
        'shipment': shipment,
        "active_sidebar": "shipments"
    })

def shipment_profit_report(request):
    from django.db.models import Sum, F
    shipments = Shipment.objects.select_related('product').all().order_by('-received_at')
    shipment_data = []
    from .models import Inventory, SaleItem, Commission  # <-- add Commission
    for shipment in shipments:
        # Use the inventory for this shipment only
        try:
            inventory = shipment.inventory  # OneToOneField, so this is the inventory for this shipment
        except Inventory.DoesNotExist:
            inventory = None
        inventories = [inventory] if inventory else []
        sale_items = SaleItem.objects.filter(
            inventory__in=inventories,
            sale__created_at__gte=shipment.received_at
        )
        total_revenue = sum(item.price * item.quantity for item in sale_items)
        purchase_cost = shipment.cost_sdg * shipment.quantity
        # --- Calculate commission for sales related to this shipment ---
        related_sales = sale_items.values_list('sale_id', flat=True).distinct()
        commission_amount = Commission.objects.filter(sale_id__in=related_sales).aggregate(total=Sum('amount'))['total'] or 0
        # --- End commission calculation ---
        profit = total_revenue - (shipment.shipment_cost + purchase_cost + commission_amount)
        # Get batch info from this shipment's inventory
        batch_number = shipment.batch_number if shipment else ''
        expiry_date = shipment.expiry_date if shipment else ''
        # --- Calculate inventory value (unrealized value) ---
        inventory_value = 0
        if inventory:
            inventory_value = inventory.quantity * (shipment.sale_usd * shipment.product.exchange_rate)
        shipment_data.append({
            'shipment': shipment,
            'batch_number': batch_number,
            'expiry_date': expiry_date,
            'total_revenue': total_revenue,
            'purchase_cost': purchase_cost,
            'commission_amount': commission_amount,
            'profit': profit,
            'inventory_value': inventory_value,  # <-- new field
        })
    return render(request, 'shipments/shipment_profit.html', {
        'shipment_data': shipment_data,
        "active_sidebar": "shipments_profit"
    })

# Employee Management Views
class EmployeeForm(forms.ModelForm):
    commission_percentage = forms.DecimalField(
        max_digits=5, decimal_places=2, required=False, label="نسبة العمولة (%)", min_value=0, max_value=100
    )
    sales_target = forms.DecimalField(
        max_digits=12, decimal_places=2, required=False, label="الهدف الشهري بالجنيه", min_value=0
    )

    class Meta:
        model = Employee
        fields = ['name', 'commission_percentage', 'sales_target']
        labels = {
            'name': 'اسم المندوب',
            'commission_percentage': 'نسبة العمولة (%)',
            'sales_target': 'الهدف الشهري',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs.update({'class': 'form-control', 'required': True})
        self.fields['commission_percentage'].widget.attrs.update({'class': 'form-control', 'step': '0.01'})
        self.fields['sales_target'].widget.attrs.update({'class': 'form-control', 'step': '0.01'})
        if self.instance.pk:
            self.fields['commission_percentage'].initial = self.instance.commission_percentage
            self.fields['sales_target'].initial = self.instance.sales_target

    def clean_commission_percentage(self):
        value = self.cleaned_data.get('commission_percentage', 0)
        if value is None:
            return 0
        if value < 0 or value > 100:
            raise forms.ValidationError("نسبة العمولة يجب أن تكون بين 0 و 100.")
        return value

    def clean_sales_target(self):
        value = self.cleaned_data.get('sales_target', 0)
        if value is None:
            return 0
        if value < 0:
            raise forms.ValidationError("الهدف الشهري يجب أن يكون أكبر من أو يساوي صفر.")
        return value

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.commission_percentage = self.cleaned_data.get('commission_percentage', 0) or 0
        instance.sales_target = self.cleaned_data.get('sales_target', 0) or 0
        if commit:
            instance.save()
        return instance

def employee_list(request):
    from datetime import date
    today = date.today()
    # Get month/year from GET params
    month = request.GET.get('month')
    year = request.GET.get('year')
    # --- Ensure valid integer values and fallback ---
    try:
        month = int(month)
        if not (1 <= month <= 12):
            month = today.month
    except (TypeError, ValueError):
        month = today.month
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = today.year
    # --- Build years list for dropdown ---
    first_employee = Employee.objects.order_by('created_at').first()
    min_year = first_employee.created_at.year if first_employee else today.year
    max_year = today.year
    years = list(range(min_year, max_year + 1))
    employees = Employee.objects.all()
    employee_data = []
    for emp in employees:
        monthly_sales = emp.get_monthly_sales(month=month, year=year)
        sales_target = emp.sales_target or 0
        commission_percentage = emp.commission_percentage or 0
        commission_amount = emp.get_monthly_commission(month=month, year=year)
        unpaid_commission = emp.get_unpaid_commission(month=month, year=year)
        progress = (float(monthly_sales) / float(sales_target) * 100) if sales_target else 0
        employee_data.append({
            'employee': emp,
            'monthly_sales': monthly_sales,
            'sales_target': sales_target,
            'commission_percentage': commission_percentage,
            'commission_amount': commission_amount,
            'progress': progress,
            'unpaid_commission': unpaid_commission,
        })
    # Prepare months for dropdown (1-12, label as "MM")
    months = [{'value': m, 'label': f"{m:02d}"} for m in range(1, 13)]
    return render(request, 'panel/employee_list.html', {
        'employee_data': employee_data,
        'months': months,
        'years': years,
        'selected_month': month,
        'selected_year': year,
        "active_sidebar": "employees"
    })

def employee_detail(request, pk):
    from datetime import date
    employee = get_object_or_404(Employee, pk=pk)
    # Get month/year from GET params
    month = request.GET.get('month')
    year = request.GET.get('year')
    today = date.today()
    if not month:
        month = today.month
    else:
        month = int(month)
    if not year:
        year = today.year
    else:
        year = int(year)
    # Get sales for this employee in the selected month
    sales = Sale.objects.filter(employee=employee, created_at__year=year, created_at__month=month)
    total_sales = sales.aggregate(total=Sum('total'))['total'] or 0
    commission_percentage = employee.commission_percentage or 0
    commission_amount = employee.get_monthly_commission(month=month, year=year)
    sales_target = employee.sales_target or 0
    progress = (float(total_sales) / float(sales_target) * 100) if sales_target else 0
    # List of sales with commission for this month
    sales_with_commission = []
    for sale in sales:
        commission = Commission.objects.filter(employee=employee, sale=sale).first()
        sales_with_commission.append({
            'sale': sale,
            'commission': commission.amount if commission else float(sale.total or 0) * float(commission_percentage) / 100
        })
    # print(sales_with_commission)
    unpaid_commission = employee.get_unpaid_commission(month=month, year=year)
    # Commission payments for this employee (for this month only)
    commission_payments = employee.commission_payments.filter(
        paid_at__year=year, paid_at__month=month
    ).order_by('-paid_at')
    # Prepare months for dropdown
    months = []
    for m in range(1, 13):
        months.append({'value': m, 'label': f"{year}-{m:02d}"})
    return render(request, 'panel/employee_detail.html', {
        'employee': employee,
        'sales': sales,
        'total_sales': total_sales,
        'commission_percentage': commission_percentage,
        'commission_amount': commission_amount,
        'sales_target': sales_target,
        'progress': progress,
        'month': month,
        'year': year,
        'months': months,
        'sales_with_commission': sales_with_commission,
        'unpaid_commission': unpaid_commission,
        'commission_payments': commission_payments,
        "active_sidebar": "employees"
    })

def employee_add(request):
    if request.method == 'POST':
        form = EmployeeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تم إضافة المندوب بنجاح.")
            return redirect('panel:employee_list')
    else:
        form = EmployeeForm()
    return render(request, 'panel/employee_form.html', {
        'form': form,
        "active_sidebar": "employees"
    })

def employee_edit(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        form = EmployeeForm(request.POST, instance=employee)
        if form.is_valid():
            form.save()
            # --- Recalculate all commissions for this employee ---
            # sales = Sale.objects.filter(employee=employee)
            # for sale in sales:
            #     commission_percentage = float(employee.commission_percentage or 0)
            #     commission_amount = float(sale.total or 0) * (commission_percentage / 100)
            #     Commission.objects.update_or_create(
            #         employee=employee, sale=sale,
            #         defaults={'amount': commission_amount}
            #     )
            # --- End recalc ---
            messages.success(request, "تم تعديل بيانات المندوب بنجاح.")
            return redirect('panel:employee_list')
    else:
        form = EmployeeForm(instance=employee)
    return render(request, 'panel/employee_form.html', {
        'form': form,
        'employee': employee,
        "active_sidebar": "employees"
    })

def employee_delete(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        employee.delete()
        messages.success(request, "تم حذف المندوب.")
        return redirect('panel:employee_list')
    return render(request, 'panel/employee_confirm_delete.html', {
        'employee': employee,
        "active_sidebar": "employees"
    })

# Commission Calculation View
def sale_commissions(request):
    sales = Sale.objects.select_related('employee').all().order_by('-created_at')
    commission_data = []
    for sale in sales:
        employee = sale.employee
        commission_percentage = getattr(employee, 'commission_percentage', None)
        commission_amount = None
        # Only display, do not create/update commissions here
        commission = Commission.objects.filter(employee=employee, sale=sale).first()
        if commission:
            commission_amount = commission.amount
        elif commission_percentage is not None:
            commission_amount = (sale.total or 0) * (commission_percentage / 100)
        commission_data.append({
            'sale': sale,
            'employee': employee,
            'commission_percentage': commission_percentage,
            'commission_amount': commission_amount,
        })
    return render(request, 'panel/sale_commissions.html', {
        'commission_data': commission_data,
        "active_sidebar": "commissions"
    })

def net_profit_dashboard(request):
    from django.db.models import Q
    from decimal import Decimal

    # Filters
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    area_id = request.GET.get('area')
    shipment_id = request.GET.get('shipment')

    sales = Sale.objects.all()
    expenses = Expense.objects.all()
    commissions = Commission.objects.all()
    shipments = Shipment.objects.all()

    # Date filtering
    if start_date:
        sales = sales.filter(created_at__date__gte=start_date)
        expenses = expenses.filter(date__gte=start_date)
        commissions = commissions.filter(created_at__date__gte=start_date)
        shipments = shipments.filter(received_at__date__gte=start_date)
    if end_date:
        sales = sales.filter(created_at__date__lte=end_date)
        expenses = expenses.filter(date__lte=end_date)
        commissions = commissions.filter(created_at__date__lte=end_date)
        shipments = shipments.filter(received_at__date__lte=end_date)

    # Area filtering (by client area)
    if area_id:
        sales = sales.filter(client__area_id=area_id)

    # Shipment filtering (by shipment id, only sales of products in that shipment)
    if shipment_id:
        try:
            shipment = Shipment.objects.get(pk=shipment_id)
            sales = sales.filter(items__inventory__shipment=shipment)
        except Shipment.DoesNotExist:
            shipment = None
    else:
        shipment = None

    total_sales = sales.aggregate(total=Sum('total'))['total'] or Decimal('0')
    total_expenses = expenses.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_commissions = commissions.aggregate(total=Sum('amount'))['total'] or Decimal('0')

    # Total purchases: sum of shipment cost + purchase cost for filtered shipments
    if shipment_id:
        filtered_shipments = shipments.filter(pk=shipment_id)
    else:
        filtered_shipments = shipments
    total_purchases = sum(
        (s.shipment_cost + (s.cost_sdg* s.quantity)) for s in filtered_shipments
    )
    # print(filtered_shipments)

    net_profit = total_sales - (total_purchases + total_expenses + total_commissions)

    # For filters
    areas = Area.objects.all()
    all_shipments = Shipment.objects.all()

    # --- Inventory value calculation ---
    from .models import Inventory
    inventory_value = 0
    inventories = Inventory.objects.select_related('shipment', 'product').all()
    for inv in inventories:
        shipment = inv.shipment
        product = inv.product
        # Calculate sale price in SDG for this inventory
        if shipment and shipment.sale_usd is not None and product.exchange_rate is not None:
            sale_price_sdg = float(shipment.cost_usd) * float(product.exchange_rate)
            inventory_value += inv.quantity * sale_price_sdg
    # --- End inventory value calculation ---

    return render(request, 'panel/net_profit_dashboard.html', {
        'total_sales': total_sales,
        'total_purchases': total_purchases,
        'total_expenses': total_expenses,
        'total_commissions': total_commissions,
        'net_profit': net_profit,
        'areas': areas,
        'all_shipments': all_shipments,
        'selected_area': int(area_id) if area_id else None,
        'selected_shipment': int(shipment_id) if shipment_id else None,
        'start_date': start_date,
        'end_date': end_date,
        "active_sidebar": "net_profit",
        'inventory_value': inventory_value,  # <-- new context variable
    })

def invoice_list(request):
    invoices = Invoice.objects.select_related('sale', 'sale__client').order_by('-created_at')
    client_name = request.GET.get('client')
    status = request.GET.get('status')
    invoice_num = request.GET.get('invoice')
    if client_name:
        invoices = invoices.filter(sale__client__name__icontains=client_name)
    if invoice_num:
        invoices = invoices.filter(pk=invoice_num)
    if status in ['paid', 'unpaid', 'partial']:
        invoices = invoices.filter(status=status)
    paginator = Paginator(invoices, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'panel/invoice_list.html', {
        'invoices': page_obj,
        "active_sidebar": "invoices",
        "today": date.today(),
    })

class InvoicePaymentForm(forms.ModelForm):
    class Meta:
        model = InvoicePayment
        fields = ['amount', 'note']
        labels = {
            'amount': 'المبلغ',
            'note': 'ملاحظة',
        }

    def __init__(self, *args, **kwargs):
        self.invoice = kwargs.pop('invoice', None)
        super().__init__(*args, **kwargs)
        if self.invoice:
            self.fields['amount'].widget.attrs['max'] = float(self.invoice.remaining_amount)
            self.fields['amount'].widget.attrs['min'] = 1
            self.fields['amount'].widget.attrs['step'] = '0.01'

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if self.invoice and amount > self.invoice.remaining_amount:
            raise forms.ValidationError("المبلغ المدفوع أكبر من المتبقي على الفاتورة.")
        if amount <= 0:
            raise forms.ValidationError("يجب أن يكون المبلغ أكبر من صفر.")
        return amount

def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    payments = invoice.payments.order_by('-paid_at')
    payment_form = None
    if not getattr(request, 'pdf_mode', False) and invoice.status != 'paid':
        payment_form = InvoicePaymentForm(invoice=invoice)
    return render(request, 'invoices/invoice_detail.html', {
        'invoice': invoice,
        'payments': payments,
        'payment_form': payment_form,
        "active_sidebar": "invoices"
    })

@require_POST
def invoice_add_payment(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = InvoicePaymentForm(request.POST, invoice=invoice)
    if form.is_valid():
        payment = form.save(commit=False)
        payment.invoice = invoice
        payment.save()
        messages.success(request, f"تم تسجيل دفعة بمبلغ {payment.amount} بنجاح.")
    else:
        for error in form.errors.values():
            messages.error(request, error)
    return redirect('panel:sale_detail', pk=invoice.pk)

@require_POST
def invoice_mark_paid(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if invoice.status != 'paid':
        remaining = invoice.remaining_amount
        if remaining > 0:
            InvoicePayment.objects.create(invoice=invoice, amount=remaining)
        invoice.update_status()
        messages.success(request, "تم تحديد الفاتورة كمدفوعة بنجاح.")
    return redirect('panel:sale_detail', pk=invoice.pk)

@require_POST
def invoice_mark_unpaid(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if invoice.status != 'unpaid':
        invoice.payments.all().delete()
        invoice.refresh_from_db()  # Ensure we have the latest state after deletion
        invoice.update_status()
        messages.success(request, "تم تحديد الفاتورة كغير مدفوعة.")
    return redirect('panel:invoice_detail', pk=invoice.pk)

@require_GET
def invoice_pdf(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    # --- Add this block ---
    discounted_items = []
    for item in invoice.sale.items.all():
        if (getattr(item, 'free_goods_discount', 0) and float(item.free_goods_discount) > 0) or \
           (getattr(item, 'price_discount', 0) and float(item.price_discount) > 0):
            discounted_items.append(item)
    has_discount = bool(discounted_items)
    # --- End block ---
    # --- Add returned products ---
    returned_products = invoice.sale.returned_products.select_related(
        'sale_item', 'sale_item__inventory', 'sale_item__inventory__product'
    ).all()
    # --- End returned products ---
    from django.template.loader import render_to_string
    from weasyprint import HTML, CSS
    import tempfile
    html_string = render_to_string(
        'invoices/invoice_pdf.html',
        {
            'invoice': invoice,
            'request': request,
            'has_discount': has_discount,  # pass to template
            'discounted_items': discounted_items,  # pass to template
            'returned_products': returned_products,  # pass to template
        }
    )
    pdf_css = """
    body { font-family: 'Cairo', Arial, sans-serif; }
    .table { width: 100%; border-collapse: collapse; }
    .table th, .table td { border: 1px solid #333; padding: 6px; }
    .header { background: #007bff; color: #fff; padding: 12px; }
    .badge { padding: 4px 8px; border-radius: 4px; }
    .bg-success { background: #28a745 !important; color: #fff !important; }
    .bg-danger { background: #dc3545 !important; color: #fff !important; }
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf") as output:
        HTML(string=html_string, base_url=request.build_absolute_uri('/')).write_pdf(
            output.name,
            stylesheets=[CSS(string=pdf_css)]
        )
        output.seek(0)
        pdf = output.read()
    response = FileResponse(
        io.BytesIO(pdf),
        as_attachment=True,
        filename=f'invoice_{invoice.pk}.pdf'
    )
    return response

def expense_list(request):
    """
    List expenses with optional month filter and pagination.
    """
    from datetime import datetime
    expenses = Expense.objects.all().order_by('-date')
    months = []
    for e in Expense.objects.dates('date', 'month', order='DESC'):
        months.append({'value': e.strftime('%Y-%m'), 'label': e.strftime('%B %Y')})
    selected_month = request.GET.get('month')
    if selected_month:
        try:
            year, month = map(int, selected_month.split('-'))
            expenses = expenses.filter(date__year=year, date__month=month)
        except Exception:
            pass
    paginator = Paginator(expenses, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'expenses/expense_list.html', {
        'expenses': page_obj,
        'months': months,
        'selected_month': selected_month,
        "active_sidebar": "expenses"
    })

def expense_add(request):
    category_choices = Expense.CATEGORY_CHOICES
    if request.method == 'POST':
        form = ExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save()
            messages.success(request, "تم إضافة المصروف بنجاح.")
            return redirect('panel:expense_list')
        expense = form.instance
    else:
        form = ExpenseForm()
        expense = None
    return render(request, 'expenses/expense_form.html', {
        'form': form,
        'expense': expense,
        'category_choices': category_choices,
        "active_sidebar": "expenses"
    })

def expense_edit(request, pk):
    expense = get_object_or_404(Expense, pk=pk)
    category_choices = Expense.CATEGORY_CHOICES
    if request.method == 'POST':
        form = ExpenseForm(request.POST, instance=expense)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تعديل المصروف بنجاح.")
            return redirect('panel:expense_list')
    else:
        form = ExpenseForm(instance=expense)
    return render(request, 'expenses/expense_form.html', {
        'form': form,
        'expense': expense,
        'category_choices': category_choices,
        "active_sidebar": "expenses"
    })

def expense_delete(request, pk):
    expense = get_object_or_404(Expense, pk=pk)
    if request.method == 'POST':
        expense.delete()
        messages.success(request, "تم حذف المصروف.")
        return redirect('panel:expense_list')
    return render(request, 'expenses/expense_confirm_delete.html', {
        'expense': expense,
        "active_sidebar": "expenses"
    })

class ExpenseForm(ModelForm):
    class Meta:
        model = Expense
        fields = ['description', 'category', 'amount', 'date']
        labels = {
            'description': 'الوصف',
            'category': 'الفئة',
            'amount': 'المبلغ',
            'date': 'التاريخ',
        }

from django import forms

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['name', "exchange_rate", "unit", "description"]
        widgets = {
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
        }
        labels = {
            'name': 'اسم المنتج',
            'exchange_rate': 'سعر الصرف',
            'unit': 'الوحدة',
            'description': 'الوصف',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # cost_sdg is calculated, not user-editable
        # self.fields['stock'].widget.attrs['required'] = True
        self.fields['name'].widget.attrs['required'] = True

class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ['name', 'phone', 'address', 'area']
        labels = {
            'name': 'اسم العميل',
            'phone': 'رقم الهاتف',
            'address': 'العنوان',
            'area': 'المنطقة',
        }

def client_add(request):
    areas = Area.objects.all()
    if request.method == 'POST':
        form = ClientForm(request.POST)
        if form.is_valid():
            client = form.save()
            messages.success(request, "تم إضافة العميل بنجاح.")
            return redirect('panel:client_list')
    else:
        form = ClientForm()
    return render(request, 'clients/client_form.html', {
        'form': form,
        'client': None,
        'areas': areas,
        "active_sidebar": "clients"
    })

def client_edit(request, pk):
    client = get_object_or_404(Client, pk=pk)
    areas = Area.objects.all()
    if request.method == 'POST':
        form = ClientForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تعديل بيانات العميل بنجاح.")
            return redirect('panel:client_list')
    else:
        form = ClientForm(instance=client)
    return render(request, 'clients/client_form.html', {
        'form': form,
        'client': client,
        'areas': areas,
        "active_sidebar": "clients"
    })

def client_delete(request, pk):
    client = get_object_or_404(Client, pk=pk)
    if request.method == 'POST':
        client.delete()
        messages.success(request, "تم حذف العميل.")
        return redirect('panel:client_list')
    return render(request, 'clients/client_confirm_delete.html', {
        'client': client,
        "active_sidebar": "clients"
    })

class AreaForm(forms.ModelForm):
    class Meta:
        model = Area
        fields = ['name']
        labels = {
            'name': 'اسم المنطقة',
        }

def area_list(request):
    # Annotate each area with total sales for all its clients
    from django.db.models import Sum
    areas = Area.objects.annotate(total_sales=Sum('client__sale__total'))
    return render(request, 'areas/area_list.html', {
        'areas': areas,
        "active_sidebar": "areas"
    })

def area_add(request):
    if request.method == 'POST':
        form = AreaForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تمت إضافة المنطقة بنجاح.")
            return redirect('panel:area_list')
    else:
        form = AreaForm()
    return render(request, 'areas/area_form.html', {
        'form': form,
        'area': None,
        "active_sidebar": "areas"
    })

def area_edit(request, pk):
    area = get_object_or_404(Area, pk=pk)
    if request.method == 'POST':
        form = AreaForm(request.POST, instance=area)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تعديل المنطقة بنجاح.")
            return redirect('panel:area_list')
    else:
        form = AreaForm(instance=area)
    return render(request, 'areas/area_form.html', {
        'form': form,
        'area': area,
        "active_sidebar": "areas"
    })

def area_delete(request, pk):
    area = get_object_or_404(Area, pk=pk)
    if request.method == 'POST':
        area.delete()
        messages.success(request, "تم حذف المنطقة.")
        return redirect('panel:area_list')
    return render(request, 'areas/area_confirm_delete.html', {
        'area': area,
        "active_sidebar": "areas"
    })

def inventory_list(request):
    inventories = Inventory.objects.select_related('product', 'shipment').order_by('shipment__expiry_date')
    search = request.GET.get('search')
    if search:
        inventories = inventories.filter(product__name__icontains=search)
    paginator = Paginator(inventories, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'inventory/inventory_list.html', {
        'inventories': page_obj,
        'search': search,
        "active_sidebar": "inventory"
    })

from django.views.decorators.http import require_POST

@require_POST
def commission_pay(request, employee_id):
    from decimal import Decimal
    employee = get_object_or_404(Employee, pk=employee_id)
    amount = request.POST.get('amount')
    note = request.POST.get('note', '')
    try:
        amount = Decimal(amount)
    except Exception:
        messages.error(request, "المبلغ غير صالح.")
        return redirect('panel:employee_detail', pk=employee.pk)
    unpaid = employee.get_unpaid_commission()
    if amount <= 0 or amount > unpaid:
        messages.error(request, "المبلغ يجب أن يكون أكبر من صفر وأقل أو يساوي العمولة غير المدفوعة.")
        return redirect('panel:employee_detail', pk=employee.pk)
    from .models import CommissionPayment
    CommissionPayment.objects.create(employee=employee, amount=amount, note=note)
    messages.success(request, f"تم تسجيل دفعة عمولة بمبلغ {amount} بنجاح.")
    return redirect('panel:employee_detail', pk=employee.pk)

def client_detail(request, pk):
    client = get_object_or_404(Client, pk=pk)
    invoices = (
        Invoice.objects
        .filter(sale__client=client)
        .select_related('sale')
        .prefetch_related('payments')
        .order_by('created_at')
    )
    # Build a timeline of events (invoice creation and payments)
    timeline = []
    for invoice in invoices:
        # Invoice creation event
        timeline.append({
            'type': 'invoice',
            'date': invoice.created_at,
            'invoice': invoice,
            'amount': invoice.total,
            'paid': 0,
            'note': f"إنشاء فاتورة #{invoice.number or invoice.pk}",
        })
        # Payment events
        for payment in invoice.payments.order_by('paid_at'):
            timeline.append({
                'type': 'payment',
                'date': payment.paid_at,
                'invoice': invoice,
                'amount': -payment.amount,
                'paid': payment.amount,
                'note': f"دفعة على الفاتورة #{invoice.number or invoice.pk}",
            })
    # Sort timeline by date
    timeline.sort(key=lambda x: x['date'])
    # Calculate running balance
    balance = 0
    for event in timeline:
        if event['type'] == 'invoice':
            balance += float(event['amount'])
        elif event['type'] == 'payment':
            balance -= float(event['paid'])
        event['balance'] = balance
    # Total unpaid amount
    total_unpaid = sum(inv.remaining_amount for inv in invoices)
    return render(request, 'panel/client_detail.html', {
        'client': client,
        'timeline': timeline,
        'invoices': invoices,
        'total_unpaid': total_unpaid,
        "active_sidebar": "clients"
    })

def debts_view(request):
    from decimal import Decimal
    today = timezone.now().date()
    # Get all clients with at least one unpaid or partial invoice
    clients = Client.objects.all()
    client_debts = []
    for client in clients:
        invoices = (
            Invoice.objects
            .filter(sale__client=client)
            .filter(status__in=['unpaid', 'partial'])
        )
        total_debt = sum(inv.remaining_amount for inv in invoices)
        overdue_debt = sum(inv.remaining_amount for inv in invoices if inv.due_date and inv.due_date <= today)
        if total_debt > 0:
            client_debts.append({
                'client': client,
                'total_debt': total_debt,
                'overdue_debt': overdue_debt,
                'invoices': invoices,
            })
    # Sort by total_debt descending
    client_debts.sort(key=lambda x: x['total_debt'], reverse=True)
    return render(request, 'panel/debts.html', {
        'client_debts': client_debts,
        "active_sidebar": "debts"
    })

class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ['name', 'phone', 'address', 'note']
        labels = {
            'name': 'اسم المورد',
            'phone': 'رقم الهاتف',
            'address': 'العنوان',
            'note': 'ملاحظات',
        }

class SupplierPaymentForm(forms.ModelForm):
    class Meta:
        model = SupplierPayment
        fields = ['amount', 'note']
        labels = {
            'amount': 'المبلغ',
            'note': 'ملاحظة',
        }

    def __init__(self, *args, **kwargs):
        self.supplier = kwargs.pop('supplier', None)
        super().__init__(*args, **kwargs)
        self.fields['amount'].widget.attrs['min'] = 1
        self.fields['amount'].widget.attrs['step'] = '0.01'
        if self.supplier:
            self.fields['amount'].widget.attrs['max'] = float(self.supplier.remaining_amount)

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if self.supplier and amount > self.supplier.remaining_amount:
            raise forms.ValidationError("المبلغ المدفوع أكبر من المتبقي للمورد.")
        if amount <= 0:
            raise forms.ValidationError("يجب أن يكون المبلغ أكبر من صفر.")
        return amount

def supplier_list(request):
    suppliers = Supplier.objects.all()
    return render(request, 'suppliers/supplier_list.html', {
        'suppliers': suppliers,
        "active_sidebar": "suppliers"
    })

def supplier_add(request):
    if request.method == 'POST':
        form = SupplierForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تم إضافة المورد بنجاح.")
            return redirect('panel:supplier_list')
    else:
        form = SupplierForm()
    return render(request, 'suppliers/supplier_form.html', {
        'form': form,
        'supplier': None,
        "active_sidebar": "suppliers"
    })

def supplier_edit(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    if request.method == 'POST':
        form = SupplierForm(request.POST, instance=supplier)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تعديل بيانات المورد بنجاح.")
            return redirect('panel:supplier_list')
    else:
        form = SupplierForm(instance=supplier)
    return render(request, 'suppliers/supplier_form.html', {
        'form': form,
        'supplier': supplier,
        "active_sidebar": "suppliers"
    })

def supplier_detail(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    shipments = supplier.shipments.select_related('product').all().order_by('-received_at')
    payments = supplier.payments.order_by('-paid_at')
    payment_form = SupplierPaymentForm(supplier=supplier)
    return render(request, 'suppliers/supplier_detail.html', {
        'supplier': supplier,
        'shipments': shipments,
        'payments': payments,
        'payment_form': payment_form,
        'remaining_amount': supplier.remaining_amount,
        "active_sidebar": "suppliers"
    })

@require_POST
def supplier_add_payment(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    form = SupplierPaymentForm(request.POST, supplier=supplier)
    if form.is_valid():
        payment = form.save(commit=False)
        payment.supplier = supplier
        payment.save()
        messages.success(request, f"تم تسجيل دفعة بمبلغ {payment.amount} بنجاح.")
    else:
        for error in form.errors.values():
            messages.error(request, error)
    return redirect('panel:supplier_detail', pk=supplier.pk)

def supplier_delete(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    if request.method == 'POST':
        supplier.delete()
        messages.success(request, "تم حذف المورد.")
        return redirect('panel:supplier_list')
    return render(request, 'suppliers/supplier_confirm_delete.html', {
        'supplier': supplier,
        "active_sidebar": "suppliers"
    })

def lost_product_list(request):
    """
    List all lost products.
    """
    lost_products = LostProduct.objects.select_related('product', 'inventory', 'inventory__shipment').order_by('-lost_at')
    return render(request, 'lost_products/lost_product_list.html', {
        'lost_products': lost_products,
        "active_sidebar": "lost_products"
    })

class LostProductForm(forms.ModelForm):
    class Meta:
        model = LostProduct
        fields = ['product', 'inventory', 'quantity', 'note']
        labels = {
            'product': 'المنتج',
            'inventory': 'الدفعة',
            'quantity': 'الكمية المفقودة',
            'note': 'ملاحظة',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].widget.attrs.update({'class': 'form-select'})
        self.fields['inventory'].widget.attrs.update({'class': 'form-select'})
        self.fields['quantity'].widget.attrs.update({'class': 'form-control', 'min': 1})
        self.fields['note'].widget.attrs.update({'class': 'form-control'})

def lost_product_add(request):
    """
    Add a new lost product entry.
    """
    if request.method == 'POST':
        form = LostProductForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تسجيل المنتج المفقود بنجاح.")
            return redirect('panel:lost_product_list')
    else:
        form = LostProductForm()
    return render(request, 'lost_products/lost_product_form.html', {
        'form': form,
        "active_sidebar": "lost_products"
    })

class ReturnedProductForm(forms.ModelForm):
    class Meta:
        model = ReturnedProduct
        fields = ['sale_item', 'quantity', 'note']
        labels = {
            'sale_item': 'العنصر المباع',
            'quantity': 'الكمية المرجعة',
            'note': 'ملاحظة',
        }

    def __init__(self, sale=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if sale:
            self.fields['sale_item'].queryset = sale.items.all()
        self.fields['quantity'].widget.attrs.update({'class': 'form-control', 'min': 1})
        self.fields['note'].widget.attrs.update({'class': 'form-control'})

    def clean(self):
        cleaned_data = super().clean()
        sale_item = cleaned_data.get('sale_item')
        quantity = cleaned_data.get('quantity')
        if sale_item and quantity:
            # Only allow returning up to sold quantity minus already returned
            already_returned = sum(r.quantity for r in sale_item.returns.all())
            max_returnable = sale_item.quantity - already_returned
            if quantity > max_returnable:
                raise forms.ValidationError(f"لا يمكن إرجاع أكثر من {max_returnable} وحدة لهذا العنصر.")
        return cleaned_data
