"""
Offline unit tests (no Django / InvenTree required).

Run from the repo root:
    python -m unittest discover -s tests -p "*_tests.py"

(Named *_tests.py because .gitignore excludes test_*.py.)
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub InvenTree's plugin registry (used lazily by the merger/normalizer).
_plugin_mod = types.ModuleType("plugin")
_registry_mod = types.ModuleType("plugin.registry")


class _FakeRegistry:
    def get_plugin(self, slug):
        return None


_registry_mod.registry = _FakeRegistry()
_plugin_mod.registry = _registry_mod
sys.modules.setdefault("plugin", _plugin_mod)
sys.modules.setdefault("plugin.registry", _registry_mod)

from inventree_smart_parts.api_clients.base import PartData  # noqa: E402
from inventree_smart_parts.api_clients.digikey import DigiKeyClient  # noqa: E402
from inventree_smart_parts.api_clients.mouser import MouserClient  # noqa: E402
from inventree_smart_parts.services.barcode_decoder import parse_barcode  # noqa: E402
from inventree_smart_parts.services.data_merger import merge_part_data  # noqa: E402

GS, RS, EOT = "\x1d", "\x1e", "\x04"

DIGIKEY_LABEL = (
    f"[)>{RS}06{GS}P296-1234-1-ND{GS}1PSN74HC00N{GS}30P296-1234-1-ND"
    f"{GS}K{GS}1K12345678{GS}10K87654321{GS}9D2345{GS}1TLOT42{GS}4LMY"
    f"{GS}Q25{GS}11ZPICK{RS}{EOT}"
)


def v4_product():
    return {
        "Description": {
            "ProductDescription": "IC GATE NAND 4CH 2-INP 14DIP",
            "DetailedDescription": "NAND Gate IC 4 Channel 14-PDIP",
        },
        "Manufacturer": {"Id": 296, "Name": "Texas Instruments"},
        "ManufacturerProductNumber": "SN74HC00N",
        "UnitPrice": 0.5,
        "ProductUrl": "https://www.digikey.com/en/products/detail/x",
        "DatasheetUrl": "https://www.ti.com/lit/ds/symlink/sn74hc00.pdf",
        "PhotoUrl": "https://mm.digikey.com/photo.jpg",
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "296-1234-5-ND",
                "PackageType": {"Id": 1, "Name": "Tube"},
                "StandardPricing": [
                    {"BreakQuantity": 1, "UnitPrice": 0.6, "TotalPrice": 0.6}
                ],
                "MinimumOrderQuantity": 1,
            },
            {
                "DigiKeyProductNumber": "296-1234-1-ND",
                "PackageType": {"Id": 2, "Name": "Cut Tape"},
                "StandardPricing": [
                    {"BreakQuantity": 10, "UnitPrice": 0.4, "TotalPrice": 4.0}
                ],
                "MinimumOrderQuantity": 10,
            },
        ],
        "QuantityAvailable": 1000,
        "Parameters": [
            {"ParameterText": "Package / Case", "ValueText": "14-DIP"},
            {"ParameterText": "Supplier Device Package", "ValueText": "14-PDIP"},
        ],
        "Category": {
            "CategoryId": 32,
            "ParentId": 0,
            "Name": "Integrated Circuits (ICs)",
            "ChildCategories": [
                {
                    "CategoryId": 2011,
                    "ParentId": 32,
                    "Name": "Logic",
                    "ChildCategories": [
                        {
                            "CategoryId": 705,
                            "ParentId": 2011,
                            "Name": "Gates and Inverters",
                            "ChildCategories": [],
                        }
                    ],
                }
            ],
        },
        "ManufacturerLeadWeeks": "6",
        "Series": {"Id": 1, "Name": "74HC"},
    }


class BarcodeTests(unittest.TestCase):
    def test_digikey_label_1p_wins_over_p(self):
        r = parse_barcode(DIGIKEY_LABEL)
        self.assertEqual(r.mpn, "SN74HC00N")
        self.assertEqual(r.supplier_sku, "296-1234-1-ND")
        self.assertEqual(r.quantity, 25)
        self.assertEqual(r.batch, "LOT42")

    def test_digikey_label_gs_stripped(self):
        raw = DIGIKEY_LABEL.replace(GS, "").replace(RS, "").replace(EOT, "")
        r = parse_barcode(raw)
        self.assertEqual(r.mpn, "SN74HC00N")
        # Without separators the weak 'K' DI that follows 30P can't be
        # delimited (pre-existing heuristic limitation), so only the prefix
        # of the SKU is reliable here.
        self.assertTrue(r.supplier_sku.startswith("296-1234-1-ND"))

    def test_p_only_label_still_gives_mpn(self):
        r = parse_barcode(f"[)>{RS}06{GS}PABC123{GS}Q5{RS}{EOT}")
        self.assertEqual(r.mpn, "ABC123")
        self.assertEqual(r.quantity, 5)

    def test_1p_first_then_p_becomes_sku(self):
        r = parse_barcode(f"[)>{RS}06{GS}1PLM317T{GS}PCUST-99{GS}Q3{RS}{EOT}")
        self.assertEqual(r.mpn, "LM317T")
        self.assertEqual(r.supplier_sku, "CUST-99")


class DigiKeyParseTests(unittest.TestCase):
    def setUp(self):
        self.client = DigiKeyClient("id", "secret")

    def test_v4_mpn_used_when_searching_by_digikey_pn(self):
        pd = self.client._parse_part(v4_product(), "296-1234-1-ND")
        self.assertEqual(pd.mpn, "SN74HC00N")
        self.assertEqual(pd.name, "SN74HC00N")
        # SKU / pricing / MOQ from the variation matching the search term
        self.assertEqual(pd.supplier_sku, "296-1234-1-ND")
        self.assertEqual(pd.minimum_order_qty, 10)
        self.assertEqual(pd.price_breaks[0].quantity, 10)

    def test_v4_metadata(self):
        pd = self.client._parse_part(v4_product(), "SN74HC00N")
        self.assertEqual(pd.supplier_sku, "296-1234-5-ND")  # first variation
        self.assertEqual(pd.image_url, "https://mm.digikey.com/photo.jpg")
        self.assertEqual(
            pd.category, "Integrated Circuits (ICs) > Logic > Gates and Inverters"
        )
        self.assertEqual(pd.series, "74HC")
        self.assertEqual(pd.lead_time, "6 Weeks")
        self.assertEqual(pd.package, "14-DIP")
        self.assertEqual(pd.supplier_device_package, "14-PDIP")
        self.assertEqual(pd.description, "IC GATE NAND 4CH 2-INP 14DIP")

    def test_v3_fallbacks(self):
        raw = {
            "ManufacturerPartNumber": "LM317T",
            "DigiKeyPartNumber": "LM317TFS-ND",
            "PrimaryPhoto": "https://x/p.jpg",
            "Manufacturer": {"Name": "onsemi"},
            "Category": {"Name": "PMIC", "ParentCategory": {"Name": "ICs"}},
        }
        pd = self.client._parse_part(raw, "LM317T")
        self.assertEqual(pd.mpn, "LM317T")
        self.assertEqual(pd.supplier_sku, "LM317TFS-ND")
        self.assertEqual(pd.image_url, "https://x/p.jpg")
        self.assertEqual(pd.category, "ICs > PMIC")

    def test_search_prefers_exact_matches(self):
        other = v4_product()
        other["ManufacturerProductNumber"] = "SN74HC00NSR"
        exact = v4_product()
        self.client._get_auth_headers = lambda: {}
        self.client._request = lambda *a, **k: {
            "Products": [other, exact],
            "ExactMatches": [exact],
        }
        pd = self.client.search_by_mpn("SN74HC00N")
        self.assertEqual(pd.mpn, "SN74HC00N")

    def test_search_by_digikey_pn_finds_product(self):
        other = v4_product()
        other["ManufacturerProductNumber"] = "OTHER1"
        other["ProductVariations"] = [{"DigiKeyProductNumber": "X-ND"}]
        self.client._get_auth_headers = lambda: {}
        self.client._request = lambda *a, **k: {"Products": [other, v4_product()]}
        pd = self.client.search_by_mpn("296-1234-1-nd")
        self.assertEqual(pd.mpn, "SN74HC00N")

    def test_pricing_uses_reported_search_currency_when_tier_omits_it(self):
        product = v4_product()
        for variant in product["ProductVariations"]:
            for tier in variant["StandardPricing"]:
                tier.pop("Currency", None)
        self.client._get_auth_headers = lambda: {}
        self.client._request = lambda *a, **k: {
            "Products": [product],
            "SearchLocaleUsed": {"Currency": "EUR"},
        }
        pd = self.client.search_by_mpn("296-1234-1-nd")
        self.assertEqual([tier.currency for tier in pd.price_breaks], ["EUR"])


class MouserPricingTests(unittest.TestCase):
    def test_preserves_reported_currencies_and_recognizes_missing_usd_code(self):
        parsed = MouserClient(api_key="key")._parse_part(
            {
                "ManufacturerPartNumber": "R-10K",
                "PriceBreaks": [
                    {"Quantity": 1, "Price": "$0.0123"},
                    {"Quantity": 10, "Price": "€0,009", "Currency": "EUR"},
                    {"Quantity": 100, "Price": "CA$0.007"},
                ],
            },
            "R-10K",
        )
        self.assertEqual(
            [(p.currency, p.price) for p in parsed.price_breaks],
            [("USD", 0.0123), ("EUR", 0.009), ("CAD", 0.007)],
        )

    def test_currency_from_code_symbol_or_fallback(self):
        cur = MouserClient._parse_currency
        self.assertEqual(cur("0,089 €"), "EUR")
        self.assertEqual(cur("£0.50"), "GBP")
        self.assertEqual(cur("USD 0.0123"), "USD")
        self.assertEqual(cur("1,23 CHF"), "CHF")
        self.assertEqual(cur("US$1,234.56"), "USD")
        self.assertEqual(cur("AU$0.10"), "AUD")
        self.assertEqual(cur("0.5"), "EUR")  # nothing recognisable
        self.assertEqual(cur("$0.10", "eur"), "EUR")  # Currency field wins


class SupplierEntriesTests(unittest.TestCase):
    """_get_supplier_entries is pure (no DB): every distributor entry is
    returned so existing supplier parts get their tiers refreshed.
    Regression: the 1.4.1 merge left it referencing an undefined `part`."""

    def test_every_distributor_entry_is_returned(self):
        from inventree_smart_parts.services.part_creator import _get_supplier_entries

        data = PartData(
            mpn="GRM32EC72A106ME05K",
            raw_data={
                "supplier_data": [
                    {
                        "source": "digikey",
                        "supplier_name": "DigiKey",
                        "supplier_sku": "490-1-ND",
                        "supplier_url": "https://dk.example/p",
                        "price_breaks": [{"qty": 1, "price": 0.5, "currency": "USD"}],
                    },
                    {
                        "source": "mouser",
                        "supplier_name": "Mouser",
                        "supplier_sku": "81-GRM32",
                        "price_breaks": [{"qty": 10, "price": 0.4}],
                    },
                    # duplicate (case-insensitive) and SKU-less entries are skipped
                    {
                        "source": "mouser",
                        "supplier_name": "mouser",
                        "supplier_sku": "81-grm32",
                    },
                    {"source": "lcsc", "supplier_name": "LCSC", "supplier_sku": ""},
                ]
            },
        )
        entries = _get_supplier_entries(data)
        self.assertEqual(
            [(e["name"], e["sku"]) for e in entries],
            [("DigiKey", "490-1-ND"), ("Mouser", "81-GRM32")],
        )
        self.assertEqual(entries[0]["url"], "https://dk.example/p")
        self.assertEqual(entries[0]["price_breaks"][0].currency, "USD")
        self.assertEqual(entries[1]["price_breaks"][0].currency, "EUR")  # default
        self.assertNotIn("fill_prices", entries[1])


class MergeIdentityTests(unittest.TestCase):
    @staticmethod
    def pd(source, mpn, sku=""):
        return PartData(mpn=mpn, name=mpn, source=source, supplier_sku=sku)

    def test_mismatched_mpn_dropped(self):
        merged = merge_part_data(
            [self.pd("mouser", "LM317T"), self.pd("digikey", "LM317-1")],
            ["mouser", "digikey"],
            search_term="LM317-1",
        )
        self.assertEqual(merged.mpn, "LM317-1")
        self.assertEqual(merged.raw_data["merged_sources"], ["digikey"])
        self.assertEqual(merged.raw_data["dropped_sources"][0]["source"], "mouser")

    def test_digikey_pn_search_anchors_to_mpn(self):
        merged = merge_part_data(
            [self.pd("mouser", "JUNK"), self.pd("digikey", "SN74HC00N", "296-1-ND")],
            ["mouser", "digikey"],
            search_term="296-1-ND",
            strict=True,
        )
        self.assertEqual(merged.mpn, "SN74HC00N")
        self.assertEqual(merged.name, "SN74HC00N")

    def test_case_insensitive_identity(self):
        merged = merge_part_data(
            [self.pd("mouser", "sn74hc00n"), self.pd("digikey", "SN74HC00N")],
            ["mouser", "digikey"],
            search_term="SN74HC00N",
            strict=True,
        )
        self.assertEqual(len(merged.raw_data["merged_sources"]), 2)

    def test_strict_without_exact_hit_returns_none(self):
        merged = merge_part_data(
            [self.pd("mouser", "LM317T")], ["mouser"], search_term="LM317", strict=True
        )
        self.assertIsNone(merged)

    def test_non_strict_falls_back_to_priority(self):
        merged = merge_part_data(
            [self.pd("digikey", "B"), self.pd("mouser", "A")],
            ["mouser", "digikey"],
            search_term="partial",
        )
        self.assertEqual(merged.mpn, "A")


class CategoryPathTests(unittest.TestCase):
    def test_split_keeps_slash_names(self):
        from inventree_smart_parts.services.category_mapper import (
            _split_category_path,
        )

        self.assertEqual(
            _split_category_path(
                "Products > Integrated Circuits (ICs) > Interface - I/O Expanders"
            ),
            ["Integrated Circuits (ICs)", "Interface - I/O Expanders"],
        )


MOUSER_LABEL_LITERAL = (
    "[)>{RS}06{GS}K606-45799789{GS}14K002{GS}1PGRM32EC72A106ME05K{GS}Q50"
    "{GS}11K087041143{GS}4LJP{GS}1VMurata{RS}<EOT>"
)
MOUSER_LABEL = (
    MOUSER_LABEL_LITERAL.replace("{GS}", GS).replace("{RS}", RS).replace("<EOT>", EOT)
)


class MouserBarcodeTests(unittest.TestCase):
    def check(self, raw):
        r = parse_barcode(raw)
        self.assertEqual(r.mpn, "GRM32EC72A106ME05K")
        self.assertEqual(r.quantity, 50)
        self.assertEqual(r.po_number, "606-45799789")
        self.assertEqual(r.manufacturer, "Murata")
        self.assertEqual(r.distributor, "mouser")
        self.assertEqual(r.supplier_sku, "")

    def test_mouser_label_control_chars(self):
        self.check(MOUSER_LABEL)

    def test_mouser_label_literal_substitutes(self):
        self.check(MOUSER_LABEL_LITERAL)

    def test_digikey_label_detected(self):
        self.assertEqual(parse_barcode(DIGIKEY_LABEL).distributor, "digikey")

    def test_gs_stripped_quantity_stops_before_11k(self):
        raw = MOUSER_LABEL.replace(GS, "").replace(RS, "").replace(EOT, "")
        r = parse_barcode(raw)
        self.assertEqual(r.mpn, "GRM32EC72A106ME05K")
        self.assertEqual(r.quantity, 50)

    def test_gs_stripped_digikey_quantity(self):
        raw = DIGIKEY_LABEL.replace(GS, "").replace(RS, "").replace(EOT, "")
        self.assertEqual(parse_barcode(raw).quantity, 25)


def mouser_part(**over):
    raw = {
        "MouserPartNumber": "81-GRM32EC72A106ME5K",
        "ManufacturerPartNumber": "GRM32EC72A106ME05K",
        "Manufacturer": "Murata Electronics",
        "Description": "Multilayer Ceramic Capacitors MLCC - SMD/SMT 100V 10uF",
        "Category": "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
        "LeadTime": "84 Days",
        "Availability": "12,345 In Stock",
        "DataSheetUrl": "https://www.mouser.com/datasheet/2/281/x.pdf",
        "ImagePath": "https://www.mouser.com/images/murata/x.jpg",
        "ProductDetailUrl": "https://www.mouser.com/ProductDetail/x",
        "Min": "1",
        "Mult": "1",
        "PriceBreaks": [
            {"Quantity": 1, "Price": "0,89 €", "Currency": "EUR"},
            {"Quantity": 1000, "Price": "0,089 €", "Currency": "EUR"},
        ],
        "ProductAttributes": [
            {"AttributeName": "Packaging", "AttributeValue": "Reel"},
            {"AttributeName": "Packaging", "AttributeValue": "Cut Tape"},
            {"AttributeName": "Standard Pack Qty", "AttributeValue": "1000"},
        ],
    }
    raw.update(over)
    return raw


class MouserParseTests(unittest.TestCase):
    def setUp(self):
        from inventree_smart_parts.api_clients.mouser import MouserClient

        self.Client = MouserClient
        self.client = MouserClient("key")

    def test_parse_fields(self):
        pd = self.client._parse_part(mouser_part(), "GRM32EC72A106ME05K")
        self.assertEqual(pd.mpn, "GRM32EC72A106ME05K")
        self.assertEqual(pd.name, "GRM32EC72A106ME05K")
        self.assertEqual(pd.supplier_name, "Mouser")
        self.assertEqual(pd.supplier_sku, "81-GRM32EC72A106ME5K")
        self.assertEqual(pd.lead_time, "12 Weeks")
        self.assertEqual(pd.stock_available, 12345)
        self.assertAlmostEqual(pd.price_breaks[1].price, 0.089)
        params = {p.name: p.value for p in pd.parameters}
        self.assertEqual(params["Packaging"], "Reel, Cut Tape")
        self.assertEqual(pd.confidence, 1.0)

    def test_lead_time_and_prices(self):
        n = self.Client._normalize_lead_time
        self.assertEqual(n("84 Days"), "12 Weeks")
        self.assertEqual(n("3 days"), "1 Week")
        self.assertEqual(n("15 Weeks"), "15 Weeks")
        self.assertEqual(n(None), "")
        p = self.Client._parse_price
        self.assertAlmostEqual(p("1.234,56 €"), 1234.56)
        self.assertAlmostEqual(p("$1,234.56"), 1234.56)
        self.assertAlmostEqual(p("0,089 €"), 0.089)
        self.assertAlmostEqual(p("$0.50"), 0.5)

    def test_none_fields_do_not_crash(self):
        pd = self.client._parse_part(
            mouser_part(ManufacturerPartNumber=None, Manufacturer=None, LeadTime=None),
            "ABC",
        )
        self.assertEqual(pd.mpn, "ABC")

    def test_errors_array_raises(self):
        from inventree_smart_parts.api_clients.base import ApiError

        self.client._request = lambda *a, **k: {
            "Errors": [{"Message": "Invalid unique identifier."}],
            "SearchResults": None,
        }
        with self.assertRaises(ApiError):
            self.client.search_by_mpn("LM7805")

    def test_best_match_exact_prefers_purchasable(self):
        dead = mouser_part(MouserPartNumber="81-DEAD", PriceBreaks=[], Availability="")
        live = mouser_part()
        other = mouser_part(ManufacturerPartNumber="GRM32EC72A106ME05L")
        self.client._request = lambda *a, **k: {
            "Errors": [],
            "SearchResults": {"Parts": [other, dead, live]},
        }
        pd = self.client.search_by_mpn("GRM32EC72A106ME05K")
        self.assertEqual(pd.supplier_sku, "81-GRM32EC72A106ME5K")

    def test_best_match_by_mouser_pn(self):
        self.client._request = lambda *a, **k: {
            "Errors": [],
            "SearchResults": {
                "Parts": [
                    mouser_part(ManufacturerPartNumber="X1", MouserPartNumber="81-X1"),
                    mouser_part(),
                ]
            },
        }
        pd = self.client.search_by_mpn("81-grm32ec72a106me5k")
        self.assertEqual(pd.mpn, "GRM32EC72A106ME05K")


class PriorityMergeTests(unittest.TestCase):
    def test_default_priority_is_digikey_first(self):
        from inventree_smart_parts.services.data_merger import get_priority_order

        self.assertEqual(get_priority_order()[:2], ["digikey", "mouser"])

    def test_digikey_wins_mouser_fills_gaps(self):
        from inventree_smart_parts.api_clients.base import PartParameter

        dk = PartData(
            mpn="X1",
            name="X1",
            source="digikey",
            description="DK desc",
            category="Capacitors > Ceramic Capacitors",
            image_url="https://dk/img.jpg",
            parameters=[PartParameter(name="Capacitance", value="10uF")],
        )
        mo = PartData(
            mpn="X1",
            name="X1",
            source="mouser",
            description="Mouser desc",
            category="MLCC - SMD/SMT",
            lead_time="12 Weeks",
            image_url="https://mo/img.jpg",
            parameters=[PartParameter(name="Packaging", value="Reel")],
        )
        merged = merge_part_data([mo, dk], None, search_term="X1", strict=True)
        fs = merged.raw_data["field_sources"]
        self.assertEqual(merged.description, "DK desc")
        self.assertEqual(fs["description"], "digikey")
        self.assertEqual(fs["category"], "digikey")
        self.assertEqual(merged.lead_time, "12 Weeks")
        self.assertEqual(fs["lead_time"], "mouser")
        self.assertEqual(
            [u["source"] for u in merged.raw_data["source_image_urls"]],
            ["digikey", "mouser"],
        )
        srcs = {p.name.lower(): p.source for p in merged.parameters}
        self.assertEqual(srcs["capacitance"], "digikey")
        self.assertEqual(srcs["package type"], "mouser")  # canonical name


class ProvenanceTests(unittest.TestCase):
    PRIO = ["digikey", "mouser", "lcsc"]

    def setUp(self):
        from inventree_smart_parts.services import provenance

        self.p = provenance

    def entry(self, src, value):
        return {"src": src, "h": self.p.value_hash(value)}

    def test_empty_is_written(self):
        self.assertTrue(self.p.should_write(None, "", "mouser", self.PRIO))
        self.assertTrue(self.p.should_write(None, "-", "", self.PRIO))

    def test_higher_priority_replaces_unedited_lower(self):
        e = self.entry("mouser", "12 Weeks")
        self.assertTrue(self.p.should_write(e, "12 Weeks", "digikey", self.PRIO))
        # whitespace-normalised hash
        self.assertTrue(self.p.should_write(e, " 12  Weeks ", "digikey", self.PRIO))

    def test_manual_edit_is_kept(self):
        e = self.entry("mouser", "12 Weeks")
        self.assertFalse(self.p.should_write(e, "10 Weeks", "digikey", self.PRIO))

    def test_lower_or_same_priority_never_overwrites(self):
        e = self.entry("digikey", "14 Weeks")
        self.assertFalse(self.p.should_write(e, "14 Weeks", "mouser", self.PRIO))
        self.assertFalse(self.p.should_write(e, "14 Weeks", "digikey", self.PRIO))

    def test_no_provenance_is_kept(self):
        self.assertFalse(self.p.should_write(None, "legacy", "digikey", self.PRIO))
        e = self.entry("mouser", "x")
        self.assertFalse(self.p.should_write(e, "x", "", self.PRIO))

    def test_fallback_description_replaced_by_any_source(self):
        e = self.entry("fallback", "Murata GRM1")
        self.assertTrue(self.p.should_write(e, "Murata GRM1", "mouser", self.PRIO))

    def test_record_without_source_protects_field(self):
        prov = self.p.empty()
        self.p.record(prov, "description", "mouser", "a")
        self.p.record(prov, "description", "", "b")
        self.assertIsNone(self.p.entry(prov, "description"))


if __name__ == "__main__":
    unittest.main()
