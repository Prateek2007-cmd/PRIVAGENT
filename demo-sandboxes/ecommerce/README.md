# ShopSphere Demo Sandbox — E-commerce (NAVIGATOR demo2, stream S3)

Original branding only: **ShopSphere Demo Sandbox — not the real site.**
Synthetic catalogue, no real purchases, no real payments.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8125
# open http://127.0.0.1:8125/
```

SQLite DB `ecommerce.db` is created/seeded in this directory on first start
(14 products, 4 sellers each, price history, cart, orders).

## API (`/api/v1/`, every response carries `ok`)

- `GET /api/v1/health`, `GET /api/v1/categories`
- `GET /api/v1/products?q=&category=` — search by name/category/description
- `GET /api/v1/products/{id}` — detail + sellers + per-seller price history
- `GET /api/v1/cart?session_id=` · `POST /api/v1/cart/add` · `POST /api/v1/cart/update`
- `POST /api/v1/checkout` — **payment disabled**: HTTP 402,
  `{"ok": false, "error": "payment_disabled", "state": "PREPARED"}`

## Operator control (for NAVIGATOR monitoring demo)

Header `X-Operator-Token: demo-operator`:

```bash
curl -X POST localhost:8125/api/v1/operator/price \
  -H 'X-Operator-Token: demo-operator' -H 'Content-Type: application/json' \
  -d '{"seller_id": 1, "price": 4999}'
curl -X POST localhost:8125/api/v1/operator/stock \
  -H 'X-Operator-Token: demo-operator' -H 'Content-Type: application/json' \
  -d '{"seller_id": 1, "stock": 2}'
```

Changes persist in SQLite and are visible only via the normal GET endpoints —
NAVIGATOR's HTTP monitoring fetch sees the new price; there is no injected
notification and no DB access for NAVIGATOR.

## Classification

VERIFIED IN REALISTIC SANDBOX (see `../../S3-REPORT.md` in worktrees2).
Do NOT claim a real store, real prices, or a real purchase.
