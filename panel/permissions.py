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


# View names (namespaced) a salesperson is explicitly allowed to reach.
# Everything else under panel/finance is manager-only.
SALESPERSON_ALLOWED_VIEWS = {
    # Sales workflow
    'panel:sale_list',
    'panel:sale_create',
    'panel:sale_detail',
    'panel:sale_edit',
    'panel:sale_return_product',
    'panel:sale_list_pdf',
    # Clients (add & edit)
    'panel:client_list',
    'panel:client_add',
    'panel:client_edit',
    'panel:client_detail',
    'panel:client_pdf',
    'panel:client_list_pdf',
    # Invoices & payments
    'panel:invoice_list',
    'panel:invoice_detail',
    'panel:invoice_add_payment',
    'panel:invoice_pdf',
    # Read-only reference data (no cost fields rendered for salespeople)
    'panel:product_list',
    'panel:product_detail',
    'panel:inventory_list',
    'panel:area_list',
    # Areas needed when adding a client
    'panel:clients_by_area',
    # Sync: a salesperson may upload their own data / check status
    'sync:status',
    'sync:run',
}


def salesperson_can_access(view_name):
    return view_name in SALESPERSON_ALLOWED_VIEWS
