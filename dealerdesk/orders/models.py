from django.db import models
from django.db.models import Q


class Order(models.Model):
    DRAFT, CONFIRMED, DISPATCHED, DELIVERED, CANCELLED = "DRAFT", "CONFIRMED", "DISPATCHED", "DELIVERED", "CANCELLED"
    STATUSES = [(s, s) for s in (DRAFT, CONFIRMED, DISPATCHED, DELIVERED, CANCELLED)]

    order_number = models.CharField(max_length=20, unique=True)  # ORD-YYYYMMDD-NNNN
    # PROTECT: an order is a financial record; deleting a dealer must never silently delete its orders.
    dealer = models.ForeignKey("catalog.Dealer", on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=12, choices=STATUSES, default=DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["dealer", "status"]), models.Index(fields=["status"])]

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    # CASCADE: a line has no meaning without its order (and orders themselves are PROTECTed).
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    # PROTECT: a product that appears on any order can't be deleted out from under it.
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField()
    # NULL while DRAFT (the line follows the live catalogue price); set once on confirm and never again.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="one_line_per_product"),
            models.CheckConstraint(condition=Q(quantity__gte=1), name="order_item_quantity_positive"),
        ]


class DailyOrderSequence(models.Model):
    """One counter row per IST day. Incremented with INSERT ... ON CONFLICT DO UPDATE (see services)."""

    day = models.DateField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)


class IdempotencyRecord(models.Model):
    """Maps an Idempotency-Key to the order its first request created."""

    key = models.CharField(max_length=255, unique=True)
    request_hash = models.CharField(max_length=64)  # sha256 of the canonical JSON body
    # CASCADE: the record only exists to point at the order; without it there is nothing to replay.
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="idempotency_record")
    created_at = models.DateTimeField(auto_now_add=True)
