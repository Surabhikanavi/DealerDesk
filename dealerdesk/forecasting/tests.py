import csv
import math
import statistics

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Inventory


class ReorderTest(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        self.api = APIClient()

    def test_forecast_file_shape(self):
        rows = list(csv.DictReader(open(settings.FORECAST_FILE, encoding="utf-8")))
        self.assertEqual(len(rows), 160)
        self.assertTrue(all(float(r["forecast_units"]) >= 0 for r in rows))

    def test_formula_matches_contract_and_uses_live_stock(self):
        sku = "FIL-1016"
        lead = {r["sku"]: int(r["lead_time_weeks"]) for r in csv.DictReader(open(settings.DATA_DIR / "lead_times.csv"))}[sku]
        fc = [float(r["forecast_units"]) for r in csv.DictReader(open(settings.FORECAST_FILE)) if r["sku"] == sku]
        hist = sorted((r["week_start"], int(r["units_sold"])) for r in csv.DictReader(open(settings.DATA_DIR / "sales_history.csv"))
                      if r["sku"] == sku)
        sigma = statistics.stdev([u for _, u in hist[-12:]])
        Inventory.objects.filter(product__sku=sku).update(quantity=3)

        entry = next(e for e in self.api.get("/api/reorder-suggestions/").json()["results"] if e["sku"] == sku)
        ltd = sum(fc[:lead])
        ss = math.ceil(1.65 * sigma * math.sqrt(lead))
        self.assertAlmostEqual(entry["lead_time_demand"], ltd, places=6)
        self.assertEqual(entry["safety_stock"], ss)
        self.assertAlmostEqual(entry["reorder_point"], ltd + ss, places=6)
        self.assertEqual(entry["current_stock"], 3)
        self.assertEqual(entry["reorder_qty"], max(0, math.ceil(ltd + ss - 3)))

    def test_forecast_endpoint(self):
        b = self.api.get("/api/forecast/FIL-1016/").json()
        self.assertEqual([w["week_start"] for w in b["forecast"]], ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"])
        self.assertEqual(self.api.get("/api/forecast/BDY-1001/").status_code, 404)  # in catalogue, not forecast
        self.assertEqual(len(self.api.get("/api/reorder-suggestions/").json()["results"]), 40)
