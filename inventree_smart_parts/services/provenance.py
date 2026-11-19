"""
Field Provenance
================
Tracks which distributor wrote each field of a Part, so that a later scan
can follow the API_PRIORITY rule without destroying data:

  A candidate value from source S is written to an existing field only if
    1. the current value is empty / a placeholder, or
    2. the field was written by source P, is unchanged since (hash match),
       and S ranks strictly above P in the priority order.

  Fields without provenance (legacy parts, editor input, manual edits) and
  fields edited after they were written are never overwritten.

Stored in ``part.metadata["smartparts"]`` (InvenTree MetadataMixin):

    {"v": 1, "fields": {"description": {"src": "mouser", "h": "<sha1-12>"},
                        "param:Series": {...}, "category": {...}}}

The pure helpers (value_hash, rank, should_write) need no Django.
"""

import hashlib
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger("inventree_smart_parts.services.provenance")

META_KEY = "smartparts"
VERSION = 1

_WS_RE = re.compile(r"\s+")


def value_hash(value) -> str:
    """Short, whitespace-normalised hash of a field value."""
    norm = _WS_RE.sub(" ", str(value if value is not None else "")).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def rank(source: str, priority_order: List[str]) -> int:
    """Position of a source in the priority order (lower = higher priority)."""
    try:
        return [p.lower() for p in priority_order].index((source or "").lower())
    except ValueError:
        return len(priority_order)


def is_empty(value) -> bool:
    from .part_creator import is_useless_value

    return is_useless_value(value)


def should_write(
    entry: Optional[Dict],
    current_value,
    candidate_source: str,
    priority_order: List[str],
) -> bool:
    """Decide whether a candidate from ``candidate_source`` may be written."""
    if is_empty(current_value):
        return True
    if not entry or not candidate_source:
        return False  # unknown origin (legacy / manual) → keep
    if entry.get("h") != value_hash(current_value):
        return False  # edited since it was written → keep
    return rank(candidate_source, priority_order) < rank(
        entry.get("src", ""), priority_order
    )


def empty() -> Dict:
    return {"v": VERSION, "fields": {}}


def load(part) -> Dict:
    """Read the provenance record of a Part (empty record if none)."""
    try:
        meta = getattr(part, "metadata", None) or {}
        prov = meta.get(META_KEY) if isinstance(meta, dict) else None
        if isinstance(prov, dict) and isinstance(prov.get("fields"), dict):
            return prov
    except Exception as e:
        logger.debug(f"provenance load failed for part {getattr(part, 'pk', '?')}: {e}")
    return empty()


def entry(prov: Dict, key: str) -> Optional[Dict]:
    return (prov or {}).get("fields", {}).get(key)


def record(prov: Dict, key: str, source: str, value) -> None:
    """Remember that ``source`` wrote ``value`` into ``key``.

    An empty source means "unknown origin": the entry is removed, so the
    field is protected from future overwrites.
    """
    fields = prov.setdefault("fields", {})
    if source:
        fields[key] = {"src": source, "h": value_hash(value)}
    else:
        fields.pop(key, None)


def save(part, prov: Dict) -> None:
    """Persist the provenance record (no-op if the model has no metadata)."""
    if not hasattr(part, "metadata"):
        return
    try:
        meta = part.metadata if isinstance(part.metadata, dict) else {}
        meta = dict(meta)
        meta[META_KEY] = prov
        part.metadata = meta
        part.save(update_fields=["metadata"])
    except Exception as e:
        logger.warning(f"provenance save failed for part {part.pk}: {e}")
