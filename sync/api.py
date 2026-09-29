"""Server-side sync API — the endpoints laptops call.

Authentication is a per-node secret token (header ``X-Sync-Token`` or a
``token`` field in the JSON body). These endpoints live under ``/sync/api/`` and
are exempt from the session-login middleware because they authenticate
themselves. They are the ONLY writable surface for a laptop, and they re-enforce
the same role policy as the UI:

* ``pull`` only returns models the node's role may read, with sensitive fields
  already stripped for salespeople.
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
from .registry import sensitive_fields_for, specs_for_push


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
        message=f'served {len(rows)} rows since {since_raw or "beginning"}',
    )
    return JsonResponse({
        'ok': True, 'changes': rows, 'cursor': cursor,
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
