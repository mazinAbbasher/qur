"""Abstract base that gives a model a global, sync-safe identity.

Every syncable model gains three columns:

* ``sync_id``          - a UUID that is stable across every laptop and the
                         server. This is the identity used when pushing/pulling,
                         so the same logical record is matched everywhere and
                         re-sending it can never create a duplicate.
* ``sync_updated_at``  - last local modification time. Used for last-write-wins
                         conflict resolution (the newer version wins; the loser
                         is preserved in a SyncConflict log, never discarded).
* ``is_deleted``       - soft-delete tombstone so deletions propagate through
                         sync without breaking foreign-key references.

The existing integer primary key is deliberately kept. It stays *local* to each
database (URLs, templates and foreign keys keep working unchanged); only
``sync_id`` crosses machine boundaries.
"""

import uuid

from django.db import models


class SyncModel(models.Model):
    sync_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    sync_updated_at = models.DateTimeField(auto_now=True, db_index=True)
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True
