"""
Part Creator
============
Orchestrates the creation of InvenTree Part, ManufacturerPart,
and SupplierPart records from merged PartData.
"""

import logging
from decimal import Decimal, DecimalException
import tempfile
import os
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

from ..api_clients.base import PartData, PriceBreak

logger = logging.getLogger("inventree_smart_parts.services.creator")

# ── Shared sentinel values: empty/placeholder parameter values ────────────────
# Used by both _create_parameters (DB writes) and the parameter_normalizer
# (display filtering) to guarantee consistent sanitisation.
EMPTY_SENTINELS: frozenset = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "null",
        "none",
        "unknown",
        "not specified",
        "not applicable",
        "tbd",
        "tba",
        "?",
        "\u2013",
        "\u2014",
    }
)


def is_useless_value(value) -> bool:
    """Return True if a parameter value is empty or a known placeholder."""
    if value is None:
        return True
    stripped = str(value).strip()
    return not stripped or stripped.lower() in EMPTY_SENTINELS


@dataclass
class CreationResult:
    """Result of a part creation operation."""

    success: bool = False
    part_id: Optional[int] = None
    part_name: str = ""
    action: str = ""  # 'created', 'updated', 'skipped'
    message: str = ""
    # Separate 'new' from 'already existed' for accurate reporting
    manufacturer_parts_created: List[int] = field(default_factory=list)
    manufacturer_parts_existing: List[int] = field(default_factory=list)
    supplier_parts_created: List[int] = field(default_factory=list)
    supplier_parts_existing: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # Audit list of parameters excluded by LIMIT_PARAMETERS_TO_CATEGORY
    dropped_parameters: List[Dict] = field(default_factory=list)
    # Human-readable list of what was written to an existing part
    # (e.g. "Mouser supplier part 81-XYZ", "Manufacturer Std Lead Time")
    filled: List[str] = field(default_factory=list)

    # Legacy shim so existing call-sites that read .manufacturer_parts / .supplier_parts still work
    @property
    def manufacturer_parts(self) -> List[int]:
        return self.manufacturer_parts_created + self.manufacturer_parts_existing

    @property
    def supplier_parts(self) -> List[int]:
        return self.supplier_parts_created + self.supplier_parts_existing


def _default_priority_order() -> List[str]:
    from .data_merger import get_priority_order

    plugin = None
    try:
        from plugin.registry import registry

        plugin = registry.get_plugin("smartparts")
    except Exception:
        pass
    return get_priority_order(plugin)


class _WriteContext:
    """Per-operation state for priority-aware field writes (see provenance)."""

    def __init__(self, part_data: PartData, priority_order: List[str], prov: Dict):
        self.data = part_data
        self.priority = priority_order
        self.prov = prov
        self.field_sources: Dict[str, str] = dict(
            (part_data.raw_data or {}).get("field_sources") or {}
        )
        self.filled: List[str] = []
        self.is_update = False

    def source_of(self, field_name: str) -> str:
        return self.field_sources.get(field_name, "") or ""

    def allowed(self, key: str, current, source: str) -> bool:
        from . import provenance

        return provenance.should_write(
            provenance.entry(self.prov, key), current, source, self.priority
        )

    def category_allowed(self, current, source: str) -> bool:
        """Priority rule, or a hierarchical source upgrading a flat one."""
        from . import provenance

        return self.allowed("category", current, source) or (
            provenance.category_upgrade_allowed(
                provenance.entry(self.prov, "category"), current, source
            )
        )

    def wrote(self, key: str, source: str, value, label: str = "") -> None:
        from . import provenance

        provenance.record(self.prov, key, source, value)
        if label:
            self.filled.append(label)


def create_part_from_data(
    part_data: PartData,
    category_id: Optional[int] = None,
    update_existing: bool = False,
    existing_part_id: Optional[int] = None,
    auto_create_companies: bool = True,
    priority_order: Optional[List[str]] = None,
) -> CreationResult:
    """
    Create or update an InvenTree Part from merged PartData.

    Creates:
    1. Part (core component record)
    2. ManufacturerPart (links to manufacturer company)
    3. SupplierPart (for each supplier with SKU)

    Updates of an existing part are priority-aware (services/provenance.py):
    empty fields are filled; a value is only replaced when a higher-priority
    source than the one that wrote it supplies a new one and the value was
    not edited since. Data without source info (dashboard editor) only fills
    empty fields.

    Args:
        part_data: Merged/normalized part data
        category_id: InvenTree category ID (None = uncategorized)
        update_existing: Whether to update an existing part
        existing_part_id: ID of existing part to update
        auto_create_companies: Auto-create manufacturer/supplier companies
        priority_order: Source priority (defaults to the API_PRIORITY setting)

    Returns:
        CreationResult with details of what was created
    """
    result = CreationResult()

    try:
        from part.models import Part, PartCategory
        from company.models import (
            Company,
            ManufacturerPart,
            SupplierPart,
        )
        from django.db import transaction
        from . import provenance

        if priority_order is None:
            priority_order = _default_priority_order()

        # ── Pre-flight: reject structural categories immediately ───────────────
        # InvenTree forbids assigning parts to structural (group-header)
        # categories.  Catching the validation error after Part.save() and
        # silently stripping the category is wrong - it creates orphaned
        # parts.  Instead we refuse immediately with a clear error.
        if category_id:
            try:
                _cat = PartCategory.objects.get(pk=category_id)
                if getattr(_cat, "structural", False):
                    result.success = False
                    result.message = (
                        f"Category '{_cat.name}' (id={category_id}) is structural "
                        f"and cannot be assigned to parts. "
                        f"Please select a non-structural (leaf) category."
                    )
                    result.errors.append(result.message)
                    logger.warning(
                        "create_part_from_data: refused structural category "
                        "id=%s ('%s') for MPN '%s'",
                        category_id,
                        _cat.name,
                        part_data.mpn,
                    )
                    return result
            except PartCategory.DoesNotExist:
                # Category gone between UI load and save; treat as uncategorized
                logger.warning(
                    "create_part_from_data: category_id=%s not found; "
                    "proceeding uncategorized",
                    category_id,
                )
                category_id = None

        with transaction.atomic():
            # ── Step 1: Get or create the Part ──
            is_update = bool(existing_part_id and update_existing)
            if is_update:
                part = Part.objects.get(pk=existing_part_id)
                ctx = _WriteContext(part_data, priority_order, provenance.load(part))
                ctx.is_update = True
                _update_existing_part(part, part_data, category_id, ctx)
                result.action = "updated"
            else:
                ctx = _WriteContext(part_data, priority_order, provenance.empty())
                part = _create_new_part(part_data, category_id)
                _record_new_part_provenance(part, part_data, category_id, ctx)
                result.action = "created"

            result.part_id = part.pk
            result.part_name = part.name

            # ── Step 2: ManufacturerPart ──
            if part_data.manufacturer and part_data.mpn:
                mfr_part, mfr_created = _create_manufacturer_part(
                    part, part_data, auto_create_companies
                )
                if mfr_part:
                    if mfr_created:
                        result.manufacturer_parts_created.append(mfr_part.pk)
                    else:
                        result.manufacturer_parts_existing.append(mfr_part.pk)

            # ── Step 3: SupplierParts ──
            # Each distributor owns its SupplierPart: a missing one is
            # created, an existing one gets its quoted tiers refreshed in
            # place (USD normalised to EUR, see services/pricing.py).
            from .pricing import PriceSnapshot

            supplier_entries = _get_supplier_entries(part_data)
            price_snapshot = PriceSnapshot()
            logger.debug(
                f"Supplier pipeline for '{part_data.mpn}': "
                f"{len(supplier_entries)} entries to create or refresh"
            )

            for i, entry in enumerate(supplier_entries):
                try:
                    # Each supplier gets its own savepoint so a constraint
                    # failure on one distributor doesn't roll back the rest.
                    with transaction.atomic():
                        stats: Dict[str, int] = {}
                        sup_part, sup_created = _create_supplier_part(
                            part,
                            part_data,
                            entry,
                            auto_create_companies,
                            price_snapshot=price_snapshot,
                            pricing_warnings=result.errors,
                            stats=stats,
                        )
                        if sup_part:
                            if sup_created:
                                result.supplier_parts_created.append(sup_part.pk)
                                if is_update:
                                    ctx.filled.append(
                                        f"{entry['name']} supplier part {entry['sku']}"
                                    )
                            else:
                                result.supplier_parts_existing.append(sup_part.pk)
                                if is_update and stats.get("price_breaks_changed"):
                                    ctx.filled.append(f"{entry['name']} prices updated")
                except Exception as e:
                    logger.error(
                        f"Failed to create SupplierPart "
                        f"{entry.get('name', '?')}:{entry.get('sku', '?')} "
                        f"for '{part_data.mpn}': {e}",
                        exc_info=True,
                    )
                    result.errors.append(
                        f"Supplier link failed ({entry.get('name', '?')}): {e}"
                    )

            # ── Step 4: Core parameters (v1.3.0) ──
            _create_core_parameters(part, part_data, ctx)

            # ── Step 4b: Distributor spec parameters ──
            # De-duplicate: parameters promoted to dedicated core templates
            # would otherwise create duplicate rows for the same template.
            core_names = {name.lower() for name, _ in CORE_PARAMETER_TEMPLATES}
            spec_params = [
                p
                for p in part_data.parameters
                if p.name.strip().lower() not in core_names
            ]
            if spec_params:
                dropped = _create_parameters(
                    part,
                    spec_params,
                    category_id=category_id,
                    ctx=ctx,
                )
                result.dropped_parameters = dropped

            # ── Step 5: Media Import (Image & Datasheet) ──
            from .image_handler import attach_datasheet_to_part

            _import_image(part, part_data, ctx, is_update)

            # Datasheet attachment only when Part.link was (re)written from it
            if part_data.datasheet_url and (
                not is_update or "Datasheet link" in ctx.filled
            ):
                attach_datasheet_to_part(part, part_data.datasheet_url)

            # ── Step 6: Persist provenance ──
            provenance.save(part, ctx.prov)
            result.filled = list(ctx.filled)

            result.success = True
            # Build an accurate human-readable summary
            parts = []
            mc = len(result.manufacturer_parts_created)
            me = len(result.manufacturer_parts_existing)
            sc = len(result.supplier_parts_created)
            se = len(result.supplier_parts_existing)
            if mc or me:
                bits = []
                if mc:
                    bits.append(f"{mc} created")
                if me:
                    bits.append(f"{me} already existed")
                parts.append(f"manufacturer part ({', '.join(bits)})")
            if sc or se:
                bits = []
                if sc:
                    bits.append(f"{sc} created")
                if se:
                    bits.append(f"{se} already existed")
                parts.append(f"supplier part ({', '.join(bits)})")
            suffix = (": " + "; ".join(parts)) if parts else " (no linked parts)"
            result.message = f"'{part.name}' {result.action} successfully{suffix}."
            if is_update:
                result.message += (
                    f" Filled: {', '.join(result.filled)}."
                    if result.filled
                    else " Nothing to fill."
                )

    except Exception as e:
        logger.error(f"Part creation failed: {e}", exc_info=True)
        result.success = False
        result.message = f"Creation failed: {str(e)}"
        result.errors.append(str(e))

    return result


def _import_image(part, data: PartData, ctx: "_WriteContext", is_update: bool):
    """Download and attach the best image, priority-aware for updates.

    Candidates come from raw_data['source_image_urls'] (priority order) and
    the merged image_url. When replacing an existing distributor image only
    sources ranking above the one that supplied it are tried.
    """
    from . import provenance
    from .image_handler import download_best_image, attach_image_to_part

    urls, seen = [], set()
    for entry in (data.raw_data or {}).get("source_image_urls", []) or []:
        url = (entry or {}).get("url", "")
        if url and url not in seen:
            urls.append({"url": url, "source": entry.get("source", "") or ""})
            seen.add(url)
    if data.image_url and data.image_url not in seen:
        urls.append({"url": data.image_url, "source": ctx.source_of("image_url")})
    if not urls:
        return

    current = part.image.name if getattr(part, "image", None) else ""
    if is_update and not provenance.is_empty(current):
        urls = [u for u in urls if ctx.allowed("image", current, u["source"])]
        if not urls:
            return

    img_data, ext, source, err = download_best_image(urls)
    if not img_data:
        logger.info(f"Image not imported (non-critical) for Part {part.pk}: {err}")
        return
    img_result = attach_image_to_part(part, img_data, ext, source)
    if img_result.success:
        src = source if source in ctx.priority else ""
        ctx.wrote("image", src, part.image.name, "Image" if is_update else "")
        logger.info(f"Image: {img_result.message}")


def _record_new_part_provenance(part, data: PartData, category_id, ctx) -> None:
    """Remember which source supplied the fields of a freshly created part."""
    if data.description:
        ctx.wrote("description", ctx.source_of("description"), part.description)
    else:
        # Synthesised "<Manufacturer> <MPN>" placeholder: any distributor may
        # replace it later.
        ctx.wrote("description", "fallback", part.description)
    if part.link:
        ctx.wrote("link", ctx.source_of("datasheet_url"), part.link)
    if part.category_id:
        ctx.wrote("category", ctx.source_of("category"), str(part.category_id))


def _create_new_part(data: PartData, category_id: Optional[int]) -> "Part":
    """Create a new InvenTree Part.

    Uses ``full_clean()`` + ``save()`` instead of ``objects.create()`` so that
    InvenTree model validation (e.g. structural-category rejection) raises a
    ``ValidationError`` that propagates to the caller's ``transaction.atomic()``
    block and triggers a full rollback.  There is no silent stripping of the
    category on validation failure.
    """
    from part.models import Part, PartCategory

    category = None
    if category_id:
        try:
            category = PartCategory.objects.get(pk=category_id)
        except PartCategory.DoesNotExist:
            logger.warning(f"Category ID {category_id} not found")

    # Explicit name (e.g. edited in the GUI editor) wins; API flows set
    # name == mpn, so for them this is still the MPN.
    name = (data.name or data.mpn or "").strip() or "Unknown"

    # Truncate name if too long
    if len(name) > 100:
        name = name[:97] + "..."

    part = Part(
        name=name,
        description=(
            data.description[:250]
            if data.description
            else f"{data.manufacturer} {data.mpn}"
        ),
        category=category,
        IPN="",  # Let InvenTree auto-generate if configured
        active=True,
        virtual=False,
        component=True,
        purchaseable=True,
        link=data.datasheet_url or "",
    )
    # full_clean() runs Django model validators (including InvenTree's check
    # that the category is not structural).  Any ValidationError raised here
    # will propagate up through transaction.atomic() and roll back everything.
    part.full_clean()
    part.save()

    logger.info(f"Created Part: {part.name} (ID: {part.pk})")
    return part


def _update_existing_part(
    part, data: PartData, category_id: Optional[int], ctx: "_WriteContext"
) -> "Part":
    """Update an existing InvenTree Part, priority-aware.

    A field is written when it is empty, or when its value was written by a
    lower-priority source and not edited since (see provenance.should_write).
    part.name is never touched (MPN identity; may be renamed manually).
    """
    from part.models import PartCategory

    changed = False

    desc = (data.description or "")[:250]
    src = ctx.source_of("description")
    if (
        desc
        and desc != part.description
        and ctx.allowed("description", part.description, src)
    ):
        part.description = desc
        ctx.wrote("description", src, desc, "Description")
        changed = True

    src = ctx.source_of("datasheet_url")
    if (
        data.datasheet_url
        and data.datasheet_url != part.link
        and ctx.allowed("link", part.link, src)
    ):
        part.link = data.datasheet_url
        ctx.wrote("link", src, part.link, "Datasheet link")
        changed = True

    src = ctx.source_of("category")
    current_cat = str(part.category_id) if part.category_id else ""
    if (
        category_id
        and str(category_id) != current_cat
        and ctx.category_allowed(current_cat, src)
    ):
        try:
            part.category = PartCategory.objects.get(pk=category_id)
            ctx.wrote("category", src, str(category_id), "Category")
            changed = True
        except PartCategory.DoesNotExist:
            pass

    if changed:
        part.save()
    logger.info(f"Updated Part: {part.name} (ID: {part.pk}, changed={changed})")
    return part


def set_default_supplier_if_empty(part_id: int, supplier_name: str) -> str:
    """Make ``supplier_name``'s SupplierPart the part's default supplier.

    Only when the part has no default supplier yet: a choice made in
    InvenTree is never overwritten. Returns a "filled" label, or "".
    """
    if not part_id or not supplier_name:
        return ""
    try:
        from company.models import SupplierPart
        from part.models import Part

        part = Part.objects.filter(pk=part_id).first()
        if part is None or getattr(part, "default_supplier_id", None):
            return ""
        sup_part = (
            SupplierPart.objects.filter(
                part_id=part_id, supplier__name__iexact=supplier_name
            )
            .order_by("pk")
            .first()
        )
        if sup_part is None:
            return ""
        part.default_supplier = sup_part
        part.save(update_fields=["default_supplier"])
        logger.info(f"Default supplier of '{part.name}' set to {supplier_name}")
        return f"Default supplier: {supplier_name}"
    except Exception as e:
        logger.warning(f"Could not set default supplier for part {part_id}: {e}")
        return ""


def _create_manufacturer_part(
    part, data: PartData, auto_create: bool
) -> Tuple[Optional["ManufacturerPart"], bool]:
    """Create a ManufacturerPart linking Part to Manufacturer.

    Uses get_or_create keyed on (part, manufacturer, MPN) so that:
    - Multiple suppliers sharing the same MPN reuse one ManufacturerPart
    - Concurrent requests or retries don't hit IntegrityError

    Returns (manufacturer_part, was_created).
    """
    from company.models import Company, ManufacturerPart

    # MPN is the identity: if this part already has a ManufacturerPart with
    # the MPN, reuse it whatever the manufacturer is called by this
    # distributor ("Vishay Dale" vs "Vishay / Dale") — no second MP/company.
    existing_mp = ManufacturerPart.objects.filter(
        part=part, MPN__iexact=(data.mpn or "").strip()
    ).first()
    if existing_mp:
        return existing_mp, False

    # Get or create the manufacturer company
    manufacturer = _get_or_create_company(
        name=data.manufacturer,
        is_manufacturer=True,
        auto_create=auto_create,
    )
    if not manufacturer:
        logger.warning(f"Could not find/create manufacturer: {data.manufacturer}")
        return None, False

    mfr_part, created = ManufacturerPart.objects.get_or_create(
        part=part,
        manufacturer=manufacturer,
        MPN=data.mpn,
        defaults={
            "link": data.supplier_url or "",
        },
    )

    if created:
        logger.info(f"Created ManufacturerPart: {data.mpn} (ID: {mfr_part.pk})")
    else:
        logger.debug(f"ManufacturerPart already exists: {data.mpn} (ID: {mfr_part.pk})")

    return mfr_part, created


def _get_supplier_entries(data: PartData) -> List[Dict]:
    """
    Extract supplier entries from merged PartData.

    All supplier info lives in raw_data['supplier_data'] (populated by the
    merger for every source that returned a result).  We iterate that list
    exclusively so every distributor gets a SupplierPart.

    Deduplicate the input by supplier and SKU; existing supplier parts still
    need to be processed so their price tiers can be updated. Pure function:
    no database access (_create_supplier_part decides create vs. refresh).
    """
    entries = []
    seen_keys: set = set()  # lower-case "name:sku" keys

    def _mem_key(name: str, sku: str) -> str:
        return f"{name.strip().lower()}:{sku.strip().lower()}"

    # ── Primary path: iterate raw_data['supplier_data'] (all API sources) ──
    supplier_data_list = data.raw_data.get("supplier_data", [])
    logger.debug(
        f"_get_supplier_entries: {len(supplier_data_list)} source(s) in raw_data "
        f"for MPN '{data.mpn}'"
    )

    for sd in supplier_data_list:
        name = (sd.get("supplier_name") or "").strip()
        sku = (sd.get("supplier_sku") or "").strip()
        src = sd.get("source", "?")

        if not name or not sku:
            logger.debug(f"  [{src}] Skipped – missing supplier_name or supplier_sku")
            continue

        key = _mem_key(name, sku)
        if key in seen_keys:
            logger.debug(
                f"  [{src}] Skipped – duplicate in current batch ({name}:{sku})"
            )
            continue

        pbs = [
            PriceBreak(
                quantity=pb["qty"],
                price=pb["price"],
                currency=pb.get("currency", "EUR"),
            )
            for pb in sd.get("price_breaks", [])
        ]
        entries.append(
            {
                "name": name,
                "sku": sku,
                "url": sd.get("supplier_url", ""),
                "price_breaks": pbs,
            }
        )
        seen_keys.add(key)
        logger.debug(f"  [{src}] Queued SupplierPart: {name} / {sku}")

    # ── Fallback: if raw_data was empty (e.g. single-source non-merged path),
    #    use the top-level supplier fields from PartData directly. ──
    if not supplier_data_list and data.supplier_name and data.supplier_sku:
        name, sku = data.supplier_name.strip(), data.supplier_sku.strip()
        key = _mem_key(name, sku)
        if key not in seen_keys:
            entries.append(
                {
                    "name": name,
                    "sku": sku,
                    "url": data.supplier_url,
                    "price_breaks": data.price_breaks,
                }
            )
            logger.debug(f"  [fallback] Queued SupplierPart: {name} / {sku}")

    logger.info(
        f"_get_supplier_entries → {len(entries)} to create or refresh for MPN "
        f"'{data.mpn}': {[e['name'] + ':' + e['sku'] for e in entries]}"
    )
    return entries


def _create_supplier_part(
    part,
    data: PartData,
    supplier_entry: Dict,
    auto_create: bool,
    *,
    price_snapshot=None,
    pricing_warnings: Optional[List[str]] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Tuple[Optional["SupplierPart"], bool]:
    """Create or refresh the SupplierPart linking ``part`` to a distributor.

    Each distributor owns its SupplierPart (matched by supplier + SKU,
    case-insensitive):
    - missing  -> created with the quoted price tiers;
    - existing -> manufacturer part / link filled only when empty, quoted
      tiers refreshed in place (_sync_price_breaks); tiers the distributor
      did not quote are kept.

    ``stats["price_breaks_changed"]`` receives the number of tiers created or
    changed. Returns (supplier_part, was_created).
    """
    from company.models import ManufacturerPart, SupplierPart

    if price_snapshot is None:
        from .pricing import PriceSnapshot

        price_snapshot = PriceSnapshot()
    if pricing_warnings is None:
        pricing_warnings = []

    supplier_name = supplier_entry["name"]
    sku = supplier_entry["sku"]
    new_price_breaks = supplier_entry.get("price_breaks", [])

    supplier = _get_or_create_company(
        name=supplier_name,
        is_supplier=True,
        auto_create=auto_create,
    )
    if not supplier:
        logger.warning(f"Could not find/create supplier: {supplier_name}")
        return None, False

    # Reuse the part's ManufacturerPart (case-insensitive) — critical when
    # multiple distributors share the same MPN from the same manufacturer.
    mfr_part = ManufacturerPart.objects.filter(
        part=part,
        MPN__iexact=data.mpn,
    ).first()

    sup_part = SupplierPart.objects.filter(
        part=part, supplier=supplier, SKU__iexact=sku
    ).first()
    was_created = False
    if sup_part is None:
        sup_part, was_created = SupplierPart.objects.get_or_create(
            part=part,
            supplier=supplier,
            SKU=sku,
            defaults={
                "link": supplier_entry.get("url", ""),
                "manufacturer_part": mfr_part,
            },
        )

    if not was_created:
        update_fields = []

        # Backfill manufacturer_part if missing (created before MfrPart existed)
        if not sup_part.manufacturer_part and mfr_part:
            sup_part.manufacturer_part = mfr_part
            update_fields.append("manufacturer_part")
            logger.info(f"Backfilled manufacturer_part on SupplierPart {sup_part.pk}")

        # Fill URL if missing (never overwrite)
        new_url = supplier_entry.get("url", "")
        if new_url and not sup_part.link:
            sup_part.link = new_url
            update_fields.append("link")

        if update_fields:
            sup_part.save(update_fields=update_fields)

        logger.debug(
            f"SupplierPart already existed: {sku} @ {supplier_name} (ID: {sup_part.pk})"
        )

    changed = _sync_price_breaks(
        sup_part,
        new_price_breaks,
        price_snapshot,
        pricing_warnings,
        label=f"{supplier_name} {sku}",
    )
    if stats is not None:
        stats["price_breaks_changed"] = changed

    if was_created:
        logger.info(
            f"Created SupplierPart: {sku} @ {supplier_name} (ID: {sup_part.pk})"
        )

    return sup_part, was_created


def _sync_price_breaks(
    sup_part, price_breaks, price_snapshot, pricing_warnings, label: str
) -> int:
    """Upsert the quoted price tiers of one SupplierPart.

    The whole quote is normalised first (USD -> EUR with InvenTree's stored
    rates, services/pricing.py): a missing rate or an invalid tier skips the
    entire quote with a warning, so stored prices are never replaced by a
    partial or mislabelled set. Tiers are updated in place; quantities the
    distributor did not quote are kept. Returns the number of tiers created
    or changed.
    """
    from django.db import transaction
    from company.models import SupplierPart, SupplierPriceBreak
    from djmoney.money import Money
    from .pricing import PricingError

    changed = 0
    try:
        # Normalize the entire quoted set first. A missing rate or invalid tier
        # must not replace any of this supplier's previously stored prices.
        prices = []
        for pb in price_breaks:
            try:
                quantity = Decimal(str(pb.quantity))
            except DecimalException as exc:
                raise PricingError("Invalid price break quantity") from exc
            if not quantity.is_finite() or quantity < 1:
                raise PricingError("Invalid price break quantity")
            prices.append((quantity, *price_snapshot.normalize(pb.price, pb.currency)))
        if prices:
            with transaction.atomic():
                SupplierPart.objects.select_for_update().get(pk=sup_part.pk)
                for quantity, amount, currency in prices:
                    money = Money(amount, currency)
                    tier = SupplierPriceBreak.objects.filter(
                        part=sup_part, quantity=quantity
                    ).first()
                    if tier is None:
                        tier = SupplierPriceBreak(part=sup_part, quantity=quantity)
                    elif tier.price == money:
                        continue
                    tier.price = money
                    tier.full_clean()
                    tier.save()
                    changed += 1
    except PricingError as exc:
        message = f"Supplier pricing skipped for {label}: {exc}"
        pricing_warnings.append(message)
        logger.warning(message)
    return changed


def _get_or_create_company(
    name: str,
    is_manufacturer: bool = False,
    is_supplier: bool = False,
    auto_create: bool = True,
) -> Optional["Company"]:
    """Get an existing company or create a new one."""
    from company.models import Company

    if not name:
        return None

    # Try exact match first
    company = Company.objects.filter(name__iexact=name.strip()).first()
    if company:
        # Update flags if needed
        changed = False
        if is_manufacturer and not company.is_manufacturer:
            company.is_manufacturer = True
            changed = True
        if is_supplier and not company.is_supplier:
            company.is_supplier = True
            changed = True
        if changed:
            company.save()
        return company

    if not auto_create:
        return None

    # Create new company
    company = Company.objects.create(
        name=name.strip(),
        is_manufacturer=is_manufacturer,
        is_supplier=is_supplier,
    )
    logger.info(
        f"Created company: {name} (manufacturer={is_manufacturer}, supplier={is_supplier})"
    )
    return company


# v1.3.0: guaranteed core parameter templates. Values come from dedicated
# PartData fields (not the spec Parameters list) and are stored as Part
# Parameters regardless of the LIMIT_PARAMETERS_TO_CATEGORY filter.
CORE_PARAMETER_TEMPLATES = [
    ("Detailed Description", "detailed_description"),
    ("Manufacturer Std Lead Time", "lead_time"),
    ("Series", "series"),
    ("Package / Case", "package"),
    ("Supplier Device Package", "supplier_device_package"),
]


def _write_parameter(part, part_type, template, value: str, source: str, ctx):
    """Priority-aware write of one Parameter row (see provenance).

    Returns True if the value was written.
    """
    from common.models import Parameter

    value = str(value).strip()[:2000]
    existing = Parameter.objects.filter(
        model_type=part_type, model_id=part.pk, template=template
    ).first()
    current = existing.data if existing else ""
    if existing and str(current).strip() == value:
        return False
    key = f"param:{template.name}"
    if ctx is not None and not ctx.allowed(key, current, source):
        return False
    if ctx is None and existing and not is_useless_value(current):
        return False  # no context: fill-only
    if existing:
        existing.data = value
        existing.save()
    else:
        Parameter.objects.create(
            model_type=part_type, model_id=part.pk, template=template, data=value
        )
    if ctx is not None:
        ctx.wrote(key, source, value, template.name if ctx.is_update else "")
    return True


def _create_core_parameters(part, data: "PartData", ctx=None) -> None:
    """Store the guaranteed core distributor metadata as Part Parameters.

    Uses find-or-create global ParameterTemplates and the priority-aware
    _write_parameter (existing values are only replaced by a higher-priority
    source, never when edited by hand). Exempt from the
    LIMIT_PARAMETERS_TO_CATEGORY filter so these fields are always written,
    even for categories without matching templates.
    """
    from common.models import ParameterTemplate
    from django.contrib.contenttypes.models import ContentType

    part_type = ContentType.objects.get_for_model(part)

    for template_name, attr in CORE_PARAMETER_TEMPLATES:
        value = str(getattr(data, attr, "") or "").strip()
        if not value or is_useless_value(value):
            continue
        try:
            template, _ = ParameterTemplate.objects.get_or_create(
                name=template_name,
                defaults={"units": ""},
            )
            source = ctx.source_of(attr) if ctx is not None else ""
            if _write_parameter(part, part_type, template, value, source, ctx):
                logger.debug(
                    f"Core parameter {template_name}={value} for Part {part.pk}"
                )
        except Exception as e:
            logger.warning(f"Failed to write core parameter {template_name}: {e}")


def _create_parameters(
    part,
    parameters: List[Any],
    category_id: Optional[int] = None,
    ctx=None,
) -> List[Dict]:
    """Create Parameter entries for the Part.

    Parameters with empty, whitespace-only, or known-placeholder values are
    silently dropped before any database interaction, so InvenTree never
    accumulates rows like Tolerance="-" or Voltage=N/A.

    When the plugin setting ``LIMIT_PARAMETERS_TO_CATEGORY`` is True, only
    parameters whose canonical names match a ParameterTemplate defined in the
    part's InvenTree category hierarchy are persisted. Parameters excluded
    solely by this rule are collected in the returned audit list.

    Existing parameter values are never deleted. They are only replaced by
    a higher-priority source than the one that wrote them, and never when
    edited by hand (``_write_parameter`` / provenance.should_write).

    Returns:
        List of dropped-parameter audit dicts
        ``{supplier_key, value, reason}`` — empty when filtering is off or
        all parameters were accepted.
    """
    from common.models import Parameter, ParameterTemplate
    from django.contrib.contenttypes.models import ContentType

    part_type = ContentType.objects.get_for_model(part)
    skipped = 0
    dropped_audit: List[Dict] = []

    from plugin.registry import registry

    plugin = None
    try:
        plugin = registry.get_plugin("smartparts")
    except Exception:
        pass
    from .parameter_normalizer import is_parameter_ignored, normalize_parameter_list

    # ── Pre-filter: sentinel / ignored values ────────────────────────────────
    clean_params = []
    for param in parameters:
        if (
            not param.name
            or is_useless_value(getattr(param, "value", None))
            or is_parameter_ignored(param.name, plugin)
        ):
            skipped += 1
            continue
        clean_params.append(param)

    # ── Category template filter ─────────────────────────────────────────────
    limit_to_category = True  # safe default
    if plugin:
        try:
            limit_to_category = bool(plugin.get_setting("LIMIT_PARAMETERS_TO_CATEGORY"))
        except Exception:
            pass

    if limit_to_category and category_id is not None:
        try:
            from part.models import PartCategory
            from ..core import get_resolved_category_templates
            from .parameter_normalizer import (
                filter_parameters_by_category,
                normalize_parameter_list,
            )

            category = PartCategory.objects.get(pk=category_id)
            resolved_templates = get_resolved_category_templates(category)

            # Separate manual (explicitly user-added or pre-existing) from distributor params
            manual_params = [p for p in clean_params if getattr(p, "manual", False)]
            distributor_params = [
                p for p in clean_params if not getattr(p, "manual", False)
            ]

            # Normalise distributor params to dicts for the filter function
            param_dicts = [
                {
                    "name": p.name,
                    "value": getattr(p, "value", ""),
                    "unit": getattr(p, "unit", ""),
                    "source": getattr(p, "source", ""),
                }
                for p in distributor_params
            ]
            filter_result = filter_parameters_by_category(
                param_dicts, resolved_templates
            )
            dropped_audit = filter_result.dropped_parameters

            if filter_result.dropped_parameters:
                logger.info(
                    "LIMIT_PARAMETERS_TO_CATEGORY: dropped %d parameter(s) for "
                    "Part %s: %s",
                    len(filter_result.dropped_parameters),
                    part.pk,
                    [d["supplier_key"] for d in filter_result.dropped_parameters],
                )

            # Replace clean_params with the accepted subset + user-added manual params
            # We rebuild lightweight SimpleNamespace objects so the write loop below works unchanged.
            from types import SimpleNamespace

            accepted_distributor = [
                SimpleNamespace(
                    name=d["name"],
                    value=d["value"],
                    unit=d.get("unit", ""),
                    source=d.get("source", ""),
                )
                for d in filter_result.accepted_parameters
            ]
            clean_params = accepted_distributor + manual_params

        except Exception as exc:
            logger.warning(
                "LIMIT_PARAMETERS_TO_CATEGORY filter failed for Part %s "
                "(category_id=%s): %s – falling back to unfiltered import",
                part.pk,
                category_id,
                exc,
            )

    # ── Persist accepted parameters ──────────────────────────────────────────
    for param in clean_params:
        try:
            # Get or create the parameter template
            template, _ = ParameterTemplate.objects.get_or_create(
                name=param.name,
                defaults={
                    "units": param.unit if hasattr(param, "unit") and param.unit else ""
                },
            )

            # Priority-aware write (fill gaps; higher-priority sources may
            # replace unedited values of lower-priority ones).
            source = getattr(param, "source", "") or ""
            if getattr(param, "manual", False):
                source = ""
            if _write_parameter(part, part_type, template, param.value, source, ctx):
                logger.debug(f"Parameter {param.name}={param.value} for Part {part.pk}")
        except Exception as e:
            logger.warning(f"Failed to create parameter {param.name}: {e}")

    if skipped:
        logger.debug(
            f"Skipped {skipped} empty/placeholder parameter(s) for Part {part.pk}"
        )

    return dropped_audit


def _attach_image(part, image_url: str):
    """Download and attach an image to a Part."""
    import requests

    try:
        response = requests.get(image_url, timeout=10, stream=True)
        response.raise_for_status()

        # Determine file extension
        content_type = response.headers.get("content-type", "")
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"

        # Save to temp file then attach
        with tempfile.NamedTemporaryFile(
            suffix=ext, delete=False, prefix="smartparts_"
        ) as tmp:
            for chunk in response.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        try:
            from django.core.files import File

            with open(tmp_path, "rb") as f:
                part.image.save(f"part_{part.pk}{ext}", File(f), save=True)
            logger.info(f"Attached image to Part {part.pk}")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.warning(f"Failed to download/attach image: {e}")


def _attach_datasheet_link(part, datasheet_url: str):
    """Attach the datasheet link to the Part."""
    if not datasheet_url:
        return

    try:
        from common.models import Attachment
        from django.contrib.contenttypes.models import ContentType

        part_type = ContentType.objects.get_for_model(part)

        # InvenTree attachments support either file uploads or external URLs.
        Attachment.objects.get_or_create(
            model_type=part_type,
            model_id=part.pk,
            link=datasheet_url,
            defaults={"comment": "Datasheet"},
        )
        logger.info(f"Attached datasheet link to Part {part.pk}")
    except Exception as e:
        logger.warning(f"Failed to attach datasheet link: {e}")
