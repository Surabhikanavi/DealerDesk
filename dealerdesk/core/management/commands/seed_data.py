import csv
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Dealer, Inventory, Product


class Command(BaseCommand):
    help = "Load data/catalogue.csv and data/dealers.csv. Safe to run any number of times."

    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR
        with transaction.atomic():
            products = self._seed_products(data_dir / "catalogue.csv")
            dealers = self._seed_dealers(data_dir / "dealers.csv")
        if options["verbosity"]:
            self.stdout.write(self.style.SUCCESS(f"seed_data: {products} products and {dealers} dealers created"))

    @staticmethod
    def _rows(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def _seed_products(self, path):
        """Insert products that don't exist yet, keyed by SKU. Existing rows (and their live stock)
        are left alone, so a second run is a no-op and never resets stock changed by orders."""
        rows = self._rows(path)
        existing = set(Product.objects.values_list("sku", flat=True))
        new_rows, seen = [], set()
        for r in rows:
            sku = r["sku"].strip()
            if not sku or sku in existing or sku in seen:
                continue
            seen.add(sku)
            new_rows.append(r)
        created = Product.objects.bulk_create([
            Product(sku=r["sku"].strip(), name=r["name"], category=r["category"], brand=r["brand"],
                    vehicle_fitment=r["vehicle_fitment"], price=Decimal(r["price"]), description=r["description"])
            for r in new_rows
        ])
        stock = {r["sku"].strip(): int(r["stock"] or 0) for r in new_rows}
        Inventory.objects.bulk_create([Inventory(product=p, quantity=max(0, stock[p.sku])) for p in created])
        # Products created through the API before seeding still need a stock row.
        Inventory.objects.bulk_create(
            [Inventory(product=p, quantity=0) for p in Product.objects.filter(inventory__isnull=True)])
        return len(created)

    def _seed_dealers(self, path):
        rows = self._rows(path)
        existing = set(Dealer.objects.values_list("dealer_code", flat=True))
        new = []
        for r in rows:  # file order, so a fresh database gets ids 1..25 matching DLR-001..DLR-025
            code = r["dealer_code"].strip()
            if code and code not in existing:
                existing.add(code)
                new.append(Dealer(dealer_code=code, name=r["name"], city=r["city"], phone=r["phone"],
                                  email=r["email"], gstin=r["gstin"], credit_limit=Decimal(r["credit_limit"] or "0")))
        Dealer.objects.bulk_create(new)
        return len(new)
