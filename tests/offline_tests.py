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


# ── v1.6.0: DigiKey category for Mouser scans, lookups memory ────────────


def _dk(**over):
    base = dict(
        mpn="X1",
        name="X1",
        source="digikey",
        supplier_name="DigiKey",
        supplier_sku="X1-ND",
        description="DK desc",
        category="Capacitors > Ceramic Capacitors",
    )
    base.update(over)
    return PartData(**base)


def _mo(**over):
    base = dict(
        mpn="X1",
        name="X1",
        source="mouser",
        supplier_name="Mouser",
        supplier_sku="81-X1",
        description="Mouser desc",
        category="MLCC - SMD/SMT",
    )
    base.update(over)
    return PartData(**base)


class CategorySourceMergeTests(unittest.TestCase):
    """Mouser scans take DigiKey's category whatever API_PRIORITY says."""

    def test_hierarchical_category_beats_flat_under_mouser_first_priority(self):
        merged = merge_part_data(
            [_mo(), _dk()], ["mouser", "digikey"], search_term="X1", strict=True
        )
        fs = merged.raw_data["field_sources"]
        self.assertEqual(merged.category, "Capacitors > Ceramic Capacitors")
        self.assertEqual(fs["category"], "digikey")
        # Other fields still follow the configured priority
        self.assertEqual(merged.description, "Mouser desc")
        self.assertEqual(fs["description"], "mouser")
        # Mouser's leaf is kept as the fallback category
        self.assertEqual(
            merged.raw_data["fallback_category"],
            {"source": "mouser", "category": "MLCC - SMD/SMT"},
        )
        # Both supplier parts are created
        self.assertEqual(
            sorted(sd["source"] for sd in merged.raw_data["supplier_data"]),
            ["digikey", "mouser"],
        )

    def test_flat_category_used_when_digikey_has_none(self):
        merged = merge_part_data(
            [_mo(), _dk(category="")], None, search_term="X1", strict=True
        )
        self.assertEqual(merged.category, "MLCC - SMD/SMT")
        self.assertEqual(merged.raw_data["field_sources"]["category"], "mouser")
        self.assertIsNone(merged.raw_data["fallback_category"])

    def test_mouser_only(self):
        merged = merge_part_data([_mo()], None, search_term="X1", strict=True)
        self.assertEqual(merged.raw_data["field_sources"]["category"], "mouser")
        self.assertIsNone(merged.raw_data["fallback_category"])

    def test_digikey_with_other_mpn_is_dropped(self):
        merged = merge_part_data(
            [_mo(), _dk(mpn="X1-OTHER")], None, search_term="X1", strict=True
        )
        self.assertEqual(merged.category, "MLCC - SMD/SMT")
        self.assertEqual(merged.raw_data["merged_sources"], ["mouser"])


class DigiKeyCategoryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.client = DigiKeyClient("id", "secret")
        self.client._get_auth_headers = lambda: {}
        self.calls = []

    def _product(self, with_category):
        product = v4_product()
        if not with_category:
            product.pop("Category", None)
        return product

    def _respond(self, details):
        def request(method, url, **kwargs):
            self.calls.append(url)
            if "keyword" in url:
                return {"Products": [self._product(False)]}
            if isinstance(details, Exception):
                raise details
            return {"Product": details}

        self.client._request = request

    def test_category_taken_from_product_details(self):
        self._respond(self._product(True))
        pd = self.client.search_by_mpn("SN74HC00N")
        self.assertTrue(pd.category)
        self.assertEqual(
            pd.category,
            DigiKeyClient("a", "b")
            ._parse_part(self._product(True), "SN74HC00N")
            .category,
        )
        self.assertTrue(any("product-details" in u for u in self.calls))

    def test_details_failure_keeps_search_result(self):
        self._respond(RuntimeError("HTTP 500"))
        pd = self.client.search_by_mpn("SN74HC00N")
        self.assertEqual(pd.mpn, "SN74HC00N")
        self.assertEqual(pd.category, "")

    def test_no_details_call_when_keyword_hit_has_category(self):
        self.client._request = lambda method, url, **k: (
            self.calls.append(url) or {"Products": [self._product(True)]}
        )
        pd = self.client.search_by_mpn("SN74HC00N")
        self.assertTrue(pd.category)
        self.assertEqual(len(self.calls), 1)


class DigiKeyTokenCacheTests(unittest.TestCase):
    def setUp(self):
        from inventree_smart_parts.api_clients import digikey

        digikey.clear_token_cache()
        self.addCleanup(digikey.clear_token_cache)
        self.posts = 0

        test = self

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"access_token": f"tok{test.posts}", "expires_in": 3600}

        class Session:
            def post(self, *args, **kwargs):
                test.posts += 1
                return Response()

        self.session = Session()

    def client(self, client_id="id", secret="secret"):
        c = DigiKeyClient(client_id, secret)
        c._session = self.session
        return c

    def test_token_shared_across_instances(self):
        first = self.client()
        first._authenticate()
        second = self.client()
        second._authenticate()
        self.assertEqual(self.posts, 1)
        self.assertEqual(second._access_token, "tok1")

    def test_other_credentials_get_their_own_token(self):
        self.client()._authenticate()
        self.client("other-id")._authenticate()
        self.assertEqual(self.posts, 2)

    def test_expired_token_is_renewed(self):
        import time
        from inventree_smart_parts.api_clients import digikey

        self.client()._authenticate()
        for key, (token, _expiry) in list(digikey._TOKEN_CACHE.items()):
            digikey._TOKEN_CACHE[key] = (token, time.time() - 1)
        self.client()._authenticate()
        self.assertEqual(self.posts, 2)


class CategoryUpgradeTests(unittest.TestCase):
    def setUp(self):
        from inventree_smart_parts.services import provenance

        self.p = provenance

    def entry(self, src, value):
        return {"src": src, "h": self.p.value_hash(value)}

    def test_digikey_replaces_unedited_mouser_category(self):
        self.assertTrue(
            self.p.category_upgrade_allowed(self.entry("mouser", "12"), "12", "digikey")
        )

    def test_empty_category_is_always_written(self):
        self.assertTrue(self.p.category_upgrade_allowed(None, "", "digikey"))

    def test_edited_category_is_kept(self):
        self.assertFalse(
            self.p.category_upgrade_allowed(self.entry("mouser", "12"), "15", "digikey")
        )

    def test_flat_never_replaces_hierarchical(self):
        self.assertFalse(
            self.p.category_upgrade_allowed(self.entry("digikey", "12"), "12", "mouser")
        )

    def test_legacy_or_manual_category_is_kept(self):
        self.assertFalse(self.p.category_upgrade_allowed(None, "12", "digikey"))
        self.assertFalse(
            self.p.category_upgrade_allowed(self.entry("mouser", "12"), "12", "")
        )


class LookupMemoryTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone

        from inventree_smart_parts.services import provenance

        self.p = provenance
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

    def test_miss_remembered_for_30_days(self):
        from datetime import timedelta

        prov = self.p.empty()
        self.assertTrue(self.p.apply_lookups(prov, [], ["DigiKey"], self.now))
        self.assertEqual(self.p.recent_misses(prov, self.now), {"digikey"})
        later = self.now + timedelta(days=29, hours=23)
        self.assertEqual(self.p.recent_misses(prov, later), {"digikey"})
        expired = self.now + timedelta(days=30, minutes=1)
        self.assertEqual(self.p.recent_misses(prov, expired), set())

    def test_found_clears_a_miss(self):
        prov = self.p.empty()
        self.p.apply_lookups(prov, [], ["mouser"], self.now)
        self.assertTrue(self.p.apply_lookups(prov, ["mouser"], [], self.now))
        self.assertEqual(self.p.recent_misses(prov, self.now), set())
        self.assertFalse(self.p.apply_lookups(prov, ["mouser"], [], self.now))

    def test_corrupt_or_naive_dates(self):
        prov = {
            "fields": {},
            "lookups": {
                "digikey": {"miss": "not a date"},
                "mouser": {"miss": "2026-11-20T10:00:00"},
            },
        }
        self.assertEqual(self.p.recent_misses(prov, self.now), {"mouser"})
        self.assertEqual(self.p.recent_misses(None, self.now), set())


# ── v1.7.0: image downloads refuse internal addresses and SVG ─────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 600


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self.body = body

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and (
            "location" in self.headers
        )

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self.body

    def close(self):
        pass


class ImageDownloadGuardTests(unittest.TestCase):
    DNS = {
        "cdn.example.com": "93.184.216.34",
        "img.example.net": "151.101.1.1",
        "router.lan": "192.168.1.1",
        "localhost": "127.0.0.1",
        "metadata.example.com": "169.254.169.254",
        "mapped.example.com": "::ffff:10.0.0.7",
    }

    def setUp(self):
        from unittest.mock import patch

        from inventree_smart_parts.services import image_handler

        self.ih = image_handler
        self.fetched = []
        self.routes = {}

        def getaddrinfo(host, port, *args, **kwargs):
            ip = self.DNS.get(host, host)  # literal IPs resolve to themselves
            family = 10 if ":" in ip else 2
            return [(family, 1, 6, "", (ip, port))]

        test = self

        class Session:
            headers = {}

            def get(self, url, **kwargs):
                test.assertFalse(kwargs.get("allow_redirects", True))
                test.fetched.append(url)
                return test.routes[url]

        for target, name, value in (
            (image_handler.socket, "getaddrinfo", getaddrinfo),
            (image_handler, "_make_session", lambda minimal=False: Session()),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def png(self):
        return _FakeResponse(headers={"content-type": "image/png"}, body=PNG)

    def test_public_image_downloads_as_before(self):
        self.routes["https://cdn.example.com/a.png"] = self.png()
        data, ext, err = self.ih.download_image("https://cdn.example.com/a.png")
        self.assertEqual((data, ext, err), (PNG, ".png", ""))

    def test_redirect_between_public_hosts_is_followed(self):
        self.routes["https://cdn.example.com/a"] = _FakeResponse(
            302, {"location": "https://img.example.net/b.png"}
        )
        self.routes["https://img.example.net/b.png"] = self.png()
        data, ext, _err = self.ih.download_image("https://cdn.example.com/a")
        self.assertEqual((data, ext), (PNG, ".png"))
        self.assertEqual(
            self.fetched,
            ["https://cdn.example.com/a", "https://img.example.net/b.png"],
        )

    def test_internal_addresses_are_never_fetched(self):
        for url in (
            "http://router.lan/admin.png",
            "http://localhost:8000/media/x.png",
            "http://127.0.0.1/x.png",
            "http://10.1.2.3/x.png",
            "http://[::1]/x.png",
            "http://metadata.example.com/latest/meta-data",
            "http://mapped.example.com/x.png",
        ):
            with self.subTest(url=url):
                data, _ext, err = self.ih.download_image(url)
                self.assertIsNone(data)
                self.assertIn("non-public", err)
        self.assertEqual(self.fetched, [])

    def test_redirect_to_an_internal_address_is_refused(self):
        self.routes["https://cdn.example.com/a.png"] = _FakeResponse(
            302, {"location": "http://169.254.169.254/latest/meta-data"}
        )
        data, _ext, err = self.ih.download_image("https://cdn.example.com/a.png")
        self.assertIsNone(data)
        self.assertIn("non-public", err)
        self.assertEqual(self.fetched, ["https://cdn.example.com/a.png"])

    def test_redirect_loops_stop(self):
        self.routes["https://cdn.example.com/loop"] = _FakeResponse(
            302, {"location": "https://cdn.example.com/loop"}
        )
        data, _ext, err = self.ih.download_image("https://cdn.example.com/loop")
        self.assertIsNone(data)
        self.assertIn("redirects", err)

    def test_svg_is_refused(self):
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script>'
            + (b" " * 600)
            + b"</svg>"
        )
        self.routes["https://cdn.example.com/a.svg"] = _FakeResponse(
            headers={"content-type": "image/svg+xml"}, body=svg
        )
        data, _ext, _err = self.ih.download_image("https://cdn.example.com/a.svg")
        self.assertIsNone(data)
        self.assertNotIn("image/svg+xml", self.ih.ALLOWED_CONTENT_TYPES)


if __name__ == "__main__":
    unittest.main()
