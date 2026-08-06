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
from math import floor

from django.db import models as dj_models
from django.db import transaction

from .models import SyncConflict
from .registry import SYNC_ORDER, specs_for_pull
from .serializers import (
    field_map, get_model, model_label, serialize_instance, _from_json,
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
    stats = {'applied': 0, 'noop': 0, 'conflicts': 0, 'oversells': 0}
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
        for label, row in deferred:
            status, result = _apply_row(label, row, is_pull=is_pull,
                                        node_name=node_name, stats=stats)
            if status == 'deferred':
                _log_conflict('missing_reference', label, row['sync_id'],
                              detail='Referenced record not found after retry.',
                              incoming=row, node_name=node_name)
                stats['conflicts'] += 1
            else:
                _handle(label, status, result)

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
