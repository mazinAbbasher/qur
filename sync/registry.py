"""The single source of truth for *what* syncs and *who* may see it.

For each syncable model we record:

* ``category``               - 'reference' (authored on the server, pulled down)
                               or 'transactional' (authored on a laptop, pushed up).
* ``salesperson_readable``   - may the server send this model to a *salesperson*
                               laptop during pull? (Managers always receive all.)
* ``salesperson_writable``   - may a *salesperson* laptop push this model up?
* ``sensitive_fields``       - fields stripped before sending to a salesperson,
                               even when the row itself is readable (e.g. purchase
                               costs on a shipment, commission % on an employee).

``SYNC_ORDER`` lists models parents-first so the apply step can insert a row
only after the rows it points at already exist (and delete in reverse).

This module contains **policy only** — no database access at import time — so it
is safe to import from migrations, views and the engine alike.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SyncSpec:
    label: str                       # "app_label.ModelName" (lowercased app on load)
    category: str                    # 'reference' | 'transactional'
    salesperson_readable: bool
    salesperson_writable: bool
    sensitive_fields: tuple = field(default_factory=tuple)


# Parents-first order. Upserts apply top-to-bottom; deletes apply bottom-to-top.
_SPECS = [
    # ---- Reference data (server is the authority; laptops pull it) ----------
    SyncSpec('panel.Area', 'reference', salesperson_readable=True, salesperson_writable=False),
    SyncSpec('panel.Supplier', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.Employee', 'reference', salesperson_readable=True, salesperson_writable=False,
             sensitive_fields=('commission_percentage', 'sales_target')),
    SyncSpec('panel.Manager', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.ExchangeRate', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.Product', 'reference', salesperson_readable=True, salesperson_writable=False),
    SyncSpec('panel.Shipment', 'reference', salesperson_readable=True, salesperson_writable=False,
             sensitive_fields=('cost_usd', 'cost_sdg', 'shipment_cost', 'supplier')),
    SyncSpec('panel.Inventory', 'reference', salesperson_readable=True, salesperson_writable=False),
    SyncSpec('panel.LostProduct', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.Expense', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.Commission', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.CommissionPayment', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.ManagerCommissionPayment', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('panel.SupplierPayment', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('finance.Currency', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('finance.Partner', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('finance.CurrencyExchange', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('finance.PartnerTransaction', 'reference', salesperson_readable=False, salesperson_writable=False),
    SyncSpec('finance.FinancialLog', 'reference', salesperson_readable=False, salesperson_writable=False),

    # ---- Transactional data (authored on laptops; pushed up) ----------------
    # Clients are shared: salespeople both read (to pick one) and write (add/edit).
    SyncSpec('panel.Client', 'transactional', salesperson_readable=True, salesperson_writable=True),
    # A salesperson only PUSHES these; they are not sent back down to salesperson
    # laptops (avoids exposing other reps' sales/totals). Managers pull them all.
    SyncSpec('panel.Sale', 'transactional', salesperson_readable=False, salesperson_writable=True),
    SyncSpec('panel.SaleItem', 'transactional', salesperson_readable=False, salesperson_writable=True),
    SyncSpec('panel.ReturnedProduct', 'transactional', salesperson_readable=False, salesperson_writable=True),
    SyncSpec('panel.Invoice', 'transactional', salesperson_readable=False, salesperson_writable=True),
    SyncSpec('panel.InvoicePayment', 'transactional', salesperson_readable=False, salesperson_writable=True),
]

# Public: labels in dependency order.
SYNC_ORDER = tuple(spec.label for spec in _SPECS)

_BY_LABEL = {spec.label: spec for spec in _SPECS}


def all_specs():
    """All specs in parents-first (dependency) order."""
    return list(_SPECS)


def get_spec(label):
    return _BY_LABEL.get(label)


def is_syncable(label):
    return label in _BY_LABEL


def specs_for_pull(role):
    """Models the server may send to a node of ``role`` during pull."""
    if role == 'salesperson':
        return [s for s in _SPECS if s.salesperson_readable]
    # manager (and any trusted node) receives everything.
    return list(_SPECS)


def specs_for_push(role):
    """Models a node of ``role`` is permitted to push up to the server."""
    if role == 'salesperson':
        return [s for s in _SPECS if s.salesperson_writable]
    return list(_SPECS)


def sensitive_fields_for(label, role):
    """Field names to strip from a row before sending it to ``role``."""
    spec = _BY_LABEL.get(label)
    if spec is None or role != 'salesperson':
        return ()
    return spec.sensitive_fields
