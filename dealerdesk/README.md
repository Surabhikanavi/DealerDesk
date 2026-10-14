# VIKMO DealerDesk

**Track: `aiml`** (AI / ML Intern)

| | |
|---|---|
| LLM provider | **Groq** — key read from `GROQ_API_KEY` |
| Model | **`openai/gpt-oss-120b`** (temperature 0, tool calling). Automatic fallback if a model is unavailable: `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`, `llama-3.3-70b-versatile`. Override with `GROQ_MODEL` / `GROQ_FALLBACK_MODELS`. |
| Video walkthrough | _add your Loom / YouTube (unlisted) / Drive link here_ |

## What's built

| Part | Status |
|---|---|
| A1 · Backend core (products, inventory, dealers, draft orders, error shape, pagination) | Required — done |
| C · Dealer Assistant (Groq tool calling, retrieval, grounding guard, session memory, injection defence) | Required — done |
| D · Forecasting (`ml/forecast.csv`, `/api/forecast/{sku}/`, `/api/reorder-suggestions/`) | Required — done |
| A2 · Order engine (status flow, all-or-nothing stock, row locks, exact GST, order numbers, idempotency) | Optional — **attempted, complete** |
| B · Supplier sync (pagination, 429/5xx retries, all-or-nothing, duplicates, idempotent) | Optional — **attempted, complete** |
| E · Frontend: dealer portal at `http://localhost:8000/` (catalogue + live stock, cart → draft → confirm with stock errors, order detail with GST and valid actions, assistant chat, reorder view) | Optional — **attempted** (plain HTML + JS, served by Django, talks only to the API) |

Design decisions and the 8 track questions are in [DECISIONS.md](DECISIONS.md).

## Layout

```
manage.py, requirements.txt      run contract (repo root)
dealerdesk/                      settings (env-driven, PostgreSQL only), root URLs
core/                            health, error shape, pagination, money helpers, seed_data, portal page
catalog/                         Product, Inventory, StockMovement, Dealer + their APIs
orders/                          Order, OrderItem, DailyOrderSequence, IdempotencyRecord; services.py = order engine
supplier/                        supplier sync (client with retries + validate/dedupe/apply)
assistant/                       Dealer Assistant: agent.py (loop + grounding check), tools.py, retrieval.py, safety.py, llm.py
forecasting/                     forecast + reorder endpoints (contract formula, live stock)
ml/forecast.py                   forecasting model + backtest; writes ml/forecast.csv (committed)
data/                            the provided data, unchanged
supplier_mock/, smoke_test.py    from the assignment bundle
```

## Setup

Requires Python 3.11+ and PostgreSQL.

```bash
python -m venv .venv && source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # set DATABASE_URL and GROQ_API_KEY
python manage.py migrate --noinput
python manage.py seed_data                               # idempotent: safe to run again
python manage.py runserver 0.0.0.0:8000 --noreload
```

Environment variables: `DATABASE_URL` (PostgreSQL), `GROQ_API_KEY`, `SUPPLIER_API_URL`, `SUPPLIER_API_KEY`. Optional: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=1`, `GROQ_MODEL`, `GROQ_FALLBACK_MODELS`. A real environment variable always wins over `.env`.

Supplier sync against the bundled mock:

```bash
python supplier_mock/mock_supplier.py            # then POST /api/sync/supplier/
python supplier_mock/mock_supplier.py --outage   # sync returns 502 SUPPLIER_UNAVAILABLE and changes nothing
```

## Tests

```bash
python manage.py test                # 35 tests; needs a PostgreSQL user that may create the test database
python smoke_test.py --base-url http://localhost:8000 --track aiml     # or --all (start the supplier mock first)
python ml/forecast.py --backtest     # forecast validation against the three baselines
python ml/forecast.py                # backtest, then regenerate ml/forecast.csv
```

The tests cover:
- **Order engine:** per-line GST half-up (including a case where banker's rounding differs), validation, the price lock, all-or-nothing confirm listing every short line, transitions, and idempotency. Real-thread tests on PostgreSQL check that parallel confirms never oversell, parallel creates get unique numbers, parallel duplicate idempotent requests create one order, and parallel cancels restore stock once.
- **Supplier sync**, with a fake supplier: retries, the 5-attempt cap, outage leaving everything unchanged, invalid items, duplicates resolved by latest `updated_at`, idempotent re-runs, and stock untouched.
- **Reorder formula**, against an independent recomputation with live stock.
- **Assistant**, with a scripted fake LLM so it runs deterministically without a key: vehicle aliases, the clarification note, descriptions never reaching the model, tool-call reporting, correction of ungrounded figures, an injected promo code never returned, drafts going through the API rules for the request's dealer, and session isolation.

Smoke test result on a fresh database (migrate, then seed twice): **12/12 checks pass with `--all`**.

## Assumptions

- **Seeding** inserts only missing products and dealers (keyed by SKU / dealer code), so a second run is a no-op and never resets live stock. Dealers are inserted in file order, so a fresh database gets ids 1–25 for DLR-001…DLR-025.
- **Product API:** `PATCH /api/products/{sku}/` with a different `sku`, or any `stock`, is `400`; stock changes only through `PUT /api/inventory/{sku}/`. `POST /api/products/` ignores a `stock` field (new products start at 0).
- **Inventory:** `quantity` must be a JSON integer; `"10"`, `2.5` and booleans are rejected.
- **Idempotency:** a replay with the same key and body returns the original order with `200`; the first request returns `201`. Keys are global and limited to 255 characters.
- **Order list** is newest first. An unknown `status` filter value or a non-numeric `dealer_id` filter returns `400`.
- **Dealer Assistant:**
  - `dealer_id` is required and must exist (`400` otherwise).
  - Conversation memory is keyed by `(session_id, dealer_id)`.
  - If Groq is unreachable after retries, the endpoint returns `503 ASSISTANT_UNAVAILABLE` rather than an unverified answer.
- **Retrieval** uses lexical TF-IDF plus structured vehicle resolution instead of embeddings; the reasons are in DECISIONS.md §2.
- **Forecast and reorder data** (`ml/forecast.csv`, `data/lead_times.csv`, `data/sales_history.csv`) are read-only files, so they are read from disk and cached. Only stock, which changes during the day, is read live from the database.
- **Reorder numbers:** `lead_time_demand` and `reorder_point` are returned as JSON numbers computed exactly from the CSV's decimal values.
- **Django** `DEBUG` defaults to off, so unknown URLs and server errors also use the JSON error shape.
