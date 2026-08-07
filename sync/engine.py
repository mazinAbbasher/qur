"""The apply engine — turns serialized rows into safe database changes.

Guarantees (this is where "reliable, no duplicates, no clobbering" lives):

* **Idempotent** — rows are matched by ``sync_id`` and upserted. Applying the
  same batch twice changes nothing the second time (content is compared; an
  unchanged row is a silent no-op).
* **No clobbering on pull** — if the server sends a row that the laptop has
  *locally edited but not yet pushed* (it's in the outbox), the local copy is
  kept and a ``SyncConflict`` is logged instead of overwriting it.
* **Authoritative stock** — after the server applies pushed sales/returns, the
  affected inventory quantities are recomputed from source rows (idempotent).
  An oversell is clamped to zero and logged, never left negative.
* **Ordered + resilient FKs** — rows apply parents-first; a row whose foreign
  key isn't present yet is retried once, then logged as a missing reference.
"""

from collections import defaultdict
from decimal import Decimal
from math import floor

from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SyncConflict
from .registry import SYNC_ORDER, is_shared_writable, specs_for_pull
from .serializers import (
    field_map, get_model, model_label, scalar_fields, serialize_instance, _from_json,
)
from .tracking import apply_guard


def _log_conflict(reason, label, sync_id, *, detail='', incoming=None,
                  existing=None, node_name=''):
    SyncConflict.objects.create(
        model_label=label, sync_id=sync_id, reason=reason, detail=detail,
        incoming=incoming, existing=existing, node_name=node_name,
    )


def _has_pending_local_change(label, sync_id):
    from .models import SyncOutbox
    return SyncOutbox.objects.filter(model_label=label, sync_id=sync_id).exists()


def _row_ts(row):
    """The ``sync_updated_at`` carried by a serialized row, as an aware datetime."""
    raw = row.get('sync_updated_at')
    if not raw:
        return None
    dt = parse_datetime(raw) if isinstance(raw, str) else raw
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return dt


def _incoming_older(row, existing):
    """True when a pushed row's edit time predates the server's current copy —
    i.e. it would overwrite a newer version (a concurrent-edit collision)."""
    inc = _row_ts(row)
    cur = getattr(existing, 'sync_updated_at', None)
    if inc is None or cur is None:
        return False
    return inc < cur


def _log_missing_reference(label, row, node_name):
    """Idempotent missing-reference log. The pull cursor now re-delivers an
    unresolved row until its parent arrives, so update the existing record in
    place instead of piling up a duplicate conflict on every retry."""
    detail = 'Referenced record not found; will retry on next pull.'
    conflict = SyncConflict.objects.filter(
        model_label=label, sync_id=row['sync_id'], reason='missing_reference',
    ).first()
    if conflict is not None:
        conflict.detail = detail
        conflict.incoming = row
        conflict.node_name = node_name
        conflict.resolved = False
        conflict.save()
    else:
        _log_conflict('missing_reference', label, row['sync_id'],
                      detail=detail, incoming=row, node_name=node_name)


def _differs(existing, model, resolved, incoming_deleted, incoming_fields):
    fmap = field_map(model)
    if bool(existing.is_deleted) != bool(incoming_deleted):
        return True
    for name, val in resolved.items():
        f = fmap[name]
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField)):
            cur = getattr(existing, f.name)
            cur_sid = str(cur.sync_id) if cur is not None else None
            new_sid = str(val.sync_id) if val is not None else None
            if cur_sid != new_sid:
                return True
        else:
            if getattr(existing, f.attname) != val:
                return True
    # Many-to-many
    for f in model._meta.local_many_to_many:
        if f.name in incoming_fields:
            cur = {str(o.sync_id) for o in getattr(existing, f.name).all()}
            new = set(incoming_fields[f.name] or [])
            if cur != new:
                return True
    return False


def _resolve(model, incoming_fields):
    """Return (resolved dict, list of unresolved (name, uuid))."""
    fmap = field_map(model)
    resolved = {}
    unresolved = []
    for name, raw in incoming_fields.items():
        f = fmap.get(name)
        if f is None:
            continue  # unknown or stripped field — ignore safely
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField)):
            if raw is None:
                resolved[name] = None
            else:
                rel = f.related_model.objects.filter(sync_id=raw).first()
                if rel is None:
                    unresolved.append((name, raw))
                else:
                    resolved[name] = rel
        else:
            resolved[name] = _from_json(f, raw)
    return resolved, unresolved


def _apply_m2m(obj, model, incoming_fields):
    for f in model._meta.local_many_to_many:
        if f.name not in incoming_fields:
            continue
        uuids = incoming_fields[f.name] or []
        related = list(f.related_model.objects.filter(sync_id__in=uuids))
        getattr(obj, f.name).set(related)


def _placeholder_for(field):
    """A neutral value for a NOT NULL column that arrived stripped.

    Sensitive financial fields (e.g. ``Shipment.shipment_cost``) are removed
    before a row is sent to a salesperson node. When such a row is inserted for
    the first time, the stripped column has no value and — if it is NOT NULL with
    no model default — the insert fails. A salesperson's shipment rows are
    read-only and never pushed back to the server, so a placeholder here only
    satisfies the local constraint; it can never overwrite the real value
    upstream.
    """
    if isinstance(field, dj_models.DecimalField):
        return Decimal('0')
    if isinstance(field, (dj_models.IntegerField, dj_models.FloatField)):
        return 0
    if isinstance(field, dj_models.BooleanField):
        return False
    return ''


def _fill_stripped_required(obj, model, resolved):
    """Give any required scalar field missing from an incoming (stripped) row a
    neutral placeholder, so a first-time insert doesn't hit a NOT NULL error."""
    for f in scalar_fields(model):
        if f.name in resolved:
            continue
        if f.null or f.has_default():
            continue
        setattr(obj, f.attname, _placeholder_for(f))


def _apply_row(label, row, *, is_pull, node_name, stats):
    model = get_model(label)
    fmap = field_map(model)
    sync_id = row['sync_id']
    incoming_fields = row.get('fields', {})
    incoming_deleted = bool(row.get('is_deleted', False))

    resolved, unresolved = _resolve(model, incoming_fields)
    if unresolved:
        return 'deferred', None
    existing = model.objects.filter(sync_id=sync_id).first()

    # A tombstone for a row we've never seen: nothing to delete, skip cleanly.
    if existing is None and incoming_deleted:
        stats['noop'] += 1
        return 'noop', None

    if existing is not None:
        if not _differs(existing, model, resolved, incoming_deleted, incoming_fields):
            stats['noop'] += 1
            return 'noop', existing
        if is_pull and _has_pending_local_change(label, sync_id):
            _log_conflict('stale_write', label, sync_id,
                          detail='Local unsynced change kept; server version logged.',
                          incoming=row, existing=serialize_instance(existing),
                          node_name=node_name)
            stats['conflicts'] += 1
            return 'conflict', existing
        # Push side: two laptops can edit a shared record (Client/Area) between
        # syncs. The server can't reliably order their clocks, so the push still
        # wins — but if it overwrites a NEWER server copy we keep the loser in the
        # conflict log so the collision is auditable rather than silent.
        if (not is_pull and is_shared_writable(label)
                and _incoming_older(row, existing)):
            _log_conflict('stale_write', label, sync_id,
                          detail='Concurrent edit overwritten on push (push wins); '
                                 'previous server copy preserved here.',
                          incoming=row, existing=serialize_instance(existing),
                          node_name=node_name)
            stats['conflicts'] += 1

    # Tombstone: actually remove the row (reproducing the origin's cascade /
    # SET_NULL) rather than just flagging it — the app's list views don't filter
    # is_deleted, so a soft-deleted row would otherwise linger as a ghost.
    if incoming_deleted:  # existing is not None here (None handled above)
        from .models import SyncTombstone
        inv_pks = _affected_inventory_pks(label, existing)
        existing.delete()
        # Record the deletion so it propagates to other nodes via pull.
        SyncTombstone.objects.update_or_create(model_label=label, sync_id=sync_id)
        stats['applied'] += 1
        return 'applied_delete', inv_pks

    obj = existing or model(sync_id=sync_id)
    for name, val in resolved.items():
        f = fmap[name]
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField)):
            setattr(obj, f.name, val)
        else:
            setattr(obj, f.attname, val)
    # New row from a stripped (salesperson) payload: back-fill required columns
    # the sender omitted so the insert doesn't violate a NOT NULL constraint.
    # Existing rows already hold their prior values, so leave them untouched.
    if existing is None:
        _fill_stripped_required(obj, model, resolved)
    obj.is_deleted = incoming_deleted
    obj.save()
    _apply_m2m(obj, model, incoming_fields)
    # A live upsert clears any prior tombstone for this id (re-creation case).
    from .models import SyncTombstone
    SyncTombstone.objects.filter(model_label=label, sync_id=sync_id).delete()
    stats['applied'] += 1
    return 'applied', obj


def _affected_inventory_pks(label, obj):
    """Inventory PKs whose stock must be recomputed after ``obj`` is deleted."""
    pks = set()
    if label == 'panel.SaleItem' and obj.inventory_id:
        pks.add(obj.inventory_id)
    elif label == 'panel.LostProduct' and obj.inventory_id:
        pks.add(obj.inventory_id)
    elif label == 'panel.ReturnedProduct' and obj.sale_item_id:
        pks.add(obj.sale_item.inventory_id)
    elif label == 'panel.Sale':
        # Deleting a sale cascade-deletes its items; capture them first.
        for si in obj.items.all():
            if si.inventory_id:
                pks.add(si.inventory_id)
    return pks


def _recompute_inventory(inv, node_name):
    """Authoritatively recompute one inventory's stock from source rows."""
    from panel.models import LostProduct, ReturnedProduct, SaleItem

    base = inv.shipment.quantity or 0
    sold = 0
    for si in SaleItem.objects.filter(inventory=inv, is_deleted=False):
        free = floor((si.quantity or 0) * float(si.free_goods_discount or 0) / 100)
        sold += (si.quantity or 0) + free
    returned = sum(r.quantity for r in ReturnedProduct.objects.filter(
        sale_item__inventory=inv, is_deleted=False))
    lost = sum(l.quantity for l in LostProduct.objects.filter(
        inventory=inv, is_deleted=False))

    remaining = base - sold + returned - lost
    new_qty = max(remaining, 0)
    if inv.quantity != new_qty:
        inv.quantity = new_qty
        inv.save()
    if remaining < 0:
        _log_conflict(
            'oversell', 'panel.Inventory', inv.sync_id,
            detail=(f'Recomputed stock {remaining} < 0 for batch '
                    f'{inv.shipment.batch_number}; clamped to 0. Needs review.'),
            node_name=node_name,
        )
        return True
    return False


@transaction.atomic
def apply_batch(rows, *, is_pull, node_name='', authoritative_inventory=False):
    """Apply serialized ``rows`` and return a stats dict.

    ``is_pull``                 - True when a laptop applies data from the server.
    ``authoritative_inventory`` - True when the server applies a push (recompute
                                  stock from source rows afterwards).
    """
    stats = {'applied': 0, 'noop': 0, 'conflicts': 0, 'oversells': 0,
             'deferred_min': None}
    by_label = defaultdict(list)
    for r in rows:
        by_label[r['label']].append(r)

    touched_inventory_pks = set()

    def _note_inventory(label, obj):
        if obj is None:
            return
        if label == 'panel.SaleItem' and obj.inventory_id:
            touched_inventory_pks.add(obj.inventory_id)
        elif label == 'panel.LostProduct' and obj.inventory_id:
            touched_inventory_pks.add(obj.inventory_id)
        elif label == 'panel.ReturnedProduct' and obj.sale_item_id:
            touched_inventory_pks.add(obj.sale_item.inventory_id)

    def _handle(label, status, result):
        if not authoritative_inventory:
            return
        if status == 'applied':
            _note_inventory(label, result)
        elif status == 'applied_delete':
            touched_inventory_pks.update(result)  # result is a set of inv PKs

    with apply_guard():
        deferred = []
        for label in [l for l in SYNC_ORDER if l in by_label]:
            for row in by_label[label]:
                status, result = _apply_row(label, row, is_pull=is_pull,
                                            node_name=node_name, stats=stats)
                if status == 'deferred':
                    deferred.append((label, row))
                else:
                    _handle(label, status, result)

        # One retry pass — parents applied above may now satisfy FKs.
        deferred_min = None
        for label, row in deferred:
            status, result = _apply_row(label, row, is_pull=is_pull,
                                        node_name=node_name, stats=stats)
            if status == 'deferred':
                # Still unresolved: log it and remember the oldest such row so
                # the pull cursor won't advance past it (see client.pull).
                _log_missing_reference(label, row, node_name)
                stats['conflicts'] += 1
                ts = _row_ts(row)
                if ts is not None and (deferred_min is None or ts < deferred_min):
                    deferred_min = ts
            else:
                _handle(label, status, result)
        stats['deferred_min'] = deferred_min

        # Authoritative stock recompute (server side, after applying a push).
        # select_for_update serialises concurrent pushes touching the same batch
        # so an oversell can't slip through when two reps sell it at once.
        if authoritative_inventory and touched_inventory_pks:
            from panel.models import Inventory
            inventories = (Inventory.objects
                           .select_for_update()
                           .filter(pk__in=touched_inventory_pks)
                           .select_related('shipment'))
            for inv in inventories:
                if _recompute_inventory(inv, node_name):
                    stats['oversells'] += 1
                    stats['conflicts'] += 1

    return stats


def collect_server_changes(role, since=None):
    """Server side: rows a node of ``role`` should receive, changed after
    ``since`` (a datetime or None for a full sync). Returns (rows, new_cursor).

    Field stripping for salespeople happens inside ``serialize_instance``.
    """
    from .models import SyncTombstone

    rows = []
    max_ts = since
    pull_specs = specs_for_pull(role)
    readable = {spec.label for spec in pull_specs}

    for spec in pull_specs:
        model = get_model(spec.label)
        qs = model.objects.all()
        if since is not None:
            qs = qs.filter(sync_updated_at__gt=since)
        for inst in qs.order_by('sync_updated_at').iterator():
            rows.append(serialize_instance(inst, role=role))
            if max_ts is None or inst.sync_updated_at > max_ts:
                max_ts = inst.sync_updated_at

    # Deletions the node hasn't seen yet (only for models it may read).
    tomb_qs = SyncTombstone.objects.filter(model_label__in=readable)
    if since is not None:
        tomb_qs = tomb_qs.filter(sync_updated_at__gt=since)
    for t in tomb_qs.order_by('sync_updated_at').iterator():
        rows.append({'label': t.model_label, 'sync_id': str(t.sync_id),
                     'is_deleted': True, 'fields': {}})
        if max_ts is None or t.sync_updated_at > max_ts:
            max_ts = t.sync_updated_at

    return rows, (max_ts.isoformat() if max_ts else None)
