"""Catalogue writes: products, manual stock counts and dealers."""
from django.db import IntegrityError, transaction

from core.exceptions import ApiError, not_found
from core.money import parse_non_negative_amount, parse_price
from core.validation import clean_text, is_int, raise_if, require_object

from .models import Dealer, Inventory, Product, StockMovement

PRODUCT_TEXT_FIELDS = {"name": 255, "category": 100, "brand": 100, "vehicle_fitment": 150, "description": 5000}


def _product_fields(data, *, partial, errors):
    fields = {}
    for field, max_length in PRODUCT_TEXT_FIELDS.items():
        if partial and field not in data:
            continue
        fields[field] = clean_text(data, field, required=(field == "name"), max_length=max_length, errors=errors)
    if not partial or "price" in data:
        price = parse_price(data.get("price"))
        if price is None:
            errors.append({"field": "price", "message": "price must be a positive amount with at most 2 decimals."})
        fields["price"] = price
    return fields


def create_product(data):
    """POST /api/products/: every product field except stock; stock always starts at 0."""
    require_object(data)
    errors = []
    sku = clean_text(data, "sku", required=True, max_length=64, errors=errors)
    fields = _product_fields(data, partial=False, errors=errors)
    raise_if(errors)
    try:
        with transaction.atomic():
            product = Product.objects.create(sku=sku, **fields)
            Inventory.objects.create(product=product, quantity=0)
    except IntegrityError:  # unique(sku): also catches two concurrent creates of the same SKU
        raise ApiError(409, "DUPLICATE", f"A product with SKU {sku} already exists.",
                       [{"field": "sku", "message": "already exists"}])
    return product


def update_product(sku, data):
    """PATCH /api/products/{sku}/. The SKU is the identity and cannot change."""
    require_object(data)
    errors = []
    if "sku" in data and data["sku"] != sku:
        errors.append({"field": "sku", "message": "sku cannot be changed."})
    if "stock" in data:
        errors.append({"field": "stock", "message": "stock is changed through PUT /api/inventory/{sku}/."})
    fields = _product_fields(data, partial=True, errors=errors)
    raise_if(errors)
    with transaction.atomic():
        product = Product.objects.select_for_update().filter(sku=sku).first()
        if product is None:
            raise not_found(f"Product {sku} not found.")
        for field, value in fields.items():
            setattr(product, field, value)
        product.save()
    return Product.objects.select_related("inventory").get(pk=product.pk)


def set_stock(sku, data):
    """PUT /api/inventory/{sku}/: set available quantity to exactly `quantity`. Orders are untouched."""
    require_object(data)
    product = Product.objects.filter(sku=sku).first()
    if product is None:
        raise not_found(f"Product {sku} not found.")
    errors = []
    quantity = data.get("quantity")
    if not is_int(quantity) or quantity < 0:
        errors.append({"field": "quantity", "message": "quantity must be an integer >= 0."})
    elif quantity > 2_000_000_000:
        errors.append({"field": "quantity", "message": "quantity is too large."})
    reason = data.get("reason", "")
    if reason is not None and not isinstance(reason, str):
        errors.append({"field": "reason", "message": "reason must be a string."})
    raise_if(errors)
    with transaction.atomic():
        # Same row lock as order confirm/cancel, so a stock count never interleaves with a deduction.
        inventory, _ = Inventory.objects.select_for_update().get_or_create(product=product)
        delta = quantity - inventory.quantity
        inventory.quantity = quantity
        inventory.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(product=product, kind=StockMovement.ADJUSTMENT, delta=delta,
                                     quantity_after=quantity, reason=(reason or "")[:255])
    return quantity


def create_dealer(data):
    require_object(data)
    errors = []
    code = clean_text(data, "dealer_code", required=True, max_length=32, errors=errors)
    name = clean_text(data, "name", required=True, max_length=255, errors=errors)
    city = clean_text(data, "city", max_length=100, errors=errors)
    phone = clean_text(data, "phone", max_length=32, errors=errors)
    email = clean_text(data, "email", max_length=254, errors=errors)
    gstin = clean_text(data, "gstin", max_length=15, errors=errors)
    if email and "@" not in email:
        errors.append({"field": "email", "message": "email is not valid."})
    credit_limit = parse_non_negative_amount(data.get("credit_limit", "0.00"))
    if credit_limit is None:
        errors.append({"field": "credit_limit", "message": "credit_limit must be an amount >= 0 with at most 2 decimals."})
    raise_if(errors)
    try:
        with transaction.atomic():
            return Dealer.objects.create(dealer_code=code, name=name, city=city, phone=phone, email=email,
                                         gstin=gstin, credit_limit=credit_limit)
    except IntegrityError:
        raise ApiError(409, "DUPLICATE", f"A dealer with code {code} already exists.",
                       [{"field": "dealer_code", "message": "already exists"}])
