"""Local change tracking (the laptop-side outbox) + the apply guard.

Whenever a syncable row is created/updated/deleted through normal app use, a
signal records it in ``SyncOutbox`` so it can later be pushed to the server.

Two safety rails:

* The guard ``sync_apply_active()`` is switched on while the engine is applying
  incoming data, so pulled/pushed rows are NOT re-queued (no echo loop) and the
  models' side-effecting ``save()`` overrides skip their side effects (stock is
  recomputed authoritatively instead — see panel/models.py).
* Tracking only records changes when this installation is actually a syncing
  laptop (``SYNC_ROLE`` is manager/salesperson). On a 'standalone' install
  (the default) nothing is recorded, so existing behaviour is unchanged.
"""

import threading

from django.conf import settings
from django.db.models.signals import post_delete, post_save

from .registry import SYNC_ORDER, is_syncable
from .serializers import get_model, model_label

_state = threading.local()


def sync_apply_active():
    return getattr(_state, 'active', False)


class apply_guard:
    """Context manager: suppress outbox recording + model side effects."""

    def __enter__(self):
        self._prev = getattr(_state, 'active', False)
        _state.active = True
        return self

    def __exit__(self, *exc):
        _state.active = self._prev
        return False


def _tracking_enabled():
    return getattr(settings, 'SYNC_ROLE', 'standalone') in ('manager', 'salesperson')


def enqueue(label, sync_id, deleted=False):
    from .models import SyncOutbox
    SyncOutbox.objects.update_or_create(
        model_label=label, sync_id=sync_id, defaults={'deleted': deleted},
    )


def _on_save(sender, instance, **kwargs):
    if sync_apply_active() or not _tracking_enabled():
        return
    label = model_label(sender)
    if not is_syncable(label):
        return
    enqueue(label, instance.sync_id, deleted=getattr(instance, 'is_deleted', False))


def _on_delete(sender, instance, **kwargs):
    if sync_apply_active() or not _tracking_enabled():
        return
    label = model_label(sender)
    if not is_syncable(label):
        return
    # Hard deletes still propagate as a tombstone keyed by the last-known sync_id.
    enqueue(label, instance.sync_id, deleted=True)


def connect():
    """Wire up post_save/post_delete for every registered model."""
    for label in SYNC_ORDER:
        model = get_model(label)
        post_save.connect(_on_save, sender=model, dispatch_uid=f'sync_save_{label}')
        post_delete.connect(_on_delete, sender=model, dispatch_uid=f'sync_del_{label}')
