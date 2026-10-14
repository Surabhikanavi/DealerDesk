from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Dealer, Product


class CatalogApiTest(TestCase):
    def setUp(self):
        self.api = APIClient()

    def test_seed_is_idempotent(self):
        call_command("seed_data", verbosity=0)
        call_command("seed_data", verbosity=0)
        self.assertEqual(Product.objects.count(), 600)
        self.assertEqual(Dealer.objects.count(), 25)
        self.assertEqual(Dealer.objects.get(dealer_code="DLR-007").id - Dealer.objects.get(dealer_code="DLR-001").id, 6)

    def test_create_product_rules(self):
        body = {"sku": "NEW-1", "name": "New", "category": "Brakes", "brand": "X", "vehicle_fitment": "Universal",
                "price": "10.50", "description": "", "stock": 99}
        r = self.api.post("/api/products/", body, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["stock"], 0)  # stock is never set through the product API
        self.assertEqual(self.api.post("/api/products/", body, format="json").json()["error"]["code"], "DUPLICATE")
        for price in ("0", "-1", "12.345", "abc", None):
            r = self.api.post("/api/products/", {**body, "sku": "NEW-2", "price": price}, format="json")
            self.assertEqual(r.status_code, 400, price)

    def test_filters_and_pagination(self):
        call_command("seed_data", verbosity=0)
        b = self.api.get("/api/products/?vehicle=duke 390&category=Brakes&page_size=100").json()
        self.assertTrue(b["count"] > 0)
        self.assertTrue(all("Duke 390" in p["vehicle_fitment"] and p["category"] == "Brakes" for p in b["results"]))
        self.assertEqual(len(self.api.get("/api/products/?page_size=500").json()["results"]), 100)
        self.assertTrue(all("endurance" in (p["brand"] + p["name"] + p["sku"]).lower()
                            for p in self.api.get("/api/products/?search=ENDURANCE").json()["results"]))

    def test_patch_cannot_change_sku_and_404s(self):
        call_command("seed_data", verbosity=0)
        self.assertEqual(self.api.patch("/api/products/BRK-1044/", {"sku": "X-1"}, format="json").status_code, 400)
        self.assertEqual(self.api.get("/api/products/NOPE/").json()["error"]["code"], "NOT_FOUND")
        self.assertEqual(self.api.get("/api/dealers/abc/").status_code, 404)

    def test_dealer_duplicate(self):
        self.assertEqual(self.api.post("/api/dealers/", {"dealer_code": "D1", "name": "A"}, format="json").status_code, 201)
        self.assertEqual(self.api.post("/api/dealers/", {"dealer_code": "D1", "name": "B"}, format="json").status_code, 409)
        self.assertEqual(self.api.post("/api/dealers/", {"name": "no code"}, format="json").status_code, 400)
