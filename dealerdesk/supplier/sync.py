"""Supplier catalogue sync (contract section 8).

Phase 1 fetches the whole feed into memory, retrying each request up to 5 times. Nothing touches the
database until every page is in hand, so a feed that can't be fully fetched changes nothing.
Phase 2 validates, de-duplicates and applies the feed in one transaction.
"""
import time
from datetime import datetime, timezone as dt_tz

import requests
from django.conf import settings
from django.db import connection, transaction

from catalog.models import Inventory, Product
from core.exceptions import ApiError
from core.money import parse_price

MAX_ATTEMPTS = 5
PAGE_LIMIT = 20
BACKOFF_SECONDS = [0.5, 1, 2, 4]  # before attempts 2..5 after a 5xx / connection error
MAX_RETRY_AFTER = 30
SYNCED_FIELDS = ["name", "category", "brand", "vehicle_fitment", "description", "price"]
FIELD_LIMITS = {"name": 255, "category": 100, "brand": 100, "vehicle_fitment": 150, "description": 5000}


class SupplierUnavailable(Exception):
    pass


class SupplierClient:
    def __init__(self, base_url, api_key, session=None, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.sleep = sleep
        self.requests_made = 0

    def get_page(self, cursor):
        params = {"limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        last_problem = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.requests_made += 1
            try:
                resp = self.session.get(f"{self.base_url}/v1/products", params=params,
                                        headers={"X-Api-Key": self.api_key}, timeout=15)
            except requests.RequestException as exc:
                last_problem = f"connection error: {exc.__class__.__name__}"
                self._backoff(attempt)
                continue
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    items, next_cursor = body["items"], body.get("next_cursor")
                except (ValueError, KeyError, TypeError):
                    last_problem = "malformed page"
                    self._backoff(attempt)
                    continue
                if not isinstance(items, list):
                    raise SupplierUnavailable("supplier returned a page without an items list")
                return items, next_cursor
            if resp.status_code in (401, 403):
                raise SupplierUnavailable("supplier rejected our API key")  # retrying can't fix a bad key
            if resp.status_code == 429:
                last_problem = "rate limited"
                if attempt < MAX_ATTEMPTS:
                    self.sleep(self._retry_after(resp))
                continue
            if resp.status_code >= 500:
                last_problem = f"HTTP {resp.status_code}"
                self._backoff(attempt)
                continue
            raise SupplierUnavailable(f"supplier returned HTTP {resp.status_code}")
        raise SupplierUnavailable(f"request failed {MAX_ATTEMPTS} times ({last_problem})")

    def _backoff(self, attempt):
        if attempt < MAX_ATTEMPTS:
            self.sleep(BACKOFF_SECONDS[attempt - 1])

    @staticmethod
    def _retry_after(resp):
        try:
            return min(MAX_RETRY_AFTER, max(0.0, float(resp.headers.get("Retry-After", "1"))))
        except ValueError:
            return 1.0

    def fetch_all(self):
        items, cursor, pages, seen_cursors = [], None, 0, set()
        while True:
            page_items, cursor = self.get_page(cursor)
            pages += 1
            items.extend(page_items)
            if not cursor:
                return items, pages
            if cursor in seen_cursors:
                raise SupplierUnavailable("supplier pagination loops")
            seen_cursors.add(cursor)


def _parse_updated_at(value):
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=dt_tz.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=dt_tz.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_tz.utc)


def _text(value, field):
    if value is None:
        return None
    return str(value)[:FIELD_LIMITS[field]]  # stored exactly as the supplier sends it, so re-runs compare equal


def clean_feed(raw_items):
    """Rules 4 and 5: drop invalid items (reported), then keep the latest updated_at per SKU."""
    rejected, latest = [], {}
    for position, item in enumerate(raw_items):
        supplier_sku = item.get("supplier_sku") if isinstance(item, dict) else None
        if not isinstance(item, dict):
            rejected.append({"supplier_sku": None, "reason": "item is not an object"})
            continue
        sku = item.get("sku")
        if not isinstance(sku, str) or not sku.strip() or len(sku.strip()) > 64:
            rejected.append({"supplier_sku": supplier_sku, "reason": "missing sku"})
            continue
        price = parse_price(item.get("price"))
        if price is None:
            rejected.append({"supplier_sku": supplier_sku, "sku": sku.strip(), "reason": "invalid price"})
            continue
        fields = {f: _text(item.get(f), f) for f in FIELD_LIMITS}
        fields["price"] = price
        candidate = (_parse_updated_at(item.get("updated_at")), position, sku.strip(), fields)
        current = latest.get(candidate[2])
        # Latest updated_at wins; on an exact tie the later item in the feed wins.
        if current is None or candidate[:2] > current[:2]:
            latest[candidate[2]] = candidate
    return {sku: fields for _, _, sku, fields in latest.values()}, rejected


def apply_feed(products_by_sku):
    """Rule 6 and 7: create / update / unchanged. Stock is never written except 0 for new products."""
    created = updated = unchanged = 0
    existing = {p.sku: p for p in Product.objects.select_for_update().filter(sku__in=list(products_by_sku))}
    for sku, fields in products_by_sku.items():
        product = existing.get(sku)
        if product is None:
            if not fields.get("name"):
                fields["name"] = sku
            product = Product.objects.create(sku=sku, **{k: (v if v is not None else "") for k, v in fields.items()})
            Inventory.objects.create(product=product, quantity=0)
            created += 1
            continue
        changed = [f for f in SYNCED_FIELDS if fields.get(f) is not None and getattr(product, f) != fields[f]]
        if changed:
            for f in changed:
                setattr(product, f, fields[f])
            product.save(update_fields=changed + ["updated_at"])
            updated += 1
        else:
            unchanged += 1
    return created, updated, unchanged


def run_sync(client=None):
    client = client or SupplierClient(settings.SUPPLIER_API_URL, settings.SUPPLIER_API_KEY)
    try:
        raw_items, pages = client.fetch_all()
    except SupplierUnavailable as exc:
        raise ApiError(502, "SUPPLIER_UNAVAILABLE", f"Could not fetch the full supplier feed: {exc}. Nothing was changed.",
                       [{"requests_made": client.requests_made}])
    products_by_sku, rejected = clean_feed(raw_items)
    with transaction.atomic():
        with connection.cursor() as cur:  # one sync at a time
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('supplier-sync', 0))")
        created, updated, unchanged = apply_feed(products_by_sku)
    return {"created": created, "updated": updated, "unchanged": unchanged, "rejected": rejected,
            "pages_fetched": pages, "requests_made": client.requests_made}
