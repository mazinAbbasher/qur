"""Deterministic sync identities for seeded reference rows.

Currencies are a fixed enum (``Currency.CODE_CHOICES``) created independently on
every database by the 0006 seed migration. Deriving their ``sync_id`` from the
natural key here — instead of a random ``uuid4`` — makes every node arrive at the
*same* id, so the sync engine matches them as one logical record instead of
colliding on the unique ``code`` when a laptop pulls the server's copy.

Imported by both the seed migration (0006) and the convergence migration (0007)
so the two can never drift apart. Contains no database access, so it is safe to
import from migrations.
"""

import uuid

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, 'qur:finance')


def currency_sync_id(code):
    """The stable, machine-independent ``sync_id`` for a currency code."""
    return uuid.uuid5(_NAMESPACE, f'currency:{code}')
