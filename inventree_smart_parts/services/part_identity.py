"""Resolve warehouse scans without guessing between multiple parts."""

import hashlib


class AmbiguousPart(ValueError):
    """A barcode identifies more than one inventory part."""


def resolve_scanned_part(mpn, sku="", manufacturer=""):
    from django.db.models import Q
    from company.models import ManufacturerPart, SupplierPart
    from part.models import Part

    def unique_id(query, field):
        ids = list(query.order_by(field).values_list(field, flat=True).distinct()[:2])
        if len(ids) > 1:
            raise AmbiguousPart(
                f"Multiple parts match {mpn or sku}; select the correct part in SmartParts"
            )
        return ids[0] if ids else None

    if mpn:
        matches = ManufacturerPart.objects.filter(MPN__iexact=mpn)
        if manufacturer:
            same_mfr = matches.filter(manufacturer__name__iexact=manufacturer)
            if same_mfr.exists():
                matches = same_mfr
        if pk := unique_id(matches, "part_id"):
            return Part.objects.get(pk=pk)
        if pk := unique_id(
            Part.objects.filter(Q(IPN__iexact=mpn) | Q(name__iexact=mpn)), "pk"
        ):
            return Part.objects.get(pk=pk)
    for code in dict.fromkeys(code for code in (sku, mpn) if code):
        if pk := unique_id(SupplierPart.objects.filter(SKU__iexact=code), "part_id"):
            return Part.objects.get(pk=pk)
    return None


def lock_part_identity(mpn):
    """Serialize PureScan creates for a canonical MPN on PostgreSQL."""
    from django.db import connection

    if connection.vendor == "postgresql":
        digest = hashlib.sha256(mpn.strip().casefold().encode()).digest()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [0x53505041, int.from_bytes(digest[:4], "big", signed=True)],
            )
