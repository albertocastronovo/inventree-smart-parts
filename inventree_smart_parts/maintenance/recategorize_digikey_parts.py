"""
Move existing DigiKey parts into their full DigiKey category chain.

Before v1.4.0 the DigiKey client walked a ``ParentCategory`` key that does
not exist in API v4 (v4 nests root→leaf via ``ChildCategories``), so only
the root category (e.g. ``Capacitors``) was seen and parts were auto-placed
into broad top-level categories.

Usage (InvenTree Django shell)::

    from inventree_smart_parts.maintenance import recategorize_digikey_parts as r
    r.run()                        # dry run: prints the plan, writes nothing
    r.run(dry_run=False)           # apply
    r.run(part_ids=[12, 34])       # limit to some parts
    r.run(only_auto_placed=False)  # also move parts that look hand-placed

By default only parts that look auto-placed by the old logic are moved:
uncategorized parts, parts in DEFAULT_CATEGORY, and parts in a top-level
category named like the DigiKey root. Everything else is reported as
"skipped (manually categorized?)".

Stale root-only LEARNED_CATEGORY_MAPPINGS entries and auto-created
categories left empty are only reported, never deleted.
"""

import json
import logging

from . import digikey_supplier_parts, get_digikey_client, print_table

logger = logging.getLogger("inventree_smart_parts.maintenance.categories")


def _looks_auto_placed(part, root_name, default_name):
    cat = part.category
    if cat is None:
        return True
    if default_name and cat.name.lower() == default_name.lower():
        return True
    return cat.parent_id is None and cat.name.lower() == (root_name or "").lower()


def _target(distributor_category, part, settings, dry_run, plugin):
    """Resolve the target category. Returns (cat_id, path, note)."""
    from ..services.category_mapper import (
        _split_category_path,
        ancestor_remainder,
        create_category_chain,
        fuzzy_match_category,
        learn_category_mapping,
        plan_category_chain,
    )

    segments = _split_category_path(distributor_category)

    cat_id, cat_path, score = fuzzy_match_category(
        distributor_category,
        settings["threshold"],
        default_category_name="",
        user_synonyms_json=settings["synonyms"],
        learned_mappings_json=settings["learned"],
    )
    # A match that is the part's current (root-only) category or any ancestor
    # of the distributor chain is the old bug — fall through to the chain.
    is_ancestor = cat_id is not None and (
        cat_id == part.category_id
        or ancestor_remainder(cat_id, distributor_category) is not None
    )
    if cat_id is not None and not is_ancestor:
        return cat_id, cat_path, f"fuzzy {score}%"

    if len(segments) < 2 and cat_id is not None:
        return cat_id, cat_path, "root-only DigiKey category"

    leaf_id, path, missing = plan_category_chain(distributor_category)
    if not missing:
        return leaf_id, path, "existing chain"
    if not settings["auto_create"]:
        return None, path, "needs new categories (AUTO_CREATE_CATEGORIES off)"
    if dry_run:
        return None, path, "would create: " + " > ".join(missing)

    leaf_id, path, created = create_category_chain(distributor_category)
    if leaf_id is not None:
        learn_category_mapping(distributor_category, path, plugin)
    return leaf_id, path, "created chain" if created else "existing chain"


def run(dry_run=True, only_auto_placed=True, part_ids=None, limit=None):
    """Re-categorize DigiKey parts. Returns a summary dict."""
    from django.db import transaction
    from part.models import PartCategory

    plugin, client = get_digikey_client()
    settings = {
        "threshold": plugin.get_setting("FUZZY_THRESHOLD") or 65,
        "synonyms": plugin.get_setting("CATEGORY_SYNONYMS") or "{}",
        "learned": plugin.get_setting("LEARNED_CATEGORY_MAPPINGS") or "{}",
        "auto_create": bool(plugin.get_setting("AUTO_CREATE_CATEGORIES")),
    }
    default_name = plugin.get_setting("DEFAULT_CATEGORY") or ""

    rows = []
    summary = {"checked": 0, "moved": 0, "unchanged": 0, "skipped": 0, "error": 0}
    roots_seen = set()
    seen_parts = set()

    qs = digikey_supplier_parts()
    if part_ids:
        qs = qs.filter(part__pk__in=list(part_ids))

    for sp in qs:
        part = sp.part
        if part is None or part.pk in seen_parts:
            continue
        seen_parts.add(part.pk)
        if limit is not None and summary["checked"] >= limit:
            break
        summary["checked"] += 1
        current = part.category.pathstring if part.category else "(none)"

        try:
            details = client.get_product_details(sp.SKU)
        except Exception as e:
            summary["error"] += 1
            rows.append([part.pk, part.name, current, "", f"ERROR: {e}"])
            continue
        dist_cat = (details.category if details else "") or ""
        if not dist_cat:
            summary["skipped"] += 1
            rows.append([part.pk, part.name, current, "", "SKIP: no DigiKey category"])
            continue
        root_name = dist_cat.split(">")[0].strip()
        roots_seen.add(root_name)

        if only_auto_placed and not _looks_auto_placed(part, root_name, default_name):
            summary["skipped"] += 1
            rows.append(
                [
                    part.pk,
                    part.name,
                    current,
                    dist_cat,
                    "skipped (manually categorized?)",
                ]
            )
            continue

        try:
            cat_id, path, note = _target(dist_cat, part, settings, dry_run, plugin)
        except Exception as e:
            summary["error"] += 1
            rows.append([part.pk, part.name, current, dist_cat, f"ERROR: {e}"])
            continue

        if cat_id is not None and cat_id == part.category_id:
            summary["unchanged"] += 1
            rows.append([part.pk, part.name, current, path, "unchanged"])
            continue

        target = PartCategory.objects.filter(pk=cat_id).first() if cat_id else None
        if target is not None and getattr(target, "structural", False):
            summary["skipped"] += 1
            rows.append([part.pk, part.name, current, path, "SKIP: target structural"])
            continue

        if dry_run:
            if cat_id is None and not note.startswith("would create"):
                summary["skipped"] += 1
                rows.append([part.pk, part.name, current, path, f"SKIP: {note}"])
            else:
                summary["moved"] += 1
                rows.append([part.pk, part.name, current, path, f"WOULD MOVE ({note})"])
            continue

        if target is None:
            summary["skipped"] += 1
            rows.append([part.pk, part.name, current, path, f"SKIP: {note}"])
            continue

        try:
            with transaction.atomic():
                part.category = target
                part.save()
            summary["moved"] += 1
            rows.append([part.pk, part.name, current, path, f"MOVED ({note})"])
        except Exception as e:
            summary["error"] += 1
            rows.append([part.pk, part.name, current, path, f"ERROR: {e}"])

    print_table(
        rows, ["part", "name", "current category", "DigiKey category", "action"]
    )
    print()

    # ── Report-only housekeeping hints ──
    try:
        learned = json.loads(settings["learned"]) or {}
    except Exception:
        learned = {}
    stale = [k for k in learned if k in roots_seen]
    if stale:
        print("Root-only learned mappings (likely from the old bug; review in")
        print("SmartParts settings → Learned category mappings):")
        for k in stale:
            print(f"  '{k}' -> '{learned[k]}'")
        print()

    if not dry_run:
        empty = [
            c.pathstring
            for c in PartCategory.objects.filter(parent__isnull=True)
            if c.name in roots_seen and not c.parts.exists() and not c.children.exists()
        ]
        if empty:
            print("Now-empty top-level categories (not deleted):")
            for p in empty:
                print(f"  {p}")
            print()

    print(("DRY RUN — nothing changed. " if dry_run else "") + str(summary))
    return summary
