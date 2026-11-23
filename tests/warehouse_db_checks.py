"""Isolated Django/SQLite DB tests. Run from repo root: python -m tests.warehouse_db_checks

The fixture models emulate the InvenTree 1.7.3 interface exercised here; a real
deployment is still needed for InvenTree roles, signals, PostgreSQL and hardware.
Requires Django and django-money in addition to the package dependencies.
"""

import hashlib
import sys
import tempfile
import unittest
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


class Part(BarcodeModel):
    class Meta:
        app_label = "part"


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


class Supplier(models.Model):
    class Meta:
        app_label = "company"

    name = models.CharField(max_length=100)


class ManufacturerPart(models.Model):
    class Meta:
        app_label = "company"

    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    MPN = models.CharField(max_length=100)


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


class WarehouseDatabaseChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.schema_editor() as schema:
            for model in (
                Part,
                StockLocation,
                StockItem,
                Supplier,
                ManufacturerPart,
                SupplierPart,
                SupplierPriceBreak,
                ExchangeBackend,
                Rate,
            ):
                schema.create_model(model)

    def setUp(self):
        stubs = patch.dict(
            sys.modules,
            {
                "InvenTree.helpers": module(
                    "InvenTree.helpers", hash_barcode=native_hash
                ),
                "company.models": module(
                    "company.models",
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
                "part.models": module("part.models", Part=Part),
                "stock.models": module(
                    "stock.models", StockItem=StockItem, StockLocation=StockLocation
                ),
            },
        )
        stubs.start()
        self.addCleanup(stubs.stop)
        self.part = Part.objects.create()
        self.location = StockLocation.objects.create()

    def test_summary_exact_bin_labels_and_permissions(self):
        child = StockLocation.objects.create(parent_id=self.location.pk)
        other = Part.objects.create()
        for label, qty, loc, part, in_stock in (
            ("STOCK_ZZZ9", "1", self.location, self.part, True),
            ("STOCK_1000", "1", self.location, self.part, True),
            ("STOCK_2000", "1", self.location, self.part, True),
            ("STOCK_3000", "1", self.location, self.part, True),
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
                "total_stock": "11.125",
                "location_stock": "4.125",
                "stock_labels": ["STOCK_1000", "STOCK_2000", "STOCK_3000"],
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


if __name__ == "__main__":
    unittest.main()
