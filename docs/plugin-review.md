# SmartParts review notes

Scope: warehouse state machine and input listeners; barcode/part resolution, stock creation and history; dashboard/browser and import permissions; supplier clients, merges and pricing; batch, labels, assets and maintenance. Target host contract: InvenTree 1.7.3.

## Addressed in this change

| Priority | Area | Finding and operator impact | Change |
| --- | --- | --- | --- |
| High | `services/token_manager.py`, `views.py` | Earlier PureScan bindings used a legacy SHA-256 hash and a stock route accepted location tokens. Two SmartParts terminals could both pass the free-token precheck. | Native assignment, strict stock tokens, an atomic receipt with a PostgreSQL advisory lock and a dry-run repair for legacy records. |
| High | `views.py`, `core.py` | First-row identity lookups and numeric PK fallback risked selecting the wrong part. The native barcode entry point also lacked the host's `user` argument. | Shared exact-match resolver refuses ambiguous part matches; no numeric fallback. Recheck a canonical MPN under a PostgreSQL lock during PureScan create. |
| High | `views.py` | Dashboard could create a stock item with only part-add rights. Session warehouse writes lacked CSRF enforcement, and API v1 validated raw token keys directly. | Use InvenTree permissions for the touched paths and host token authentication. Dashboard/PureScan browser writes check CSRF; stock summary and token reads have explicit view checks. |
| High | `services/part_creator.py`, `api_clients/` | Existing supplier parts were filtered out before refresh; delete-first refresh and currency fallbacks could discard or mislabel prices. | Update submitted tiers in place, preserve unmentioned tiers and old data on missing FX, normalize with native rates. Expose missing rates to operators. |
| Medium | `static/.../purescan.js`, `services/stock_manager.py` | Burst timers survived some command changes; stock history used an outdated endpoint and attribution path. | Clear burst timers, guard late FASTADD/INFO results, use native 1.7.3 history and stock creation attribution. |
| Medium | `static/.../scanner.js`, `editor_main.js` | The dashboard scan handler clears focused inputs and immediately starts a new MPN search; the error path replaced the whole form. | Bypass interception for the new stock field, preserve the form, and retry only receipt if the part was already saved. |

## Follow-up findings

| Priority | Area | Finding and proposed next step |
| --- | --- | --- |
| High | `views.py` / `services/image_handler.py` | The dashboard accepts image/datasheet URLs and download code runs on the server. Review routing these downloads through InvenTree's hardened remote-fetch path, including redirect and response-size handling. Several older batch/settings routes still exempt browser requests from CSRF; audit them separately. |
| Medium | `views.py` / `services/category_mapper.py` | Normal dashboard search can create categories before the operator saves a part. Move auto-create to confirmed ingestion or establish an explicit separate permission for it. API v1 dry-run/lookups now avoid category creation. |
| Medium | `batch/importer.py`, `views.py` | Batch jobs use in-process state, and distributor searches in `_run_all_api_searches` are sequential despite parallel-search wording in the README. Use the host worker for durable jobs, measure provider latency, and introduce bounded parallel lookups only if needed. |
| Medium | `services/part_identity.py`, `services/token_manager.py` | The advisory locks coordinate SmartParts on PostgreSQL. Core InvenTree writes and third-party integrations don't acquire these locks. If cross-client claims are common, coordinate with a native uniqueness guarantee. |
| Low | `core.py`, `tools/` | Global scanner/static integration relies on host UI patch points, and there is a separate generated command sheet. Verify these on each upgrade and consolidate the duplicate command catalogs when updating them next. |

## Verify on the warehouse instance before rollout

- With the operator account, scan `SYS:FASTADD`, a known distributor reel and a free label. Continue with another DataMatrix before 30 seconds, merge into the first stock item, then let the second burst expire. Also try a missing MPN and two terminals racing to claim the same free label.
- Set `SYS:LAST` on a parent location with stock both directly inside it and in child bins. Check the total, the exact-location subtotal and the label list with `SYS:INFO`. Check one part with no local stock and one fresh browser session with no default saved.
- Create a part by typing an MPN in the dashboard. Scan the stock token into **Receive Stock**, confirm scanner Enter does not change the MPN or submit the editor, then save. Verify barcode lookup in both native Quick Scan and PureScan, and check the stock item history and any selected printer output.
- Refresh exchange rates, inspect a part with several supplier tiers in different currencies, and preview/apply/re-run the conversion on a test database. Check a sub-cent tier and confirm part pricing is recalculated.

## 1.6.0 review (whole repository)

Overall: MPN identity, per-MPN advisory locks, provenance-aware gap filling, in-place price refresh and PureScan output escaping are solid. Most risk sits in older peripheral code.

| Priority | Area | Finding | Status |
| --- | --- | --- | --- |
| High | `core.py` `_serve_media` | An auth-exempt `/media/` route serves all of `MEDIA_ROOT` (attachments, reports, images) without login whenever Django is reached without the Caddy/nginx front. | Open — remove the route or require login. |
| High | `views.py` | `@csrf_exempt` session writes: `api_synonyms`, `api_learned`, `api_parameter_mappings`, `api_unknown_parameters` rewrite plugin settings with part-level permissions only; `api_logs_clear` clears the log. A cross-site POST can trigger them. | Open — drop `csrf_exempt` for session callers (as API v1 does) and require staff for settings. |
| High | `views.py` `api_search`, `api_search_details` | Dashboard searches auto-create categories with no permission check (also CSRF-exempt). PureScan create/gap-fill ignores `part.add_partcategory` too; API v1 enforces it. | Open |
| Medium | `api_clients/digikey.py` | OAuth token cached per client instance; a new client per search meant a token request on every scan. | **Fixed in 1.6.0** (process-wide cache). |
| Medium | `batch/importer.py` | Own copy of the search loop (element14/TME ignored), in-memory jobs on a thread (lost on restart, invisible across gunicorn workers, DB connection never closed), no identity lock, category creation without permission check. | Open — reuse `_run_all_api_searches` and the host worker. |
| Medium | `core.py` | Import-time side effects: appends a script tag to InvenTree's own `favicon.html` on disk (survives an uninstall), silently sets the global download User-Agent, injects URL patterns. | Open |
| Medium | `purescan.js` | Missing flow guards: a `SYS:CANCEL` during a slow call was overwritten by the late result. | **CREATEPART fixed in 1.6.0**; DELETEPART start/commit and IDLE stock-item/location quick-scans still open. |
| Low | `core.py` settings | `AUTO_CREATE_SUPPLIERS` and `LOG_RETENTION_DAYS` are never read; suppliers follow `AUTO_CREATE_MANUFACTURERS`. | Open |
| Low | `purescan.js` FASTADD merge | Merging a reel into an existing item ignores the reel's batch. | Open |
| Low | `views.py` | 3.5k-line module; `api_barcode_lookup` re-implements native barcode routing (~500 lines). | Open |
| Low | comments | "DigiKey KeywordSearch has no Category" (`views.py` `api_search_details`, `editor_main.js`) is stale since 1.4.0. | Open |
| Low | CI | Only flake8/black ran; no tests. | **Fixed in 1.6.0** (`.github/workflows/tests.yml`). |
