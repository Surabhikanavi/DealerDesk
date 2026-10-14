"""Forecast lookup and reorder suggestions (contract section 10).

The forecast is a committed artefact (ml/forecast.csv) and the history/lead times are read-only
reference files in data/, so they are read from disk and cached per process. Stock, the only
value that changes during the day, is read live from the database on every request.
"""
import csv
import math
import statistics
from decimal import Decimal
from functools import lru_cache

from django.conf import settings

from catalog.models import Inventory

SIGMA_WEEKS = 12
Z = Decimal("1.65")


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@lru_cache(maxsize=1)
def forecasts():
    """{sku: [(week_start, Decimal units), ...]} sorted by week. Decimal keeps sums exact."""
    out = {}
    for r in _read(settings.FORECAST_FILE):
        out.setdefault(r["sku"].strip(), []).append((r["week_start"].strip(), Decimal(r["forecast_units"].strip())))
    return {sku: sorted(rows) for sku, rows in out.items()}


@lru_cache(maxsize=1)
def lead_times():
    return {r["sku"].strip(): int(r["lead_time_weeks"]) for r in _read(settings.DATA_DIR / "lead_times.csv")}


@lru_cache(maxsize=1)
def demand_sigma():
    """Sample std dev (n-1) of units_sold over each SKU's most recent 12 weeks (all weeks if fewer)."""
    history = {}
    for r in _read(settings.DATA_DIR / "sales_history.csv"):
        history.setdefault(r["sku"].strip(), []).append((r["week_start"].strip(), int(r["units_sold"])))
    sigma = {}
    for sku, rows in history.items():
        recent = [units for _, units in sorted(rows)[-SIGMA_WEEKS:]]
        sigma[sku] = statistics.stdev(recent) if len(recent) >= 2 else 0.0
    return sigma


def forecast_for(sku):
    rows = forecasts().get(sku)
    if rows is None or sku not in lead_times():
        return None
    return [{"week_start": week, "forecast_units": float(units)} for week, units in rows]


def _number(value):
    """Decimal -> int when whole, else float (JSON number)."""
    return int(value) if value == value.to_integral_value() else float(value)


def reorder_suggestions():
    leads = lead_times()
    stock = dict(Inventory.objects.filter(product__sku__in=list(leads)).values_list("product__sku", "quantity"))
    sigma = demand_sigma()
    results = []
    for sku, lead in leads.items():
        weeks = forecasts().get(sku, [])
        lead_time_demand = sum((units for _, units in weeks[:lead]), Decimal("0"))
        safety_stock = math.ceil(float(Z) * sigma.get(sku, 0.0) * math.sqrt(lead))
        reorder_point = lead_time_demand + safety_stock
        current = stock.get(sku, 0)
        reorder_qty = max(0, math.ceil(reorder_point - current))
        results.append({
            "sku": sku,
            "current_stock": current,
            "lead_time_weeks": lead,
            "lead_time_demand": _number(lead_time_demand),
            "safety_stock": safety_stock,
            "reorder_point": _number(reorder_point),
            "reorder_qty": reorder_qty,
        })
    return results
