"""
Mouser API Client
=================
Integration with Mouser Electronics Search API v2.
Searches parts by MPN and normalizes results to PartData.

Mouser DataMatrix labels carry no Mouser part number (only 1P = MPN), so
PureScan always looks Mouser parts up by MPN. The search API returns no
technical specs — ProductAttributes holds only packaging data — and no
detailed description / series / package.
"""

import logging
import re
from typing import Optional, Dict, Any, List

from .base import BaseApiClient, PartData, PriceBreak, PartParameter, ApiError

logger = logging.getLogger("inventree_smart_parts.api.mouser")

_DAYS_RE = re.compile(r"^(\d+)\s*days?$", re.IGNORECASE)

# ISO 4217 code inside a localised price ("USD 0.0123", "0,009 EUR").
_ISO_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")

# Currency symbols in Mouser's localised price text, longest first so that
# "CA$" is not read as "A$" or "$". A bare "$" is the US storefront (USD).
# Ambiguous symbols ("¥", "kr") are left to the EUR fallback.
_CURRENCY_SYMBOLS = (
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("AU$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("SG$", "SGD"),
    ("NT$", "TWD"),
    ("MX$", "MXN"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("₹", "INR"),
    ("₩", "KRW"),
    ("₪", "ILS"),
    ("฿", "THB"),
    ("zł", "PLN"),
    ("Kč", "CZK"),
    ("Ft", "HUF"),
)


class MouserClient(BaseApiClient):
    """Client for the Mouser Electronics Search API v2."""

    SOURCE_NAME = "mouser"
    BASE_URL = "https://api.mouser.com/api/v2"

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key

    def search_by_mpn(self, mpn: str) -> Optional[PartData]:
        """
        Search Mouser for a part by MPN (or Mouser part number).

        Uses the SearchByPartNumber endpoint which gives the most
        precise results for exact MPN lookups.
        """
        if not self.api_key:
            raise ApiError("Mouser API key is not configured")

        url = f"{self.BASE_URL}/search/partnumber"
        params = {"apiKey": self.api_key}

        # SearchByPartRequest only knows mouserPartNumber + partSearchOptions
        # (records/startingRecord/searchOptions belong to keyword search).
        # mouserPartNumber accepts a Mouser PN *or* a manufacturer PN.
        payload = {
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "None",
            }
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        logger.info(f"[Mouser] Searching for MPN: {mpn}")
        data = self._request(
            "POST", url, headers=headers, json_data=payload, params=params
        )

        # Mouser reports API errors (bad key, quota, ...) as HTTP 200 with an
        # Errors array — surface them instead of pretending "not found".
        errors = data.get("Errors") or []
        if errors:
            msgs = "; ".join(
                (
                    str(e.get("Message") or e.get("Code") or e)
                    if isinstance(e, dict)
                    else str(e)
                )
                for e in errors
            )
            raise ApiError(f"Mouser API error: {msgs}")

        # Parse the response — guard against None/unexpected shape
        search_results = data.get("SearchResults") or {}
        if not isinstance(search_results, dict):
            logger.warning(
                f"[Mouser] Unexpected SearchResults type: {type(search_results).__name__}"
            )
            return None
        parts = [p for p in (search_results.get("Parts") or []) if isinstance(p, dict)]
        if not parts:
            logger.info(f"[Mouser] No results found for MPN: {mpn}")
            return None

        # Find the best match – prefer exact MPN match
        best_match = self._find_best_match(parts, mpn)
        if best_match is None:
            best_match = parts[0]

        return self._parse_part(best_match, mpn)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _s(raw: Dict, key: str) -> str:
        """Null-safe stripped string field."""
        return str(raw.get(key) or "").strip()

    @classmethod
    def _purchasable_score(cls, part: Dict) -> int:
        """Prefer listings that have prices and stock (Mouser lists some
        MPNs several times, e.g. obsolete / non-stocked duplicates)."""
        score = 0
        if part.get("PriceBreaks"):
            score += 2
        if cls._parse_stock(part):
            score += 1
        return score

    def _find_best_match(self, parts: List[Dict], mpn: str) -> Optional[Dict]:
        """Find the part with the closest MPN match.

        Order: exact MPN → search term is the Mouser PN → MPN substring.
        Among several exact-MPN listings, the purchasable one wins.
        """
        mpn_lower = (mpn or "").lower().strip()

        exact = [
            p
            for p in parts
            if self._s(p, "ManufacturerPartNumber").lower() == mpn_lower
        ]
        if exact:
            # max() keeps the first of equal scores → API order preserved
            return max(exact, key=self._purchasable_score)

        for part in parts:
            if self._s(part, "MouserPartNumber").lower() == mpn_lower:
                return part

        for part in parts:
            mfr_pn = self._s(part, "ManufacturerPartNumber").lower()
            if mfr_pn and mpn_lower and mpn_lower in mfr_pn:
                return part

        return None

    @staticmethod
    def _parse_price(price_str) -> float:
        """Parse a localised Mouser price ("0,089 €", "$1,234.56",
        "1.234,56 €"). With both separators the last one is the decimal
        point; a lone comma is a decimal comma."""
        if isinstance(price_str, (int, float)):
            return float(price_str)
        clean = "".join(c for c in str(price_str or "") if c.isdigit() or c in ".,")
        if not clean:
            return 0.0
        if "," in clean and "." in clean:
            if clean.rfind(",") > clean.rfind("."):
                clean = clean.replace(".", "").replace(",", ".")
            else:
                clean = clean.replace(",", "")
        elif "," in clean:
            clean = clean.replace(",", ".")
        try:
            return float(clean)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_currency(price_str, explicit=None) -> str:
        """ISO currency of a Mouser price break.

        The PriceBreak ``Currency`` field wins. Without it the code or symbol
        in the localised price text decides ("$0.0123" -> USD, "CA$0.007" ->
        CAD, "0,089 €" -> EUR). Unrecognised text falls back to EUR. USD is
        converted to EUR later, when the price is stored (services/pricing).
        """
        for value in (explicit, price_str):
            text = str(value or "").strip()
            if not text:
                continue
            if len(text) == 3 and text.isalpha():
                return text.upper()
            match = _ISO_CODE_RE.search(text)
            if match:
                return match.group(1)
            for symbol, code in _CURRENCY_SYMBOLS:
                if symbol in text:
                    return code
        return "EUR"

    @staticmethod
    def _parse_stock(raw: Dict) -> Optional[int]:
        """Stock from AvailabilityInStock, else digits of "1,234 In Stock"."""
        for key in ("AvailabilityInStock", "Availability"):
            val = str(raw.get(key) or "")
            digits = "".join(c for c in val.split("In Stock")[0] if c.isdigit())
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    pass
        return None

    @staticmethod
    def _normalize_lead_time(lead) -> str:
        """Mouser reports "84 Days"; DigiKey "N Weeks" — normalise to weeks."""
        text = str(lead or "").strip()
        m = _DAYS_RE.match(text)
        if m:
            days = int(m.group(1))
            if days <= 0:
                return ""
            weeks = -(-days // 7)  # ceiling
            return f"{weeks} Week" if weeks == 1 else f"{weeks} Weeks"
        return text

    # ── Parsing ─────────────────────────────────────────────────────

    def _parse_part(self, raw: Dict[str, Any], search_mpn: str) -> PartData:
        """Convert raw Mouser API response to PartData."""
        # Price breaks
        price_breaks = []
        for pb in raw.get("PriceBreaks") or []:
            try:
                qty = int(pb.get("Quantity", 0))
                price = self._parse_price(pb.get("Price", "0"))
                currency = self._parse_currency(pb.get("Price"), pb.get("Currency"))
                if qty > 0 and price > 0:
                    price_breaks.append(
                        PriceBreak(quantity=qty, price=price, currency=currency)
                    )
            except (ValueError, TypeError, AttributeError):
                continue

        stock = self._parse_stock(raw)

        # Parameters from ProductAttributes. Mouser repeats a name for
        # multi-valued attributes (Packaging: Reel / Cut Tape / MouseReel) —
        # collapse them into one comma-joined value, keeping order.
        attr_values: Dict[str, List[str]] = {}
        for attr in raw.get("ProductAttributes") or []:
            if not isinstance(attr, dict):
                continue
            label = str(attr.get("AttributeName") or "").strip()
            value = str(attr.get("AttributeValue") or "").strip()
            if label and value:
                vals = attr_values.setdefault(label, [])
                if value not in vals:
                    vals.append(value)
        parameters = [
            PartParameter(name=name, value=", ".join(vals))
            for name, vals in attr_values.items()
        ]

        mpn_result = (
            self._s(raw, "ManufacturerPartNumber") or (search_mpn or "").strip()
        )
        manufacturer = self._s(raw, "Manufacturer")
        description = self._s(raw, "Description")

        # Series / Package / Supplier Device Package if Mouser ever sends
        # them as attributes — promote to the dedicated fields.
        lead_time = self._normalize_lead_time(raw.get("LeadTime"))
        series = ""
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

        # Category — Mouser only provides the leaf (no path)
        category = self._s(raw, "Category")

        # Datasheet — 'DataSheetUrl'; Mouser sometimes returns '#' placeholders
        datasheet_url = str(
            raw.get("DataSheetUrl") or raw.get("DatasheetUrl") or ""
        ).strip()
        if datasheet_url and not datasheet_url.startswith("http"):
            datasheet_url = ""

        image_url = self._s(raw, "ImagePath")
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"

        mouser_pn = self._s(raw, "MouserPartNumber")
        product_url = self._s(raw, "ProductDetailUrl")

        min_qty = 1
        try:
            min_qty = int(raw.get("Min") or 1)
        except (ValueError, TypeError):
            pass

        mult = 1
        try:
            mult = int(raw.get("Mult") or 1)
        except (ValueError, TypeError):
            pass

        confidence = 1.0
        if mpn_result.lower().strip() != (search_mpn or "").lower().strip():
            confidence = 0.8

        # v1.3.0: bare MPN as part name (see digikey.py note)
        return PartData(
            mpn=mpn_result,
            manufacturer=manufacturer,
            description=description,
            name=mpn_result,
            category=category,
            supplier_name="Mouser",
            supplier_sku=mouser_pn,
            supplier_url=product_url,
            datasheet_url=datasheet_url,
            image_url=image_url,
            package=package,
            parameters=parameters,
            price_breaks=price_breaks,
            stock_available=stock,
            minimum_order_qty=min_qty,
            order_multiple=mult,
            lead_time=lead_time,
            series=series,
            supplier_device_package=supplier_device_package,
            source="mouser",
            raw_data=raw,
            confidence=confidence,
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test Mouser API connectivity by performing a minimal search."""
        try:
            if not self.api_key:
                return {
                    "success": False,
                    "message": "API key is not configured",
                }

            # Search for a well-known part as a connectivity test
            result = self.search_by_mpn("LM7805")
            if result:
                return {
                    "success": True,
                    "message": f"Connected successfully. Test search returned: {result.mpn}",
                    "details": {
                        "test_mpn": result.mpn,
                        "manufacturer": result.manufacturer,
                    },
                }
            else:
                return {
                    "success": True,
                    "message": "Connected, but test search returned no results.",
                }

        except ApiError as e:
            from .base import sanitize_error_message

            return {
                "success": False,
                "message": f"Connection failed: {sanitize_error_message(str(e))}",
            }
        except Exception as e:
            from .base import sanitize_error_message

            return {
                "success": False,
                "message": f"Unexpected error: {sanitize_error_message(str(e))}",
            }
