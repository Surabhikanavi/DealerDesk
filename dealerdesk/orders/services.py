"""Order engine: validation, draft creation, idempotency, order numbers, status flow and stock.

Every state change runs in one transaction that first locks the order row (SELECT ... FOR UPDATE),
so two requests acting on the same order are serialised and the second sees the first's result.
"""
import hashlib
import json
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.utils import timezone

from catalog.models import Dealer, Inventory, Product, StockMovement
from core.exceptions import ApiError, not_found, validation_error
from core.money import fmt, gst
from core.validation import is_int, raise_if, require_object

from .models import DailyOrderSequence, IdempotencyRecord, Order, OrderItem

IST = ZoneInfo("Asia/Kolkata")
MAX_LINE_QUANTITY = 1_000_000
MAX_LINES = 200


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def validate_items(raw_items):
    """Contract 7.1 rules. Returns [(Product, quantity)] in request order, or raises 400 with every problem."""
    if not isinstance(raw_items, list) or not raw_items:
        raise validation_error("items must be a non-empty list.", [{"field": "items", "message": "at least one item is required."}])
    if len(raw_items) > MAX_LINES:
        raise validation_error(f"An order can have at most {MAX_LINES} lines.")
    errors, wanted, seen = [], [], set()
    for i, item in enumerate(raw_items):
        field = f"items[{i}]"
        if not isinstance(item, dict):
            errors.append({"field": field, "message": "each item must be an object with sku and quantity."})
            continue
        sku, qty = item.get("sku"), item.get("quantity")
        if not isinstance(sku, str) or not sku.strip():
            errors.append({"field": f"{field}.sku", "message": "sku is required."})
            sku = None
        else:
            sku = sku.strip()
            if sku in seen:
                errors.append({"field": f"{field}.sku", "message": f"SKU {sku} appears more than once.", "sku": sku})
            seen.add(sku)
        if not is_int(qty) or qty < 1:
            errors.append({"field": f"{field}.quantity", "message": "quantity must be an integer >= 1."})
        elif qty > MAX_LINE_QUANTITY:
            errors.append({"field": f"{field}.quantity", "message": f"quantity must be at most {MAX_LINE_QUANTITY}."})
        wanted.append((sku, qty))
    products = {p.sku: p for p in Product.objects.filter(sku__in=[s for s, _ in wanted if s])}
    for i, (sku, _) in enumerate(wanted):
        if sku and sku not in products:
            errors.append({"field": f"items[{i}].sku", "message": f"Unknown SKU {sku}.", "sku": sku})
    raise_if(errors)
    return [(products[sku], qty) for sku, qty in wanted]


def validate_dealer(dealer_id):
    if not is_int(dealer_id):
        raise validation_error("dealer_id must be an integer.", [{"field": "dealer_id", "message": "required integer"}])
    dealer = Dealer.objects.filter(pk=dealer_id).first() if 0 < dealer_id < 2**63 else None
    if dealer is None:
        raise validation_error(f"Dealer {dealer_id} does not exist.", [{"field": "dealer_id", "message": "unknown dealer"}])
    return dealer


# ---------------------------------------------------------------------------------------------
# Order numbers and idempotency
# ---------------------------------------------------------------------------------------------
def next_order_number():
    """ORD-YYYYMMDD-NNNN, IST date, daily sequence. Must run inside the creating transaction.

    The upsert is one atomic statement: concurrent callers for the same day queue on the counter
    row's lock and each gets the next value. If the order's transaction rolls back, so does the
    increment, so numbers are unique (unique index on order_number as a backstop).
    """
    day = timezone.now().astimezone(IST).date()
    table = DailyOrderSequence._meta.db_table
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} (day, last_number) VALUES (%s, 1) "
            f"ON CONFLICT (day) DO UPDATE SET last_number = {table}.last_number + 1 "
            f"RETURNING last_number",
            [day],
        )
        number = cur.fetchone()[0]
    return f"ORD-{day:%Y%m%d}-{number:04d}"


def request_fingerprint(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_idempotency_key(key):
    """Transaction-scoped advisory lock on the key: a concurrent duplicate waits here until the
    first request commits, then finds its IdempotencyRecord instead of creating a second order."""
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


# ---------------------------------------------------------------------------------------------
# Draft creation and editing
# ---------------------------------------------------------------------------------------------
def create_draft_order(body, idempotency_key=None):
    """POST /api/orders/. Returns (order, created). Used by the API and the Dealer Assistant alike."""
    require_object(body)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 255:
            raise validation_error("Idempotency-Key must be 1-255 characters.")
    fingerprint = request_fingerprint(body)

    with transaction.atomic():
        if idempotency_key:
            _lock_idempotency_key(idempotency_key)
            record = IdempotencyRecord.objects.select_related("order").filter(key=idempotency_key).first()
            if record:
                if record.request_hash != fingerprint:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT",
                                   "This Idempotency-Key was already used with a different request body.")
                return record.order, False
        dealer = validate_dealer(body.get("dealer_id"))
        lines = validate_items(body.get("items"))
        order = Order.objects.create(order_number=next_order_number(), dealer=dealer, status=Order.DRAFT)
        OrderItem.objects.bulk_create([OrderItem(order=order, product=p, quantity=q) for p, q in lines])
        if idempotency_key:
            IdempotencyRecord.objects.create(key=idempotency_key, request_hash=fingerprint, order=order)
    return order, True


def _locked_order(order_number):
    order = Order.objects.select_for_update().filter(order_number=order_number).first()
    if order is None:
        raise not_found(f"Order {order_number} not found.")
    return order


def replace_items(order_number, body):
    """PUT /api/orders/{n}/: replaces every line, only while DRAFT."""
    require_object(body)
    with transaction.atomic():
        order = _locked_order(order_number)
        if order.status != Order.DRAFT:
            raise ApiError(409, "ORDER_LOCKED", f"Order {order_number} is {order.status} and can no longer be edited.")
        lines = validate_items(body.get("items"))
        order.items.all().delete()
        OrderItem.objects.bulk_create([OrderItem(order=order, product=p, quantity=q) for p, q in lines])
        order.save(update_fields=["updated_at"])
    return order


# ---------------------------------------------------------------------------------------------
# Status flow
# ---------------------------------------------------------------------------------------------
TRANSITIONS = {  # action -> (allowed from, to)
    "confirm": ({Order.DRAFT}, Order.CONFIRMED),
    "dispatch": ({Order.CONFIRMED}, Order.DISPATCHED),
    "deliver": ({Order.DISPATCHED}, Order.DELIVERED),
    "cancel": ({Order.DRAFT, Order.CONFIRMED}, Order.CANCELLED),
}


def _check_transition(order, action):
    allowed_from, target = TRANSITIONS[action]
    if order.status not in allowed_from:
        raise ApiError(409, "INVALID_TRANSITION", f"Cannot {action} an order that is {order.status}.",
                       [{"status": order.status, "action": action}])
    return target


def _lock_inventory(product_ids):
    """Lock the stock rows in a fixed order (by product id) so two orders sharing products can't deadlock."""
    rows = Inventory.objects.select_for_update().filter(product_id__in=product_ids).order_by("product_id")
    inventories = {inv.product_id: inv for inv in rows}
    for pid in product_ids:  # a product created without an inventory row has 0 stock
        if pid not in inventories:
            inventories[pid] = Inventory.objects.create(product_id=pid, quantity=0)
    return inventories


def confirm(order_number):
    """DRAFT -> CONFIRMED: all-or-nothing stock check and deduction, then lock the prices."""
    with transaction.atomic():
        order = _locked_order(order_number)
        target = _check_transition(order, "confirm")
        items = list(order.items.select_related("product"))
        inventories = _lock_inventory(sorted({i.product_id for i in items}))

        # Check every line against the stock we now hold locks on. Nobody else can change these rows
        # until we commit, so "check then deduct" is safe; a concurrent confirm waits, then re-reads.
        short = [(i, inventories[i.product_id].quantity) for i in items if i.quantity > inventories[i.product_id].quantity]
        if short:
            raise ApiError(
                409, "INSUFFICIENT_STOCK",
                ". ".join(f"Insufficient stock for {i.product.name}. Available: {avail}, Requested: {i.quantity}"
                          for i, avail in short),
                [{"sku": i.product.sku, "requested": i.quantity, "available": avail} for i, avail in short],
            )
        for item in items:
            inv = inventories[item.product_id]
            inv.quantity -= item.quantity
            inv.save(update_fields=["quantity", "updated_at"])
            StockMovement.objects.create(product_id=item.product_id, order=order, kind=StockMovement.ORDER_CONFIRM,
                                         delta=-item.quantity, quantity_after=inv.quantity, reason=f"confirm {order_number}")
            item.unit_price = item.product.price  # price lock: from now on the line never follows the catalogue
            item.save(update_fields=["unit_price"])
        order.status, order.confirmed_at = target, timezone.now()
        order.save(update_fields=["status", "confirmed_at", "updated_at"])
    return order


def cancel(order_number):
    """DRAFT -> CANCELLED (no stock effect) or CONFIRMED -> CANCELLED (restore stock exactly once).

    Exactly once: the status check runs under the order row lock, so of two concurrent cancels the
    second waits, then sees CANCELLED and gets INVALID_TRANSITION. The unique (order, product, kind)
    constraint on StockMovement would also reject a second restore at the database level.
    """
    with transaction.atomic():
        order = _locked_order(order_number)
        was = order.status
        target = _check_transition(order, "cancel")
        if was == Order.CONFIRMED:
            items = list(order.items.all())
            inventories = _lock_inventory(sorted({i.product_id for i in items}))
            for item in items:
                inv = inventories[item.product_id]
                inv.quantity += item.quantity
                inv.save(update_fields=["quantity", "updated_at"])
                StockMovement.objects.create(product_id=item.product_id, order=order, kind=StockMovement.ORDER_CANCEL,
                                             delta=item.quantity, quantity_after=inv.quantity, reason=f"cancel {order_number}")
        order.status, order.cancelled_at = target, timezone.now()
        order.save(update_fields=["status", "cancelled_at", "updated_at"])
    return order


def simple_transition(order_number, action):
    """dispatch / deliver: status only, no stock effect."""
    with transaction.atomic():
        order = _locked_order(order_number)
        order.status = _check_transition(order, action)
        order.save(update_fields=["status", "updated_at"])
    return order


# ---------------------------------------------------------------------------------------------
# Representation
# ---------------------------------------------------------------------------------------------
def line_amounts(unit_price, quantity):
    line_subtotal = unit_price * quantity  # Decimal * int is exact
    line_tax = gst(line_subtotal)  # rounded per line, half-up
    return line_subtotal, line_tax, line_subtotal + line_tax


def order_dict(order):
    items, subtotal, tax_total = [], 0, 0
    for item in order.items.select_related("product").order_by("id"):
        # DRAFT (and a draft that was cancelled) follows the live price; confirmed lines use the locked one.
        unit_price = item.unit_price if item.unit_price is not None else item.product.price
        line_subtotal, line_tax, line_total = line_amounts(unit_price, item.quantity)
        subtotal += line_subtotal
        tax_total += line_tax
        items.append({
            "sku": item.product.sku, "name": item.product.name, "quantity": item.quantity,
            "unit_price": fmt(unit_price), "line_subtotal": fmt(line_subtotal),
            "line_tax": fmt(line_tax), "line_total": fmt(line_total),
        })
    return {
        "order_number": order.order_number,
        "dealer_id": order.dealer_id,
        "status": order.status,
        "items": items,
        "subtotal": fmt(subtotal),
        "tax_total": fmt(tax_total),
        "grand_total": fmt(subtotal + tax_total),
        "created_at": order.created_at.astimezone(IST).isoformat(),
        "updated_at": order.updated_at.astimezone(IST).isoformat(),
    }
