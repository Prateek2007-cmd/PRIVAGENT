# Demo Sandbox — Travel booking

Fictional travel booking app for the NAVIGATOR demo (2026-11-27). **Demo Sandbox —
not the real site.** All operators, services, prices and seats are synthetic.
No real payment: checkout always returns `402 payment_disabled` and the booking
stays `PREPARED`. No real credentials: demo login is username `demo`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8124
# open http://127.0.0.1:8124/
```

SQLite DB `travel.db` is created in this directory on first start (seeded with
16 train/bus options across 12 Indian cities).

## API (all under /api/v1/, every response includes `"ok"`)

- `GET /api/v1/health`, `GET /api/v1/cities?q=`
- `GET /api/v1/search?from_code=HYD&to_code=BLR&date=2026-12-05&mode=&sort=price|departure&max_price=&dep_after=`
- `GET /api/v1/options/{id}?date=2026-12-05` (includes seat map)
- `POST /api/v1/holds` `{option_id, date, seats:[...]}` → booking `HELD`, seats decrement
- `POST /api/v1/bookings/{id}/passenger` `{name, age, gender, email, phone}`
- `GET /api/v1/bookings/{id}`
- `POST /api/v1/bookings/{id}/checkout` → **402** `{"ok": false, "error": "payment_disabled", "state": "PREPARED"}`
- `POST /api/v1/auth/login` `{username: "demo"}` → synthetic demo session
- Operator (header `X-Operator-Token: demo-operator`):
  - `POST /api/v1/operator/fare` `{option_id, date, new_fare}`
  - `POST /api/v1/operator/availability` `{option_id, date, seats_available}`
  - Changes persist in SQLite and are visible via normal GET endpoints only.
