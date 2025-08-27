from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Sum, Q, F, ExpressionWrapper, DecimalField
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from reportlab.lib.pagesizes import A4
from .models import (
     CurrencyExchange, Partner, PartnerTransaction, Currency, FinancialLog, convert_to_sdg
)
from django.db import models
from .forms import PartnerForm, PartnerTransactionForm, CurrencyPurchaseForm
from panel.models import Shipment, SupplierPayment, Expense, CommissionPayment, InvoicePayment
from django.views.decorators.http import require_GET

# --- Company Balances and Dashboard ---
def calculate_company_balance(currency):
    # Total bought into this currency
    bought_sum = CurrencyExchange.objects.filter(bought_currency=currency).aggregate(total=Sum('bought_amount'))['total'] or 0
    # Total sold from this currency
    sold_sum = CurrencyExchange.objects.filter(sold_currency=currency).aggregate(total=Sum('sold_amount'))['total'] or 0

    # Partner deposits
    deposits_sum = PartnerTransaction.objects.filter(currency=currency, transaction_type='deposit').aggregate(total=Sum('amount'))['total'] or 0

    # Partner withdrawals
    withdrawals_sum = PartnerTransaction.objects.filter(currency=currency, transaction_type='withdrawal').aggregate(total=Sum('amount'))['total'] or 0

    # shipments costs
    if currency.code == 'SDG':
        shipment_costs = Shipment.objects.all().aggregate(total=Sum('shipment_cost'))['total'] or 0
        expense_costs = Expense.objects.all().aggregate(total=Sum('amount'))['total'] or 0
        commission_sum = CommissionPayment.objects.all().aggregate(total=Sum('amount'))['total'] or 0 
        sale_payments = InvoicePayment.objects.all().aggregate(total=Sum('amount'))['total'] or 0
    else:
        expense_costs = 0
        shipment_costs = 0
        commission_sum = 0
        sale_payments = 0

    if currency.code == 'USD':
        supplier_sum = SupplierPayment.objects.all().aggregate(total=Sum('amount'))['total'] or 0
    else:
        supplier_sum = 0
    return bought_sum + deposits_sum + sale_payments - (sold_sum + withdrawals_sum + shipment_costs + supplier_sum + expense_costs + commission_sum)

def financial_dashboard(request):
    """
    Main dashboard view.
    Shows all supported currencies, even if no balance record exists yet.
    """
    currencies = Currency.objects.all()
    balances_sdg = []
    balances_total_sdg = 0
    for currency in currencies:
        # Use reliable calculation
        balance = calculate_company_balance(currency)
        sdg_equiv = convert_to_sdg(balance, currency.code)
        balances_sdg.append({
            'currency': currency.code,
            'balance': balance,
            'sdg_equiv': sdg_equiv
        })
        balances_total_sdg += sdg_equiv
    purchases = CurrencyExchange.objects.select_related('bought_currency').all()
    # purchase_summary = purchases.values('bought_currency__code').annotate(
    #     total_amount=Sum('bought_amount'),
    #     total_sdg_cost=Sum(models.F('bought_amount') * models.F('exchange_rate'))
    # )
    # partners = Partner.objects.all()
    # partner_withdrawals = PartnerTransaction.objects.filter(transaction_type='withdrawal').aggregate(total_withdrawn=Sum('amount'))
    # withdrawals_per_partner = {
    #     p.id: PartnerTransaction.objects.filter(partner=p, transaction_type='withdrawal').aggregate(total=Sum('amount'))['total'] or 0
    #     for p in partners
    # }
    # # Add: deposits per partner
    # deposits_per_partner = {
    #     p.id: PartnerTransaction.objects.filter(partner=p, transaction_type='deposit').aggregate(total=Sum('amount'))['total'] or 0
    #     for p in partners
    # }
    # logs = FinancialLog.objects.order_by('-timestamp')[:50]
    return render(request, 'finance/financial_dashboard.html', {
        'balances_sdg': balances_sdg,
        'balances_total_sdg': balances_total_sdg,
        # 'purchase_summary': purchase_summary,
        # 'partners': partners,
        # 'partner_withdrawals': partner_withdrawals,
        # 'withdrawals_per_partner': withdrawals_per_partner,
        # 'deposits_per_partner': deposits_per_partner,
        # 'show_currency_purchases_link': True,
        'active_sidebar': 'finance_dashboard',
        # 'logs': logs,
    })



def partners_list(request):
    partners = Partner.objects.all()
    # Fetch all balances for all partners and currencies
    partner_balances = {}
    for partner in partners:
        partner_balances[partner.id] = partner.get_all_balances()     

    return render(request, 'finance/partners_list.html', {
        'partners': partners,
        'partner_balances': partner_balances,
        'active_sidebar': 'partners_list'
    })

def partner_add(request):
    if request.method == 'POST':
        form = PartnerForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('partners_list')
    else:
        form = PartnerForm()
    return render(request, 'finance/partner_form.html', {'form': form, 'active_sidebar': 'partners_list'})

def partner_edit(request, partner_id):
    partner = get_object_or_404(Partner, pk=partner_id)
    if request.method == 'POST':
        form = PartnerForm(request.POST, instance=partner)
        if form.is_valid():
            form.save()
            return redirect('partners_list')
    else:
        form = PartnerForm(instance=partner)
    return render(request, 'finance/partner_form.html', {'form': form, 'partner': partner, 'active_sidebar': 'partners_list'})

def partner_delete(request, partner_id):
    partner = get_object_or_404(Partner, pk=partner_id)
    if request.method == 'POST':
        partner.delete()
        return redirect('partners_list')
    return render(request, 'finance/partner_confirm_delete.html', {'partner': partner, 'active_sidebar': 'partners_list'})

def partner_transactions(request, partner_id):
    try:
        partner = Partner.objects.get(pk=partner_id)
        transactions = partner.transactions.select_related('currency').all().order_by('-pk')
        # git the balances with currency by calculating them from partner transactions model
        balances = list(transactions.values('currency__code').annotate(
            total_balance=Sum('amount', filter=Q(transaction_type='deposit')) - Sum('amount', filter=Q(transaction_type='withdrawal'))
        ))
        # balances = list(partner.balances.select_related('currency').all())
    except Partner.DoesNotExist:
        partner = None
        transactions = []
        balances = []

    # Filtering
    currency_id = request.GET.get('currency')
    tx_type = request.GET.get('transaction_type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    filtered_transactions = transactions
    if currency_id:
        filtered_transactions = filtered_transactions.filter(currency_id=currency_id)
    if tx_type:
        filtered_transactions = filtered_transactions.filter(transaction_type=tx_type)
    if date_from:
        filtered_transactions = filtered_transactions.filter(date__gte=date_from)
    if date_to:
        filtered_transactions = filtered_transactions.filter(date__lte=date_to)
    currencies = Currency.objects.all()

    # PDF download
    if request.GET.get('download') == 'pdf':
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="partner_{partner_id}_transactions.pdf"'
        doc = SimpleDocTemplate(response, pagesize=A4, rightMargin=20, leftMargin=20, topMargin=30, bottomMargin=20)
        elements = []
        styles = getSampleStyleSheet()
        title = Paragraph(f"Partner Transactions: {partner.full_name}", styles['Title'])
        elements.append(title)
        elements.append(Spacer(1, 8))
        data = [
            ["Type", "Amount", "Currency", "Date", "Note"]
        ]
        for tx in filtered_transactions:
            data.append([
                tx.get_transaction_type_display(),
                f"{int(tx.amount):,}",
                tx.currency.code,
                tx.date.strftime('%Y-%m-%d'),
                (tx.note or '')[:80]
            ])
        table = Table(data, colWidths=[30*mm, 30*mm, 30*mm, 35*mm, 60*mm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
            ('TEXTCOLOR', (0,0), (-1,0), colors.black),
            ('ALIGN', (0,0), (-2,-1), 'CENTER'),
            ('ALIGN', (4,1), (4,-1), 'RIGHT'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,0), 12),
            ('FONTSIZE', (0,1), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,0), 8),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ]))
        elements.append(table)
        doc.build(elements)
        return response

    return render(request, 'finance/partner_transactions.html', {
        'partner': partner,
        'transactions': filtered_transactions,
        'balances': balances,
        'currencies': currencies,
        'selected_currency': currency_id,
        'selected_type': tx_type,
        'date_from': date_from,
        'date_to': date_to,
        'active_sidebar': 'partners_list'
    })

@require_GET
def partner_transactions_pdf(request, partner_id):
    """
    Export the current partner transactions as a PDF for sharing/printing.
    """
    from django.template.loader import render_to_string
    from weasyprint import HTML, CSS
    import tempfile
    from datetime import date
    import io

    partner = get_object_or_404(Partner, pk=partner_id)
    transactions = partner.transactions.select_related('currency').all().order_by('-pk')

    # Filtering (same as partner_transactions)
    currency_id = request.GET.get('currency')
    tx_type = request.GET.get('transaction_type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    filtered_transactions = transactions
    if currency_id:
        filtered_transactions = filtered_transactions.filter(currency_id=currency_id)
    if tx_type:
        filtered_transactions = filtered_transactions.filter(transaction_type=tx_type)
    if date_from:
        filtered_transactions = filtered_transactions.filter(date__gte=date_from)
    if date_to:
        filtered_transactions = filtered_transactions.filter(date__lte=date_to)

    logo_url = request.build_absolute_uri('/static/logo.png')  # Adjust path as needed

    html_string = render_to_string('suppliers/supplier_transactions_pdf.html', {
        'partner': partner,
        'transactions': filtered_transactions,
        'today': date.today(),
        'logo_url': logo_url,
    })
    pdf_css = """
    body { font-family: 'Cairo', Arial, sans-serif; }
    table { width: 100%; border-collapse: collapse; margin-top: 20px; }
    th, td { border: 1px solid #333; padding: 6px; text-align: left; }
    th { background: #f8f8f8; }
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as output:
        HTML(string=html_string, base_url=request.build_absolute_uri('/')).write_pdf(
            output.name,
            stylesheets=[CSS(string=pdf_css)]
        )
        output.seek(0)
        pdf = output.read()
    from django.http import FileResponse
    filename = f"partner_{partner_id}_transactions_{date.today().isoformat()}.pdf"
    response = FileResponse(
        io.BytesIO(pdf),
        as_attachment=True,
        filename=filename
    )
    return response

def transaction_add(request, partner_id):
    partner = get_object_or_404(Partner, pk=partner_id)
    if request.method == 'POST':
        form = PartnerTransactionForm(request.POST)
        if form.is_valid():
            tx = form.save(commit=False)
            tx.partner = partner
            try:
                tx.save()
                return redirect('partner_transactions', partner_id=partner.id)
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, 'message') else e.messages)
    else:
        form = PartnerTransactionForm()
    return render(request, 'finance/transaction_form.html', {'form': form, 'partner': partner, 'active_sidebar': 'partners_list'})

def transaction_edit(request, tx_id):
    tx = get_object_or_404(PartnerTransaction, pk=tx_id)
    if request.method == 'POST':
        form = PartnerTransactionForm(request.POST, instance=tx)
        if form.is_valid():
            tx = form.save(commit=False)
            try:
                tx.save()
                return redirect('partner_transactions', partner_id=tx.partner.id)
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, 'message') else e.messages)
    else:
        form = PartnerTransactionForm(instance=tx)
    return render(request, 'finance/transaction_form.html', {'form': form, 'partner': tx, 'active_sidebar': 'partners_list'})

def transaction_delete(request, tx_id):
    tx = get_object_or_404(PartnerTransaction, pk=tx_id)
    partner_id = tx.partner.id
    if request.method == 'POST':
        tx.delete()
        return redirect('partner_transactions', partner_id=partner_id)
    return render(request, 'finance/transaction_confirm_delete.html', {'tx': tx, 'active_sidebar': 'partners_list'})

# --- Currency Purchases ---

def currency_purchases_list(request):
    # Filtering
    currency_id = request.GET.get('currency')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    purchases = CurrencyExchange.objects.select_related('bought_currency').order_by('-pk')
    if currency_id:
        purchases = purchases.filter(bought_currency__id=currency_id)
    if date_from:
        purchases = purchases.filter(date__gte=date_from)
    if date_to:
        purchases = purchases.filter(date__lte=date_to)
    currencies = Currency.objects.all()

    # PDF download
    if request.GET.get('download') == 'pdf':
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="currency_purchases.pdf"'
        doc = SimpleDocTemplate(response, pagesize=A4, rightMargin=20, leftMargin=20, topMargin=30, bottomMargin=20)
        elements = []
        styles = getSampleStyleSheet()
        title = Paragraph("Currency Purchases Log", styles['Title'])
        elements.append(title)
        elements.append(Spacer(1, 8))
        data = [
            ["Currency", "Amount", "Exchange Rate", "Date", "Note"]
        ]
        for purchase in purchases:
            data.append([
                str(purchase.bought_currency.code),
                f"{int(purchase.bought_amount):,}",
                f"{int(purchase.exchange_rate):,}",
                purchase.date.strftime('%Y-%m-%d'),
                (purchase.note or '')[:80]
            ])
        table = Table(data, colWidths=[30*mm, 30*mm, 30*mm, 35*mm, 60*mm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
            ('TEXTCOLOR', (0,0), (-1,0), colors.black),
            ('ALIGN', (0,0), (-2,-1), 'CENTER'),
            ('ALIGN', (4,1), (4,-1), 'RIGHT'),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
            ('FONTSIZE', (0,0), (-1,0), 12),
            ('FONTSIZE', (0,1), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,0), 8),
            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ]))
        elements.append(table)
        doc.build(elements)
        return response

    return render(request, 'finance/currency_purchases_list.html', {
        'purchases': purchases,
        'currencies': currencies,
        'active_sidebar': 'currency_purchases',
        'selected_currency': currency_id,
        'date_from': date_from,
        'date_to': date_to,
    })

def currency_purchase_add(request):
    if request.method == 'POST':
        form = CurrencyPurchaseForm(request.POST)
        if form.is_valid():
            # ensure sold amount is existed in the company balances
            sold_currency = form.cleaned_data['sold_currency']
            sold_amount = form.cleaned_data['sold_amount']
            if sold_currency and sold_amount:
                balance = calculate_company_balance(sold_currency)
                if balance < sold_amount:
                    form.add_error('sold_amount', f"Insufficient balance for {sold_currency.code}. Available: {balance}")
                    return render(request, 'finance/currency_purchase_form.html', {'form': form, 'active_sidebar': 'currency_purchases'})
            # calculate exchange rate
            bought_currency = form.cleaned_data['bought_currency']
            bought_amount = form.cleaned_data['bought_amount']
            if bought_currency and bought_amount and sold_currency and sold_amount:
                exchange_rate =  sold_amount /  bought_amount
                form.instance.exchange_rate = exchange_rate
            form.save()
            return redirect('currency_purchases_list')
    else:
        form = CurrencyPurchaseForm()
    return render(request, 'finance/currency_purchase_form.html', {'form': form, 'active_sidebar': 'currency_purchases'})

def currency_purchase_edit(request, purchase_id):
    purchase = get_object_or_404(CurrencyExchange, pk=purchase_id)
    if request.method == 'POST':
        form = CurrencyPurchaseForm(request.POST, instance=purchase)
        if form.is_valid():
            form.save()
            return redirect('currency_purchases_list')
    else:
        form = CurrencyPurchaseForm(instance=purchase)
    return render(request, 'finance/currency_purchase_form.html', {'form': form, 'purchase': purchase, 'active_sidebar': 'currency_purchases'})

def currency_purchase_delete(request, purchase_id):
    purchase = get_object_or_404(CurrencyExchange, pk=purchase_id)
    if request.method == 'POST':
        purchase.delete()
        return redirect('currency_purchases_list')
    return render(request, 'finance/currency_purchase_confirm_delete.html', {'purchase': purchase, 'active_sidebar': 'currency_purchases'})
