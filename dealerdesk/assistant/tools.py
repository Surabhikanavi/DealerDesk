"""The three tools the model can call. Each one reads the database at call time, so every price and
stock figure the model sees is the live value at the moment of the request."""
import hashlib
import json

from catalog.models import Product
from core.exceptions import ApiError
from core.money import fmt
from orders import services as order_services

from . import retrieval
from .safety import safe_name

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_parts",
        "description": "Search the catalogue for parts. Returns matching products with their live price (INR) and "
                       "available stock. Put the part type in `query` (e.g. 'brake pads') and the vehicle in `vehicle` "
                       "(e.g. 'KTM Duke 390'). If the result has a `note`, follow it.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What part the dealer wants, e.g. 'brake pads', 'chain lube', or a SKU."},
            "vehicle": {"type": "string", "description": "Vehicle make/model if known, e.g. 'Bajaj Pulsar 150'."},
            "category": {"type": "string", "description": "Optional category filter, e.g. 'Brakes'."},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "check_stock",
        "description": "Get the current price and available stock for specific SKUs. Call this whenever you state a "
                       "price or stock figure for a SKU you already know.",
        "parameters": {"type": "object", "properties": {
            "skus": {"type": "array", "items": {"type": "string"}, "description": "SKU codes, e.g. ['BRK-1044']."},
        }, "required": ["skus"]},
    }},
    {"type": "function", "function": {
        "name": "create_draft_order",
        "description": "Create a DRAFT order for the current dealer. Drafts do not reserve stock; the dealer confirms "
                       "later in the portal. Only call when the dealer clearly asked to order/put aside/reserve specific "
                       "SKUs and quantities.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "sku": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
                "required": ["sku", "quantity"]}},
        }, "required": ["items"]},
    }},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


def product_view(p):
    """What the model sees. No supplier description: it is untrusted free text the model doesn't need."""
    stock = p.inventory.quantity if hasattr(p, "inventory") else 0
    return {"sku": p.sku, "name": safe_name(p), "brand": p.brand, "category": p.category,
            "vehicle_fitment": p.vehicle_fitment, "price_inr": fmt(p.price), "stock_units": stock}


def search_parts(args, ctx):
    query = str(args.get("query") or "").strip()[:200]
    vehicle = str(args.get("vehicle") or "").strip()[:100] or None
    category = str(args.get("category") or "").strip()[:100] or None
    if not query:
        return {"error": "query is required"}
    found = retrieval.search(query, vehicle=vehicle, category=category)
    products = Product.objects.select_related("inventory").in_bulk(found["product_ids"])
    results = [product_view(products[pid]) for pid in found["product_ids"] if pid in products]
    out = {"results": results}
    if found["resolved_vehicle"]:
        out["vehicle_matched"] = found["resolved_vehicle"]
    if found["note"]:
        out["note"] = found["note"]
    if not results and not found["note"]:
        out["note"] = "No matching products in the catalogue. Tell the dealer we don't have it; do not suggest anything else."
    return out


def check_stock(args, ctx):
    skus = args.get("skus")
    if isinstance(skus, str):
        skus = [skus]
    if not isinstance(skus, list) or not skus:
        return {"error": "skus must be a non-empty list"}
    skus = [str(s).strip().upper() for s in skus[:20]]
    products = {p.sku: p for p in Product.objects.select_related("inventory").filter(sku__in=skus)}
    return {"results": [product_view(products[s]) if s in products else {"sku": s, "error": "unknown SKU"} for s in skus]}


def create_draft_order(args, ctx):
    items = args.get("items")
    if not isinstance(items, list):
        return {"error": "items must be a list of {sku, quantity}"}
    clean = []
    for it in items:
        if isinstance(it, dict):
            qty = it.get("quantity")
            if isinstance(qty, str) and qty.isdigit():
                qty = int(qty)
            clean.append({"sku": str(it.get("sku", "")).strip().upper(), "quantity": qty})
    body = {"dealer_id": ctx["dealer"].id, "items": clean}  # the dealer comes from the request, never from the model
    # Same session + turn + items => same order, so a model that repeats the call can't create two drafts.
    key = "assistant:" + hashlib.sha256(
        f"{ctx['session'].pk}:{ctx['turn_id']}:{json.dumps(clean, sort_keys=True)}".encode()).hexdigest()
    try:
        order, created = order_services.create_draft_order(body, idempotency_key=key)  # same rules as POST /api/orders/
    except ApiError as exc:
        return {"error": exc.code, "message": exc.message, "details": exc.details}
    data = order_services.order_dict(order)
    if created or order.order_number not in [a["order_number"] for a in ctx["actions"]]:
        ctx["actions"].append({"type": "draft_order_created", "order_number": order.order_number})
    ctx["order_skus"].extend(i["sku"] for i in data["items"])
    stock = {p.sku: p.inventory.quantity for p in
             Product.objects.select_related("inventory").filter(sku__in=[i["sku"] for i in data["items"]])}
    for line in data["items"]:
        line["stock_units_now"] = stock.get(line["sku"], 0)
        if line["quantity"] > line["stock_units_now"]:
            line["warning"] = "requested more than is currently in stock; confirming would fail unless stock is added"
    data["note"] = "This is a DRAFT. Stock is not reserved until the dealer confirms the order in the portal."
    return data


HANDLERS = {"search_parts": search_parts, "check_stock": check_stock, "create_draft_order": create_draft_order}


def run_tool(name, args, ctx):
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name}"}
    return handler(args, ctx)
