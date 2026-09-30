"""Bookkeeping tables for the sync engine.

All installations run the same code, so every table exists everywhere; each role
only uses the ones relevant to it:

* ``Node``         - lives on the SERVER: one row per registered laptop, holding
                     its role and secret API token.
* ``SyncConflict`` - lives on the SERVER (and manager laptop): a durable record
                     of anything that could not be applied cleanly (a stale
                     write that would overwrite newer data, an oversell, a
                     missing reference). Nothing is silently dropped.
* ``SyncLog``      - audit trail of every sync run, either side.
* ``SyncOutbox``   - lives on a LAPTOP: the queue of locally-changed rows still
                     waiting to be pushed to the server.
* ``SyncState``    - lives on a LAPTOP: the pull cursor + last-sync timestamps.
"""

import secrets

from django.db import models
from django.utils import timezone


class Node(models.Model):
    """A laptop authorised to talk to the central server (server-side table)."""

    ROLE_CHOICES = [
        ('manager', 'Manager'),
        ('salesperson', 'Salesperson'),
    ]

    name = models.CharField(max_length=100, unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    token = models.CharField(max_length=64, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(32)

    def __str__(self):
        return f"{self.name} ({self.role})"


class SyncConflict(models.Model):
    """Anything that couldn't be applied cleanly, kept for manual review."""

    REASON_CHOICES = [
        ('stale_write', 'Ignored older change (kept newer data)'),
        ('oversell', 'Sale exceeded available stock'),
        ('missing_reference', 'Referenced record not found'),
        ('error', 'Unexpected error'),
    ]

    model_label = models.CharField(max_length=100)
    sync_id = models.UUIDField(null=True, blank=True)
    reason = models.CharField(max_length=32, choices=REASON_CHOICES)
    detail = models.TextField(blank=True)
    incoming = models.JSONField(null=True, blank=True)
    existing = models.JSONField(null=True, blank=True)
    node_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_reason_display()} — {self.model_label} {self.sync_id}"


class SyncLog(models.Model):
    node_name = models.CharField(max_length=100, blank=True)
    direction = models.CharField(max_length=10)  # push | pull | sync
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    pushed = models.IntegerField(default=0)
    pulled = models.IntegerField(default=0)
    conflicts = models.IntegerField(default=0)
    ok = models.BooleanField(default=False)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        state = 'ok' if self.ok else 'failed'
        return f"{self.direction} {state} @ {self.started_at:%Y-%m-%d %H:%M}"


class SyncOutbox(models.Model):
    """Laptop-side queue of local changes pending upload (deduped per record)."""

    model_label = models.CharField(max_length=100)
    sync_id = models.UUIDField()
    deleted = models.BooleanField(default=False)
    enqueued_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('model_label', 'sync_id')
        ordering = ['enqueued_at']

    def __str__(self):
        return f"{self.model_label} {self.sync_id}{' (del)' if self.deleted else ''}"


class SyncTombstone(models.Model):
    """Record that a row was deleted, so the deletion propagates through pull.

    When the engine deletes a row (reproducing an origin delete), it drops a
    tombstone here. ``pull`` then hands these out like any other change, and
    other laptops delete their local copy. A *push* of a tombstoned row is
    refused (delete wins, see ``engine._apply_row``); an upsert pulled from the
    server clears it, so a record is ever either live *or* tombstoned, never
    both.
    """

    model_label = models.CharField(max_length=100)
    sync_id = models.UUIDField()
    sync_updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = ('model_label', 'sync_id')

    def __str__(self):
        return f"tombstone {self.model_label} {self.sync_id}"


class SyncState(models.Model):
    """Singleton (per key) holding the laptop's pull cursor + last-run times."""

    key = models.CharField(max_length=50, unique=True, default='default')
    # Server-clock high-water mark: 'give me changes newer than this'.
    last_pull_cursor = models.DateTimeField(null=True, blank=True)
    # The server's registry.pull_policy the cursor was earned under. When the
    # server's differs, it resends everything once (see sync.api.api_pull).
    pull_policy = models.CharField(max_length=64, blank=True, default='')
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    last_pushed_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(key='default')
        return obj

    def __str__(self):
        return f"SyncState(cursor={self.last_pull_cursor})"
