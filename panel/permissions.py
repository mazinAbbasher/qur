"""Role helpers, view decorators and the salesperson access policy.

Two roles exist, implemented with Django auth Groups so no custom user model
is required:

  * ``manager``     - full access (also implicitly any superuser).
  * ``salesperson`` - restricted: may create, edit and delete sales, manage
                      clients, record and correct payments, record returns,
                      record currency exchanges, manage commissions (staff
                      and their percentages, commission payments) and view
                      inventory/prices, but must NOT see purchase costs,
                      profit or finance data (company balances, partners).

Access is enforced *default-deny* for salespeople in
``cafe.middleware.AccessControlMiddleware`` using the allowlist below, so a
newly added sensitive view is manager-only unless it is explicitly opted in.
"""

from functools import wraps

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

MANAGER_GROUP = 'manager'
SALESPERSON_GROUP = 'salesperson'


def is_manager(user):
    """True for superusers and members of the manager group."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=MANAGER_GROUP).exists()


def is_salesperson(user):
    """True for a logged-in, non-manager user (explicit group or by default).

    Any authenticated non-manager is treated as a salesperson so access is
    never accidentally elevated.
    """
    if not user or not user.is_authenticated:
        return False
    return not is_manager(user)


def role_context(request):
    """Template context processor exposing role flags to every template."""
    user = getattr(request, 'user', None)
    return {
        'is_manager': is_manager(user),
        'is_salesperson': is_salesperson(user),
    }


def manager_required(view_func):
    """Decorator: allow only managers/superusers, else raise 403.

    The middleware already enforces the policy globally; this is defence in
    depth for views that must never be reachable by a salesperson.
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_manager(request.user):
            raise PermissionDenied("هذه الصفحة متاحة للإدارة فقط.")
        return view_func(request, *args, **kwargs)

    return _wrapped


# Salespeople get broad operational access. Only *purely financial* pages
# (net profit, profit reports and the finance app apart from currency
# exchange) stay manager-only. This is a blocklist: anything not listed here
# is allowed, so salespeople can manage sales, clients, products, inventory,
# shipments, employees, managers, commissions, suppliers, areas, lost
# products, their own daily expenses and currency exchanges — but the
# cost/profit figures inside those pages are still hidden (templates) and
# never reach a salesperson's laptop (sync strips them).
#
# Commissions (staff pay) are NOT here: reps view them, pay them, correct the
# payments and set staff percentages. On a rep laptop the payments go through
# the server, which checks the company's cash without showing it to them
# (see sync.client.submit_commission_action).
MANAGER_ONLY_VIEWS = {
    # Financial dashboards / profit reports
    'panel:index',                       # the net-profit / totals dashboard
    'panel:net_profit_dashboard',
    'panel:shipment_profit_report',
    # NOTE: expenses are intentionally NOT here — salespeople manage their own
    # daily field expenses (add/edit/delete/list/PDF). The company-wide expense
    # total stays private because expenses aren't synced back down to reps
    # (see panel.Expense in sync/registry.py), and the SDG balance guard in
    # expense_add/expense_edit is manager-only.
    # Purchasing records are VIEW-ONLY for reps (they can open the
    # lists/details, with cost columns hidden, but not create/edit — those
    # carry financial data and are manager functions).
    # Products are catalog/reference data (exchange rate drives cost conversion) —
    # reps view but don't edit.
    'panel:product_add', 'panel:product_edit', 'panel:product_delete',
    'panel:product_stock_movement', 'panel:product_stock_movement_pdf',
    'panel:shipment_create', 'panel:shipment_edit', 'panel:shipment_delete',
    'panel:supplier_add', 'panel:supplier_edit', 'panel:supplier_delete',
    'panel:supplier_add_payment',
    # Reps correct the customer payments they record, editing or deleting one
    # at a time; wiping all of an invoice's payments at once stays a manager's.
    'panel:invoice_mark_unpaid',
}

# Whole URL trees that are manager-only (the finance app: balances, partners).
MANAGER_ONLY_PREFIXES = ('/finance/',)

# Finance pages carved out of MANAGER_ONLY_PREFIXES: reps record the currency
# exchanges they make in the field (list/add/PDF). They get only the exchange
# log and form — the company balances, partners and financial dashboard stay
# manager-only, and the balance check in currency_purchase_add never shows them
# the balance. Editing/deleting an exchange is a manager correction: a rep's
# laptop can't balance-check it or send it to the server.
SALESPERSON_FINANCE_VIEWS = {
    'currency_purchases_list',
    'currency_purchase_add',
}


def salesperson_can_access(view_name, path=''):
    """Default-allow: a salesperson may reach anything not explicitly financial."""
    if view_name in SALESPERSON_FINANCE_VIEWS:
        return True
    if any(path.startswith(p) for p in MANAGER_ONLY_PREFIXES):
        return False
    return view_name not in MANAGER_ONLY_VIEWS
