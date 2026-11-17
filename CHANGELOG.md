# Changelog

All notable changes to SmartParts are documented in this file.

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