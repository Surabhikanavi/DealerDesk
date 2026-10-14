#!/usr/bin/env python3
"""Weekly demand forecast for the 40 SKUs in data/lead_times.csv.

    python ml/forecast.py              # backtest, then write ml/forecast.csv
    python ml/forecast.py --backtest   # backtest only

Model (multiplicative, one shared structure for every SKU):

    forecast(sku, week) = level(sku) x festive[weeks_to_diwali(week)] x promo_lift(sku) ** promo_flag

* festive[r]  how much a week r weeks before/after Diwali week sells versus that SKU's normal level,
              learned from past Diwali seasons. Diwali moves (Nov 1 2024, Oct 21 2025, Nov 8 2026), so the
              lift is indexed by weeks-to-Diwali, not by calendar week.
* promo_lift  sales in promo weeks versus the SKU's normal level, pooled across SKUs and shrunk per SKU.
* level       mean of the last few weeks after dividing out the festive and promo effects ("deseasonalised"),
              with a longer window for rarely-sold SKUs and a short one for new SKUs.

Every estimate is made from `history[history.week_start <= cutoff]` only (see fit()), so the backtest
never sees the weeks it is scored on.
"""
import argparse
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = Path(__file__).resolve().parent / "forecast.csv"
FORECAST_WEEKS = pd.to_datetime(["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"])

FESTIVE_WINDOW = range(-6, 2)    # weeks relative to Diwali week that may carry a festive effect
BASE_WINDOW = range(-14, -6)     # "normal" weeks before each season, used as that season's baseline
POST_BASE_WINDOW = range(2, 10)  # fallback baseline after Diwali when the pre-season weeks aren't in the data
PROMO_SHRINK = 4.0               # pseudo-observations pulling a SKU's promo lift towards the pooled lift
LEVEL_WEEKS = 6                  # regular SKUs
LEVEL_WEEKS_INTERMITTENT = 52    # rarely sold SKUs: a long mean is the only stable estimate
LEVEL_WEEKS_NEW = 3              # new SKUs: still ramping, so only the most recent weeks
INTERMITTENT_ZERO_SHARE = 0.3
NEW_SKU_WEEKS = 13


# ---------------------------------------------------------------------------------------------
# Data and calendar
# ---------------------------------------------------------------------------------------------
def load():
    sales = pd.read_csv(DATA / "sales_history.csv", parse_dates=["week_start"])
    promos = pd.read_csv(DATA / "promo_calendar.csv", parse_dates=["week_start"])
    leads = pd.read_csv(DATA / "lead_times.csv")
    festivals = pd.read_csv(DATA / "festival_calendar.csv", parse_dates=["date"])
    diwali = festivals[festivals.festival.str.lower() == "diwali"].date
    diwali_weeks = sorted(d - pd.Timedelta(days=d.dayofweek) for d in diwali)  # the Monday of Diwali week
    return sales.sort_values(["sku", "week_start"]), promos, leads, diwali_weeks


def weeks_to_diwali(week, diwali_weeks):
    """Signed weeks from the nearest Diwali week (negative = before)."""
    return min(((week - d).days // 7 for d in diwali_weeks), key=abs)


def add_calendar(df, diwali_weeks):
    df = df.copy()
    df["rel"] = df.week_start.map(lambda w: weeks_to_diwali(w, diwali_weeks))
    df["festive"] = df.rel.isin(FESTIVE_WINDOW)
    return df


# ---------------------------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------------------------
def sku_kind(d):
    if len(d) < NEW_SKU_WEEKS:
        return "new"
    if (d.units_sold == 0).mean() > INTERMITTENT_ZERO_SHARE:
        return "intermittent"
    return "regular"


def fit_festive(hist, kinds, diwali_weeks):
    """festive[r] = mean over (regular SKU, past season) of non-promo sales at r / that season's baseline."""
    ratios = {r: [] for r in FESTIVE_WINDOW}
    regular = hist[hist.sku.map(kinds) == "regular"]
    for dw in diwali_weeks:
        season = regular[(regular.week_start - dw).dt.days.between(BASE_WINDOW.start * 7, (POST_BASE_WINDOW.stop - 1) * 7)]
        if season.empty:
            continue
        for sku, d in season.groupby("sku"):
            d = d.assign(r=(d.week_start - dw).dt.days // 7)
            base = d[d.r.isin(BASE_WINDOW) & (d.promo_flag == 0)].units_sold
            if len(base) < 3:  # 2024: history starts 4 weeks before Diwali, so use the weeks after it
                base = d[d.r.isin(POST_BASE_WINDOW) & (d.promo_flag == 0)].units_sold
            if len(base) < 3 or base.mean() <= 0:
                continue
            for _, row in d[d.r.isin(FESTIVE_WINDOW) & (d.promo_flag == 0)].iterrows():
                ratios[row.r].append(row.units_sold / base.mean())
    # Weeks never observed in a past season (or too thinly) get no lift.
    return {r: (float(np.mean(v)) if len(v) >= 5 else 1.0) for r, v in ratios.items()}


def deseasonalise(d, festive, promo_lift):
    mult = d.rel.map(lambda r: festive.get(r, 1.0)) * np.where(d.promo_flag == 1, promo_lift, 1.0)
    return d.units_sold / mult


def fit_promo(hist, kinds, festive):
    """Pooled promo lift (promo week / mean of the 8 preceding clean weeks), shrunk per SKU."""
    per_sku = {}
    for sku, d in hist[hist.sku.map(kinds) == "regular"].groupby("sku"):
        d = d.reset_index(drop=True)
        clean = d.units_sold / d.rel.map(lambda r: festive.get(r, 1.0))
        vals = []
        for i in d.index[d.promo_flag == 1]:
            prior = clean[max(0, i - 8):i][d.promo_flag[max(0, i - 8):i] == 0]
            if len(prior) >= 4 and prior.mean() > 0:
                vals.append(clean[i] / prior.mean())
        per_sku[sku] = vals
    pooled = float(np.median([v for vals in per_sku.values() for v in vals])) if any(per_sku.values()) else 2.0
    lifts = {sku: (sum(vals) + PROMO_SHRINK * pooled) / (len(vals) + PROMO_SHRINK) for sku, vals in per_sku.items()}
    return lifts, pooled


def fit(history, cutoff, diwali_weeks):
    """Fit on weeks up to and including `cutoff`. Nothing after the cutoff is visible from here on."""
    hist = history[history.week_start <= cutoff]  # <- the anti-leakage line: training data ends at the cutoff
    hist = add_calendar(hist, diwali_weeks)
    kinds = {sku: sku_kind(d) for sku, d in hist.groupby("sku")}
    festive = fit_festive(hist, kinds, diwali_weeks)
    promo_lifts, pooled_promo = fit_promo(hist, kinds, festive)
    levels = {}
    for sku, d in hist.groupby("sku"):
        kind = kinds[sku]
        lift = promo_lifts.get(sku, pooled_promo)
        clean = deseasonalise(d, festive, lift)
        n = {"regular": LEVEL_WEEKS, "intermittent": LEVEL_WEEKS_INTERMITTENT, "new": LEVEL_WEEKS_NEW}[kind]
        levels[sku] = float(clean.tail(n).mean())
    return {"festive": festive, "promo": promo_lifts, "pooled_promo": pooled_promo, "levels": levels, "kinds": kinds}


def predict(model, sku, weeks, promo_flags, diwali_weeks):
    level = model["levels"][sku]
    lift = model["promo"].get(sku, model["pooled_promo"])
    out = []
    for week, flag in zip(weeks, promo_flags):
        mult = model["festive"].get(weeks_to_diwali(week, diwali_weeks), 1.0)
        out.append(max(0.0, level * mult * (lift if flag else 1.0)))
    return out


# ---------------------------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------------------------
def baselines(hist_sku, weeks):
    """The grader's three baselines: last value, recent average (4 weeks) and same week last year."""
    last = float(hist_sku.units_sold.iloc[-1])
    recent = float(hist_sku.units_sold.tail(4).mean())
    by_week = hist_sku.set_index("week_start").units_sold
    seasonal = [float(by_week.get(w - pd.Timedelta(weeks=52), np.nan)) for w in weeks]
    return {"last_value": [last] * len(weeks), "recent_avg": [recent] * len(weeks), "same_week_ly": seasonal}


def backtest(sales, diwali_weeks, origins, horizon=4):
    rows = []
    for origin in origins:
        model = fit(sales, origin, diwali_weeks)
        weeks = [origin + pd.Timedelta(weeks=h) for h in range(1, horizon + 1)]
        for sku, d in sales.groupby("sku"):
            past = d[d.week_start <= origin]
            future = d[d.week_start.isin(weeks)].set_index("week_start")
            if past.empty or len(future) < horizon:
                continue
            # Promo flags for the forecast weeks are known in advance (promo_calendar.csv), as in production.
            flags = [int(future.promo_flag.get(w, 0)) for w in weeks]
            preds = {"model": predict(model, sku, weeks, flags, diwali_weeks), **baselines(past, weeks)}
            for i, w in enumerate(weeks):
                row = {"origin": origin, "sku": sku, "kind": model["kinds"][sku], "week": w,
                       "actual": float(future.units_sold[w])}
                row.update({name: p[i] for name, p in preds.items()})
                rows.append(row)
    return pd.DataFrame(rows)


def score(df, methods=("model", "last_value", "recent_avg", "same_week_ly")):
    """WAPE and MAE, every method scored on the same rows (where same-week-last-year exists)."""
    out = {}
    ok = df[list(methods)].notna().all(axis=1)
    if not ok.any():  # new SKUs have no last-year week: compare on the other baselines
        methods = [m for m in methods if m != "same_week_ly"]
        ok = df[methods].notna().all(axis=1)
    for m in methods:
        out[m] = {"WAPE": float((df[m][ok] - df.actual[ok]).abs().sum() / df.actual[ok].sum()),
                  "MAE": float((df[m][ok] - df.actual[ok]).abs().mean()),
                  "n": int(ok.sum())}
    return pd.DataFrame(out).T


def run_backtests(sales, diwali_weeks):
    # (a) The festive analogue of the real task: the 2025 season, cut off 6 weeks before Diwali week
    #     (exactly where 2026-09-21 sits), plus neighbouring cutoffs. Lifts are learned from 2024 only.
    festive = backtest(sales, diwali_weeks, pd.to_datetime(["2025-08-25", "2025-09-01", "2025-09-08", "2025-09-15"]))
    # (b) Ordinary weeks: every 2nd week of 2026 up to the last full horizon.
    normal = backtest(sales, diwali_weeks, pd.date_range("2025-12-01", "2026-08-24", freq="14D"))
    print("\n== Festive-season backtest (cutoffs 2025-08-25 .. 2025-09-15, 4-week horizon) ==")
    print(score(festive).round(3))
    print("\n   ... analogue cutoff 2025-09-08 only (forecasts weeks -5..-2 before Diwali, like 2026):")
    print(score(festive[festive.origin == "2025-09-08"]).round(3))
    print("\n== Ordinary-weeks backtest (19 cutoffs, Dec 2025 .. Aug 2026) ==")
    print(score(normal).round(3))
    print("\n   ... by SKU type (model WAPE vs best baseline):")
    for kind, d in pd.concat([festive, normal]).groupby("kind"):
        s = score(d)
        print(f"   {kind:13s} model={s.loc['model', 'WAPE']:.3f}  best baseline={s.drop('model').WAPE.min():.3f}  rows={len(d)}")
    worst = pd.concat([festive, normal]).assign(err=lambda x: (x.model - x.actual).abs())
    worst = worst.groupby("sku").apply(lambda d: d.err.sum() / max(d.actual.sum(), 1)).sort_values(ascending=False)
    print("\n   worst SKUs by WAPE:", worst.head(6).round(3).to_dict())
    return festive, normal


def main():
    warnings.simplefilter("ignore", category=FutureWarning)
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true", help="only run the backtest")
    args = ap.parse_args()
    sales, promos, leads, diwali_weeks = load()
    run_backtests(sales, diwali_weeks)
    if args.backtest:
        return
    cutoff = sales.week_start.max()  # 2026-09-21: the last week we have
    model = fit(sales, cutoff, diwali_weeks)
    print("\nfestive multipliers:", {k: round(v, 3) for k, v in model["festive"].items()})
    print(f"pooled promo lift: {model['pooled_promo']:.3f}")
    flags = promos.set_index(["sku", "week_start"]).promo_flag
    rows = []
    for sku in leads.sku:
        sku_flags = [int(flags.get((sku, w), 0)) for w in FORECAST_WEEKS]
        for w, f in zip(FORECAST_WEEKS, predict(model, sku, FORECAST_WEEKS, sku_flags, diwali_weeks)):
            rows.append({"sku": sku, "week_start": w.strftime("%Y-%m-%d"), "forecast_units": round(f, 2)})
    pd.DataFrame(rows).to_csv(OUT, index=False)
    print(f"wrote {OUT} ({len(rows)} rows)")
    assert len(rows) == 160 and all(math.isfinite(r["forecast_units"]) and r["forecast_units"] >= 0 for r in rows)


if __name__ == "__main__":
    main()
