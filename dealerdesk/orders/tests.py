import threading
from decimal import Decimal

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from catalog.models import Dealer, Inventory, Product
from orders.models import Order
from orders.services import line_amounts


def make_product(sku, price="100.00", stock=10, name=None):
    p = Product.objects.create(sku=sku, name=name or f"Part {sku}", category="Brakes", brand="B",
                               vehicle_fitment="KTM Duke 390", price=Decimal(price))
    Inventory.objects.create(product=p, quantity=stock)
    return p


def stock(sku):
    return Inventory.objects.get(product__sku=sku).quantity


class OrderRulesTest(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.dealer = Dealer.objects.create(dealer_code="DLR-T1", name="Test Motors")
        make_product("BRK-1", "1459.50", 10, name="Brake Pad Set - Test")
        make_product("BRK-2", "0.03", 100)

    def create(self, items, **headers):
        return self.api.post("/api/orders/", {"dealer_id": self.dealer.id, "items": items}, format="json", **headers)

    def test_gst_is_per_line_half_up(self):
        # 3 x 1459.50 = 4378.50, tax 788.13 (788.13 exactly); 1 x 0.03 -> tax 0.0054 -> 0.01 (half-up per line)
        r = self.create([{"sku": "BRK-1", "quantity": 3}, {"sku": "BRK-2", "quantity": 1}])
        self.assertEqual(r.status_code, 201)
        o = r.json()
        self.assertEqual(o["items"][0]["line_tax"], "788.13")
        self.assertEqual(o["items"][1]["line_tax"], "0.01")
        self.assertEqual(o["subtotal"], "4378.53")
        self.assertEqual(o["tax_total"], "788.14")
        self.assertEqual(o["grand_total"], "5166.67")

    def test_half_up_not_bankers(self):
        # 0.25 * 0.18 = 0.045 -> 0.05 with ROUND_HALF_UP (banker's rounding would give 0.04)
        self.assertEqual(line_amounts(Decimal("0.25"), 1)[1], Decimal("0.05"))

    def test_validation(self):
        for items in ([], [{"sku": "BRK-1", "quantity": 0}], [{"sku": "BRK-1", "quantity": 1.5}],
                      [{"sku": "NOPE", "quantity": 1}], [{"sku": "BRK-1", "quantity": 1}, {"sku": "BRK-1", "quantity": 2}],
                      [{"sku": "BRK-1", "quantity": True}]):
            r = self.create(items)
            self.assertEqual(r.status_code, 400, items)
            self.assertEqual(r.json()["error"]["code"], "VALIDATION_ERROR")
        r = self.api.post("/api/orders/", {"dealer_id": 99999, "items": [{"sku": "BRK-1", "quantity": 1}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_order_number_format_and_sequence(self):
        a = self.create([{"sku": "BRK-1", "quantity": 1}]).json()["order_number"]
        b = self.create([{"sku": "BRK-1", "quantity": 1}]).json()["order_number"]
        self.assertRegex(a, r"^ORD-\d{8}-0001$")
        self.assertEqual(b[:-4] + b[-4:], a[:-4] + "0002")

    def test_draft_follows_price_confirm_locks_it(self):
        n = self.create([{"sku": "BRK-1", "quantity": 1}]).json()["order_number"]
        self.api.patch("/api/products/BRK-1/", {"price": "2000.00"}, format="json")
        self.assertEqual(self.api.get(f"/api/orders/{n}/").json()["items"][0]["unit_price"], "2000.00")
        self.api.post(f"/api/orders/{n}/confirm/")
        self.api.patch("/api/products/BRK-1/", {"price": "10.00"}, format="json")
        self.assertEqual(self.api.get(f"/api/orders/{n}/").json()["items"][0]["unit_price"], "2000.00")

    def test_confirm_all_or_nothing_lists_every_short_line(self):
        make_product("BRK-3", "50.00", 2, name="Brake Lever")
        n = self.create([{"sku": "BRK-1", "quantity": 11}, {"sku": "BRK-2", "quantity": 5},
                         {"sku": "BRK-3", "quantity": 3}]).json()["order_number"]
        r = self.api.post(f"/api/orders/{n}/confirm/")
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "INSUFFICIENT_STOCK")
        self.assertEqual(err["details"], [{"sku": "BRK-1", "requested": 11, "available": 10},
                                          {"sku": "BRK-3", "requested": 3, "available": 2}])
        self.assertIn("Insufficient stock for Brake Pad Set - Test. Available: 10, Requested: 11", err["message"])
        self.assertEqual((stock("BRK-1"), stock("BRK-2"), stock("BRK-3")), (10, 100, 2))  # nothing changed

    def test_transitions_and_cancel_restores_once(self):
        n = self.create([{"sku": "BRK-1", "quantity": 4}]).json()["order_number"]
        self.assertEqual(self.api.post(f"/api/orders/{n}/dispatch/").json()["error"]["code"], "INVALID_TRANSITION")
        self.api.post(f"/api/orders/{n}/confirm/")
        self.assertEqual(stock("BRK-1"), 6)
        r = self.api.put(f"/api/orders/{n}/", {"items": [{"sku": "BRK-1", "quantity": 1}]}, format="json")
        self.assertEqual(r.json()["error"]["code"], "ORDER_LOCKED")
        self.assertEqual(self.api.post(f"/api/orders/{n}/cancel/").status_code, 200)
        self.assertEqual(stock("BRK-1"), 10)
        self.assertEqual(self.api.post(f"/api/orders/{n}/cancel/").status_code, 409)
        self.assertEqual(stock("BRK-1"), 10)

    def test_dispatched_cannot_cancel(self):
        n = self.create([{"sku": "BRK-1", "quantity": 1}]).json()["order_number"]
        for action in ("confirm", "dispatch"):
            self.api.post(f"/api/orders/{n}/{action}/")
        self.assertEqual(self.api.post(f"/api/orders/{n}/cancel/").status_code, 409)
        self.assertEqual(self.api.post(f"/api/orders/{n}/deliver/").json()["status"], "DELIVERED")

    def test_idempotency(self):
        body = [{"sku": "BRK-1", "quantity": 1}]
        a = self.create(body, HTTP_IDEMPOTENCY_KEY="k1")
        b = self.create(body, HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(a.status_code, 201)
        self.assertIn(b.status_code, (200, 201))
        self.assertEqual(a.json()["order_number"], b.json()["order_number"])
        c = self.create([{"sku": "BRK-1", "quantity": 2}], HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(c.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(Order.objects.count(), 1)

    def test_inventory_put_validation(self):
        for q in (-1, 2.5, "10", None):
            self.assertEqual(self.api.put("/api/inventory/BRK-1/", {"quantity": q}, format="json").status_code, 400)
        self.assertEqual(self.api.put("/api/inventory/NOPE/", {"quantity": 1}, format="json").status_code, 404)


class ConcurrencyTest(TransactionTestCase):
    """Real threads against real PostgreSQL: each thread has its own connection and transaction."""

    def setUp(self):
        self.dealer = Dealer.objects.create(dealer_code="DLR-C", name="Concurrent")
        make_product("HOT-1", "10.00", 5)

    def run_parallel(self, fn, n):
        barrier, results = threading.Barrier(n), []

        def worker(i):
            close_old_connections()
            try:
                barrier.wait()
                results.append(fn(i))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_parallel_confirms_never_oversell(self):
        api = APIClient()
        numbers = [api.post("/api/orders/", {"dealer_id": self.dealer.id, "items": [{"sku": "HOT-1", "quantity": 2}]},
                            format="json").json()["order_number"] for _ in range(8)]
        codes = self.run_parallel(lambda i: APIClient().post(f"/api/orders/{numbers[i]}/confirm/").status_code, 8)
        self.assertEqual(sorted(codes).count(200), 2)  # 5 units -> only two orders of 2 fit
        self.assertEqual(codes.count(409), 6)
        self.assertEqual(stock("HOT-1"), 1)

    def test_parallel_creates_get_unique_numbers(self):
        body = {"dealer_id": self.dealer.id, "items": [{"sku": "HOT-1", "quantity": 1}]}
        numbers = self.run_parallel(lambda i: APIClient().post("/api/orders/", body, format="json").json()["order_number"], 10)
        self.assertEqual(len(set(numbers)), 10)

    def test_parallel_idempotent_duplicates_create_one_order(self):
        body = {"dealer_id": self.dealer.id, "items": [{"sku": "HOT-1", "quantity": 1}]}
        numbers = self.run_parallel(lambda i: APIClient().post("/api/orders/", body, format="json",
                                                                HTTP_IDEMPOTENCY_KEY="same").json()["order_number"], 8)
        self.assertEqual(len(set(numbers)), 1)
        self.assertEqual(Order.objects.count(), 1)

    def test_parallel_cancels_restore_once(self):
        api = APIClient()
        n = api.post("/api/orders/", {"dealer_id": self.dealer.id, "items": [{"sku": "HOT-1", "quantity": 3}]},
                     format="json").json()["order_number"]
        api.post(f"/api/orders/{n}/confirm/")
        codes = self.run_parallel(lambda i: APIClient().post(f"/api/orders/{n}/cancel/").status_code, 6)
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(stock("HOT-1"), 5)
