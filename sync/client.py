"""Laptop-side sync client — talks to the central server over HTTPS.

Used by the "Sync Data" / "Upload" buttons and the ``sync`` management command.

``run_sync()`` performs push-then-pull inside one logged run:

1. **Push** every locally-changed row (from the outbox) up to the server. On
   success those outbox entries are cleared. Re-running is safe: the server
   upserts by ``sync_id`` so nothing is duplicated.
2. **Pull** everything changed on the server since our cursor and apply it
   locally without clobbering un-pushed local edits.

All network calls have a timeout; any failure is caught and recorded in a
``SyncLog`` so the UI can show what happened, and nothing is left half-applied
(each apply runs in a single transaction).
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .engine import apply_batch
from .models import SyncLog, SyncOutbox, SyncState
from .serializers import get_model, serialize_instance


class SyncNotConfigured(Exception):
    pass


def _config():
    url = getattr(settings, 'SYNC_SERVER_URL', '')
    token = getattr(settings, 'SYNC_NODE_TOKEN', '')
    if not url or not token:
        raise SyncNotConfigured(
            "Set SYNC_SERVER_URL and SYNC_NODE_TOKEN (see .env.example)."
        )
    return url, token


def _post(path, payload):
    import requests  # imported lazily so a standalone install needn't have it

    url, token = _config()
    timeout = getattr(settings, 'SYNC_HTTP_TIMEOUT', 30)
    resp = requests.post(
        f"{url}{path}", json=payload,
        headers={'X-Sync-Token': token, 'Content-Type': 'application/json'},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def build_push_payload():
    """Serialize outbox rows to send. Returns (rows, outbox_entry_pks).

    Serialized with THIS node's own role, so a salesperson laptop never even
    sends fields it isn't allowed to hold (it can't null out server-side costs).
    """
    role = 'salesperson' if getattr(settings, 'SYNC_ROLE', '') == 'salesperson' else 'manager'
    rows, pks = [], []
    for entry in SyncOutbox.objects.all().order_by('enqueued_at'):
        model = get_model(entry.model_label)
        inst = model.objects.filter(sync_id=entry.sync_id).first()
        if entry.deleted or inst is None:
            rows.append({
                'label': entry.model_label, 'sync_id': str(entry.sync_id),
                'is_deleted': True, 'fields': {},
            })
        else:
            rows.append(serialize_instance(inst, role=role))
        pks.append(entry.pk)
    return rows, pks


def push():
    rows, pks = build_push_payload()
    if not rows:
        return {'ok': True, 'applied': 0, 'noop': 0, 'conflicts': 0, 'rejected': []}
    data = _post('/sync/api/push/', {'changes': rows})
    if data.get('ok'):
        # Clear exactly the entries we sent (new edits enqueued meanwhile stay).
        SyncOutbox.objects.filter(pk__in=pks).delete()
        state = SyncState.get()
        state.last_pushed_at = timezone.now()
        state.save(update_fields=['last_pushed_at'])
    return data


def submit_currency_exchange(exchange):
    """Salesperson laptop: have the server check the company balance and record
    ``exchange`` (an unsaved CurrencyExchange). Returns None on success, else an
    error message for the form — never the balance itself.

    Our pending outbox is pushed first so this rep's own collected payments
    count toward the balance. On success the exchange is saved locally under the
    apply guard: the server already holds it, so it must not be queued for push
    (reps can't push exchanges — see sync/registry.py).
    """
    import requests

    from .tracking import apply_guard

    try:
        push()
        _post('/sync/api/currency-exchange/',
              {'row': serialize_instance(exchange, role='salesperson')})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            return (f"رصيد الشركة من {exchange.sold_currency.code} "
                    f"غير كافٍ لإتمام عملية التبديل.")
        return "تعذّر تسجيل العملية على الخادم. حاول مرة أخرى."
    except Exception:  # offline / not configured / timeout
        return ("تعذّر الاتصال بالخادم للتحقق من الرصيد. "
                "تأكد من الاتصال بالإنترنت ثم حاول مرة أخرى.")
    with apply_guard():
        exchange.save()
    return None


def _clamped_cursor(server_cursor, deferred_min):
    """Keep the pull cursor from advancing past a row we couldn't apply yet.

    A row whose foreign key isn't present is logged as a conflict, not applied.
    If the cursor jumped past it, the server (which only re-sends rows newer than
    the cursor) would never offer it again — a silent, permanent skip. Clamping
    to just before that row makes the next pull re-deliver it once its parent
    exists; re-applying already-applied rows is a safe idempotent noop.
    """
    if deferred_min is None:
        return server_cursor
    gap = deferred_min - timedelta(microseconds=1)
    if server_cursor is None or gap < server_cursor:
        return gap
    return server_cursor


def pull():
    state = SyncState.get()
    since = state.last_pull_cursor
    data = _post('/sync/api/pull/', {'since': since.isoformat() if since else None})
    rows = data.get('changes', [])
    stats = apply_batch(rows, is_pull=True, node_name=getattr(settings, 'SYNC_NODE_NAME', ''))
    server_cursor = parse_datetime(data['cursor']) if data.get('cursor') else None
    cursor = _clamped_cursor(server_cursor, stats.get('deferred_min'))
    if cursor is not None:
        state.last_pull_cursor = cursor
    state.last_pulled_at = timezone.now()
    state.save()
    stats['received'] = len(rows)
    return stats


def run_sync():
    """Push then pull; record one SyncLog. Returns a summary dict."""
    log = SyncLog.objects.create(
        node_name=getattr(settings, 'SYNC_NODE_NAME', ''), direction='sync',
    )
    summary = {'pushed': 0, 'pulled': 0, 'conflicts': 0, 'ok': False, 'message': ''}
    try:
        push_res = push()
        pull_res = pull()
        summary['pushed'] = push_res.get('applied', 0)
        summary['pulled'] = pull_res.get('applied', 0)
        summary['conflicts'] = push_res.get('conflicts', 0) + pull_res.get('conflicts', 0)
        rejected = push_res.get('rejected', [])
        summary['ok'] = True
        summary['message'] = (
            f"pushed {summary['pushed']}, pulled {summary['pulled']}, "
            f"conflicts {summary['conflicts']}"
            + (f", rejected {rejected}" if rejected else "")
        )
    except SyncNotConfigured as exc:
        summary['message'] = str(exc)
    except Exception as exc:  # network / server / data error
        summary['message'] = f"{type(exc).__name__}: {exc}"

    log.finished_at = timezone.now()
    log.pushed = summary['pushed']
    log.pulled = summary['pulled']
    log.conflicts = summary['conflicts']
    log.ok = summary['ok']
    log.message = summary['message']
    log.save()
    return summary
