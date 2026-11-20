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
            "Products": [product], "SearchLocaleUsed": {"Currency": "EUR"},
        }
        pd = self.client.search_by_mpn("296-1234-1-nd")
        self.assertEqual([tier.currency for tier in pd.price_breaks], ["EUR"])


class MouserPricingTests(unittest.TestCase):
    def test_preserves_reported_currencies_and_recognizes_missing_usd_code(self):
        parsed = MouserClient(api_key="key")._parse_part({
            "ManufacturerPartNumber": "R-10K",
            "PriceBreaks": [
                {"Quantity": 1, "Price": "$0.0123"},
                {"Quantity": 10, "Price": "€0,009", "Currency": "EUR"},
                {"Quantity": 100, "Price": "CA$0.007"},
            ],
        }, "R-10K")
        self.assertEqual([(p.currency, p.price) for p in parsed.price_breaks],
                         [("USD", 0.0123), ("EUR", 0.009), ("CAD", 0.007)])


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


if __name__ == "__main__":
    unittest.main()
