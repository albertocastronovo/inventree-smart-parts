# Changelog

All notable changes to SmartParts are documented in this file.

## [1.7.3] - 2026-11-27

### Added
- **PureScan: DataMatrix labels without a part number.** Some labels (for example from a sample warehouse) carry no `1P`, `P` or `30P` field at all. The part number is a separate 1-D barcode on the bag. Example: `[)>{RS}06{GS}KVY5570{GS}14K001{GS}Q25{GS}11K076270558{GS}4LCN{GS}1VVishay{RS}<EOT>`. Before, PureScan refused such a label ("Incomplete label scan ... Scan the label again"), so creating the part from the terminal was impossible.
  - In INFO (and the idle quick scan), CREATEPART, FASTADD (including the receive burst), CREATESTOCK and DELETEPART, the terminal now asks for the part number. It shows "This label has no part number", the label's details (distributor, manufacturer, quantity) and "Scan the part number's 1-D barcode".
  - The next scan supplies the MPN; a `1P` prefix is removed. The flow then continues as for a normal label: the reel quantity, manufacturer and distributor still come from the DataMatrix.
  - A 1-D barcode without a part number (for example the `Q25` barcode) asks again. Scanning a DataMatrix replaces the pending label (a rescan after a bad read). An action code leaves the step, and `SYS:CANCEL` resets as usual.
  - Server: `api/barcode/lookup/` and `api/purescan/createpart/` accept an optional `part_barcode`. It is used only when the DataMatrix has no part number of its own, so a label's `1P`/`P`/`30P` always wins.
  - Labels with a part number behave exactly as before.

### Changed
- The incomplete-label message now reads "This DataMatrix has no part number (1P): scan the part number's 1-D barcode, or rescan the label if it was a bad read." It fits both labels that carry no part number and damaged scans.

### Housekeeping
- Cache-buster `purescan.js?v=15`.
- Tests: decoder (`apply_part_barcode`), CREATEPART/lookup with a `part_barcode`, and terminal JS (the part-number step in INFO, CREATEPART, FASTADD and CREATESTOCK; retry, rescan and action-code exits).

## [1.7.2] - 2026-11-26

### Fixed
- **PureScan: a damaged DataMatrix was searched as if it were an MPN.** Example: a Mouser label for `80-CKC33C454KWGLAUTO` arrived at the terminal as `[)>{{{{{{{{{{{{{{RS}06{GS}K31K077548699{GS}4LUS{GS}1VKEMET{RS}<EOT>`. It had repeated `{` characters, and its `14K`/`1P`/`Q` fields were missing. The decoder did not recognise the `[)>` envelope, so it fell back to using the whole raw string as the MPN. INFO then showed that string, and CREATEPART searched the distributors for it, with wrong or empty results.
  - A `[)>` envelope with no part number (`1P`/`P`) and no supplier SKU (`30P`) is now reported as an incomplete scan, and nothing is searched or created. The terminal shows "Incomplete label scan: the DataMatrix arrived without its part number (1P). Scan the label again." in INFO, FASTADD, CREATEPART and the other label flows.
  - The terminal ignores keyboard auto-repeat (`keydown` with `repeat`). The OS sends repeats when it misses a key-up during a scan; they are never scanner data.
  - `<EOT>` is accepted as an end-of-transmission marker, like `{EOT}`.
  - Labels with a part number decode as before.

### Housekeeping
- Cache-buster `purescan.js?v=14`.
- Tests: decoder (the damaged scan, quantity-only and complete text-separator labels), CREATEPART/lookup on an incomplete scan (no distributor call), and terminal JS (auto-repeat filter; rescan message in INFO, FASTADD and CREATESTOCK).

## [1.7.1] - 2026-11-26

### Fixed
- **Saving a part failed with "Creation failed: {'link': ['Enter a valid URL']}"** when the distributor's datasheet URL was not a valid URL. This happened for Taiyo Yuden parts from Mouser, for example `963-MCASH21GCC7684MT`, whose datasheet path contains a space: `.../specsheets/Taiyo Yuden_1102023_MC_mlcc_all_e.pdf`. `Part.full_clean()` rejected the link and rolled back the whole part. This affected the dashboard, PureScan CREATEPART/FASTADD, batch import and API v1.
  - Datasheet, manufacturer-part and supplier-part links are now cleaned before saving:
    - spaces and non-ASCII characters in the path, query and fragment are percent-encoded;
    - protocol-relative `//host/...` links get `https:`;
    - links that are already valid are stored unchanged.
  - A link that still isn't a valid http(s) URL is left empty, and the part is created anyway. The response lists "Datasheet link dropped (not a valid URL): ...".
  - Supplier and manufacturer part links were never validated by the server, so an invalid value could be stored silently. They are cleaned the same way.

### Housekeeping
- Tests: the fixture `Part.link` is now a validated URL field, as in InvenTree. Regression scenarios cover the Taiyo Yuden datasheet and an unrepairable link, plus unit tests for `clean_link()`.

## [1.7.0] - 2026-11-25

Security hardening. Users who hold the matching InvenTree permissions see no change. The changes only affect users without those permissions, anonymous requests and forged requests.

### Security
- **Every endpoint checks an explicit permission.** Before, several endpoints only required a login.

  | Permission | Endpoints |
  | --- | --- |
  | `part.view_part` | dashboard, dashboard search and search details, the Logs page and log list, canonical parameter names |
  | `part.add_part` | batch import page, batch job status and CSV report |
  | `stock.view_stocklocation` | stock location list |
  | `stock.view_stockitem` | label template list |
  | **staff only** | Settings page, Parameters page, Test connection, Clear logs, and reading or saving the synonym, learned-category, parameter-mapping and unknown-parameter editors |

- **Category auto-creation needs `part.add_partcategory`.** This covers dashboard search, search details, PureScan CREATEPART, FASTADD gap-fill and batch import. API v1 already had this rule. A user without the permission still creates and fills parts; categories come from the fuzzy match or `DEFAULT_CATEGORY`.
- **Barcode lookup (`api/barcode/lookup/`) checks the object's own view permission.** This applies to stock items, locations, purchase/sales/return orders, builds, and supplier and manufacturer parts. Before, any user with `part.view_part` could look them up. A refused scan shows "Permission denied" in the scanner toast. `INV-` short codes are resolved with the user passed to the barcode plugins.
- **`/media/` requires a login.** The route SmartParts adds when Django is reached without Caddy/nginx served every uploaded file (attachments, images, reports) to anonymous requests. Behind Caddy nothing changes, because the proxy serves `/media/` itself.
- **CSRF protection on session writes.** This covers dashboard search and search details, batch upload, Test connection, Clear logs, the four settings editors and API v1 raw lookup. Browser requests must carry the CSRF token, which the plugin's pages already send. Requests with `Authorization: Token <key>` stay exempt, as with API v1 import. They run as the token's user, and an invalid token is a 401 that never falls back to the browser session.
- **Image downloads refuse internal addresses.** Image URLs, which can come from the dashboard request body, must resolve to public addresses only. Private, loopback, link-local, metadata and IPv4-mapped addresses are refused, and every redirect hop (at most 5) is checked again.
- **SVG images are no longer accepted.** An SVG can carry scripts that would run on the InvenTree origin.

### Housekeeping
- Tests:
  - `warehouse_db_checks`: permission matrix (anonymous → 401, missing permission or not staff → 403), category creation without `part.add_partcategory` (create, gap-fill, dashboard search), per-object barcode permissions, the media route, CSRF and token authentication;
  - offline: image download guard (internal hosts, redirects, redirect loops, SVG).
- Review status updated in `docs/plugin-review.md`.

## [1.6.0] - 2026-11-25

### Added
- **DigiKey and Mouser supplier parts from either label.** PureScan CREATEPART and FASTADD already queried every enabled distributor for new parts. Now FASTADD also adds a missing DigiKey or Mouser supplier part to an *existing* part (not only the label's own distributor). A distributor that answers "not carried" is remembered on the part (`Part.metadata["smartparts"]["lookups"]`) and not asked again for 30 days. API errors are never remembered, so an outage is retried on the next scan.
- **Default supplier.** The scanned label's distributor becomes `Part.default_supplier` on creation, and on existing parts when none is set. A default chosen in InvenTree is never overwritten.
- **FASTADD / CREATEPART: stock already in the default location.** After the DataMatrix, the terminal shows how much of the part is already in the `SYS:LAST` location and up to three `STOCK_` labels (largest first), so the reel can be merged instead of becoming a new instance.
- PureScan stock card: part number, description, counts and the `STOCK_` labels drawn as printed stickers, for INFO, FASTADD and CREATEPART.

### Changed
- **Category always from DigiKey when DigiKey has the part**, whatever `API_PRIORITY` says (a stored Mouser-first priority no longer leaves Mouser scans uncategorized). Order: DigiKey's category chain → if it cannot be placed, Mouser's flat leaf, fuzzy-matched only → `DEFAULT_CATEGORY`. Other fields still follow `API_PRIORITY`.
- A category that came from Mouser (fuzzy or default) is replaced by DigiKey's on a later scan, unless it was edited since.
- **SYS:INFO on a DataMatrix** shows the MPN, description and `Total stock: N (M in default location: [STOCK_…, …])`, followed by the label details (distributor, manufacturer, SKU, reel quantity, batch). It replaces the 1.4.1 two-line output. The parenthesis is shown only when the default location holds stock. An idle DataMatrix scan of a known part shows the same view.
- Stock labels are ordered by quantity (largest first) instead of alphabetically. The stock-summary endpoint also returns `mpn`, `description` and per-label quantities.
- `api/barcode/lookup/` returns `missing_suppliers` and `enrich_needed`; FASTADD gap-fills on `enrich_needed`.

### Fixed
- DigiKey requested a new OAuth token on every search (the token was cached per client instance, and a new client is built per search). Tokens are now shared by the process.
- DigiKey keyword hits without a category fall back to the product-details endpoint.
- A `SYS:CANCEL` during the CREATEPART distributor search was overwritten by the late result.

### Housekeeping
- Cache-buster `purescan.js?v=13`.
- Tests: offline (merge category source, DigiKey fallback and token cache, category upgrade, lookup memory), Django/SQLite integration scenarios for CREATEPART / FASTADD with both distributors, and PureScan JS tests for the new displays. New CI workflow `tests.yml` runs all suites.
- Code review findings recorded in `docs/plugin-review.md`.

## [1.5.1] - 2026-11-23

Repairs the merge of the 1.4.1 warehouse changes onto 1.5.0. Both feature sets now work together.

### Fixed
- **Every distributor search crashed** (`NameError: only`). This broke dashboard search, PureScan CREATEPART/FASTADD and API v1. `_run_all_api_searches()` takes the `only` source filter again, together with the 1.4.1 `prices` / `raw_prices` options.
- **Every part create or update with distributor data failed** ("name 'part' is not defined"). `_get_supplier_entries()` no longer reads the database.
- **Supplier price tiers**: there is one code path again, for new and existing supplier parts. The leftover 1.5.0 path ("fill only when empty") skipped the 1.4.1 in-place refresh and USD→EUR normalisation, and stored USD tiers unconverted.
- **Mouser currencies**: the code is read from the price text when the `Currency` field is missing (`$`→USD, `CA$`→CAD, `£`→GBP, ISO codes). It is then normalised like DigiKey prices.
- **PureScan gap-filling is restored.** SYS:CREATEPART on an existing part and the one-time FASTADD Mouser gap-fill work again, on top of the 1.4.1 part resolution (ambiguous match → 409). They require `part.change_part`. Gap-fill writes take the same per-MPN lock as PureScan creates, and pricing warnings appear in the terminal.
- PureScan CREATEPART uses `API_PRIORITY` again, with DigiKey first by default, instead of a hard-coded Mouser-first fallback.
- API v1 import: Mouser's flat category is never auto-created again. The 1.4.1 rule (no category creation on dry runs or without `part.add_partcategory`) is kept.
- FASTADD: a cancel or a new command during the gap-fill request is no longer overwritten by its late response.

### Changed
- Existing supplier parts now get their quoted price tiers refreshed in place (1.4.1 behaviour), instead of being filled only when empty (1.5.0). Tiers the distributor did not quote are kept. A missing exchange rate skips that supplier's quote and shows a warning. The supplier part link and manufacturer part are still only filled when empty. Gap-fill reports changed tiers as "<Supplier> prices updated".

### Housekeeping
- `black` formatting (the push CI failed on 10 files).
- Cache-busters: `purescan.js?v=12`, and `scanner.js?v=7` on the settings page.
- New tests:
  - offline: Mouser currencies, supplier entries;
  - `warehouse_db_checks`: gap-fill supplier part, `only` filter;
  - `purescan.test.cjs`: FASTADD gap-fill and cancel during the gap-fill.

## [1.5.0] - 2026-11-19

### Added
- **Mouser labels in PureScan, on par with DigiKey**: scanning a Mouser DataMatrix creates the part named after the MPN, with supplier part (Mouser P/N looked up by MPN — Mouser labels carry only `1P`), price breaks, lead time, image, datasheet and parameters.
- **Priority-aware gap filling for existing parts**: SYS:CREATEPART on an existing part now adds the missing supplier parts and fills empty fields instead of stopping at "already exists". SYS:FASTADD does the same once, when the part has no supplier part from the scanned label's distributor yet (no API calls on later scans, so burst mode stays fast). The PureScan prompt lists what was filled.
- **Field provenance** (`services/provenance.py`, stored in `Part.metadata["smartparts"]`): each written field records the distributor that supplied it. A higher-priority source (`API_PRIORITY`) may replace a lower-priority value only if it was not edited since; manual edits, editor input and legacy values are never overwritten. Covers description, datasheet link, category, image, core and spec parameters.
- Barcode decoder: `1V` → `manufacturer`, distributor detection (`digikey` / `mouser`) on ANSI labels; PureScan INFO shows `Label:` / `Mfr:`.

### Changed
- **`API_PRIORITY` default is now `digikey,mouser,element14,tme,lcsc`** (was Mouser first). Existing installs keep their stored value — set it in the plugin settings.
- Mouser-only parts: the flat Mouser leaf category is fuzzy-matched into the existing tree or falls back to `DEFAULT_CATEGORY`; it is never auto-created as a root category.
- Updates of existing parts (dashboard editor, API v1 / batch `update`) no longer overwrite parameter values (`update_or_create` replaced values despite the docstring), no longer replace the image unconditionally, and reuse the part's existing ManufacturerPart when distributors spell the manufacturer differently. Existing supplier parts only get price breaks / link filled when they have none.

### Fixed
- Mouser client: correct `SearchByPartRequest` body (`partSearchOptions`); API `Errors` (e.g. invalid key) are raised instead of reported as "not found"; lead time `84 Days` → `12 Weeks`; localised prices (`1.234,56 €`); null-safe best match, preferring a purchasable listing and matching by Mouser P/N; repeated attributes (`Packaging`) are joined instead of dropped.
- Image fallback across sources: the merger now passes all sources' image URLs in priority order.
- GS-stripped labels: quantity no longer swallows the next field (`Q50`+`11K…` read as 501, DigiKey `Q25`+`11Z` as 2511).

### Housekeeping
- Cache-buster `purescan.js?v=11`. Shared helpers `get_priority_order()` and `resolve_category_for_source()` replace copy-pasted blocks.

## [1.4.1] - 2026-11-20

- FASTADD keeps the terminal armed for 30 seconds after a receipt or merge.
- DataMatrix `SYS:INFO` shows total stock and an exact `SYS:LAST` subtotal with up to three linked stock labels.
- The dashboard Receive Stock form can bind an optional `STOCK_XXXX` label. New bindings use native InvenTree hashing, and a maintenance preview can identify older labels to repair.
- SmartParts converts USD supplier price breaks to EUR using stored exchange rates. A separate preview/apply tool converts existing USD supplier price breaks.

## [1.4.0] - 2026-11-18

### Fixed
- **Parts named with the DigiKey part number (critical)**: PureScan CREATEPART/FASTADD and the dashboard editor created parts whose `Part.name` and `ManufacturerPart.MPN` were the DigiKey PN (e.g. `296-1234-1-ND`). Two causes:
  - The DigiKey client read the v3 field `ManufacturerPartNumber`; v4 calls it `ManufacturerProductNumber`, so the MPN fell back to the search string. Best-match selection read the same key and never matched.
  - All barcode parsers (`barcode_decoder.py`, `scanner.js`, `scanner_global.js`) let a generic `P` field claim the MPN slot. DigiKey labels put `P<DigiKey PN>` before `1P<MPN>`, so the DigiKey PN was searched. `1P` now always wins; `P` falls back to MPN (no 1P) or SKU (no 30P).
- **DigiKey v4 fields**: image from `PhotoUrl`, full category path from `Category.ChildCategories` (v4 nests root→leaf; previously only the root was seen), Series from top-level `Series.Name`, lead time from `ManufacturerLeadWeeks`, MOQ/SKU/pricing from the `ProductVariations` entry matching the searched DigiKey PN, `Package / Case` from Parameters (no longer from packaging). Keyword search uses v4 `Limit`/`Offset` and prefers `ExactMatches`.
- **Category chains**: auto-created chains split only on `>` (names like `Interface - I/O Expanders` are no longer split on `/`); segment lookup is case-insensitive. When a fuzzy match is an exact ancestor of the distributor chain, the rest of the chain is found/created beneath it instead of stopping at the broad parent.
- **Editor Name field** is now honoured (it was ignored; the MPN field was always used).

### Changed
- **MPN is the part identity**: only distributor results with the same MPN (case-insensitive) are merged; others are dropped and flagged `≠MPN` in the editor. PureScan CREATEPART, batch import and API v1 import are strict (no exact MPN/SKU hit → nothing created). Duplicate detection is exact MPN only — the suffix-stripping fuzzy match was removed, so a slightly different MPN creates a new part.

### Added
- Maintenance scripts (dry-run by default, run from the InvenTree Django shell):
  - `inventree_smart_parts.maintenance.rename_digikey_parts.run()` — renames parts created with a DigiKey PN and fixes their ManufacturerPart MPN; conflicts are reported, never merged.
  - `inventree_smart_parts.maintenance.recategorize_digikey_parts.run()` — moves auto-placed DigiKey parts into their full DigiKey category chain.
- Offline unit tests (`tests/`, stdlib `unittest`).

### Housekeeping
- Cache-busters: `scanner.js?v=6`, `editor_main.js?v=12`. `scanner_global.js` is unversioned — hard-refresh browsers once.

## [1.3.0] - 2026-11-16

### Fixed
- **DigiKey descriptions lost (critical)**: DigiKey v4 nests descriptions under a `Description` object, but the client read top-level `ProductDescription`/`DetailedDescription` — so fetched parts were created with the fallback `"Manufacturer MPN"` description. Fixed by unwrapping the nested object; the brief description now lands in `Part.description` and the detailed text is preserved separately.

### Added
- **Uniform distributor metadata** (Part Parameters, always written regardless of `LIMIT_PARAMETERS_TO_CATEGORY`):
  - `Detailed Description`, `Manufacturer Std Lead Time`, `Series`, `Package / Case`, `Supplier Device Package`.
  - New `_create_core_parameters()` in part_creator: find-or-create global ParameterTemplates, same non-destructive update_or_create pattern; de-duplicated against the spec parameter list.
  - DigiKey: lead time from `ManufacturerStandardLeadTime`, Series/Package/SupplierDevicePackage promoted from Parameters.
  - Mouser: lead time from `LeadTime`, Series/Package/SupplierDevicePackage promoted from ProductAttributes.
  - element14/TME/LCSC: promoted where their APIs provide them; unsupported fields stay empty.
- **Editor UI**: the part editor now shows the distributor's detailed description as a read-only block under the Description field (lead time deliberately not shown).

### Changed
- **Part naming — bare MPN**: `Part.name` is now the bare MPN (`STM32WL55CCU6`) instead of `"STMicroelectronics STM32WL55CCU6"`. The manufacturer remains on the ManufacturerPart. Cross-supplier MPN is the natural key; bare naming avoids duplicate parts per distributor. Existing parts keep their names (update path never touches `part.name`).

### Housekeeping
- `editor_helpers.js` now has a cache-buster (`?v=1`) in dashboard.html (previously unversioned); `editor_main.js` bumped to `?v=11`.

## [1.2.6] - 2026-11-16

### Added
- **FASTADD burst mode**: after a FASTADD commit, the terminal stays armed for 15s. The steady-state loop becomes 2 scans per reel (`DataMatrix → free token` for new instances, `DataMatrix → STOCK_ label` for merges into an existing instance) with no action-code re-scan. Any control code, other action, or expiry cleanly disarms the burst.

### Changed
- **FASTADD free-token fallback**: in the location step, scanning a free `STOCK_XXXX` token now commits immediately with the stored `SYS:LAST` location (qty pre-filled from the DataMatrix `Q` field). `SYS:FASTADD → DataMatrix → free token` = 3 scans. If no default location is stored, the flow degrades safely (error prompt, state preserved). CREATESTOCK keeps its explicit location step.
- **FASTADD merge shortcut**: in the location step, scanning a bound `STOCK_XXXX` token of the same part now merges directly into that instance (2-scan merge loop).

### Housekeeping
- purescan.js cache-buster `?v=9` → `?v=10`.

## [1.2.5] - 2026-11-16

### Fixed
- **Search 500 (critical)**: `resolve_category_with_autocreate` passed an undefined `user_synonyms` variable to `fuzzy_match_category` (NameError). Every search whose merged result carried a category crashed with a 500 before the hang fix was even relevant. Fixed to pass the `user_synonyms_json` parameter.

## [1.2.4] - 2026-11-16

### Fixed
- **MPN search hang (critical)**: DigiKey category parsing walked the full ParentCategory chain but an empty/absent Category produced an infinite loop (`while isinstance(current, dict)` spins on `{}`). Every MPN search hung indefinitely on "Search in progress". Fixed with `and current` termination + `current.get("ParentCategory")` (no default) so the loop ends on any non-dict.

### Added
- **DigiKey category at click time**: DigiKey's KeywordSearch responses do not carry the Category field (only ProductDetails does), so the MPN search editor now fetches it in the background after the user clicks a best match:
  - New `get_product_details(dkpn)` in the DigiKey client (ProductDetails v4 endpoint, URL-encoded DigiKey part number).
  - New `api/search/details/` endpoint (source + sku, generic for later expansion; DigiKey-only for now). Runs the returned category through the same learned/fuzzy/auto-create pipeline. Failure paths are silent (empty response).
  - Editor (`editor_main.js`) fires the background fetch after render; on response it updates the category select (adding the option if the category was auto-created), the Auto badge, and the on-save learning variables. Parameters are re-gated only if the category actually changed.

### Housekeeping
- editor_main.js now has a cache-buster (`?v=10`) in dashboard.html (previously unversioned, risking stale JS).

## [1.2.3] — 2026-11-14

### Added
- **Category auto-creation** (DigiKey/distributor categories):
  - `fuzzy_match_category` now walks the full ParentCategory chain (previously truncated at 2 levels, e.g. 'Products > Kits > Integrated Circuits (IC) Kits').
  - New `resolve_category_with_autocreate` wrapper: learned mapping / fuzzy match first; if no match and no default category, auto-creates the category chain (find-or-create top-down, generic roots like 'Products' stripped) and records it as a learned mapping.
  - Applies to all part-creation flows: PureScan CREATEPART/FASTADD, dashboard import, v1 import, and batch import.
  - New plugin setting **AUTO_CREATE_CATEGORIES** (default ON) as a kill-switch.
  - `createpart` response now includes `category_path` + `category_created`; the terminal shows the category on success.

## [1.2.2] — 2026-11-14

### Fixed
- Version bump only (no code changes): forces pip to actually reinstall the package — the 1.2.1 template fix (Fast Add hint card) was skipped by `pip install -U` because the version was unchanged.

## [1.2.1] — 2026-11-14

### Added
- **SYS:FASTADD (Fast Add / Receive)**: one-command receive flow for distributor reels.
  - Added to the IDLE hint cards in the PureScan terminal.
  - Scan a distributor DataMatrix or an instance (STOCK_) label after SYS:FASTADD.
  - Instance label → falls back to the SYS:ADD combo flow (qty accumulation).
  - DataMatrix → resolves the part (auto-creates it via SYS:CREATEPART if not in DB), then asks for a location (SYS:LAST default), a quantity (label's Q field default), and a free STOCK_ token.
  - Scanning a bound token in the token step: if it belongs to the same part AND same location → merges the reel quantity into the existing instance (refill); otherwise errors and asks to scan another label.
  - SYS:QTY:nnn shortcut accepted in the location step to keep the SYS:LAST location and set qty in one scan.

### Fixed
- **Depletion handling**: removing exactly the remaining quantity no longer blocks the operation — it now warns and routes through the double-scan confirm gate, deleting the stock item (label freed for reuse) on confirmation. Over-removals (more than available) are still blocked.
- `purescan_deletestock` API now accepts `stock_id` as an alternative to `token` (needed for depletion confirm on JSON-label scans).
- `purescan_token` API now returns `location_id` for bound stock items (needed for the FASTADD merge check).

## [1.2.0] — 2026-09-09

### Added
- **Category Parameter Whitelist & Inheritance Engine (`LIMIT_PARAMETERS_TO_CATEGORY`)**:
  - Gated incoming distributor parameters to valid InvenTree `ParameterTemplate` definitions in the chosen category.
  - Recursive ancestor category template inheritance (from leaf up to root).
  - Child-over-parent precedence (deepest leaf definition takes priority over parent categories).
  - Structural category rejection with atomic transaction rollback (`structural=True` rejected with HTTP 400).
  - Dropped parameter audit trail (`dropped_parameters`) in dry-run previews, full API responses, and Live Editor UI.
- **SI Unit Normalization & Engineering Shorthand**:
  - Canonical normalization for capacitance, resistance, inductance, voltage, current, power, frequency, and temperature.
  - Engineering shorthand parser (e.g. `4k7` $\rightarrow$ `4.7 kΩ`, `2R2` $\rightarrow$ `2.2 Ω`, `1M0` $\rightarrow$ `1.0 MΩ`).
  - Multiplier scaling into standard engineering $[1, 1000)$ range and sentinel empty value stripping (`-`, `N/A`, `unknown`, `tbd`).
- **Global Hardware Barcode Scanner Wedge (`scanner_global.js`)**:
  - Universal capture-phase keystroke listener for USB/Bluetooth HID scanners (<50ms inter-key duration, $\ge 80\%$ fast transitions, $\ge 4$ characters).
  - Active input element value restoration to prevent high-speed keystrokes from corrupting focused text fields.
  - Native InvenTree QR & DataMatrix barcode discrimination: routes directly to stock locations, stock items, internal parts, purchase orders, build orders, sales orders, return orders, and supplier parts without import prompts.
  - Context-aware modal suppression on SmartParts UI (`isSmartPartsActive()`): scans on dashboard or embedded panels suppress global modals and auto-forward directly to panel search.
  - Clean coexistence with dedicated PureScan kiosk terminal.
- **Token-Authenticated REST API**:
  - Single-shot import endpoint (`POST /plugin/smartparts/api/v1/import/`) with token authentication (`Authorization: Token <key>`).
  - Barcode classification endpoint (`POST /plugin/smartparts/api/barcode/lookup/`).
  - Category parameter whitelist endpoint (`GET/POST /plugin/smartparts/api/category/parameters/`).
  - Raw distributor diagnostic endpoint (`POST /plugin/smartparts/api/v1/raw-lookup/`).
- **Official Documentation Wiki**:
  - Comprehensive documentation covering hardware barcode scanner configuration, parameter engine inheritance rules, and REST API reference in `./wiki/`.

## [1.1.1] — 2026-08-10

### Fixed
- **PureScan Barcode Format Support**: Resolved issue where PureScan terminal only accepted JSON format barcodes and rejected InvenTree default shortcode QR codes (`INV-SI722`, `INV-SL5`, `INV-PA42`) and linked barcodes. Updated both backend (`purescan_resolve_barcode`) and frontend JS (`_parseInvenTreeBarcode`) parsers to natively handle shortcode, JSON, key=value, and custom linked barcodes.

## [1.1.0] — 2026-05-31

### Added
- **Dedicated Parameter Normalization Dashboard**: Built a separate Django View and HTML Template specifically for the Parameter Normalization Dashboard, moving management cleanly out of cramped plugin settings.
- **Permanent Ignore Filter Heuristics**: Added permanent ignore dropdown support. Ignored parameters are saved with `is_ignored = True` and silently dropped during the data merge phase in `data_merger.py` (and Creator safeguards).
- **Autocomplete Dynamic Dropdowns**: Integrated autocomplete inputs for Canonical Values utilizing the database-backed `ParameterTemplate` options combined with hardcoded canonical electronic maps.
- **TME.eu API Client**: Secure, HMAC-SHA1 signed API integration to retrieve TME pricing, stock, and parameters.
- **element14 / Farnell API Client**: regional storefront selection with fallback lookup capabilities.
- **Regex-Based Parameter Sanitization**: String pre-processing filter to strip punctuation (hyphens, underscores, brackets, parentheses, slashes) and normalize whitespaces for robust canonical matches.
- **"Catch & Learn" Parameter Map UI**: Interactive Dashboard interface to track unknown parameters returned from APIs and map them dynamically.
- **GS character Wedge Handling**: Parser support for high-density DataMatrix barcodes containing non-printable GS (ASCII 29) group separator characters from hardware keyboard wedge scanners.
- **CI/CD Integration**: Configured GitHub Actions workflows for automated PEP 8 lints (`flake8`), `black` code styling checks, and full normalizer test execution.

### Changed
- **Parameter Normalization Expansion**: Expanded standard `PARAMETER_MAP` to cover 100+ common electronic, mechanical, and semiconductor units with SI scaling (e.g. converting `0.00001 F` -> `10 µF`).
- **Codebase Sanitization & Formatting**: Reformatted the entire Python codebase (22 modules) using the `black` formatter to strictly conform to PEP 8.
- **Documentation Overhaul**: Rewrote the entire `README.md` to showcase the new multi-source features, parameter mapping architecture, PureScan terminal commands, and configuration variables.

### Fixed
- **Sanitized API Diagnostics & Logs**: Uniformly standardized all connection test results to return clean, standard success formats (e.g. `Connected successfully. Test search returned: LM7805`) and implemented a robust query parameter filter to scrub sensitive parameters/URLs from all API error exceptions and activity log warnings.
- **Dead Code & Unused Imports Cleanup**: Removed unused imports and legacy variables across all Python files, including `duplicate_checker.py`, `image_handler.py`, `views.py`, and `tools/generate_command_sheet.py`.
- **Unicode Console Output**: Resolved cp1252 encoding and character crashes on Windows terminal environments when testing values containing Ω or µ symbols.

## [1.0.1] — 2026-05-16

### Fixed
- **Multi-Supplier Creation**: All confirmed API hits now create `SupplierPart` records. Previously, only the first supplier was saved when an item was found on multiple distributors (e.g. Mouser AND DigiKey).
- **DigiKey Empty SKU**: Added a three-tier fallback chain for extracting DigiKey Part Numbers (`DigiKeyPartNumber` → `ExactDigiKeyPartNumber` → `ProductVariations` → MPN-based synthetic). Parts from DigiKey are no longer silently dropped when the primary SKU field is empty.
- **DigiKey Price Breaks**: Fixed pricing extraction for DigiKey API v4, which nests `StandardPricing` inside `ProductVariations` instead of at the root level. Added `UnitPrice` single-price fallback.
- **Parameter Sanitization Bypass**: The part update path now routes all parameter data through the same sentinel filters (`is_useless_value`) as the creation path. Empty/placeholder values (`-`, `N/A`, `?`) are no longer written to the database during updates.
- **SupplierPart Backfill**: Legacy `SupplierPart` records created without a `manufacturer_part` FK are automatically backfilled on subsequent imports, making them visible in the standard InvenTree UI.

### Added
- **UI Edit Highlighting**: Manual changes to API-parsed fields are now visually highlighted with an orange left border (`sp-input-edited`), preventing accidental overwrites.
- **SKU Fallback Badge**: Supplier cards using a fallback SKU display a `⚠ SKU Fallback` warning badge so the user can review and correct the value.
- **Dashboard Escape Hatch**: "Back to InvenTree" button added to the SmartParts Dashboard header for kiosk/fullscreen environments.

### Changed
- Diagnostic `print()` statements replaced with production `logger.debug()` calls.
- Test and diagnostic scripts excluded from packaging via `.gitignore`.

## [1.0.0] — 2026-05-10

### Initial Release
- MPN lookup across Mouser, DigiKey, and LCSC with configurable priority merging
- Intelligent fuzzy category matching with persistent learning
- Batch BOM import (Altium CSV/XLSX) with auto-column mapping
- PureScan zero-click warehouse terminal (Transfer, Add, Remove, Stocktake, Info, Undo)
- Auto label printing (Dymo/Zebra via inventreelabelmachine)
- Duplicate detection with update-in-place support
- DataMatrix barcode parsing (ANSI MH10.8.2 / ISO/IEC 15434)
- PUI integration (panels + dashboard widgets)
