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
* **Deletes win** — a laptop pushing its stale copy of a row the server already
  deleted doesn't bring it back; the copy is logged and the deletion re-sent.
"""

from collections import defaultdict
from decimal import Decimal
from math import floor

from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SyncConflict
from .registry import SYNC_ORDER, get_spec, is_shared_writable, specs_for_pull
from .serializers import (
    field_map, fk_fields, get_model, model_label, scalar_fields, serialize_instance,
    _from_json,
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


def _unique_field_groups(model):
    """Tuples of field names that must be unique together for ``model`` —
    ``unique_together`` plus any unconditional ``UniqueConstraint``. Used to spot
    a divergent-id twin whose collision spans several columns (e.g. Commission's
    ``(employee, sale)``)."""
    groups = [tuple(ut) for ut in model._meta.unique_together]
    for c in model._meta.constraints:
        if isinstance(c, dj_models.UniqueConstraint) and not getattr(c, 'condition', None):
            groups.append(tuple(c.fields))
    return groups


def _natural_key_match(model, label, resolved):
    """A local *reference* row that already holds the incoming row's unique
    natural key under a different ``sync_id`` — or None.

    Reference rows are created independently on each database, so the same
    logical record ends up with a different random ``sync_id`` per machine
    (a seeded Currency by ``code``, a Commission by ``(employee, sale)``, an
    Inventory by its ``shipment``). Matching only by ``sync_id`` would try to
    INSERT a duplicate and trip the unique constraint, aborting the whole batch.
    When the natural key already exists locally we treat it as the same record
    and adopt the incoming ``sync_id`` instead.

    Restricted to ``reference`` models on purpose: a unique collision on
    *transactional* data (e.g. two laptops minting the same ``Invoice.number``)
    is a genuine conflict between distinct records, not one entity, and must
    never be merged. Both single-column uniques (scalar or a one-to-one/foreign
    key) and multi-column unique constraints are considered.
    """
    spec = get_spec(label)
    if spec is None or spec.category != 'reference':
        return None
    fmap = field_map(model)

    def _col_and_value(f, val):
        # FK/O2O values resolve to an instance; match on the stored id column.
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField)):
            return f.attname, (val.pk if val is not None else None)
        return f.attname, val

    # Single-column uniques — e.g. Currency.code, or Inventory.shipment (O2O).
    for name, val in resolved.items():
        f = fmap.get(name)
        if f is None or val is None or not getattr(f, 'unique', False):
            continue
        col, lookup_val = _col_and_value(f, val)
        match = model.objects.filter(**{col: lookup_val}).first()
        if match is not None:
            return match

    # Multi-column uniques — e.g. Commission (employee, sale).
    for group in _unique_field_groups(model):
        lookup = {}
        complete = True
        for fname in group:
            f = fmap.get(fname)
            if f is None or fname not in resolved:
                complete = False
                break
            col, lookup_val = _col_and_value(f, resolved[fname])
            lookup[col] = lookup_val
        if complete and lookup:
            match = model.objects.filter(**lookup).first()
            if match is not None:
                return match
    return None


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
    from .models import SyncTombstone

    model = get_model(label)
    fmap = field_map(model)
    sync_id = row['sync_id']
    incoming_fields = row.get('fields', {})
    incoming_deleted = bool(row.get('is_deleted', False))

    # Delete wins. A pushed live copy of a row this server already deleted comes
    # from a laptop that re-saved its copy (an edit, a return recalculating the
    # total...) before it heard of the delete. Re-creating it would bring the
    # record back for every node, often only half of it (a sale without its
    # items). Refuse it, keep it in the conflict log, and re-announce the
    # deletion so that laptop's next pull removes its copy.
    if not is_pull and not incoming_deleted:
        tombstone = SyncTombstone.objects.filter(
            model_label=label, sync_id=sync_id).first()
        if tombstone is not None:
            _log_conflict('stale_write', label, sync_id,
                          detail='Change to a record already deleted on the server; '
                                 'not restored. The deletion was re-sent.',
                          incoming=row, node_name=node_name)
            tombstone.save()  # auto_now: newer than that laptop's cursor again
            stats['conflicts'] += 1
            return 'conflict', None

    resolved, unresolved = _resolve(model, incoming_fields)
    if unresolved:
        return 'deferred', None
    existing = model.objects.filter(sync_id=sync_id).first()

    # A tombstone for a row we've never seen: nothing to delete, skip cleanly.
    if existing is None and incoming_deleted:
        stats['noop'] += 1
        return 'noop', None

    # Same reference row, divergent identity: a seed row (e.g. a Currency) was
    # created separately on each database and so carries a different sync_id here
    # than upstream. Adopt the incoming id onto the local row rather than insert
    # a duplicate that would trip its unique natural key and abort the pull. The
    # id is persisted now so it converges even when nothing else about the row
    # changed (the update below would otherwise be a no-op).
    if existing is None:
        match = _natural_key_match(model, label, resolved)
        if match is not None:
            match.sync_id = sync_id
            match.save(update_fields=['sync_id', 'sync_updated_at'])
            existing = match

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
    # A live upsert clears any prior tombstone for this id. A push of a
    # tombstoned id was refused above, so this is a pull of a row the server
    # holds live.
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


def _update_commissions(sale_pks, employee_pks):
    """Server side, after applying a push: give every pushed sale its commission
    (a salesperson laptop holds no commission percentages, so its sales arrive
    without one) and apply a pushed percentage change to the sales the pushing
    laptop hadn't pulled yet."""
    from panel.models import Employee, Sale, update_sale_commission

    for sale in Sale.objects.filter(pk__in=sale_pks).select_related('employee'):
        update_sale_commission(sale)
    # Partly paid commissions were already re-rated by the laptop that changed
    # the percentage (and pushed in this batch); re-rating here would do it twice.
    for employee in Employee.objects.filter(pk__in=employee_pks):
        employee.recalculate_commissions(rerate_partly_paid=False)


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
    pushed_sale_pks = set()
    pushed_employee_pks = set()

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
        if status == 'applied' and not is_pull:
            if label == 'panel.Sale':
                pushed_sale_pks.add(result.pk)
            elif label == 'panel.Employee':
                pushed_employee_pks.add(result.pk)
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

        if pushed_sale_pks or pushed_employee_pks:
            _update_commissions(pushed_sale_pks, pushed_employee_pks)

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

    for spec in specs_for_pull(role):
        model = get_model(spec.label)
        # Join the foreign keys serialize_instance reads; otherwise a full pull
        # costs one query per key per row, which on a real sales history runs
        # past the HTTP timeout.
        qs = model.objects.select_related(*[f.name for f in fk_fields(model)])
        if since is not None:
            qs = qs.filter(sync_updated_at__gt=since)
        for inst in qs.order_by('sync_updated_at').iterator():
            rows.append(serialize_instance(inst, role=role))
            if max_ts is None or inst.sync_updated_at > max_ts:
                max_ts = inst.sync_updated_at

    # Deletions the node hasn't seen yet, for every model. A tombstone is only a
    # label and a random id, so it reveals nothing, and a node can hold its own
    # copy of a row it may not pull (a salesperson's expenses and exchanges).
    tomb_qs = SyncTombstone.objects.all()
    if since is not None:
        tomb_qs = tomb_qs.filter(sync_updated_at__gt=since)
    for t in tomb_qs.order_by('sync_updated_at').iterator():
        rows.append({'label': t.model_label, 'sync_id': str(t.sync_id),
                     'is_deleted': True, 'fields': {}})
        if max_ts is None or t.sync_updated_at > max_ts:
            max_ts = t.sync_updated_at

    return rows, (max_ts.isoformat() if max_ts else None)
