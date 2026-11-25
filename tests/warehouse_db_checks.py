"""Isolated Django/SQLite DB tests. Run from repo root: python -m tests.warehouse_db_checks

The fixture models emulate the InvenTree 1.7.3 interface exercised here; a real
deployment is still needed for InvenTree roles, signals, PostgreSQL and hardware.
Requires Django and django-money in addition to the package dependencies.
"""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from django.conf import settings

settings.configure(
    INSTALLED_APPS=["djmoney.contrib.exchange"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    SECRET_KEY="tests only",
    USE_TZ=True,
)

import django

django.setup()

from django.db import connection, models
from django.test import RequestFactory
from djmoney.contrib.exchange.models import ExchangeBackend, Rate
from djmoney.models.fields import MoneyField
from djmoney.money import Money

from inventree_smart_parts.api_clients.base import PartData, PriceBreak
from inventree_smart_parts.maintenance import (
    convert_supplier_prices,
    repair_barcode_hashes,
)
from inventree_smart_parts.services import (
    part_creator,
    pricing,
    stock_manager,
    token_manager,
)
from inventree_smart_parts.services import provenance
from inventree_smart_parts.views import purescan_stock_summary


def native_hash(barcode):
    return hashlib.md5(barcode.encode()).hexdigest()


class BarcodeModel(models.Model):
    class Meta:
        abstract = True

    barcode_data = models.CharField(max_length=500, blank=True)
    barcode_hash = models.CharField(max_length=128, blank=True)

    def assign_barcode(self, *, barcode_data, save=True):
        self.barcode_data = barcode_data
        self.barcode_hash = native_hash(barcode_data)
        if save:
            self.save()


class PartCategory(models.Model):
    class Meta:
        app_label = "part"

    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, related_name="children", on_delete=models.CASCADE
    )
    structural = models.BooleanField(default=False)

    @property
    def pathstring(self):
        names, cat = [], self
        while cat is not None:
            names.insert(0, cat.name)
            cat = cat.parent
        return " > ".join(names)


class Part(BarcodeModel):
    class Meta:
        app_label = "part"

    name = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=250, blank=True)
    IPN = models.CharField(max_length=100, blank=True)
    link = models.CharField(max_length=500, blank=True)
    category = models.ForeignKey(
        PartCategory, null=True, blank=True, on_delete=models.SET_NULL
    )
    default_supplier = models.ForeignKey(
        "company.SupplierPart",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    metadata = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    virtual = models.BooleanField(default=False)
    component = models.BooleanField(default=True)
    purchaseable = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class StockLocation(BarcodeModel):
    class Meta:
        app_label = "stock"

    parent_id = models.IntegerField(null=True)


class StockItem(BarcodeModel):
    class Meta:
        app_label = "stock"

    IN_STOCK_FILTER = models.Q(quantity__gt=0, in_stock=True)
    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    location = models.ForeignKey(
        StockLocation, null=True, blank=True, on_delete=models.CASCADE
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=5)
    in_stock = models.BooleanField(default=True)
    delete_on_deplete = models.BooleanField(default=True)

    def tracking_event(self):
        pass

    def save(self, *args, user=None, notes=None, **kwargs):
        super().save(*args, **kwargs)


class Company(models.Model):
    class Meta:
        app_label = "company"

    name = models.CharField(max_length=100)
    is_supplier = models.BooleanField(default=False)
    is_manufacturer = models.BooleanField(default=False)


Supplier = Company


class ManufacturerPart(models.Model):
    class Meta:
        app_label = "company"

    part = models.ForeignKey(
        Part, related_name="manufacturer_parts", on_delete=models.CASCADE
    )
    manufacturer = models.ForeignKey(Company, null=True, on_delete=models.CASCADE)
    MPN = models.CharField(max_length=100)
    link = models.CharField(max_length=500, blank=True)


class SupplierPart(models.Model):
    class Meta:
        app_label = "company"

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    part = models.ForeignKey(Part, null=True, on_delete=models.CASCADE)
    SKU = models.CharField(max_length=100)
    link = models.CharField(max_length=100, blank=True)
    manufacturer_part = models.ForeignKey(
        ManufacturerPart, null=True, on_delete=models.CASCADE
    )


class SupplierPriceBreak(models.Model):
    class Meta:
        app_label = "company"

    part = models.ForeignKey(SupplierPart, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=15, decimal_places=5)
    price = MoneyField(
        max_digits=19, decimal_places=6, null=True, default_currency="USD"
    )
    updated = models.DateTimeField(auto_now=True)


def module(name, **attrs):
    result = ModuleType(name)
    vars(result).update(attrs)
    return result


class _FixtureDatabase(unittest.TestCase):
    """Creates the fixture schema once; stubs InvenTree modules per test."""

    _schema_ready = False

    @classmethod
    def setUpClass(cls):
        if _FixtureDatabase._schema_ready:
            return
        _FixtureDatabase._schema_ready = True
        with connection.schema_editor() as schema:
            for model in (
                PartCategory,
                Part,
                StockLocation,
                StockItem,
                Company,
                ManufacturerPart,
                SupplierPart,
                SupplierPriceBreak,
                ExchangeBackend,
                Rate,
            ):
                schema.create_model(model)

    def setUp(self):
        self.settings = {
            "MOUSER_ENABLED": True,
            "DIGIKEY_ENABLED": True,
            "API_PRIORITY": "digikey,mouser",
            "AUTO_CREATE_CATEGORIES": True,
            "AUTO_CREATE_MANUFACTURERS": True,
            "FUZZY_THRESHOLD": 45,
            "DEFAULT_CATEGORY": "Uncategorized",
            "CATEGORY_SYNONYMS": "{}",
            "LEARNED_CATEGORY_MAPPINGS": "{}",
        }
        self.plugin = SimpleNamespace(
            get_setting=self.settings.get, set_setting=self.settings.__setitem__
        )
        stubs = patch.dict(
            sys.modules,
            {
                "plugin": module("plugin"),
                "plugin.registry": module(
                    "plugin.registry",
                    registry=SimpleNamespace(get_plugin=lambda slug: self.plugin),
                ),
                "InvenTree.helpers": module(
                    "InvenTree.helpers", hash_barcode=native_hash
                ),
                "company.models": module(
                    "company.models",
                    Company=Company,
                    SupplierPriceBreak=SupplierPriceBreak,
                    ManufacturerPart=ManufacturerPart,
                    SupplierPart=SupplierPart,
                ),
                "plugin.base.barcodes.helper": module(
                    "plugin.base.barcodes.helper",
                    get_supported_barcode_models_map=lambda: {
                        "stockitem": StockItem,
                        "stocklocation": StockLocation,
                        "part": Part,
                    },
                ),
                "part.models": module(
                    "part.models", Part=Part, PartCategory=PartCategory
                ),
                "stock.models": module(
                    "stock.models", StockItem=StockItem, StockLocation=StockLocation
                ),
            },
        )
        stubs.start()
        self.addCleanup(stubs.stop)
        self.part = Part.objects.create()
        self.location = StockLocation.objects.create()


class WarehouseDatabaseChecks(_FixtureDatabase):
    def test_summary_exact_bin_labels_and_permissions(self):
        child = StockLocation.objects.create(parent_id=self.location.pk)
        other = Part.objects.create()
        for label, qty, loc, part, in_stock in (
            ("STOCK_ZZZ9", "0.5", self.location, self.part, True),
            ("STOCK_1000", "1", self.location, self.part, True),
            ("STOCK_2000", "1", self.location, self.part, True),
            ("STOCK_3000", "2", self.location, self.part, True),
            ("plain text", "0.125", self.location, self.part, True),
            ("STOCK_KID1", "7", child, self.part, True),
            ("STOCK_DONT", "5", self.location, self.part, False),
            ("STOCK_OTHR", "8", self.location, other, True),
        ):
            StockItem.objects.create(
                part=part,
                location=loc,
                quantity=qty,
                barcode_data=label,
                in_stock=in_stock,
            )

        summary = stock_manager.get_part_stock_summary(self.part.pk, self.location.pk)
        self.assertEqual(
            summary,
            {
                "total_stock": "11.625",
                "location_stock": "4.625",
                "stock_labels": ["STOCK_3000", "STOCK_1000", "STOCK_2000"],
                "stock_label_items": [
                    {"label": "STOCK_3000", "quantity": "2"},
                    {"label": "STOCK_1000", "quantity": "1"},
                    {"label": "STOCK_2000", "quantity": "1"},
                ],
                "more_labels": True,
            },
        )
        self.assertIsNone(
            stock_manager.get_part_stock_summary(self.part.pk, 9999)["location_stock"]
        )
        empty = StockLocation.objects.create()
        self.assertEqual(
            stock_manager.get_part_stock_summary(self.part.pk, empty.pk)[
                "location_stock"
            ],
            "0",
        )
        request = RequestFactory().get(
            "/", {"part_id": self.part.pk, "location_id": self.location.pk}
        )
        request.user = SimpleNamespace(
            is_authenticated=True,
            username="reader",
            has_perm=lambda p: p == "part.view_part",
        )
        self.assertEqual(purescan_stock_summary(request).status_code, 403)

    def test_stock_binding_rolls_back_and_native_hash_resolves(self):
        result = stock_manager.create_stock_item(
            self.part.pk, "2.5", self.location.pk, token=" stock_a123 "
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["token"], "STOCK_A123")
        bound = StockItem.objects.get(barcode_hash=native_hash("STOCK_A123"))
        self.assertEqual(bound.pk, result["stock_id"])
        self.assertEqual(
            token_manager.resolve_token("STOCK_A123"), ("stockitem", bound)
        )
        self.assertEqual(
            stock_manager.create_stock_item(self.part.pk, 1, token="STOCK_A123")[
                "status"
            ],
            409,
        )
        self.assertEqual(
            stock_manager.create_stock_item(self.part.pk, 1, token="LOC_1234")[
                "status"
            ],
            400,
        )
        with patch.object(
            StockItem, "assign_barcode", side_effect=ValueError("binding failed")
        ):
            self.assertFalse(
                stock_manager.create_stock_item(self.part.pk, 1, token="STOCK_B123")[
                    "success"
                ]
            )
        self.assertEqual(StockItem.objects.filter(part=self.part).count(), 1)

    def test_legacy_repair_preserves_labels_and_refuses_ambiguous_bindings(self):
        good = StockItem.objects.create(
            part=self.part,
            quantity=1,
            barcode_data="STOCK_LEG1",
            barcode_hash=token_manager.legacy_hash_token("STOCK_LEG1"),
        )
        conflict = StockItem.objects.create(
            part=self.part,
            quantity=1,
            barcode_data="STOCK_LEG2",
            barcode_hash=token_manager.legacy_hash_token("STOCK_LEG2"),
        )
        StockItem.objects.create(
            part=self.part,
            quantity=1,
            barcode_data="STOCK_LEG2",
            barcode_hash=native_hash("STOCK_LEG2"),
        )
        self.assertEqual(
            [r["status"] for r in repair_barcode_hashes.run()],
            ["would repair", "conflict"],
        )
        good.refresh_from_db()
        self.assertEqual(
            good.barcode_hash, token_manager.legacy_hash_token("STOCK_LEG1")
        )
        self.assertEqual(
            [r["status"] for r in repair_barcode_hashes.run(dry_run=False)],
            ["repaired", "conflict"],
        )
        good.refresh_from_db()
        conflict.refresh_from_db()
        self.assertEqual(good.barcode_hash, native_hash("STOCK_LEG1"))
        self.assertEqual(good.barcode_data, "STOCK_LEG1")
        self.assertEqual(
            conflict.barcode_hash, token_manager.legacy_hash_token("STOCK_LEG2")
        )
        conflict.delete()

    def test_supplier_refresh_leaves_old_tiers_when_rate_missing(self):
        supplier = Supplier.objects.create(name="Test supplier")
        sku = SupplierPart.objects.create(
            part=self.part, supplier=supplier, SKU="TEST-100K"
        )
        old = SupplierPriceBreak.objects.create(
            part=sku, quantity=1, price=Money("0.001234", "EUR")
        )
        unmentioned = SupplierPriceBreak.objects.create(
            part=sku, quantity=100, price=Money("0.001100", "EUR")
        )
        entry = {
            "name": supplier.name,
            "sku": sku.SKU,
            "price_breaks": [
                PriceBreak(1, "0.002000", "USD"),
                PriceBreak(10, "0.001600", "EUR"),
            ],
        }
        warnings = []
        with patch.object(
            part_creator, "_get_or_create_company", return_value=supplier
        ):
            with patch.object(
                pricing,
                "get_usd_to_eur_rate",
                side_effect=pricing.PricingError("Missing rate"),
            ):
                part_creator._create_supplier_part(
                    self.part,
                    PartData(mpn="100K"),
                    entry,
                    True,
                    price_snapshot=pricing.PriceSnapshot(),
                    pricing_warnings=warnings,
                )
            old.refresh_from_db()
            self.assertEqual(old.price, Money("0.001234", "EUR"))
            self.assertEqual(SupplierPriceBreak.objects.filter(part=sku).count(), 2)
            self.assertIn("Missing rate", warnings[0])
            snapshot = pricing.PriceSnapshot()
            snapshot.rate = pricing.ExchangeRate(Decimal("0.9"), None)
            part_creator._create_supplier_part(
                self.part,
                PartData(mpn="100K"),
                entry,
                True,
                price_snapshot=snapshot,
                pricing_warnings=[],
            )
        old.refresh_from_db()
        unmentioned.refresh_from_db()
        self.assertEqual(old.price, Money("0.001800", "EUR"))
        self.assertEqual(unmentioned.price, Money("0.001100", "EUR"))
        self.assertEqual(
            SupplierPriceBreak.objects.get(part=sku, quantity=10).price,
            Money("0.001600", "EUR"),
        )

    def test_gap_fill_supplier_part_is_created_once_with_normalised_tiers(self):
        # A Mouser reel of a part created from DigiKey: the Mouser supplier part
        # is added (reusing the part's ManufacturerPart), a rescan changes nothing.
        supplier = Supplier.objects.create(name="Mouser")
        mfr = ManufacturerPart.objects.create(part=self.part, MPN="GRM32EC72A106ME05K")
        entry = {
            "name": supplier.name,
            "sku": "81-GRM32EC72A106ME5K",
            "url": "https://mouser.example/p",
            "price_breaks": [
                PriceBreak(1, "0.010000", "USD"),
                PriceBreak(10, "0.008000", "EUR"),
            ],
        }
        snapshot = pricing.PriceSnapshot()
        snapshot.rate = pricing.ExchangeRate(Decimal("0.9"), None)
        with patch.object(
            part_creator, "_get_or_create_company", return_value=supplier
        ):
            stats = {}
            created_sp, created = part_creator._create_supplier_part(
                self.part,
                PartData(mpn="grm32ec72a106me05k"),
                entry,
                True,
                price_snapshot=snapshot,
                pricing_warnings=[],
                stats=stats,
            )
            self.assertTrue(created)
            self.assertEqual(created_sp.manufacturer_part, mfr)
            self.assertEqual(created_sp.link, "https://mouser.example/p")
            self.assertEqual(stats["price_breaks_changed"], 2)
            self.assertEqual(
                SupplierPriceBreak.objects.get(part=created_sp, quantity=1).price,
                Money("0.009000", "EUR"),
            )
            entry["sku"] = entry["sku"].lower()  # same supplier part, any case
            stats = {}
            again, created = part_creator._create_supplier_part(
                self.part,
                PartData(mpn="GRM32EC72A106ME05K"),
                entry,
                True,
                price_snapshot=snapshot,
                pricing_warnings=[],
                stats=stats,
            )
        self.assertFalse(created)
        self.assertEqual(again.pk, created_sp.pk)
        self.assertEqual(stats["price_breaks_changed"], 0)
        self.assertEqual(SupplierPart.objects.filter(part=self.part).count(), 1)

    def test_distributor_search_can_be_limited_to_the_label_source(self):
        from inventree_smart_parts import api_clients, views

        queried = []

        def client(source):
            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                def search_by_mpn(self, mpn):
                    queried.append(source)
                    return PartData(mpn=mpn, source=source)

            return Client

        enabled = {"MOUSER_ENABLED": True, "DIGIKEY_ENABLED": True}
        plugin = SimpleNamespace(get_setting=enabled.get)
        with patch.object(api_clients, "MouserClient", client("mouser")), patch.object(
            api_clients, "DigiKeyClient", client("digikey")
        ):
            found, by_source = views._run_all_api_searches(
                plugin, "GRM32", only={"Mouser"}
            )
            self.assertEqual([pd.source for pd in found], ["mouser"])
            self.assertEqual(list(by_source), ["mouser"])
            views._run_all_api_searches(plugin, "GRM32", raw_prices=True)
        self.assertEqual(queried, ["mouser", "mouser", "digikey"])

    @patch(
        "djmoney.contrib.exchange.models.get_default_backend_name",
        return_value="test-rates",
    )
    def test_backfill_uses_native_rates_skips_edited_tier_and_can_rollback(self, _):
        backend = ExchangeBackend.objects.create(name="test-rates", base_currency="GBP")
        Rate.objects.create(backend=backend, currency="USD", value="1.25")
        Rate.objects.create(backend=backend, currency="EUR", value="1.10")
        supplier = Supplier.objects.create(name="Test supplier")
        sku = SupplierPart.objects.create(supplier=supplier, SKU="TEST-10K")

        def tier(quantity, amount, currency):
            return SupplierPriceBreak.objects.create(
                part=sku, quantity=quantity, price=Money(amount, currency)
            )

        usd = tier(1, "0.000123", "USD")
        edited = tier(10, "0.000121", "USD")
        eur = tier(100, "0.000100", "EUR")
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "prices.json"
            self.assertEqual(convert_supplier_prices.preview(plan)["candidates"], 2)
            usd.refresh_from_db()
            self.assertEqual(usd.price_currency, "USD")
            edited.price = Money("0.000100", "USD")
            edited.save()
            Rate.objects.filter(backend=backend, currency="EUR").update(value="1.00")
            self.assertEqual(
                convert_supplier_prices.apply(plan),
                {"changed": [usd.pk], "skipped": [edited.pk]},
            )
            usd.refresh_from_db()
            self.assertEqual(usd.price.amount, Decimal("0.000108"))
            self.assertEqual(usd.price_currency, "EUR")
            self.assertEqual(convert_supplier_prices.apply(plan)["changed"], [])
            self.assertEqual(
                convert_supplier_prices.rollback(plan)["changed"], [usd.pk]
            )
        usd.refresh_from_db()
        eur.refresh_from_db()
        self.assertEqual(usd.price, Money("0.000123", "USD"))
        self.assertEqual(eur.price, Money("0.000100", "EUR"))
        Rate.objects.filter(backend=backend).delete()
        with self.assertRaises(pricing.PricingError):
            pricing.get_usd_to_eur_rate()


# ── v1.6.0: dual DigiKey/Mouser supplier parts on PureScan scans ──────────

GS, RS, EOT = "\x1d", "\x1e", "\x04"
DK_CATEGORY = "Capacitors > Ceramic Capacitors"


def mouser_label(mpn, qty=50):
    return (
        f"[)>{RS}06{GS}K606-45799789{GS}14K002{GS}1P{mpn}{GS}Q{qty}"
        f"{GS}11K087041143{GS}4LJP{GS}1VMurata{RS}{EOT}"
    )


def digikey_label(mpn, dkpn):
    return (
        f"[)>{RS}06{GS}P{dkpn}{GS}1P{mpn}{GS}30P{dkpn}{GS}K{GS}1K12345678"
        f"{GS}10K87654321{GS}9D2345{GS}1TLOT42{GS}4LMY{GS}Q25{GS}11ZPICK{RS}{EOT}"
    )


def digikey_hit(mpn, category=DK_CATEGORY):
    return PartData(
        mpn=mpn,
        name=mpn,
        manufacturer="Murata Electronics",
        description="CAP CER 10UF 100V X7S 1210",
        category=category,
        supplier_name="DigiKey",
        supplier_sku=f"490-{mpn}-1-ND",
        source="digikey",
    )


def mouser_hit(mpn, category="Zzyzx Gadgets"):
    return PartData(
        mpn=mpn,
        name=mpn,
        manufacturer="Murata",
        description="Multilayer Ceramic Capacitors MLCC - SMD/SMT 10uF",
        category=category,
        supplier_name="Mouser",
        supplier_sku=f"81-{mpn}",
        source="mouser",
    )


class DualSupplierScanChecks(_FixtureDatabase):
    """PureScan CREATEPART / FASTADD end to end on the fixture database.

    Only the distributor clients, media downloads and parameter writers are
    replaced; part, supplier, category and provenance code is the real one.
    """

    _counter = 0

    def setUp(self):
        super().setUp()
        from inventree_smart_parts import views
        from inventree_smart_parts.services import category_mapper, part_creator

        category_mapper.clear_cache()
        self.addCleanup(category_mapper.clear_cache)
        PartCategory.objects.get_or_create(name="Uncategorized", parent=None)
        for target, name, value in (
            (views, "log_activity", lambda *a, **k: None),
            (part_creator, "_create_core_parameters", lambda *a, **k: None),
            (part_creator, "_create_parameters", lambda *a, **k: []),
            (part_creator, "_import_image", lambda *a, **k: None),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.views = views
        self.queried = []
        self.answers = {}
        self.user = SimpleNamespace(
            is_authenticated=True, username="operator", has_perm=lambda perm: True
        )
        # Rows persist across tests (no per-test rollback, and the fixture
        # apps are not installed so cascades cannot run): one MPN per test.
        DualSupplierScanChecks._counter += 1
        self.mpn = f"GRM32EC{DualSupplierScanChecks._counter:03d}ME05K"

    # ── helpers ──
    def distributors(self, **answers):
        """Fake clients: source -> PartData | None | Exception."""
        from inventree_smart_parts import api_clients

        self.answers = answers
        test = self

        def client(source):
            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                def search_by_mpn(self, mpn):
                    test.queried.append(source)
                    answer = test.answers.get(source)
                    if isinstance(answer, Exception):
                        raise answer
                    return copy.deepcopy(answer)

            return Client

        return patch.multiple(
            api_clients, MouserClient=client("mouser"), DigiKeyClient=client("digikey")
        )

    def post(self, view, body):
        request = RequestFactory().post(
            "/", data=json.dumps(body), content_type="application/json"
        )
        request.user = self.user
        response = view(request)
        return response.status_code, json.loads(response.content)

    def createpart(self, barcode, **extra):
        return self.post(self.views.purescan_createpart, {"barcode": barcode, **extra})

    def lookup(self, barcode):
        return self.post(self.views.api_barcode_lookup, {"barcode": barcode})[1]

    def suppliers(self, part):
        return sorted(
            SupplierPart.objects.filter(part=part).values_list(
                "supplier__name", flat=True
            )
        )

    def scanned_part(self):
        return Part.objects.get(name=self.mpn)

    # ── scenarios ──
    def test_mouser_label_creates_both_suppliers_with_digikey_category(self):
        # Even with a Mouser-first API_PRIORITY the category is DigiKey's
        self.settings["API_PRIORITY"] = "mouser,digikey"
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            status, body = self.createpart(mouser_label(self.mpn))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])
        self.assertEqual(body["default_supplier"], "Mouser")
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])
        self.assertEqual(part.category.pathstring, DK_CATEGORY)
        self.assertEqual(part.default_supplier.supplier.name, "Mouser")
        # Other fields follow API_PRIORITY (Mouser first here)
        self.assertTrue(part.description.startswith("Multilayer"))
        self.assertEqual(provenance.load(part)["fields"]["category"]["src"], "digikey")

    def test_digikey_label_also_fetches_mouser_and_keeps_digikey_default(self):
        dkpn = f"490-{self.mpn}-1-ND"
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            status, body = self.createpart(digikey_label(self.mpn, dkpn))
        self.assertEqual(status, 200, body)
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])
        self.assertEqual(part.default_supplier.supplier.name, "DigiKey")
        self.assertEqual(part.category.pathstring, DK_CATEGORY)

    def test_digikey_down_then_fastadd_adds_digikey_and_upgrades_category(self):
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=RuntimeError("DigiKey offline")
        ):
            status, body = self.createpart(mouser_label(self.mpn))
        self.assertEqual(status, 200, body)
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["Mouser"])
        # Mouser leaf fuzzy-matched nothing -> DEFAULT_CATEGORY
        self.assertEqual(part.category.name, "Uncategorized")
        self.assertEqual(part.default_supplier.supplier.name, "Mouser")
        # An outage is not a "not carried" answer: nothing remembered
        self.assertEqual(provenance.recent_misses(provenance.load(part)), set())

        found = self.lookup(mouser_label(self.mpn))
        self.assertEqual(found["missing_suppliers"], ["digikey"])
        self.assertTrue(found["enrich_needed"])

        self.queried.clear()
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            status, body = self.createpart(mouser_label(self.mpn), enrich_scope="label")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["already_exists"])
        self.assertEqual(self.queried, ["digikey"])  # only the missing one
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])
        self.assertEqual(part.category.pathstring, DK_CATEGORY)
        self.assertIn("Category", body["enriched"])
        self.assertEqual(part.default_supplier.supplier.name, "Mouser")

        self.queried.clear()
        found = self.lookup(mouser_label(self.mpn))
        self.assertEqual(found["missing_suppliers"], [])
        self.assertFalse(found["enrich_needed"])

    def test_not_carried_is_remembered_for_30_days(self):
        with self.distributors(mouser=mouser_hit(self.mpn), digikey=None):
            self.createpart(mouser_label(self.mpn))
        part = self.scanned_part()
        self.assertEqual(provenance.recent_misses(provenance.load(part)), {"digikey"})
        found = self.lookup(mouser_label(self.mpn))
        self.assertEqual(found["missing_suppliers"], [])
        self.assertFalse(found["enrich_needed"])

        # FASTADD makes no distributor call while the miss is remembered
        self.queried.clear()
        with self.distributors(mouser=mouser_hit(self.mpn), digikey=None):
            status, body = self.createpart(mouser_label(self.mpn), enrich_scope="label")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.queried, [])

        # After 30 days DigiKey is asked again
        meta = dict(part.metadata)
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        meta["smartparts"]["lookups"]["digikey"]["miss"] = old
        Part.objects.filter(pk=part.pk).update(metadata=meta)
        self.assertEqual(
            self.lookup(mouser_label(self.mpn))["missing_suppliers"], ["digikey"]
        )

    def test_scanned_distributor_down_sets_default_on_a_later_scan(self):
        with self.distributors(
            mouser=RuntimeError("Mouser offline"), digikey=digikey_hit(self.mpn)
        ):
            status, body = self.createpart(mouser_label(self.mpn))
        self.assertEqual(status, 200, body)
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey"])
        self.assertIsNone(part.default_supplier)
        self.assertEqual(body["default_supplier"], "")

        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            self.createpart(mouser_label(self.mpn), enrich_scope="label")
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])
        self.assertEqual(part.default_supplier.supplier.name, "Mouser")

    def test_manual_default_supplier_and_category_are_kept(self):
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=RuntimeError("down")
        ):
            self.createpart(mouser_label(self.mpn))
        part = self.scanned_part()
        manual = PartCategory.objects.create(name=f"Hand picked {self.mpn}")
        Part.objects.filter(pk=part.pk).update(category=manual)
        dkpn = f"490-{self.mpn}-1-ND"
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            # DigiKey label: DigiKey gets added but Mouser stays the default
            self.createpart(digikey_label(self.mpn, dkpn), enrich_scope="label")
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])
        self.assertEqual(part.default_supplier.supplier.name, "Mouser")
        self.assertEqual(part.category_id, manual.pk)

    def test_existing_part_without_default_gets_it_without_api_calls(self):
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn)
        ):
            self.createpart(mouser_label(self.mpn))
        part = self.scanned_part()
        Part.objects.filter(pk=part.pk).update(default_supplier=None)
        found = self.lookup(digikey_label(self.mpn, f"490-{self.mpn}-1-ND"))
        self.assertEqual(found["missing_suppliers"], [])
        self.assertTrue(found["enrich_needed"])
        self.queried.clear()
        with self.distributors(mouser=None, digikey=None):
            status, body = self.createpart(
                digikey_label(self.mpn, f"490-{self.mpn}-1-ND"), enrich_scope="label"
            )
        self.assertEqual(self.queried, [])
        self.assertEqual(body["enriched"], ["Default supplier: DigiKey"])
        self.assertEqual(self.scanned_part().default_supplier.supplier.name, "DigiKey")

    def test_category_chain_falls_back_to_mouser_fuzzy_then_default(self):
        from inventree_smart_parts.services.data_merger import merge_part_data

        PartCategory.objects.get_or_create(name="Tantalum Capacitors", parent=None)
        self.settings["AUTO_CREATE_CATEGORIES"] = False

        def resolve(dk_category, mouser_category):
            merged = merge_part_data(
                [
                    digikey_hit(self.mpn, category=dk_category),
                    mouser_hit(self.mpn, category=mouser_category),
                ],
                ["digikey", "mouser"],
                search_term=self.mpn,
                strict=True,
            )
            cat_id, path, _score, _created = self.views._resolve_category(
                self.plugin, merged, default_threshold=45
            )
            return path, merged.raw_data["field_sources"]["category"]

        # 1. DigiKey's chain when it can be placed
        PartCategory.objects.get_or_create(name="Qwerty Modules", parent=None)
        self.assertEqual(
            resolve("Qwerty Modules", "Tantalum Capacitors - Solid SMD"),
            ("Qwerty Modules", "digikey"),
        )
        # 2. DigiKey's category cannot be placed -> Mouser leaf, fuzzy
        self.assertEqual(
            resolve("Plugh > Xyzzy Thingamajigs", "Tantalum Capacitors - Solid SMD"),
            ("Tantalum Capacitors", "mouser"),
        )
        # 3. Neither matches -> default category
        self.assertEqual(
            resolve("Plugh > Xyzzy Thingamajigs", "Zzyzx Gadgets"),
            ("Uncategorized", "mouser"),
        )

    def test_lookup_outcomes_ignore_errors(self):
        from inventree_smart_parts.services.data_merger import merge_part_data

        merged = merge_part_data(
            [mouser_hit(self.mpn)], None, search_term=self.mpn, strict=True
        )
        self.assertEqual(
            self.views._lookup_outcomes(
                {"mouser": {"mpn": self.mpn}, "digikey": None}, merged
            ),
            (["mouser"], ["digikey"]),
        )
        self.assertEqual(
            self.views._lookup_outcomes(
                {"mouser": {"mpn": self.mpn}, "digikey": {"error": "timeout"}}, merged
            ),
            (["mouser"], []),
        )
        # A hit with another MPN is "not carried"
        self.assertEqual(
            self.views._lookup_outcomes({"mouser": {"mpn": "OTHER"}}, None),
            ([], ["mouser"]),
        )

    def _without(self, *perms):
        return SimpleNamespace(
            is_authenticated=True,
            username="operator",
            is_staff=False,
            has_perm=lambda perm: perm not in perms,
        )

    def _new_chain(self):
        root = f"Plugh{self.mpn}"
        return root, f"{root} > Xyzzy Widgets"

    def test_createpart_needs_add_partcategory_to_create_categories(self):
        root, chain = self._new_chain()
        self.user = self._without("part.add_partcategory")
        dkpn = f"490-{self.mpn}-1-ND"
        with self.distributors(
            mouser=None, digikey=digikey_hit(self.mpn, category=chain)
        ):
            status, body = self.createpart(digikey_label(self.mpn, dkpn))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["success"])  # the part is still created
        self.assertFalse(body["category_created"])
        self.assertFalse(PartCategory.objects.filter(name=root).exists())
        self.assertEqual(self.scanned_part().category.name, "Uncategorized")

    def test_gap_fill_needs_add_partcategory_to_create_categories(self):
        root, chain = self._new_chain()
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=RuntimeError("DigiKey offline")
        ):
            status, body = self.createpart(mouser_label(self.mpn))
        self.assertEqual(status, 200, body)

        self.user = self._without("part.add_partcategory")
        with self.distributors(
            mouser=mouser_hit(self.mpn), digikey=digikey_hit(self.mpn, category=chain)
        ):
            status, body = self.createpart(mouser_label(self.mpn), enrich_scope="label")
        self.assertEqual(status, 200, body)
        part = self.scanned_part()
        self.assertEqual(self.suppliers(part), ["DigiKey", "Mouser"])  # still filled
        self.assertFalse(PartCategory.objects.filter(name=root).exists())
        self.assertEqual(part.category.name, "Uncategorized")

    def test_dashboard_search_needs_add_partcategory_to_create_categories(self):
        root, chain = self._new_chain()
        request = RequestFactory().post(
            "/", data=json.dumps({"mpn": self.mpn}), content_type="application/json"
        )
        request.user = self._without("part.add_partcategory")
        with self.distributors(
            mouser=None, digikey=digikey_hit(self.mpn, category=chain)
        ):
            response = self.views.api_search(request)
        self.assertEqual(response.status_code, 200, response.content)
        match = json.loads(response.content)["category_match"]
        self.assertEqual(match["path"], "Uncategorized")
        self.assertFalse(PartCategory.objects.filter(name=root).exists())

    def test_stock_summary_reports_mpn_and_description(self):
        part = Part.objects.create(name="R-10K", description="RES 10K 1% 0603")
        ManufacturerPart.objects.create(part=part, MPN="RC0603FR-0710KL")
        StockItem.objects.create(
            part=part, location=self.location, quantity=5, barcode_data="STOCK_SUM1"
        )
        request = RequestFactory().get(
            "/", {"part_id": part.pk, "location_id": self.location.pk}
        )
        request.user = self.user
        data = json.loads(purescan_stock_summary(request).content)
        self.assertEqual(data["mpn"], "RC0603FR-0710KL")
        self.assertEqual(data["description"], "RES 10K 1% 0603")
        self.assertEqual(data["location_stock"], "5")
        self.assertEqual(data["stock_labels"], ["STOCK_SUM1"])


def _user(*, authenticated=True, staff=False, missing=()):
    return SimpleNamespace(
        is_authenticated=authenticated,
        username="someone" if authenticated else "",
        is_staff=staff,
        has_perm=lambda perm: authenticated and perm not in missing,
    )


class PermissionMatrixChecks(_FixtureDatabase):
    """Every SmartParts endpoint refuses anonymous users and users without
    its permission; users holding it get past the gate."""

    STAFF = "staff"
    # (view name, HTTP method, URL args, required permission or STAFF)
    MATRIX = [
        ("dashboard", "get", (), "part.view_part"),
        ("logs_view", "get", (), "part.view_part"),
        ("api_logs", "get", (), "part.view_part"),
        ("api_canonical_parameters", "get", (), "part.view_part"),
        ("api_search", "post", (), "part.view_part"),
        ("api_search_details", "post", (), "part.view_part"),
        ("batch_import", "get", (), "part.add_part"),
        ("batch_status", "get", ("abc123",), "part.add_part"),
        ("batch_report", "get", ("abc123",), "part.add_part"),
        ("api_stock_locations", "get", (), "stock.view_stocklocation"),
        ("api_label_templates", "get", (), "stock.view_stockitem"),
        ("plugin_settings", "get", (), STAFF),
        ("parameter_dashboard", "get", (), STAFF),
        ("test_connection", "get", ("mouser",), STAFF),
        ("api_logs_clear", "post", (), STAFF),
        ("api_synonyms", "get", (), STAFF),
        ("api_synonyms", "post", (), STAFF),
        ("api_learned", "get", (), STAFF),
        ("api_learned", "post", (), STAFF),
        ("api_parameter_mappings", "get", (), STAFF),
        ("api_parameter_mappings", "post", (), STAFF),
        ("api_unknown_parameters", "get", (), STAFF),
        ("api_unknown_parameters", "post", (), STAFF),
    ]

    def setUp(self):
        super().setUp()
        from django.http import HttpResponse
        from inventree_smart_parts import views

        self.views = views
        for name, value in (
            ("log_activity", lambda *a, **k: None),
            ("render", lambda *a, **k: HttpResponse("page")),
        ):
            patcher = patch.object(views, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, name, method, args, user, body=None):
        factory = RequestFactory()
        if method == "post":
            request = factory.post(
                "/", data=json.dumps(body or {}), content_type="application/json"
            )
        else:
            request = factory.get("/")
        request.user = user
        return getattr(self.views, name)(request, *args)

    def test_every_endpoint_refuses_anonymous_and_unprivileged_users(self):
        for name, method, args, required in self.MATRIX:
            with self.subTest(view=name, method=method):
                anonymous = self.call(name, method, args, _user(authenticated=False))
                self.assertEqual(anonymous.status_code, 401)
                if required == self.STAFF:
                    # every role permission, but not staff
                    denied = _user()
                else:
                    denied = _user(staff=True, missing=(required,))
                self.assertEqual(self.call(name, method, args, denied).status_code, 403)

    def test_users_with_the_permission_pass_the_gate(self):
        allowed = _user(staff=True)
        for name, args in (
            ("dashboard", ()),
            ("logs_view", ()),
            ("batch_import", ()),
            ("plugin_settings", ()),
            ("parameter_dashboard", ()),
        ):
            with self.subTest(view=name):
                self.assertEqual(self.call(name, "get", args, allowed).status_code, 200)
        for name in ("api_synonyms", "api_learned"):
            with self.subTest(view=name):
                response = self.call(name, "get", (), allowed)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.content), {"value": "{}"})
        # batch job lookups reach the job store (404 = unknown job, not denied)
        self.assertEqual(
            self.call("batch_status", "get", ("abc123",), allowed).status_code, 404
        )

    def lookup(self, barcode, user):
        return self.call("api_barcode_lookup", "post", (), user, {"barcode": barcode})

    def test_barcode_lookup_needs_the_view_permission_of_each_object(self):
        item = StockItem.objects.create(part=self.part, quantity=1)
        for barcode, perm in (
            (json.dumps({"stockitem": item.pk}), "stock.view_stockitem"),
            (
                json.dumps({"stocklocation": self.location.pk}),
                "stock.view_stocklocation",
            ),
            (json.dumps({"purchaseorder": 1}), "order.view_purchaseorder"),
            (json.dumps({"sales_order": 1}), "order.view_salesorder"),
            (json.dumps({"returnorder": 1}), "order.view_returnorder"),
            (json.dumps({"build": 1}), "build.view_build"),
            (json.dumps({"supplierpart": 1}), "company.view_supplierpart"),
            (json.dumps({"manufacturer_part": 1}), "company.view_manufacturerpart"),
        ):
            with self.subTest(perm=perm):
                response = self.lookup(barcode, _user(missing=(perm,)))
                self.assertEqual(response.status_code, 403)
                data = json.loads(response.content)
                self.assertTrue(data["is_native"])
                self.assertFalse(data["found"])
                self.assertIn(perm, data["error"])
        # Whoever may see stock items still gets the item
        response = self.lookup(json.dumps({"stockitem": item.pk}), _user())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["found"])
        self.assertEqual(data["barcode_data"], {"type": "stockitem", "pk": item.pk})

    def test_media_route_requires_login(self):
        import importlib

        host = {
            "plugin": module("plugin", InvenTreePlugin=type("InvenTreePlugin", (), {})),
            "plugin.mixins": module(
                "plugin.mixins",
                **{
                    name: type(name, (), {})
                    for name in (
                        "BarcodeMixin",
                        "SettingsMixin",
                        "UrlsMixin",
                        "UserInterfaceMixin",
                    )
                },
            ),
        }
        with patch.dict(sys.modules, host):
            sys.modules.pop("inventree_smart_parts.core", None)
            core = importlib.import_module("inventree_smart_parts.core")

        with tempfile.TemporaryDirectory() as root:
            Path(root, "secret.txt").write_text("attachment", encoding="utf-8")
            self.assertFalse(getattr(core._serve_media, "auth_exempt", False))
            request = RequestFactory().get("/media/secret.txt")
            request.user = _user(authenticated=False)
            response = core._serve_media(request, "secret.txt", document_root=root)
            self.assertEqual(response.status_code, 401)

            request.user = _user()
            response = core._serve_media(request, "secret.txt", document_root=root)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"attachment")
            response.close()  # release the file before the directory is removed


if __name__ == "__main__":
    unittest.main()
