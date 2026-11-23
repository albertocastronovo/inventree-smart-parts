"""Preview, apply or roll back a USD supplier price conversion in the Django shell."""

import json
from decimal import Decimal
from pathlib import Path

from ..services.pricing import get_usd_to_eur_rate, normalize_price


def preview(path):
    """Write a read-only conversion plan and backup; refuse to overwrite it."""
    from company.models import SupplierPriceBreak
    from django.utils import timezone

    tiers = (
        SupplierPriceBreak.objects.filter(price_currency="USD")
        .exclude(price=None)
        .select_related("part__supplier")
        .order_by("pk")
    )
    rate = None
    rows = []
    for tier in tiers.iterator():
        if rate is None:
            rate = get_usd_to_eur_rate()
        amount, currency = normalize_price(tier.price.amount, "USD", rate)
        rows.append(
            {
                "id": tier.pk,
                "supplier_part_id": tier.part_id,
                "supplier": tier.part.supplier.name,
                "sku": tier.part.SKU,
                "quantity": str(tier.quantity),
                "updated": tier.updated.isoformat(),
                "before": {"amount": str(tier.price.amount), "currency": "USD"},
                "after": {"amount": str(amount), "currency": currency},
            }
        )

    plan = {
        "type": "smartparts-supplier-usd-to-eur-v1",
        "created_at": timezone.now().isoformat(),
        "eur_per_usd": str(rate.eur_per_usd) if rate else None,
        "rates_fetched_at": rate.fetched_at.isoformat() if rate else None,
        "rows": rows,
    }
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(plan, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return {
        "candidates": len(rows),
        "eur_per_usd": plan["eur_per_usd"],
        "file": str(path),
    }


def _change(path, *, reverse):
    from company.models import SupplierPriceBreak
    from django.db import transaction
    from djmoney.money import Money

    with Path(path).open(encoding="utf-8") as source:
        plan = json.load(source)
    if plan.get("type") != "smartparts-supplier-usd-to-eur-v1":
        raise ValueError("Not a SmartParts supplier price conversion plan")

    before, after = ("after", "before") if reverse else ("before", "after")
    changed, skipped = [], []
    with transaction.atomic():
        for row in sorted(plan["rows"], key=lambda row: row["id"]):
            tier = (
                SupplierPriceBreak.objects.select_for_update()
                .filter(pk=row["id"])
                .first()
            )
            if (
                tier is None
                or tier.part_id != row["supplier_part_id"]
                or tier.quantity != Decimal(row["quantity"])
                or tier.price is None
                or tier.price.amount != Decimal(row[before]["amount"])
                or tier.price_currency != row[before]["currency"]
                or (not reverse and tier.updated.isoformat() != row["updated"])
            ):
                skipped.append(row["id"])
                continue
            tier.price = Money(Decimal(row[after]["amount"]), row[after]["currency"])
            tier.full_clean()
            tier.save()  # InvenTree's model hooks schedule part pricing updates.
            changed.append(row["id"])
    return {"changed": changed, "skipped": skipped}


def apply(path):
    """Apply a previously reviewed plan; keep the saved rate for the whole run."""
    return _change(path, reverse=False)


def rollback(path):
    """Restore originals still matching the recorded EUR result."""
    return _change(path, reverse=True)
