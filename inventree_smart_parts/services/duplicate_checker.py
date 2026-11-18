"""
Duplicate Checker
=================
Detects existing parts in InvenTree by MPN matching.
Returns existing part info with options to update or skip.
"""

import logging
from typing import Optional, Dict, List
from dataclasses import dataclass

logger = logging.getLogger("inventree_smart_parts.services.duplicates")


@dataclass
class DuplicateResult:
    """Result of a duplicate check."""

    is_duplicate: bool = False
    existing_part_id: Optional[int] = None
    existing_part_name: str = ""
    existing_mpn: str = ""
    existing_manufacturer: str = ""
    match_type: str = ""  # 'exact_mpn', 'fuzzy_mpn', 'name_match'
    confidence: float = 0.0


def check_duplicate(mpn: str, manufacturer: str = "") -> DuplicateResult:
    """
    Check if a part with the given MPN already exists in InvenTree.

    Parts are keyed by MPN: only an exact (case-insensitive, whitespace-
    stripped) MPN match counts as a duplicate. A slightly different MPN
    (e.g. a -TR or #PBF variant) is a different part. A match from the
    given manufacturer is preferred when several parts share the MPN.

    Args:
        mpn: Manufacturer Part Number to check
        manufacturer: Optional manufacturer name for stricter matching

    Returns:
        DuplicateResult with match details
    """
    if not mpn:
        return DuplicateResult()

    try:
        from company.models import ManufacturerPart

        mpn_clean = mpn.strip()

        matches = ManufacturerPart.objects.filter(MPN__iexact=mpn_clean)
        if manufacturer:
            matches_mfr = matches.filter(
                manufacturer__name__iexact=manufacturer.strip()
            )
            if matches_mfr.exists():
                matches = matches_mfr

        match = matches.select_related("part", "manufacturer").first()
        if match is None or match.part is None:
            return DuplicateResult()

        return DuplicateResult(
            is_duplicate=True,
            existing_part_id=match.part.pk,
            existing_part_name=match.part.name,
            existing_mpn=match.MPN,
            existing_manufacturer=(
                str(match.manufacturer) if match.manufacturer else ""
            ),
            match_type="exact_mpn",
            confidence=1.0 if match.MPN == mpn_clean else 0.95,
        )

    except ImportError:
        logger.error("Could not import InvenTree models – running outside InvenTree?")
        return DuplicateResult()
    except Exception as e:
        logger.error(f"Duplicate check failed: {e}")
        return DuplicateResult()


def check_duplicates_batch(mpn_list: List[str]) -> Dict[str, DuplicateResult]:
    """
    Check multiple MPNs for duplicates in a single operation.

    Returns a dict mapping MPN -> DuplicateResult.
    """
    results = {}
    for mpn in mpn_list:
        results[mpn] = check_duplicate(mpn)
    return results
