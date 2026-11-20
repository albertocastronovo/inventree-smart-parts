"""
Token Manager
=============
Custom barcode token system for the PureScan slow-workflow commands.

Tokens are opaque, human-friendly labels that are pre-printed in bulk
(e.g. ``STOCK_7K3A``, ``LOC_AB12``) and bound at creation time to an
InvenTree object's custom-barcode field.  Because the binding lives ON the
object, deleting the object automatically frees the token for reuse.

Formats:
    STOCK_[A-Z0-9]{4}   -> StockItem instance labels  (36^4 = 1.68M)
    LOC_[A-Z0-9]{4}     -> StockLocation labels       (36^4 = 1.68M)

All InvenTree model imports are deferred so the module can be imported
without a fully-initialised Django environment.
"""

import hashlib
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger("inventree_smart_parts.services.token")

TOKEN_PATTERNS = {
    "STOCK": re.compile(r"^STOCK_[A-Z0-9]{4}$"),
    "LOC": re.compile(r"^LOC_[A-Z0-9]{4}$"),
}

# Candidate field names for the "custom barcode" field on InvenTree models,
# in priority order (field names differ between InvenTree versions).
_BARCODE_FIELD_CANDIDATES = ("custom_barcode_data", "barcode_data")


def normalize_token(token: str) -> str:
    """Uppercase + strip a scanned token string."""
    return (token or "").strip().upper()


def is_valid_token(token: str) -> bool:
    """True when the string matches one of the known token formats."""
    t = normalize_token(token)
    return any(p.match(t) for p in TOKEN_PATTERNS.values())


def hash_token(token: str) -> str:
    """Use the host's current barcode hashing rules."""
    from InvenTree.helpers import hash_barcode

    return hash_barcode(normalize_token(token))


def legacy_hash_token(token: str) -> str:
    """Hash written by earlier SmartParts releases."""
    return hashlib.sha256(normalize_token(token).encode()).hexdigest()


def stock_token(token: str) -> str:
    """Normalize a stock-only token, or raise a validation error."""
    if not isinstance(token, str):
        raise ValueError("Stock barcode must match STOCK_XXXX")
    token = normalize_token(token)
    if not TOKEN_PATTERNS["STOCK"].fullmatch(token):
        raise ValueError("Stock barcode must match STOCK_XXXX")
    return token


class TokenConflict(ValueError):
    """The requested token is already bound."""


def lock_token(token: str):
    """Serialize SmartParts claims for the same token inside a transaction."""
    from django.db import connection

    if connection.vendor == "postgresql":
        # The token space fits in a signed integer; use a separate namespace.
        namespace = 0x53505349 if token.startswith("STOCK_") else 0x53504C4F
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [namespace, int(token.rsplit("_", 1)[1], 36)],
            )


def require_free_token(token: str):
    """Check native barcode models and legacy SmartParts bindings."""
    kind, bound = resolve_token(token)
    if kind == "ambiguous":
        raise TokenConflict(f"{token} has conflicting bindings")
    if bound is not None:
        raise TokenConflict(f"{token} is already linked to a {kind}")


def barcode_field_name(model) -> Optional[str]:
    """Return the name of the custom-barcode field on ``model``, if any."""
    try:
        field_names = {f.name for f in model._meta.get_fields()}
    except Exception:
        return None
    for name in _BARCODE_FIELD_CANDIDATES:
        if name in field_names:
            return name
    return None


def resolve_token(token: str) -> Tuple[Optional[str], Optional[object]]:
    """
    Resolve a token to the InvenTree object it is bound to.

    Returns:
        (model_name, obj)   e.g. ("stockitem", <StockItem #722>)
        ("free", None)      the token is valid but not bound to anything
        ("ambiguous", None) multiple records of the matching type use it
        (None, None)        the string is not a valid token at all
    """
    t = normalize_token(token)
    if not is_valid_token(t):
        return (None, None)

    from django.db.models import Q
    from part.models import Part
    from stock.models import StockItem, StockLocation

    models = (
        ("stockitem", StockItem),
        ("stocklocation", StockLocation),
        ("part", Part),
    )
    try:
        from plugin.base.barcodes.helper import get_supported_barcode_models_map
    except ImportError:
        pass
    else:
        models = tuple({**dict(models), **get_supported_barcode_models_map()}.items())

    hashes = (hash_token(t), legacy_hash_token(t))
    for name, model in models:
        matches = Q(barcode_hash__in=hashes)
        if field := barcode_field_name(model):
            matches |= Q(**{f"{field}__iexact": t})
        options = list(model.objects.filter(matches).order_by("pk")[:2])
        if len(options) > 1:
            return ("ambiguous", None)
        if options:
            return (name, options[0])

    return ("free", None)


def describe_object(kind: Optional[str], obj) -> str:
    """Human-readable one-liner describing the object a token is bound to."""
    if obj is None:
        return "nothing (free)"
    if kind == "stockitem":
        part = getattr(obj, "part", None)
        base = str(part) if part else str(obj)
        try:
            loc = getattr(obj, "location", None)
            if loc:
                base += f" @ {getattr(loc, 'pathstring', '') or str(loc)}"
        except Exception:
            pass
        return base
    if kind == "stocklocation":
        return getattr(obj, "pathstring", "") or str(obj)
    return str(obj)


def bind_token(token: str, obj, *, save: bool = True) -> dict:
    """
    Bind a token to an InvenTree object (StockItem / StockLocation / Part).

    Writes the token into the object's custom-barcode field and updates
    ``barcode_hash`` so InvenTree's native ``/api/barcode/`` endpoint can
    resolve the token as a first-class label.

    Returns: {'success': bool, 'token': str, 'error': str (on failure)}
    """
    t = normalize_token(token)
    if not is_valid_token(t):
        return {"success": False, "error": f"Invalid token format: {t}"}

    if hasattr(obj, "assign_barcode"):
        obj.assign_barcode(barcode_data=t, save=save)
        return {"success": True, "token": t}

    fname = barcode_field_name(type(obj))
    if not fname:
        return {
            "success": False,
            "error": (
                f"{type(obj).__name__} has no custom barcode field "
                "in this InvenTree version"
            ),
        }

    setattr(obj, fname, t)

    update_fields = [fname]

    obj.barcode_hash = hash_token(t)
    if "barcode_hash" in {f.name for f in type(obj)._meta.get_fields()}:
        update_fields.append("barcode_hash")

    if not save:
        return {"success": True, "token": t}

    try:
        obj.save(update_fields=update_fields)
    except Exception as e:
        try:
            obj.save()
        except Exception as e2:
            logger.error(f"bind_token: save failed for {t}: {e2}", exc_info=True)
            return {"success": False, "error": str(e2)}

    logger.info(f"bind_token: bound {t} to {type(obj).__name__} #{obj.pk}")
    return {"success": True, "token": t}
