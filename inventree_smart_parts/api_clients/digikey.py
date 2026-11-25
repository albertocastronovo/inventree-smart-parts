"""
DigiKey API Client
==================
Integration with DigiKey Product Information API v4.
Uses OAuth2 Client Credentials flow for server-to-server authentication.
"""

import time
import logging
import threading
from typing import Optional, Dict, Any, List

from .base import (
    BaseApiClient,
    PartData,
    PriceBreak,
    PartParameter,
    ApiError,
    ApiAuthError,
)

logger = logging.getLogger("inventree_smart_parts.api.digikey")

# OAuth2 tokens shared by every client instance of the process, keyed by
# client ID: views create a new DigiKeyClient per search, so a per-instance
# cache meant one token request per scan.
_TOKEN_CACHE: Dict[str, tuple] = {}
_TOKEN_LOCK = threading.Lock()


def clear_token_cache():
    """Forget cached OAuth2 tokens (tests, credential changes)."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()


class DigiKeyClient(BaseApiClient):
    """Client for the DigiKey Product Information API v4."""

    SOURCE_NAME = "digikey"
    BASE_URL = "https://api.digikey.com"
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
    DETAILS_URL = (
        "https://api.digikey.com/products/v4/product-details/{digiKeyPartNumber}"
    )

    def __init__(self, client_id: str, client_secret: str, **kwargs):
        super().__init__(**kwargs)
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    def _authenticate(self):
        """
        Obtain an OAuth2 access token using Client Credentials grant.
        Tokens are cached until expiry.
        """
        if self._access_token and time.time() < self._token_expiry:
            return  # Token still valid

        if not self.client_id or not self.client_secret:
            raise ApiAuthError("DigiKey Client ID and/or Client Secret not configured")

        cache_key = f"{self.client_id}:{hash(self.client_secret)}"
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(cache_key)
        if cached and time.time() < cached[1]:
            self._access_token, self._token_expiry = cached
            return

        logger.info("[DigiKey] Requesting OAuth2 access token...")

        try:
            response = self.session.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            response.raise_for_status()

            token_data = response.json()
            self._access_token = token_data["access_token"]
            # Expire 60 seconds early to be safe
            expires_in = token_data.get("expires_in", 3600)
            self._token_expiry = time.time() + expires_in - 60
            with _TOKEN_LOCK:
                _TOKEN_CACHE[cache_key] = (self._access_token, self._token_expiry)

            logger.info("[DigiKey] OAuth2 token obtained successfully")

        except Exception as e:
            self._access_token = None
            self._token_expiry = 0
            with _TOKEN_LOCK:
                _TOKEN_CACHE.pop(cache_key, None)
            raise ApiAuthError(f"DigiKey authentication failed: {e}")

    def _get_auth_headers(self) -> Dict[str, str]:
        """Return headers with a valid OAuth2 bearer token."""
        self._authenticate()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def search_by_mpn(self, mpn: str) -> Optional[PartData]:
        """Search DigiKey for a part by MPN."""
        if not self.client_id or not self.client_secret:
            raise ApiError("DigiKey credentials are not configured")

        headers = self._get_auth_headers()

        # v4 KeywordRequest uses Limit/Offset (v3 used RecordCount/...)
        payload = {
            "Keywords": mpn,
            "Limit": 10,
            "Offset": 0,
        }

        logger.info(f"[DigiKey] Searching for MPN: {mpn}")
        data = self._request(
            "POST", self.SEARCH_URL, headers=headers, json_data=payload
        )

        # v4 returns ExactMatches alongside Products; check exact ones first
        candidates = list(data.get("ExactMatches") or []) + list(
            data.get("Products") or []
        )
        if not candidates:
            logger.info(f"[DigiKey] No results found for MPN: {mpn}")
            return None

        # Find best match
        best = self._find_best_match(candidates, mpn)
        if best is None:
            best = candidates[0]

        part = self._parse_part(best, mpn, self._response_currency(data))
        if part is not None and not part.category:
            self._fill_category_from_details(part)
        return part

    def _fill_category_from_details(self, part: PartData) -> None:
        """Safety net: take the category from ProductDetails when the keyword
        hit carried none. Failures are logged; the search result stands."""
        sku = (part.supplier_sku or "").strip()
        if not sku or sku.lower() == (part.mpn or "").strip().lower():
            return  # no real DigiKey part number to look up
        try:
            details = self.get_product_details(sku)
        except Exception as e:
            logger.info(f"[DigiKey] Category lookup for {sku} failed: {e}")
            return
        if details is not None and details.category:
            part.category = details.category

    def get_product_details(self, dkpn: str) -> Optional[PartData]:
        """
        Fetch full product details for a DigiKey part number.

        Unlike KeywordSearch responses, ProductDetails responses include
        the Category field (full ParentCategory chain) and Parameters.
        """
        if not self.client_id or not self.client_secret:
            raise ApiError("DigiKey credentials are not configured")

        dkpn = (dkpn or "").strip()
        if not dkpn:
            raise ApiError("DigiKey part number is required")

        headers = self._get_auth_headers()

        # DigiKey part numbers are case-insensitive with separator characters
        # (e.g. "xc7a35t-1cpg236c" vs "XC7A35T-1CPG236C"); the API expects
        # them URL-encoded as-is.
        from urllib.parse import quote

        url = self.DETAILS_URL.format(digiKeyPartNumber=quote(dkpn, safe=""))

        logger.info(f"[DigiKey] Fetching product details for: {dkpn}")
        data = self._request("GET", url, headers=headers)

        product = data.get("Product")
        if not product:
            logger.info(f"[DigiKey] No details found for: {dkpn}")
            return None

        return self._parse_part(product, dkpn, self._response_currency(data))

    @staticmethod
    def _response_currency(data):
        locale = data.get("SearchLocaleUsed") or data.get("LocaleUsed") or {}
        return (
            data.get("Currency")
            or data.get("CurrencyCode")
            or (locale.get("Currency") if isinstance(locale, dict) else None)
        )

    @staticmethod
    def _mfr_pn(product: Dict[str, Any]) -> str:
        """Manufacturer part number: v4 'ManufacturerProductNumber', v3 fallback."""
        return str(
            product.get("ManufacturerProductNumber")
            or product.get("ManufacturerPartNumber")
            or ""
        ).strip()

    @staticmethod
    def _variations(product: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            v for v in (product.get("ProductVariations") or []) if isinstance(v, dict)
        ]

    @classmethod
    def _dk_pns(cls, product: Dict[str, Any]) -> set:
        """Lower-cased set of all DigiKey part numbers of a product."""
        pns = set()
        for key in ("DigiKeyPartNumber", "DigiKeyProductNumber"):
            val = str(product.get(key) or "").strip().lower()
            if val:
                pns.add(val)
        for var in cls._variations(product):
            val = str(
                var.get("DigiKeyProductNumber") or var.get("DigiKeyPartNumber") or ""
            ).strip()
            if val:
                pns.add(val.lower())
        return pns

    @classmethod
    def _select_variation(
        cls, product: Dict[str, Any], search_term: str
    ) -> Optional[Dict[str, Any]]:
        """Variation whose DigiKey PN equals the search term, else the first one."""
        variations = cls._variations(product)
        term = (search_term or "").strip().lower()
        if term:
            for var in variations:
                pn = str(
                    var.get("DigiKeyProductNumber")
                    or var.get("DigiKeyPartNumber")
                    or ""
                ).strip()
                if pn.lower() == term:
                    return var
        return variations[0] if variations else None

    def _find_best_match(self, products: List[Dict], mpn: str) -> Optional[Dict]:
        """Find product with closest MPN match.

        Order: exact MPN → search term is one of the product's DigiKey PNs
        (e.g. a scanned DigiKey label) → MPN substring.
        """
        mpn_lower = mpn.lower().strip()

        for product in products:
            if self._mfr_pn(product).lower() == mpn_lower:
                return product

        for product in products:
            if mpn_lower in self._dk_pns(product):
                return product

        for product in products:
            mfr_pn = self._mfr_pn(product).lower()
            if mfr_pn and mpn_lower in mfr_pn:
                return product

        return None

    @staticmethod
    def _category_path(cat: Any) -> List[str]:
        """Category path root→leaf.

        v4 nests root→leaf via ChildCategories; v3 nested leaf→root via
        ParentCategory. Both shapes are supported.
        """
        if isinstance(cat, str):
            return [cat] if cat.strip() else []
        if not isinstance(cat, dict) or not cat:
            return []

        parts: List[str] = []
        if cat.get("ChildCategories") or "ParentCategory" not in cat:
            # v4: walk down the (single-branch) child chain
            current = cat
            seen_ids = set()
            depth = 0
            while isinstance(current, dict) and current and depth < 12:
                cid = current.get("CategoryId")
                if cid is not None:
                    if cid in seen_ids:
                        break
                    seen_ids.add(cid)
                name = str(current.get("Name") or "").strip()
                if name:
                    parts.append(name)
                children = current.get("ChildCategories") or []
                current = children[0] if children else None
                depth += 1
        else:
            # v3: walk up the parent chain
            current = cat
            depth = 0
            while isinstance(current, dict) and current and depth < 12:
                name = str(current.get("Name") or "").strip()
                if name:
                    parts.insert(0, name)
                current = current.get("ParentCategory")
                depth += 1
        return parts

    def _parse_part(
        self, raw: Dict[str, Any], search_mpn: str, response_currency=None
    ) -> PartData:
        """Convert DigiKey product data to PartData."""
        variation = self._select_variation(raw, search_mpn)

        def code(value):
            if isinstance(value, dict):
                value = value.get("Code") or value.get("CurrencyCode")
            return str(value or "").strip().upper()

        product_currency = code(raw.get("Currency")) or code(response_currency) or "USD"

        # Price breaks — handle both v3 (StandardPricing at root) and
        # v4 (StandardPricing nested inside ProductVariations; taken from the
        # selected variation so SKU and pricing match).
        price_breaks = []
        pricing_list = raw.get("StandardPricing", [])
        tier_currency = product_currency

        if not pricing_list and variation:
            pricing_list = variation.get("StandardPricing") or []
            tier_currency = code(variation.get("Currency")) or product_currency
        if not pricing_list:
            for var in self._variations(raw):
                pricing_list = var.get("StandardPricing") or []
                if pricing_list:
                    tier_currency = code(var.get("Currency")) or product_currency
                    break

        for pb in pricing_list:
            try:
                qty = int(pb.get("BreakQuantity", 0))
                price = float(pb.get("UnitPrice", 0) or pb.get("Price", 0))
                currency = code(pb.get("Currency")) or tier_currency
                if qty > 0 and price > 0:
                    price_breaks.append(
                        PriceBreak(
                            quantity=qty,
                            price=price,
                            currency=currency,
                        )
                    )
            except (ValueError, TypeError):
                continue

        # Last resort: single unit price from top-level field
        if not price_breaks:
            unit_price = raw.get("UnitPrice", 0)
            if unit_price:
                try:
                    price_breaks.append(
                        PriceBreak(
                            quantity=1,
                            price=float(unit_price),
                            currency=product_currency,
                        )
                    )
                except (ValueError, TypeError):
                    pass

        # Parameters
        parameters = []
        for param in raw.get("Parameters", []):
            p_name = param.get("ParameterText", "")
            p_value = param.get("ValueText", "")
            if p_name and p_value:
                parameters.append(
                    PartParameter(
                        name=p_name,
                        value=p_value,
                    )
                )

        # Stock
        stock = None
        qty_available = raw.get("QuantityAvailable", None)
        if qty_available is not None:
            try:
                stock = int(qty_available)
            except (ValueError, TypeError):
                pass

        # Category — full root→leaf path (v4 ChildCategories, v3 ParentCategory)
        category_parts = self._category_path(raw.get("Category", {}))
        category = " > ".join(category_parts) if category_parts else ""

        # Datasheet — DigiKey v4 field name changed across API versions;
        # try all known locations in priority order.
        datasheet_url = (
            raw.get("DatasheetUrl", "")  # v4 primary field
            or raw.get("PrimaryDatasheet", "")  # v3 / legacy alias
            or ""
        )
        if not datasheet_url:
            # MediaLinks array: [{"MediaType": "Datasheets", "Url": "..."}]
            for link in raw.get("MediaLinks") or raw.get("MediaLinks", []):
                if isinstance(link, dict):
                    media_type = (
                        link.get("MediaType", "")
                        or link.get("Type", "")
                        or link.get("mediaType", "")
                    ).lower()
                    if "datasheet" in media_type:
                        datasheet_url = link.get("Url", "") or link.get("url", "")
                        break
        if datasheet_url and not datasheet_url.startswith("http"):
            datasheet_url = f"https://www.digikey.com{datasheet_url}"

        # Image
        # Image — v4 'PhotoUrl', v3 'PrimaryPhoto'
        image_url = str(raw.get("PhotoUrl") or raw.get("PrimaryPhoto") or "")
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"

        # MPN — v4 'ManufacturerProductNumber' (v3 'ManufacturerPartNumber').
        # Only fall back to the search term if DigiKey returned neither.
        mpn_result = self._mfr_pn(raw) or (search_mpn or "").strip()
        manufacturer_info = raw.get("Manufacturer", {})
        manufacturer = ""
        if isinstance(manufacturer_info, dict):
            manufacturer = manufacturer_info.get("Name", "")
        elif isinstance(manufacturer_info, str):
            manufacturer = manufacturer_info

        # v1.3.0: DigiKey v4 nests descriptions under a "Description" object
        # (ProductDescription = brief, DetailedDescription = full text).
        # Fall back to top-level keys for older/flat response shapes.
        desc_obj = raw.get("Description", {})
        if isinstance(desc_obj, dict):
            description = desc_obj.get("ProductDescription", "") or desc_obj.get(
                "DetailedDescription", ""
            )
            detailed_description = desc_obj.get("DetailedDescription", "")
        else:
            description = (
                str(desc_obj) if desc_obj else raw.get("ProductDescription", "")
            )
            detailed_description = raw.get("DetailedDescription", "")

        # Lead time — v4 'ManufacturerLeadWeeks' (string of weeks), v3 fallbacks
        lead_time = ""
        lead_weeks = str(raw.get("ManufacturerLeadWeeks") or "").strip()
        if lead_weeks:
            lead_time = f"{lead_weeks} Weeks" if lead_weeks.isdigit() else lead_weeks
        if not lead_time:
            lead_time = str(
                raw.get("ManufacturerStandardLeadTime")
                or raw.get("StandardLeadTime")
                or ""
            )

        # Series — v4 top-level {"Id", "Name"}; fall back to the Parameters list
        series = ""
        series_obj = raw.get("Series")
        if isinstance(series_obj, dict):
            series = str(series_obj.get("Name") or "").strip()
        elif isinstance(series_obj, str):
            series = series_obj.strip()

        # Package / Case and Supplier Device Package from the Parameters list
        package = ""
        supplier_device_package = ""
        for param in parameters:
            pname = param.name.lower().strip()
            if pname == "series" and not series:
                series = param.value
            elif pname == "supplier device package" and not supplier_device_package:
                supplier_device_package = param.value
            elif pname == "package / case" and not package:
                package = param.value

        # DigiKey part number as SKU — prefer the variation matching the
        # search term (a scanned/typed DigiKey PN), then legacy fields.
        dk_pn = ""
        if variation:
            dk_pn = str(
                variation.get("DigiKeyProductNumber")
                or variation.get("DigiKeyPartNumber")
                or ""
            ).strip()
        if not dk_pn:
            dk_pn = str(raw.get("DigiKeyPartNumber") or "").strip()
        if not dk_pn:
            dk_pn = str(raw.get("ExactDigiKeyPartNumber") or "").strip()

        # Last resort: synthesise from MPN (data confirmed by API)
        if not dk_pn and mpn_result:
            dk_pn = mpn_result

        # Product URL
        product_url = raw.get("ProductUrl", "")
        if product_url and not product_url.startswith("http"):
            product_url = f"https://www.digikey.com{product_url}"

        # Min order — v4 lives in ProductVariations, v3 at top level
        min_qty = 1
        min_val = (variation or {}).get("MinimumOrderQuantity") or raw.get(
            "MinimumOrderQuantity", None
        )
        if min_val:
            try:
                min_qty = int(min_val)
            except (ValueError, TypeError):
                pass

        # Confidence
        confidence = 1.0
        if mpn_result.lower().strip() != (search_mpn or "").lower().strip():
            confidence = 0.8

        # v1.3.0: bare MPN as part name (manufacturer prefix removed —
        # MPN is the cross-supplier natural key; duplicates come from
        # distributor-local part numbers, not from MPN collisions).
        return PartData(
            mpn=mpn_result,
            manufacturer=manufacturer,
            description=description,
            name=mpn_result,
            category=category,
            supplier_name="DigiKey",
            supplier_sku=dk_pn,
            supplier_url=product_url,
            datasheet_url=datasheet_url,
            image_url=image_url,
            package=package,
            parameters=parameters,
            price_breaks=price_breaks,
            stock_available=stock,
            minimum_order_qty=min_qty,
            detailed_description=detailed_description,
            lead_time=lead_time,
            series=series,
            supplier_device_package=supplier_device_package,
            source="digikey",
            raw_data=raw,
            confidence=confidence,
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test DigiKey API connectivity by authenticating."""
        try:
            if not self.client_id or not self.client_secret:
                return {
                    "success": False,
                    "message": "Client ID and/or Client Secret not configured",
                }

            self._authenticate()
            return {
                "success": True,
                "message": "Connected successfully. OAuth2 authentication OK.",
                "details": {
                    "token_expires_in": int(self._token_expiry - time.time()),
                },
            }

        except ApiAuthError as e:
            from .base import sanitize_error_message

            return {
                "success": False,
                "message": f"Authentication failed: {sanitize_error_message(str(e))}",
            }
        except Exception as e:
            from .base import sanitize_error_message

            return {
                "success": False,
                "message": f"Unexpected error: {sanitize_error_message(str(e))}",
            }
