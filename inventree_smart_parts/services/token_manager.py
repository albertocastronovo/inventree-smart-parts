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
    """SHA-256 hex digest of the token — matches InvenTree's hash_barcode()."""
    return hashlib.sha256(normalize_token(token).encode()).hexdigest()


def _barcode_field_name(model) -> Optional[str]:
    """Return the name of the custom-barcode field on ``model``, if any."""
    try:
        field_names = {f.name for f in model._meta.get_fields()}
    except Exception:
        return None
    for name in _BARCODE_FIELD_CANDIDATES:
        if name in field_names:
            return name
    return None


def _lookup_by_field(model, token: str):
    fname = _barcode_field_name(model)
    if not fname:
        return None
    try:
        return model.objects.filter(**{f"{fname}__iexact": token}).first()
    except Exception as e:
        logger.warning(f"token lookup by field failed on {model.__name__}: {e}")
        return None


def _lookup_by_hash(model, token: str):
    """Fallback lookup via barcode_hash (versions that store only the hash)."""
    try:
        return model.objects.filter(barcode_hash=hash_token(token)).first()
    except Exception:
        return None


def resolve_token(token: str) -> Tuple[Optional[str], Optional[object]]:
    """
    Resolve a token to the InvenTree object it is bound to.

    Returns:
        (model_name, obj)   e.g. ("stockitem", <StockItem #722>)
        ("free", None)      the token is valid but not bound to anything
        (None, None)        the string is not a valid token at all
    """
    t = normalize_token(token)
    if not is_valid_token(t):
        return (None, None)

    from stock.models import StockItem, StockLocation
    from part.models import Part

    for name, model in (
        ("stockitem", StockItem),
        ("stocklocation", StockLocation),
        ("part", Part),
    ):
        obj = _lookup_by_field(model, t) or _lookup_by_hash(model, t)
        if obj is not None:
            return (name, obj)

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


def bind_token(token: str, obj) -> dict:
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

    fname = _barcode_field_name(type(obj))
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

    # Keep barcode_hash in sync so native barcode lookups resolve the token
    try:
        if hasattr(obj, "generate_barcode_hash"):
            obj.generate_barcode_hash()
        else:
            obj.barcode_hash = hash_token(t)
        if "barcode_hash" in {f.name for f in type(obj)._meta.get_fields()}:
            update_fields.append("barcode_hash")
    except Exception as e:
        logger.warning(f"bind_token: could not update barcode_hash for {t}: {e}")

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
