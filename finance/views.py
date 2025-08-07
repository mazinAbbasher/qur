from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Sum, Q
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from reportlab.lib.pagesizes import A4
from .models import (
    CompanyBalance, CurrencyPurchase, Partner, PartnerTransaction, Currency, FinancialLog, convert_to_sdg
)
from django.db import models
from .forms import PartnerForm, PartnerTransactionForm, CurrencyPurchaseForm

# --- Company Balances and Dashboard ---

def calculate_company_balance(currency):
    # Sum of all purchases for this currency
    purchases_sum = CurrencyPurchase.objects.filter(currency=currency).aggregate(total=Sum('amount'))['total'] or 0
    # Sum of all partner deposits for this currency
    deposits_sum = PartnerTransaction.objects.filter(currency=currency, transaction_type='deposit').aggregate(total=Sum('amount'))['total'] or 0
    # Sum of all partner withdrawals for this currency
    withdrawals_sum = PartnerTransaction.objects.filter(currency=currency, transaction_type='withdrawal').aggregate(total=Sum('amount'))['total'] or 0
    # Company balance = purchases + deposits - withdrawals
    return purchases_sum + deposits_sum - withdrawals_sum

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
    purchases = CurrencyPurchase.objects.select_related('currency').all()
    purchase_summary = purchases.values('currency__code').annotate(
        total_amount=Sum('amount'),
        total_sdg_cost=Sum(models.F('amount') * models.F('exchange_rate'))
    )
    partners = Partner.objects.all()
    partner_withdrawals = PartnerTransaction.objects.filter(transaction_type='withdrawal').aggregate(total_withdrawn=Sum('amount'))
    withdrawals_per_partner = {
        p.id: PartnerTransaction.objects.filter(partner=p, transaction_type='withdrawal').aggregate(total=Sum('amount'))['total'] or 0
        for p in partners
    }
    # Add: deposits per partner
    deposits_per_partner = {
        p.id: PartnerTransaction.objects.filter(partner=p, transaction_type='deposit').aggregate(total=Sum('amount'))['total'] or 0
        for p in partners
    }
    logs = FinancialLog.objects.order_by('-timestamp')[:50]
    return render(request, 'finance/financial_dashboard.html', {
        'balances_sdg': balances_sdg,
        'balances_total_sdg': balances_total_sdg,
        'purchase_summary': purchase_summary,
        'partners': partners,
        'partner_withdrawals': partner_withdrawals,
        'withdrawals_per_partner': withdrawals_per_partner,
        'deposits_per_partner': deposits_per_partner,
        'show_currency_purchases_link': True,
        'active_sidebar': 'finance_dashboard',
        'logs': logs,
    })

def company_balances(request):
    """
    View for company balances by currency, with SDG equivalent.
    Shows all supported currencies, even if no balance record exists yet.
    """
    currencies = Currency.objects.all()
    balances_sdg = []
    for currency in currencies:
        # Use reliable calculation
        balance = calculate_company_balance(currency)
        sdg_equiv = convert_to_sdg(balance, currency.code)
        balances_sdg.append({
            'currency': f"{currency.code} - {currency.name}",
            'balance': balance,
            'sdg_equiv': sdg_equiv
        })
    return render(request, 'finance/company_balances.html', {
        'balances_sdg': balances_sdg,
        'show_currency_purchases_link': True,
        'active_sidebar': 'company_balances',
    })

# --- Partner Management ---

def partners_list(request):
    partners = Partner.objects.all()
    # Fetch all balances for all partners and currencies
    partner_balances = {}
    for partner in partners:
        partner_balances[partner.id] = list(partner.balances.select_related('currency').all())
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
        balances = list(partner.balances.select_related('currency').all())
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
    purchases = CurrencyPurchase.objects.select_related('currency').order_by('-date')
    if currency_id:
        purchases = purchases.filter(currency_id=currency_id)
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
                str(purchase.currency.code),
                f"{int(purchase.amount):,}",
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
            form.save()
            return redirect('currency_purchases_list')
    else:
        form = CurrencyPurchaseForm()
    return render(request, 'finance/currency_purchase_form.html', {'form': form, 'active_sidebar': 'currency_purchases'})

def currency_purchase_edit(request, purchase_id):
    purchase = get_object_or_404(CurrencyPurchase, pk=purchase_id)
    if request.method == 'POST':
        form = CurrencyPurchaseForm(request.POST, instance=purchase)
        if form.is_valid():
            form.save()
            return redirect('currency_purchases_list')
    else:
        form = CurrencyPurchaseForm(instance=purchase)
    return render(request, 'finance/currency_purchase_form.html', {'form': form, 'purchase': purchase, 'active_sidebar': 'currency_purchases'})

def currency_purchase_delete(request, purchase_id):
    purchase = get_object_or_404(CurrencyPurchase, pk=purchase_id)
    if request.method == 'POST':
        purchase.delete()
        return redirect('currency_purchases_list')
    return render(request, 'finance/currency_purchase_confirm_delete.html', {'purchase': purchase, 'active_sidebar': 'currency_purchases'})
