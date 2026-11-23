"""Repair native lookup for unambiguous legacy PureScan STOCK_/LOC_ labels."""

from ..services.token_manager import (
    TOKEN_PATTERNS,
    barcode_field_name,
    hash_token,
    legacy_hash_token,
    lock_token,
)


def run(dry_run=True):
    """Report candidates by default. Apply only confirmed legacy hashes."""
    from django.db import transaction
    from django.db.models import Q
    from plugin.base.barcodes.helper import get_supported_barcode_models_map
    from stock.models import StockItem, StockLocation

    models = get_supported_barcode_models_map().values()
    results = []
    for model, prefix in ((StockItem, "STOCK"), (StockLocation, "LOC")):
        field = barcode_field_name(model)
        if not field:
            continue
        pattern = TOKEN_PATTERNS[prefix].pattern
        candidates = model.objects.filter(**{f"{field}__regex": pattern}).order_by("pk")
        for pk, token, legacy in candidates.values_list(
            "pk", field, "barcode_hash"
        ).iterator():
            native = hash_token(token)
            if legacy != legacy_hash_token(token) or legacy == native:
                continue
            with transaction.atomic():
                lock_token(token)
                item = (
                    model.objects.select_for_update()
                    .filter(pk=pk, barcode_hash=legacy, **{field: token})
                    .first()
                )
                if item is None:
                    status = "changed since scan"
                elif any(
                    _has_conflict(other, model, pk, token, native, legacy, Q)
                    for other in models
                ):
                    status = "conflict"
                elif dry_run:
                    status = "would repair"
                else:
                    # Preserve the existing label text and stock identity.
                    item.assign_barcode(barcode_data=token)
                    status = "repaired"
                results.append(
                    {"model": prefix, "id": pk, "token": token, "status": status}
                )
    return results


def _has_conflict(other, model, pk, token, native, legacy, Q):
    matches = Q(barcode_hash__in=(native, legacy))
    field = barcode_field_name(other)
    if field:
        matches |= Q(**{f"{field}__iexact": token})
    candidates = other.objects.filter(matches)
    if other is model:
        candidates = candidates.exclude(pk=pk)
    return candidates.exists()
