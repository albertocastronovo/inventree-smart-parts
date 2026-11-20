"""
Stock Manager
=============
Creates StockItems and triggers label printing for newly received parts.
All InvenTree model imports are deferred so the module can be imported
at plugin load time without a fully-initialised Django environment.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

logger = logging.getLogger("inventree_smart_parts.services.stock")


def get_part_stock_summary(part_id: int, location_id: Optional[int] = None) -> dict:
    """Summarize physical in-stock quantities for one part (no variants)."""
    from django.db.models import Q, Sum
    from stock.models import StockItem, StockLocation

    from .token_manager import TOKEN_PATTERNS, barcode_field_name

    entries = StockItem.objects.filter(StockItem.IN_STOCK_FILTER, part_id=part_id)
    aggregations = {"total": Sum("quantity")}
    has_location = (
        location_id is not None
        and StockLocation.objects.filter(pk=location_id).exists()
    )
    if has_location:
        aggregations["local"] = Sum("quantity", filter=Q(location_id=location_id))
    quantities = entries.aggregate(**aggregations)

    def display(quantity):
        value = format(quantity or 0, "f")
        return value.rstrip("0").rstrip(".") if "." in value else value

    labels = []
    field = barcode_field_name(StockItem)
    if has_location and quantities["local"] and field:
        labels = list(
            entries.filter(
                location_id=location_id,
                **{f"{field}__regex": TOKEN_PATTERNS["STOCK"].pattern},
            )
            .order_by(field)
            .values_list(field, flat=True)
            .distinct()[:4]
        )
    return {
        "total_stock": display(quantities["total"]),
        "location_stock": display(quantities["local"]) if has_location else None,
        "stock_labels": labels[:3],
        "more_labels": len(labels) > 3,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  StockItem creation
# ─────────────────────────────────────────────────────────────────────────────


def create_stock_item(
    part_id: int,
    quantity: Union[Decimal, int, float, str],
    location_id: Optional[int] = None,
    batch: str = "",
    delete_on_deplete: bool = True,
    user=None,
    token: str = "",
) -> dict:
    """
    Create a new StockItem for the given Part.

    Args:
        user: The Django User object to record in the stock tracking history.
              Pass request.user from the view so the history page shows who
              booked the stock rather than "No user".

    Returns a dict with keys:
        success (bool), stock_id (int|None), message (str), and a token or error status.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from .token_manager import (
        TokenConflict,
        bind_token,
        lock_token,
        require_free_token,
        stock_token,
    )

    try:
        from part.models import Part
        from stock.models import StockItem

        quantity = Decimal(str(quantity))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("Quantity must be a positive number")
        normalized = stock_token(token) if token != "" else ""
        part = Part.objects.filter(pk=part_id).first()
        if part is None:
            raise ValueError("Part not found")

        kwargs = {
            "part": part,
            "quantity": quantity,
            "delete_on_deplete": delete_on_deplete,
        }

        if location_id is not None and location_id != "":
            from stock.models import StockLocation

            location = StockLocation.objects.filter(pk=location_id).first()
            if location is None:
                raise ValueError("Stock location not found")
            if getattr(location, "structural", False):
                raise ValueError("Select a non-structural stock location")
            if (
                user is not None
                and hasattr(location, "check_ownership")
                and not location.check_ownership(user)
            ):
                return {
                    "success": False,
                    "stock_id": None,
                    "message": "Stock location is not editable by this user",
                    "status": 403,
                }
            kwargs["location"] = location

        if batch and not isinstance(batch, str):
            raise ValueError("Invalid stock batch")
        if batch:
            kwargs["batch"] = batch[:100]

        with transaction.atomic():
            if normalized:
                lock_token(normalized)
                require_free_token(normalized)
            stock_item = StockItem(**kwargs)
            if normalized:
                binding = bind_token(normalized, stock_item, save=False)
                if not binding["success"]:
                    raise ValueError(binding["error"])
            stock_item.full_clean()
            if hasattr(stock_item, "tracking_event"):
                stock_item.save(user=user, notes="Created via Smart Parts plugin")
            else:
                stock_item.save()
                _attribute_legacy_creation(stock_item, quantity, kwargs, user)

        logger.info(
            f'Created StockItem {stock_item.pk} for Part {part_id} (qty={quantity}, user={getattr(user, "username", "anon")})'
        )
        message = f"Stock item created (ID {stock_item.pk})"
        if normalized:
            message += f" and linked to {normalized}"
        return {
            "success": True,
            "stock_id": stock_item.pk,
            "message": message,
            "token": normalized or None,
        }
    except TokenConflict as e:
        return {"success": False, "stock_id": None, "message": str(e), "status": 409}
    except (InvalidOperation, ValueError, ValidationError) as e:
        message = (
            "Quantity must be a positive number"
            if isinstance(e, InvalidOperation)
            else str(e)
        )
        return {"success": False, "stock_id": None, "message": message, "status": 400}
    except Exception:
        logger.exception("Failed to receive stock for part %s", part_id)
        return {
            "success": False,
            "stock_id": None,
            "message": "Could not receive stock",
            "status": 500,
        }


def _attribute_legacy_creation(stock_item, quantity, kwargs, user):
    """Older InvenTree releases do not accept a user on StockItem.save()."""
    try:
        from django.db import transaction
        from stock.models import StockItemTracking
        from stock.status_codes import StockHistoryCode

        with transaction.atomic():
            auto_entry = (
                StockItemTracking.objects.filter(
                    item=stock_item, tracking_type=StockHistoryCode.CREATED
                )
                .order_by("pk")
                .last()
            )
            if auto_entry is not None:
                auto_entry.user = user
                auto_entry.notes = "Created via Smart Parts plugin"
                auto_entry.save(update_fields=["user", "notes"])
            else:
                # No auto-entry exists (older InvenTree) – create one manually.
                deltas: dict = {
                    "quantity": float(quantity),
                    "status": stock_item.status,
                }
                if kwargs.get("location"):
                    deltas["location"] = kwargs["location"].pk
                stock_item.add_tracking_entry(
                    StockHistoryCode.CREATED,
                    user,
                    deltas=deltas,
                    notes="Created via Smart Parts plugin",
                )
    except Exception as te:
        logger.warning("Could not attribute stock creation for %s: %s", stock_item.pk, te)


# ─────────────────────────────────────────────────────────────────────────────
#  Label template discovery
# ─────────────────────────────────────────────────────────────────────────────


def get_label_templates() -> list:
    """
    Return available StockItem label templates as a list of dicts:
        [{'id': pk, 'name': name}, ...]

    InvenTree moved labels from label.models into report.models.LabelTemplate
    (filtered by model_type='stockitem').  We try the paths in order:
      1. report.models.LabelTemplate  (current InvenTree ≥ 0.15)
      2. label.models.LabelTemplate   (InvenTree 0.13-0.14)
      3. label.models.StockItemLabel  (InvenTree < 0.13 legacy)
    """
    # ── Path 1: report.models.LabelTemplate (current) ────────────
    try:
        from report.models import LabelTemplate

        # Filter to StockItem labels only
        qs = LabelTemplate.objects.filter(
            enabled=True, model_type="stockitem"
        ).order_by("name")
        results = [{"id": t.pk, "name": t.name} for t in qs]
        if results:
            return results
        # If nothing for stockitem, fall back to ALL label templates
        qs_all = LabelTemplate.objects.filter(enabled=True).order_by("name")
        results = [{"id": t.pk, "name": f"{t.name} [{t.model_type}]"} for t in qs_all]
        if results:
            logger.info(
                "get_label_templates: no stockitem-specific templates found, returning all"
            )
            return results
    except ImportError:
        logger.debug("report.models.LabelTemplate not available")
    except Exception as e:
        logger.warning(f"report.models.LabelTemplate query failed: {e}", exc_info=True)

    # ── Path 2: label.models.LabelTemplate (InvenTree 0.13-0.14) ─
    try:
        from label.models import LabelTemplate  # type: ignore[import]

        qs = LabelTemplate.objects.filter(enabled=True).order_by("name")
        results = [{"id": t.pk, "name": t.name} for t in qs]
        if results:
            return results
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"label.models.LabelTemplate query failed: {e}")

    # ── Path 3: StockItemLabel (InvenTree < 0.13 legacy) ─────────
    try:
        from label.models import StockItemLabel  # type: ignore[import]

        qs = StockItemLabel.objects.filter(enabled=True).order_by("name")
        return [{"id": t.pk, "name": t.name} for t in qs]
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"label.models.StockItemLabel query failed: {e}")

    logger.warning(
        "get_label_templates: no label template model found in this InvenTree version"
    )
    return []


# ─────────────────────────────────────────────────────────────────────────────
#  Stock location tree
# ─────────────────────────────────────────────────────────────────────────────


def get_stock_locations() -> list:
    """
    Return the full StockLocation tree as a flat list of dicts for a
    <select> dropdown, showing indented names.

    Uses InvenTree's MPTT tree ordering so the list is already depth-first.
    """
    try:
        from stock.models import StockLocation

        result = []
        # order_by('tree_id', 'lft') gives correct MPTT pre-order traversal
        try:
            qs = StockLocation.objects.all().order_by("tree_id", "lft")
        except Exception:
            qs = StockLocation.objects.all().order_by("name")

        for loc in qs:
            # Calculate depth: count ancestors by checking level attribute (MPTT)
            depth = getattr(loc, "level", 0)
            indent = "\u00a0\u00a0" * depth + ("\u2514\u2500 " if depth else "")
            path = loc.pathstring if hasattr(loc, "pathstring") else loc.name
            description = getattr(loc, "description", "") or ""
            result.append(
                {
                    "id": loc.pk,
                    "name": indent + loc.name,
                    "path": path,
                    "description": description,
                }
            )

        return result

    except Exception as e:
        logger.error(f"get_stock_locations failed: {e}", exc_info=True)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Label printing
# ─────────────────────────────────────────────────────────────────────────────


def print_stock_label(
    stock_item_id: int,
    template_id: int,
    plugin_slug: str = "",
    request=None,
) -> dict:
    """
    Trigger InvenTree's built-in label print for a StockItem.

    Routing strategy:
      1. Direct template.print() with the resolved LabelPrintingMixin plugin.
         For machine-based printers (inventreelabelmachine / Dymo), the machine
         UUID is resolved automatically from MachineConfig and passed via the
         options dict so inventreelabelmachine.print_labels() can select it.
      2. Legacy StockItemLabel fallback (InvenTree < 0.13).

    NOTE: We intentionally bypass the /api/label/print/ REST endpoint.
    That path requires machine-specific serializer fields (e.g. a machine UUID
    ChoiceField built dynamically from the request context) that cannot be
    satisfied by a synthetic RequestFactory request. template.print() is the
    correct internal API.

    The ``plugin_slug`` should be the **key** of a LabelPrintingMixin plugin
    (e.g. ``inventreelabelmachine`` for machine-routed printers like Dymo,
    or ``inventreelabel`` for the built-in PDF generator).

    Returns: {'success': bool, 'message': str}
    """
    # ── Resolve the StockItem ─────────────────────────────────────────────
    try:
        from stock.models import StockItem

        stock_item = StockItem.objects.get(pk=stock_item_id)
    except Exception as e:
        return {
            "success": False,
            "message": f"StockItem {stock_item_id} not found: {e}",
        }

    # ── Resolve the print plugin ──────────────────────────────────────────
    # ── Strategy 1: Direct template.print() ───────────────────────────────
    try:
        from report.models import LabelTemplate

        template = LabelTemplate.objects.get(pk=template_id, enabled=True, model_type="stockitem")
        resolved_key = _resolve_print_plugin_key(plugin_slug)

        from plugin.registry import registry as plugin_registry

        printing_plugins = plugin_registry.with_mixin("labels", active=True)
        output_plugin = None

        if resolved_key:
            output_plugin = next(
                (p for p in printing_plugins if p.slug == resolved_key),
                None,
            )
        if not output_plugin and printing_plugins:
            output_plugin = printing_plugins[0]

        if not output_plugin:
            return {
                "success": False,
                "message": (
                    "No active label printing plugin found. "
                    "Enable a printing plugin (e.g. InvenTree Label Machine "
                    "for Dymo/Zebra printers) in InvenTree."
                ),
            }

        # Build printing options.
        # inventreelabelmachine requires {'machine': <uuid>} so it knows which
        # physical printer to route the job to.
        options = _build_printing_options(output_plugin)

        output = template.print(
            items=[stock_item],
            plugin=output_plugin,
            options=options,
            user=getattr(request, "user", None),
            request=request,
        )
        logger.info(
            f"Label printed (direct): StockItem {stock_item_id} via template "
            f'"{template.name}" using plugin "{output_plugin.slug}"'
            + (f' machine={options.get("machine")}' if options.get("machine") else "")
        )
        return {
            "success": True,
            "message": f'Label sent to "{output_plugin.name}" successfully',
        }

    except ImportError:
        logger.debug(
            "report.models.LabelTemplate not available, trying legacy fallback"
        )
    except Exception as e:
        logger.error(f"Label print failed (direct): {e}", exc_info=True)
        return {"success": False, "message": f"Label print error: {e}"}

    # ── Strategy 2: Legacy fallback (InvenTree < 0.13) ────────────────────
    try:
        from label.models import StockItemLabel  # type: ignore[import]
        from label.views import StockItemLabelPrint  # type: ignore[import]

        template = StockItemLabel.objects.get(pk=template_id)

        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser

        factory = RequestFactory()
        fake_req = factory.get("/", {"items": str(stock_item_id)})
        fake_req.user = AnonymousUser()

        view = StockItemLabelPrint()
        view.request = fake_req
        view.print_labels(template, [stock_item])
        logger.info(f"Label printed (legacy API): StockItem {stock_item_id}")
        return {"success": True, "message": "Label sent to printer (legacy API)"}

    except Exception as e:
        logger.error(f"Label print failed (legacy API): {e}", exc_info=True)
        return {"success": False, "message": f"Label print error (legacy): {e}"}


def _build_printing_options(plugin) -> dict:
    """Return the options dict required by the given label printing plugin.

    For ``inventreelabelmachine`` (slug contains 'machine'), InvenTree's
    built-in machine label plugin reads
    ``printing_options.get('machine', '')`` to select the physical printer.
    We auto-resolve the first active label-printer machine from MachineConfig.

    For all other plugins (e.g. inventreelabel / PDF) no extra options are needed.
    """
    slug = getattr(plugin, "slug", "") or ""

    # Only machine-based plugins need a machine UUID
    if "machine" not in slug.lower():
        return {}

    try:
        from machine.models import MachineConfig

        machines = MachineConfig.objects.filter(
            active=True, machine_type="label-printer"
        )
        machine = machines.first()
        if machine:
            machine_pk = str(machine.pk)
            logger.info(
                f"_build_printing_options: auto-selected machine "
                f"pk={machine_pk} name={machine.name}"
            )
            return {"machine": machine_pk}
        else:
            logger.warning(
                "_build_printing_options: no active label-printer machine found in DB"
            )
    except Exception as e:
        logger.warning(f"_build_printing_options: could not resolve machine: {e}")

    return {}


def _resolve_print_plugin_key(plugin_slug: str) -> str:
    """Resolve user-provided plugin slug to the correct InvenTree plugin key.

    If the user configured a machine-driver plugin (like inventree-dymo-plugin),
    we auto-route to 'inventreelabelmachine' which is the bridge plugin that
    forwards print jobs to machine drivers.
    """
    if not plugin_slug:
        return ""

    slug = plugin_slug.strip().lower()

    # Direct match to known label-printing plugin keys
    from plugin.registry import registry as plugin_registry

    label_plugins = plugin_registry.with_mixin("labels", active=True)
    label_keys = {p.slug for p in label_plugins}

    if slug in label_keys:
        return slug

    # Check if the slug matches a machine-driver plugin (e.g. inventree-dymo-plugin)
    # These need routing through 'inventreelabelmachine'
    all_plugins = plugin_registry.plugins
    for key, p in all_plugins.items():
        p_slug = getattr(p, "slug", "")
        if p_slug == slug or key == slug:
            mro_names = [c.__name__ for c in type(p).__mro__]
            if "MachineDriverMixin" in mro_names:
                logger.info(
                    f'Plugin "{slug}" is a machine driver — routing via inventreelabelmachine'
                )
                return "inventreelabelmachine"

    # Partial match: try matching by substring
    for key in label_keys:
        if slug in key or key in slug:
            logger.info(f'Plugin slug "{slug}" fuzzy-matched to "{key}"')
            return key

    # Default: use first available label plugin
    if label_keys:
        default = next(iter(label_keys))
        logger.warning(f'Plugin slug "{slug}" not found, defaulting to "{default}"')
        return default

    return slug
