"""
Rename parts that were created with a DigiKey part number as their name.

Before v1.4.0 the DigiKey client read the v3 field ``ManufacturerPartNumber``
(absent in API v4) and the barcode parsers fed the DigiKey PN into the MPN
slot, so PureScan / editor parts got ``Part.name`` and
``ManufacturerPart.MPN`` = DigiKey PN (e.g. ``296-1234-1-ND``).

This script looks up the real MPN via DigiKey ProductDetails and fixes both.

Usage (InvenTree Django shell)::

    from inventree_smart_parts.maintenance import rename_digikey_parts
    rename_digikey_parts.run()               # dry run: prints the plan
    rename_digikey_parts.run(dry_run=False)  # apply

Parts whose real MPN is already used by another part are reported as
CONFLICT and left untouched (merge those manually).
"""

import logging

from . import digikey_supplier_parts, get_digikey_client, print_table

logger = logging.getLogger("inventree_smart_parts.maintenance.names")

NAME_MAX = 100


def _candidates():
    """(supplier_part, manufacturer_parts_equal_to_sku) where the part looks
    like it was named / MPN'd with the DigiKey SKU."""
    from company.models import ManufacturerPart

    seen_parts = set()
    for sp in digikey_supplier_parts():
        sku = (sp.SKU or "").strip()
        part = sp.part
        if not sku or part is None or part.pk in seen_parts:
            continue
        bad_mfr = list(ManufacturerPart.objects.filter(part=part, MPN__iexact=sku))
        name_is_sku = (part.name or "").strip().lower() == sku.lower()
        if name_is_sku or bad_mfr:
            seen_parts.add(part.pk)
            yield sp, name_is_sku, bad_mfr


def _conflict(part, mpn):
    """Another part already uses ``mpn`` as name or ManufacturerPart.MPN?"""
    from company.models import ManufacturerPart
    from part.models import Part

    other = Part.objects.filter(name__iexact=mpn).exclude(pk=part.pk).first()
    if other:
        return other
    mp = (
        ManufacturerPart.objects.filter(MPN__iexact=mpn)
        .exclude(part=part)
        .select_related("part")
        .first()
    )
    return mp.part if mp else None


def run(dry_run=True, limit=None):
    """Find and (optionally) fix parts named with a DigiKey PN.

    Returns a summary dict.
    """
    from django.db import transaction

    _plugin, client = get_digikey_client()

    rows = []
    summary = {"candidates": 0, "fixed": 0, "conflict": 0, "skipped": 0, "error": 0}

    for i, (sp, name_is_sku, bad_mfr) in enumerate(_candidates()):
        if limit is not None and i >= limit:
            break
        summary["candidates"] += 1
        part = sp.part
        sku = sp.SKU.strip()

        try:
            details = client.get_product_details(sku)
        except Exception as e:
            summary["error"] += 1
            rows.append([part.pk, part.name, "", sku, f"ERROR: {e}"])
            continue

        mpn = (details.mpn if details else "").strip()
        if not mpn or mpn.lower() == sku.lower():
            summary["skipped"] += 1
            rows.append([part.pk, part.name, "", sku, "SKIP: no MPN from DigiKey"])
            continue

        new_name = mpn[:NAME_MAX] if name_is_sku else part.name
        other = _conflict(part, mpn)
        if other is not None:
            summary["conflict"] += 1
            rows.append(
                [part.pk, part.name, mpn, sku, f"CONFLICT: part #{other.pk} uses it"]
            )
            continue

        actions = []
        if name_is_sku:
            actions.append("rename")
        if bad_mfr:
            actions.append(f"fix {len(bad_mfr)} MPN")
        status = ("WOULD " if dry_run else "") + " + ".join(actions)

        if not dry_run:
            try:
                with transaction.atomic():
                    if name_is_sku:
                        part.name = new_name
                        part.full_clean()
                        part.save()
                    for mp in bad_mfr:
                        mp.MPN = mpn
                        mp.save()
                summary["fixed"] += 1
            except Exception as e:
                summary["error"] += 1
                status = f"ERROR: {e}"
        rows.append([part.pk, part.name if dry_run else new_name, mpn, sku, status])

    print_table(rows, ["part", "current name", "real MPN", "DigiKey SKU", "status"])
    print()
    print(("DRY RUN — nothing changed. " if dry_run else "") + str(summary))
    return summary
