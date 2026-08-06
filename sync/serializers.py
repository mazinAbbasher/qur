"""Convert model instances to/from portable, machine-independent dicts.

Rules that make sync safe:

* Records are identified only by ``sync_id`` (never the local integer PK).
* Foreign keys are emitted as the *related row's* ``sync_id``, so a reference
  means the same thing on every machine regardless of local PK numbering.
* Decimals/dates are stringified so JSON round-trips without precision loss.
* Sensitive fields (e.g. purchase costs) are dropped for salesperson nodes.
"""

from decimal import Decimal

from django.apps import apps as django_apps
from django.conf import settings
from django.db import models as dj_models
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .registry import sensitive_fields_for


def model_label(model):
    return f"{model._meta.app_label}.{model.__name__}"


def get_model(label):
    app_label, model_name = label.split('.')
    return django_apps.get_model(app_label, model_name)


# --- Field introspection ---------------------------------------------------
_META = ('sync_id', 'sync_updated_at', 'is_deleted')


def scalar_fields(model):
    """Concrete non-relational fields, excluding the PK and sync metadata."""
    out = []
    for f in model._meta.concrete_fields:
        if f.primary_key:
            continue
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField)):
            continue
        if f.name in _META:
            continue
        out.append(f)
    return out


def fk_fields(model):
    return [
        f for f in model._meta.concrete_fields
        if isinstance(f, (dj_models.ForeignKey, dj_models.OneToOneField))
    ]


def m2m_fields(model):
    return list(model._meta.local_many_to_many)


# --- Value <-> JSON --------------------------------------------------------
def _to_json(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _from_json(field, value):
    if value is None:
        return None
    if isinstance(field, dj_models.DecimalField):
        return Decimal(str(value))
    if isinstance(field, dj_models.DateTimeField):
        val = parse_datetime(value) if isinstance(value, str) else value
        # Normalise naive -> aware so values round-trip and compare consistently
        # (otherwise an incoming naive time never equals the stored aware one and
        # the row would re-apply on every sync).
        if val is not None and settings.USE_TZ and timezone.is_naive(val):
            val = timezone.make_aware(val, timezone.get_default_timezone())
        return val
    if isinstance(field, dj_models.DateField):
        return parse_date(value) if isinstance(value, str) else value
    if isinstance(field, dj_models.BooleanField):
        return bool(value)
    return value


# --- Serialize -------------------------------------------------------------
def serialize_instance(instance, role='manager'):
    """Return a portable dict for one instance, stripped for ``role``."""
    model = type(instance)
    label = model_label(model)
    strip = set(sensitive_fields_for(label, role))

    fields = {}
    for f in scalar_fields(model):
        if f.name in strip:
            continue
        fields[f.name] = _to_json(getattr(instance, f.attname))

    for f in fk_fields(model):
        if f.name in strip:
            continue
        # Read the related sync_id cheaply without always fetching the object.
        rel = getattr(instance, f.name)
        fields[f.name] = str(rel.sync_id) if rel is not None else None

    for f in m2m_fields(model):
        if f.name in strip:
            continue
        fields[f.name] = [str(o.sync_id) for o in getattr(instance, f.name).all()]

    return {
        'label': label,
        'sync_id': str(instance.sync_id),
        'sync_updated_at': instance.sync_updated_at.isoformat() if instance.sync_updated_at else None,
        'is_deleted': instance.is_deleted,
        'fields': fields,
    }


def field_map(model):
    """name -> field object, for scalar + fk fields (used when applying)."""
    out = {f.name: f for f in scalar_fields(model)}
    out.update({f.name: f for f in fk_fields(model)})
    return out
