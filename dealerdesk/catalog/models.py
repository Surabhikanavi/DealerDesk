from django.db import models
from django.db.models import Q


class Product(models.Model):
    """A catalogue item. Stock lives in Inventory so stock locks never block catalogue reads/edits."""

    sku = models.CharField(max_length=64, unique=True)  # unique => btree index for lookups by SKU
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=100, blank=True, db_index=True)  # exact-match filter
    brand = models.CharField(max_length=100, blank=True)
    vehicle_fitment = models.CharField(max_length=150, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(blank=True)  # supplier-written: untrusted data
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sku"]
        constraints = [models.CheckConstraint(condition=Q(price__gt=0), name="product_price_positive")]

    def __str__(self):
        return f"{self.sku} {self.name}"


class Inventory(models.Model):
    """Available quantity for one product. One row per product; this row is what confirm/cancel lock."""

    product = models.OneToOneField(Product, on_delete=models.CASCADE, primary_key=True, related_name="inventory")
    quantity = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Last line of defence: even a buggy code path can never store negative stock.
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=0), name="inventory_quantity_non_negative")]


class StockMovement(models.Model):
    """Append-only audit of every stock change: manual counts, confirms and cancels."""

    ADJUSTMENT, ORDER_CONFIRM, ORDER_CANCEL = "ADJUSTMENT", "ORDER_CONFIRM", "ORDER_CANCEL"
    KINDS = [(ADJUSTMENT, ADJUSTMENT), (ORDER_CONFIRM, ORDER_CONFIRM), (ORDER_CANCEL, ORDER_CANCEL)]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_movements")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, null=True, blank=True,
                              related_name="stock_movements")
    kind = models.CharField(max_length=20, choices=KINDS)
    delta = models.IntegerField()
    quantity_after = models.IntegerField()
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # An order can deduct a product once and restore it once: a DB-level "exactly once".
            models.UniqueConstraint(fields=["order", "product", "kind"], condition=Q(order__isnull=False),
                                    name="one_movement_per_order_product_kind"),
        ]


class Dealer(models.Model):
    dealer_code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.CharField(max_length=254, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=Q(credit_limit__gte=0), name="dealer_credit_limit_non_negative")]

    def __str__(self):
        return f"{self.dealer_code} {self.name}"
