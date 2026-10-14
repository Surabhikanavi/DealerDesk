from decimal import Decimal

from django.test import TestCase

from catalog.models import Inventory, Product
from core.exceptions import ApiError
from supplier.sync import SupplierClient, run_sync


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeSession:
    """Serves pages by cursor; `script` maps cursor -> list of statuses returned before the page succeeds."""

    def __init__(self, pages, script=None):
        self.pages, self.script, self.calls = pages, script or {}, []

    def get(self, url, params=None, headers=None, timeout=None):
        cursor = params.get("cursor")
        self.calls.append(cursor)
        queue = self.script.get(cursor, [])
        if queue:
            status = queue.pop(0)
            return FakeResponse(status, {"error": "x"}, {"Retry-After": "0"} if status == 429 else {})
        idx = int(cursor or 0)
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
        return FakeResponse(200, {"items": self.pages[idx], "next_cursor": nxt})


def item(sku, price="100.00", updated="2026-10-01T00:00:00Z", **kw):
    return {"supplier_sku": f"SUP-{sku}", "sku": sku, "name": kw.get("name", f"Part {sku}"), "category": "Brakes",
            "brand": "B", "vehicle_fitment": "Universal", "description": "d", "price": price,
            "supplier_stock": 999, "updated_at": updated}


class SyncTest(TestCase):
    def setUp(self):
        p = Product.objects.create(sku="OLD-1", name="Part OLD-1", category="Brakes", brand="B",
                                   vehicle_fitment="Universal", description="d", price=Decimal("100.00"))
        Inventory.objects.create(product=p, quantity=42)

    def make_client(self, pages, script=None):
        return SupplierClient("http://supplier", "k", session=FakeSession(pages, script), sleep=lambda s: None)

    def test_full_sync_rules(self):
        pages = [
            [item("OLD-1", "120.00"), item("NEW-1"), item("", "1.00"), item("BAD-1", "12.345")],
            [item("DUP-1", "50.00", "2026-10-05T00:00:00Z"), item("BAD-2", "N/A")],
            [item("DUP-1", "40.00", "2026-10-01T00:00:00Z")],
        ]
        result = run_sync(self.make_client(pages, {"1": [429, 500, 503]}))
        self.assertEqual((result["created"], result["updated"], result["unchanged"]), (2, 1, 0))
        self.assertEqual(len(result["rejected"]), 3)
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(result["requests_made"], 6)
        self.assertEqual(Product.objects.get(sku="DUP-1").price, Decimal("50.00"))  # latest updated_at wins
        self.assertEqual(Inventory.objects.get(product__sku="OLD-1").quantity, 42)  # our stock untouched
        self.assertEqual(Inventory.objects.get(product__sku="NEW-1").quantity, 0)

        again = run_sync(self.make_client(pages))
        self.assertEqual((again["created"], again["updated"], again["unchanged"]), (0, 0, 3))

    def test_outage_changes_nothing(self):
        pages = [[item("OLD-1", "999.00")], [item("NEW-9")]]
        client = self.make_client(pages, {"1": [503] * 5})
        with self.assertRaises(ApiError) as ctx:
            run_sync(client)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (502, "SUPPLIER_UNAVAILABLE"))
        self.assertEqual(client.session.calls.count("1"), 5)  # at most 5 attempts
        self.assertEqual(Product.objects.get(sku="OLD-1").price, Decimal("100.00"))
        self.assertFalse(Product.objects.filter(sku="NEW-9").exists())

    def test_bad_key_fails_fast(self):
        client = self.make_client([[item("X-1")]], {None: [401]})
        with self.assertRaises(ApiError):
            run_sync(client)
        self.assertEqual(client.requests_made, 1)
