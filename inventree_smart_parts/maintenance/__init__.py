"""
One-off maintenance scripts.

Run inside InvenTree's Django shell (``invoke dev.shell`` or
``python manage.py shell``), e.g.::

    from inventree_smart_parts.maintenance import rename_digikey_parts
    rename_digikey_parts.run()                 # dry run (default)
    rename_digikey_parts.run(dry_run=False)    # apply

All scripts default to ``dry_run=True`` and print a report.
"""


def get_digikey_client():
    """Build one DigiKeyClient from the plugin settings (token is cached)."""
    from plugin.registry import registry

    from ..api_clients.digikey import DigiKeyClient

    plugin = registry.get_plugin("smartparts")
    if plugin is None:
        raise RuntimeError("SmartParts plugin is not loaded")
    if not plugin.get_setting("DIGIKEY_ENABLED"):
        raise RuntimeError("DigiKey is disabled in the SmartParts settings")
    client = DigiKeyClient(
        client_id=plugin.get_setting("DIGIKEY_CLIENT_ID"),
        client_secret=plugin.get_setting("DIGIKEY_CLIENT_SECRET"),
    )
    return plugin, client


def digikey_supplier_parts():
    """All SupplierParts whose supplier is DigiKey."""
    from company.models import SupplierPart

    return (
        SupplierPart.objects.filter(supplier__name__iexact="DigiKey")
        .select_related("part", "part__category", "manufacturer_part")
        .order_by("part__pk")
    )


def print_table(rows, headers):
    """Plain-text table for shell output."""
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = min(max(widths[i], len(c)), 60)
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*[c[:60] for c in r]))
