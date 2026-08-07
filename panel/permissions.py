"""Role helpers, view decorators and the salesperson access policy.

Two roles exist, implemented with Django auth Groups so no custom user model
is required:

  * ``manager``     - full access (also implicitly any superuser).
  * ``salesperson`` - restricted: may create sales, manage clients, record
                      payments/returns and view inventory/prices, but must NOT
                      see purchase costs, profit, commissions or finance data.

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
# (costs, profit, commissions, expenses and the whole finance app) stay
# manager-only. This is a blocklist: anything not listed here is allowed, so
# salespeople can manage sales, clients, products, inventory, shipments,
# employees, managers, suppliers, areas and lost products — but the cost/profit
# figures inside those pages are still hidden (templates) and never reach a
# salesperson's laptop (sync strips them).
MANAGER_ONLY_VIEWS = {
    # Financial dashboards / profit reports
    'panel:index',                       # the net-profit / totals dashboard
    'panel:net_profit_dashboard',
    'panel:shipment_profit_report',
    # Commissions (staff pay)
    'panel:sale_commissions',
    'panel:commission_pay',
    'panel:manager_commission_pay',
    'panel:get_employee_commission',
    # Expenses (company spending)
    'panel:expense_list',
    'panel:expense_add',
    'panel:expense_edit',
    'panel:expense_delete',
    'panel:expense_list_pdf',
    # Purchasing & staff/pay records are VIEW-ONLY for reps (they can open the
    # lists/details, with cost/commission columns hidden, but not create/edit —
    # those carry financial data and are manager functions).
    # Products are catalog/reference data (exchange rate drives cost conversion) —
    # reps view but don't edit.
    'panel:product_add', 'panel:product_edit', 'panel:product_delete',
    'panel:product_stock_movement', 'panel:product_stock_movement_pdf',
    'panel:shipment_create', 'panel:shipment_edit', 'panel:shipment_delete',
    'panel:employee_add', 'panel:employee_edit', 'panel:employee_delete',
    'panel:manager_add', 'panel:manager_edit', 'panel:manager_delete',
    'panel:supplier_add', 'panel:supplier_edit', 'panel:supplier_delete',
    'panel:supplier_add_payment',
}

# Whole URL trees that are manager-only (the finance app: balances, partners,
# currency exchange).
MANAGER_ONLY_PREFIXES = ('/finance/',)


def salesperson_can_access(view_name, path=''):
    """Default-allow: a salesperson may reach anything not explicitly financial."""
    if any(path.startswith(p) for p in MANAGER_ONLY_PREFIXES):
        return False
    return view_name not in MANAGER_ONLY_VIEWS
