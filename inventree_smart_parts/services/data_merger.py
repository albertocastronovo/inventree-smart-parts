"""
Data Merger
===========
Merges PartData from multiple API sources using configurable priority.
First source with a non-empty field wins for that field.
"""

import logging
from typing import List, Optional
from ..api_clients.base import PartData, PartParameter

logger = logging.getLogger("inventree_smart_parts.services.merger")

# Fields to merge (in order of importance)
MERGEABLE_FIELDS = [
    "mpn",
    "manufacturer",
    "description",
    "name",
    "category",
    "subcategory",
    "datasheet_url",
    "image_url",
    "package",
    # v1.3.0: extended distributor metadata
    "detailed_description",
    "lead_time",
    "series",
    "supplier_device_package",
]


def _norm_mpn(value: str) -> str:
    """Normalise an MPN for identity comparison (strip + case-fold).

    Case is ignored because InvenTree's own Part uniqueness check on
    name/IPN/revision is case-insensitive.
    """
    return (value or "").strip().upper()


def resolve_identity_mpn(
    results: List[PartData],
    search_term: str = "",
    strict: bool = False,
) -> str:
    """Decide which MPN a set of distributor results represents.

    Parts are keyed by MPN: any MPN difference means a different part.

      a) the search term, if any result's MPN equals it;
      b) else the MPN of a result whose supplier SKU equals the search term
         (a scanned/typed distributor part number, e.g. a DigiKey PN);
      c) else — only when not ``strict`` — the first result's MPN
         (results are expected in priority order).

    Returns the normalised identity MPN, or "" if none could be established.
    """
    term = _norm_mpn(search_term)
    if term:
        for r in results:
            if _norm_mpn(r.mpn) == term:
                return term
        for r in results:
            if _norm_mpn(r.supplier_sku) == term and _norm_mpn(r.mpn):
                return _norm_mpn(r.mpn)
    if strict:
        return ""
    for r in results:
        if _norm_mpn(r.mpn):
            return _norm_mpn(r.mpn)
    return ""


def merge_part_data(
    results: List[PartData],
    priority_order: Optional[List[str]] = None,
    search_term: str = "",
    strict: bool = False,
) -> Optional[PartData]:
    """
    Merge multiple PartData results into a single unified PartData.

    Only results sharing the same MPN are merged (MPN is the part identity);
    results with any other MPN are dropped and listed in
    ``raw_data['dropped_sources']``.

    Args:
        results: List of PartData from different API sources
        priority_order: List of source names in priority order
                       e.g. ['mouser', 'digikey', 'lcsc']
        search_term: The string that was searched (MPN or distributor PN)
        strict: Require the search term to match a result's MPN or SKU
                exactly; otherwise return None. Use for unattended flows.

    Returns:
        Merged PartData or None if no results
    """
    if not results:
        return None

    # Always run through the full merge path so that raw_data['supplier_data']
    # is built consistently, even for single-source results.
    # This ensures _get_supplier_entries() in part_creator always has the full
    # supplier list to iterate, creating one SupplierPart per distributor found.

    # Default priority
    if priority_order is None:
        priority_order = ["mouser", "digikey", "lcsc"]

    # Sort results by priority
    def sort_key(pd: PartData) -> int:
        try:
            return priority_order.index(pd.source)
        except ValueError:
            return len(priority_order)

    sorted_results = sorted(results, key=sort_key)

    # ── MPN identity: keep only results describing the same MPN ──
    identity = resolve_identity_mpn(sorted_results, search_term, strict)
    if not identity:
        logger.info(
            f"Merge: no exact MPN match for '{search_term}' "
            f"(strict={strict}) – nothing merged"
        )
        return None

    dropped_sources = [
        {"source": r.source, "mpn": r.mpn}
        for r in sorted_results
        if _norm_mpn(r.mpn) != identity
    ]
    sorted_results = [r for r in sorted_results if _norm_mpn(r.mpn) == identity]
    for d in dropped_sources:
        logger.info(f"Merge: dropped {d['source']} – MPN '{d['mpn']}' != '{identity}'")
    if not sorted_results:
        return None

    logger.info(
        f"Merging {len(sorted_results)} results. "
        f"Priority: {[r.source for r in sorted_results]}"
    )

    # Start with the highest-priority result as base
    merged = PartData()

    # Merge simple string fields: first non-empty wins
    for field_name in MERGEABLE_FIELDS:
        for result in sorted_results:
            if result.has_field(field_name):
                setattr(merged, field_name, getattr(result, field_name))
                break

    # Merge parameters: collect all unique parameters
    merged.parameters = _merge_parameters(sorted_results)

    # Merge price breaks: keep all from all sources (tagged by supplier)
    merged.price_breaks = sorted_results[0].price_breaks if sorted_results else []

    # Source metadata
    merged.source = "merged"
    merged.confidence = max(r.confidence for r in sorted_results)

    # Collect all supplier info for later use
    merged.raw_data = {
        "merged_sources": [r.source for r in sorted_results],
        "dropped_sources": dropped_sources,
        "supplier_data": [
            {
                "source": r.source,
                "supplier_name": r.supplier_name,
                # Fallback: if API confirmed a match but returned no SKU,
                # use the MPN so the supplier card is never silently dropped.
                "supplier_sku": r.supplier_sku or r.mpn or "UNKNOWN-SKU",
                "supplier_url": r.supplier_url,
                "price_breaks": [
                    {"qty": pb.quantity, "price": pb.price, "currency": pb.currency}
                    for pb in r.price_breaks
                ],
                "stock": r.stock_available,
            }
            for r in sorted_results
        ],
        # Per-source datasheet URLs so downstream code can fall back if the
        # merged value is empty (highest-priority source may have no datasheet).
        "source_datasheet_urls": [
            {"source": r.source, "url": r.datasheet_url}
            for r in sorted_results
            if r.datasheet_url and r.datasheet_url.startswith("http")
        ],
    }

    logger.info(
        f"Merged result: {merged.mpn} ({merged.manufacturer}) – {len(sorted_results)} source(s)"
    )
    return merged


import re

_PUNCT_RE = re.compile(r"[-_()\[\]/\\.]")
_SPACES_RE = re.compile(r"\s+")


def sanitize_parameter_name(raw_name: str) -> str:
    """
    Sanitize a parameter name by replacing punctuation with a single space,
    collapsing multiple spaces, and returning the lowercased, stripped result.
    """
    if not raw_name:
        return ""
    # Replace punctuation with a space
    s = _PUNCT_RE.sub(" ", raw_name)
    # Collapse multiple spaces
    s = _SPACES_RE.sub(" ", s)
    return s.lower().strip()


def _merge_parameters(results: List[PartData]) -> List[PartParameter]:
    """Merge parameters from all sources, deduplicating by name."""
    from .parameter_normalizer import normalize_parameter_name
    from plugin.registry import registry

    plugin = None
    try:
        plugin = registry.get_plugin("smartparts")
    except Exception:
        pass

    seen_names = set()
    merged_params = []

    from .parameter_normalizer import is_parameter_ignored

    for result in results:
        for param in result.parameters:
            if is_parameter_ignored(param.name, plugin):
                logger.debug(f"Silently dropping ignored parameter: {param.name}")
                continue

            sanitized_name = sanitize_parameter_name(param.name)
            canonical_name = normalize_parameter_name(sanitized_name, plugin)
            param.name = canonical_name

            name_key = canonical_name.lower().strip()
            if name_key not in seen_names:
                seen_names.add(name_key)
                merged_params.append(param)

    return merged_params
