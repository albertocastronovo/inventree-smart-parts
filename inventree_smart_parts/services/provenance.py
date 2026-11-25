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
                        "param:Series": {...}, "category": {...}},
     "lookups": {"digikey": {"miss": "2026-11-25T10:00:00+00:00"}}}

A category written from a flat source (Mouser's leaf string, fuzzy-matched
or defaulted) may also be replaced by a hierarchical source (DigiKey), if
unchanged since, whatever the priority order (category_upgrade_allowed).

``lookups`` remembers distributors that do not carry the part ("miss"), so
FASTADD does not query them again on every scan (recent_misses).

The pure helpers (value_hash, rank, should_write) need no Django.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger("inventree_smart_parts.services.provenance")

META_KEY = "smartparts"
VERSION = 1
MISS_TTL_DAYS = 30

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


def category_upgrade_allowed(
    entry: Optional[Dict], current_value, candidate_source: str
) -> bool:
    """A hierarchical source may replace an unedited flat-source category."""
    from .data_merger import is_flat_category_source

    if is_empty(current_value):
        return True
    if not entry or not candidate_source or is_flat_category_source(candidate_source):
        return False
    if not is_flat_category_source(entry.get("src", "")):
        return False
    return entry.get("h") == value_hash(current_value)


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def recent_misses(prov: Dict, now=None, ttl_days: int = MISS_TTL_DAYS) -> Set[str]:
    """Distributors that reported "not carried" within the last ``ttl_days``."""
    cutoff = _now(now) - timedelta(days=ttl_days)
    misses = set()
    for source, info in ((prov or {}).get("lookups") or {}).items():
        try:
            when = datetime.fromisoformat(str((info or {}).get("miss", "")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            misses.add(source.lower())
    return misses


def apply_lookups(
    prov: Dict, found: Iterable[str], missed: Iterable[str], now=None
) -> bool:
    """Record lookup outcomes in ``prov``; returns True if anything changed."""
    lookups = prov.setdefault("lookups", {})
    changed = False
    for source in found:
        if lookups.pop(source.lower(), None) is not None:
            changed = True
    stamp = _now(now).isoformat()
    for source in missed:
        lookups[source.lower()] = {"miss": stamp}
        changed = True
    return changed


def record_lookups(part_id, found: Iterable[str], missed: Iterable[str], now=None):
    """Persist lookup outcomes on a part (reloads it: callers may hold a
    stale instance after create_part_from_data)."""
    found, missed = list(found), list(missed)
    if not found and not missed:
        return
    try:
        from part.models import Part

        part = Part.objects.filter(pk=part_id).first()
        if part is None or not hasattr(part, "metadata"):
            return
        prov = load(part)
        if apply_lookups(prov, found, missed, now):
            save(part, prov)
    except Exception as e:
        logger.warning(f"lookup memory save failed for part {part_id}: {e}")


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
