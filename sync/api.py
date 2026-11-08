"""Server-side sync API — the endpoints laptops call.

Authentication is a per-node secret token (header ``X-Sync-Token`` or a
``token`` field in the JSON body). These endpoints live under ``/sync/api/`` and
are exempt from the session-login middleware because they authenticate
themselves. They are the ONLY writable surface for a laptop, and they re-enforce
the same role policy as the UI:

* ``pull`` only returns models the node's role may read, with sensitive fields
  already stripped for salespeople (deletions go to every node: a tombstone
  carries no data).
* ``push`` only applies models the node's role may write; anything else is
  rejected server-side regardless of what the client sent.
"""

import json

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .engine import apply_batch, collect_server_changes
from .models import Node, SyncLog
from .registry import pull_policy, sensitive_fields_for, specs_for_push


def _get_token(request, body):
    return (
        request.headers.get('X-Sync-Token')
        or (body or {}).get('token')
        or request.GET.get('token')
    )


def _authenticate(request, body):
    token = _get_token(request, body)
    if not token:
        return None
    node = Node.objects.filter(token=token, is_active=True).first()
    if node:
        node.last_seen = timezone.now()
        node.save(update_fields=['last_seen'])
    return node


def _load_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return None


@csrf_exempt
@require_POST
def api_pull(request):
    body = _load_body(request)
    if body is None:
        return JsonResponse({'ok': False, 'error': 'invalid_json'}, status=400)
    node = _authenticate(request, body)
    if node is None:
        return JsonResponse({'ok': False, 'error': 'unauthorized'}, status=401)

    since_raw = body.get('since')
    since = parse_datetime(since_raw) if since_raw else None
    # The laptop reports the policy its cursor was earned under. If what its
    # role may read has changed since (e.g. sales became visible to
    # salespeople), that cursor is past rows it was never sent: resend
    # everything once. Laptops too old to report one stay incremental.
    policy = pull_policy(node.role)
    resend_all = body.get('policy') is not None and body['policy'] != policy
    if resend_all:
        since, since_raw = None, None
    try:
        rows, cursor = collect_server_changes(node.role, since)
    except Exception as exc:  # never leak an HTML 500 to the client
        SyncLog.objects.create(
            node_name=node.name, direction='pull', finished_at=timezone.now(),
            ok=False, message=f'{type(exc).__name__}: {exc}',
        )
        return JsonResponse({'ok': False, 'error': 'server_error'}, status=500)

    SyncLog.objects.create(
        node_name=node.name, direction='pull', finished_at=timezone.now(),
        pulled=len(rows), ok=True,
        message=(f'served {len(rows)} rows since {since_raw or "beginning"}'
                 + (' (full resend: sync policy changed)' if resend_all else '')),
    )
    return JsonResponse({
        'ok': True, 'changes': rows, 'cursor': cursor, 'policy': policy,
        'server_time': timezone.now().isoformat(),
    })


@csrf_exempt
@require_POST
def api_push(request):
    body = _load_body(request)
    if body is None:
        return JsonResponse({'ok': False, 'error': 'invalid_json'}, status=400)
    node = _authenticate(request, body)
    if node is None:
        return JsonResponse({'ok': False, 'error': 'unauthorized'}, status=401)

    changes = body.get('changes') or []
    allowed = {s.label for s in specs_for_push(node.role)}
    accepted = [r for r in changes if r.get('label') in allowed]
    rejected = [r.get('label') for r in changes if r.get('label') not in allowed]

    # Defence in depth: never let a node write fields its role may not hold
    # (e.g. a tampered client trying to null out purchase costs).
    for r in accepted:
        strip = sensitive_fields_for(r.get('label'), node.role)
        if strip and r.get('fields'):
            for f in strip:
                r['fields'].pop(f, None)

    # The server is authoritative for stock, so recompute after applying.
    # The whole batch is atomic: on error nothing is applied and the client
    # keeps its outbox for a safe retry.
    try:
        stats = apply_batch(
            accepted, is_pull=False, node_name=node.name,
            authoritative_inventory=True,
            node_computes_commissions=node.role != 'salesperson',
        )
    except Exception as exc:
        SyncLog.objects.create(
            node_name=node.name, direction='push', finished_at=timezone.now(),
            ok=False, message=f'{type(exc).__name__}: {exc}',
        )
        return JsonResponse({'ok': False, 'error': 'server_error'}, status=500)

    SyncLog.objects.create(
        node_name=node.name, direction='push', finished_at=timezone.now(),
        pushed=stats['applied'], conflicts=stats['conflicts'], ok=True,
        message=(f"applied={stats['applied']} noop={stats['noop']} "
                 f"conflicts={stats['conflicts']} rejected={len(rejected)}"),
    )
    return JsonResponse({
        'ok': True,
        'applied': stats['applied'],
        'noop': stats['noop'],
        'conflicts': stats['conflicts'],
        'oversells': stats['oversells'],
        'rejected': sorted(set(rejected)),
        'server_time': timezone.now().isoformat(),
    })


@csrf_exempt
@require_POST
def api_currency_exchange(request):
    """Record one currency exchange from a laptop — only if the company holds
    enough of the sold currency.

    A salesperson laptop can't check the company balance itself (finance data
    isn't synced to reps), and exchanges aren't accepted through ``push`` from
    them, so this is the only way a rep's exchange reaches the server. The
    response never includes the balance: a shortfall is just a 409.
    """
    from decimal import Decimal, InvalidOperation

    from finance.models import Currency, CurrencyExchange
    from finance.views import calculate_company_balance

    body = _load_body(request)
    if body is None:
        return JsonResponse({'ok': False, 'error': 'invalid_json'}, status=400)
    node = _authenticate(request, body)
    if node is None:
        return JsonResponse({'ok': False, 'error': 'unauthorized'}, status=401)

    row = body.get('row') or {}
    fields = row.get('fields') or {}
    if row.get('label') != 'finance.CurrencyExchange' or row.get('is_deleted'):
        return JsonResponse({'ok': False, 'error': 'invalid_row'}, status=400)
    # A retry of an exchange we already recorded (the laptop lost our reply).
    if CurrencyExchange.objects.filter(sync_id=row.get('sync_id')).exists():
        return JsonResponse({'ok': True})
    try:
        sold_amount = Decimal(str(fields['sold_amount']))
    except (KeyError, InvalidOperation):
        return JsonResponse({'ok': False, 'error': 'invalid_row'}, status=400)

    with transaction.atomic():
        # Lock the sold currency so two reps can't both spend the same balance.
        sold = (Currency.objects.select_for_update()
                .filter(sync_id=fields.get('sold_currency')).first())
        if sold is None:
            return JsonResponse({'ok': False, 'error': 'invalid_row'}, status=400)
        if calculate_company_balance(sold) < sold_amount:
            return JsonResponse({'ok': False, 'error': 'insufficient_balance'}, status=409)
        stats = apply_batch([row], is_pull=False, node_name=node.name)
        if stats['applied'] != 1:
            transaction.set_rollback(True)
            return JsonResponse({'ok': False, 'error': 'invalid_row'}, status=400)

    SyncLog.objects.create(
        node_name=node.name, direction='push', finished_at=timezone.now(),
        pushed=1, ok=True, message=f'currency exchange {row.get("sync_id")}',
    )
    return JsonResponse({'ok': True})


# What each commission-payment action is carried out on (see
# panel.views.commission_action): the payee to pay, or the payment to change.
_COMMISSION_TARGETS = {
    'pay': ('panel.Employee', 'panel.Manager'),
    'edit': ('panel.CommissionPayment', 'panel.ManagerCommissionPayment'),
    'delete': ('panel.CommissionPayment', 'panel.ManagerCommissionPayment'),
}


def _commission_data(data):
    """The amount, month, year and note a pay or edit was sent with, as
    panel.views.commission_action takes them; None when they don't parse."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(data['amount']))
        month, year = int(data['month']), int(data['year'])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not amount.is_finite() or not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return None
    return {'amount': amount, 'month': month, 'year': year,
            'note': str(data.get('note') or '')[:255]}


@csrf_exempt
@require_POST
def api_commission_payment(request):
    """Pay a commission, or edit or delete a commission payment, for a laptop.

    A salesperson laptop holds commissions only as it last pulled them, and
    none of the company's cash a payment comes out of, so it can't do this
    itself (reps never push commission rows). The server does it as its own
    pages would, against its own figures, and replies with the message for
    the rep's page: a refusal is a 409 whose message never shows the balance.
    The laptop pulls the result afterwards.
    """
    from finance.models import Currency
    from panel.views import commission_action

    from .serializers import get_model

    body = _load_body(request)
    if body is None:
        return JsonResponse({'ok': False, 'error': 'invalid_json'}, status=400)
    node = _authenticate(request, body)
    if node is None:
        return JsonResponse({'ok': False, 'error': 'unauthorized'}, status=401)

    action = body.get('action')
    target = body.get('target') or {}
    label = target.get('label')
    if label not in _COMMISSION_TARGETS.get(action, ()):
        return JsonResponse({'ok': False, 'error': 'invalid_action'}, status=400)
    data = None
    if action != 'delete':
        data = _commission_data(body.get('data') or {})
        if data is None:
            return JsonResponse({'ok': False, 'error': 'invalid_data'}, status=400)
    try:
        obj = get_model(label).objects.filter(sync_id=target.get('sync_id')).first()
    except Exception:  # not a valid sync_id
        return JsonResponse({'ok': False, 'error': 'invalid_action'}, status=400)
    if obj is None:
        if action == 'delete':  # a retry, or someone else deleted it first
            return JsonResponse({'ok': True, 'message': "دفعة العمولة محذوفة بالفعل."})
        return JsonResponse({'ok': False, 'error': "لم يُعثر على السجل على الخادم. "
                                                   "قم بمزامنة البيانات ثم حاول مرة أخرى."},
                            status=409)

    with transaction.atomic():
        # Lock the SDG cash it is paid from, so two payments (or a payment and
        # a currency exchange) can't both spend the same balance.
        Currency.objects.select_for_update().filter(code='SDG').first()
        ok, message = commission_action(action, obj, data, show_balance=False)
    if not ok:
        return JsonResponse({'ok': False, 'error': message}, status=409)

    SyncLog.objects.create(
        node_name=node.name, direction='push', finished_at=timezone.now(),
        pushed=1, ok=True, message=f'commission {action} {label} {target.get("sync_id")}',
    )
    return JsonResponse({'ok': True, 'message': message})
