# InvenTree 1.7.3: stock and search workflows

## See or change the linked stock barcode

1. Open the **stock item** from the stock table or search results. In the header, select **Barcode Actions → Unlink Barcode**, then **Barcode Actions → Link Barcode** and scan or type the replacement. An unlabelled item only needs **Link Barcode**. The operator needs **Stock Items → Change** group permission.
2. If you have the label in hand, scan or enter it with the top-bar barcode icon to jump to the item. The stock table's text search does not search linked barcode data in 1.7.3. For `STOCK_` or `LOC_` labels, enter uppercase text.
3. To **read** the saved text without Database Admin, create a separate **Stock Item** template under **Admin Center → Label Templates** that renders `{{ stock_item.barcode_data }}`. Enable it, then open the item and use **Print → Print Label** with the PDF printer. **View Barcode** and **Barcode Show Data** display InvenTree's generated code rather than the linked label.
4. For an on-screen field, use a [UI plugin stock-item panel](https://docs.inventree.org/en/stable/plugins/mixins/ui/#panels). Have it fetch linked text from a permission-checked endpoint and reuse the [native barcode link/unlink actions](https://docs.inventree.org/en/stable/barcodes/external/#linking-barcodes). The [1.7.3 stock table](https://github.com/inventree/InvenTree/blob/1.7.3/src/frontend/src/tables/stock/StockItemTable.tsx) has no linked-barcode column; the stock API returns a hash rather than the stored text. For an actual column, request it upstream or maintain a frontend customization against the version you run. No ready-made column plugin was found in the [official directory](https://inventree.org/plugins.html).

## Change the exact stock quantity

Select the row in a stock table and choose **Stock Actions → Count Stock**. Enter the new total and submit. From a stock item page, use **Stock Operations → Count Stock**. Use **Add Stock** or **Remove Stock** for changes by a given amount. These actions record history and require **Stock Items → Change**. Before counting to zero, check **Delete on deplete** if you want to retain the item and its linked label.

## Filter category, value, tolerance and description together

1. Open **Parts**, pick a category, and select **Parametric View** in its **Parts** panel. Use **Include Subcategories** to control scope. The table search matches description along with other text fields; enable the **Description** column to see it alongside your parameters.
2. Combine filters using the parameter column headers. For example, in **Resistors**, set **Resistance** to `>= 10 kΩ` and `<= 47 kΩ`, then add a **Tolerance** filter matching the stored format (e.g. text `1%` or a numeric range on a unit-aware template). Filters are combined together. See [parameter filtering](https://docs.inventree.org/en/stable/concepts/parameters/#filtering-by-parameter-value).
3. If a field is missing, check **Admin Center → Parameters** and **Category Parameters**, then verify the part has a value on its **Parameters** tab. Reuse canonical templates such as **Resistance** and **Capacitance**. SmartParts' **Limit Parameters to Category Templates** can omit parameters not attached to a category or ancestor. Existing parts still need actual parameter values after you add a template.
4. For repeatable queries, see your instance's `/api-doc/` and use `/api/part/` with `category`, `cascade`, `search`, and parameters such as `parameter_<template-id>_gte` and `parameter_<template-id>_lte`. The browser Network tab can show the actual query produced by your Parametric View filters.

## Prepare native exchange rates for SmartParts

In **Admin Center**, open currency management and use **Refresh currency exchange rates**. Check **Last fetched** and confirm USD and EUR are enabled in the global pricing and currency settings. Setting the default currency to EUR is optional for SmartParts conversion; it changes InvenTree's calculated price displays and defaults, not the amount and currency on existing supplier records. See [InvenTree's pricing documentation](https://docs.inventree.org/en/stable/concepts/pricing/#currency-support).

### Convert existing USD supplier price breaks

After deploying the updated SmartParts plugin and checking rates, open a shell with `docker exec -it inventree-server invoke shell`. With the local Compose layout, `/home/inventree/data` is the persistent data volume. Use a unique filename for each preview:

```python
from inventree_smart_parts.maintenance import convert_supplier_prices as prices

plan = "/home/inventree/data/supplier-prices-usd-to-eur.json"
prices.preview(plan)
```

Review the JSON on the host or copy it out with `docker cp`. Check the before/after amounts, tier counts, and fixed conversion rate. Keep a copy of the JSON and a database backup before applying the plan. In the same Django shell:

```python
prices.apply(plan)
```

`changed` lists the updated tier IDs; `skipped` includes rows that changed or were removed after the preview, and already converted rows on a second run. Re-preview with a new file if necessary. If you need to revert, `prices.rollback(plan)` restores original amounts only where the row still matches the recorded conversion. Existing order, sales, and stock unit prices are outside this tool's scope. Check that the worker has updated the pricing overview for a sample part.

### Repair older SmartParts labels for native Quick Scan

If a label works in PureScan but fails to open its item with the native Quick Scan, check for a legacy hash:

```python
from inventree_smart_parts.maintenance import repair_barcode_hashes as barcodes

barcodes.run()               # preview
barcodes.run(dry_run=False)  # after reviewing the preview
```

Rows with competing bindings are reported as conflicts. Compare the affected stock records and physical labels before resolving the conflict manually. This repair preserves the printed token and its stock item.
