# Mail/Calendar Demo Sandbox

**Demo Sandbox — not the real site.** Synthetic test data only. All senders
are `@demo.test`; all deadlines, amounts and events are invented test values.
No real email account is connected, nothing is ever sent, and there is no
send endpoint at all.

## Run

```bash
cd demo-sandboxes/mailcal
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8126
```

Then open http://127.0.0.1:8126/ and sign in as `demo@demo.test` with any
non-empty password (demo only). The SQLite database `mailcal.db` is created
on first start and seeded with 10 test messages (6 unread), 1 draft,
5 calendar events and 2 pending invitations. Delete `mailcal.db` to reseed.

## Auth

Every `/api/v1/*` endpoint except `/health` and `/auth/login` requires:

```
Authorization: Bearer demo-mailcal-token
```

Override with env `MAILCAL_DEMO_TOKEN`. Operator injection uses header
`X-Operator-Token: demo-operator` (env `MAILCAL_OPERATOR_TOKEN`).

## API (all responses include `{"ok": true, ...}`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health` | no auth; message count |
| POST | `/api/v1/auth/login` | `{email, password}` → token (demo account only) |
| GET | `/api/v1/auth/me` | demo account |
| GET | `/api/v1/messages?unread=true&q=&limit=` | inbox list / search |
| GET | `/api/v1/messages/{id}` | message + body + attachment metadata |
| POST | `/api/v1/messages/{id}/read` | mark read |
| GET | `/api/v1/threads/{thread_id}` | all messages in thread, oldest first |
| GET | `/api/v1/messages/{id}/attachments/{att_id}/download` | synthetic text file |
| GET/POST | `/api/v1/drafts` | list / create (stored only, **never sent**) |
| DELETE | `/api/v1/drafts/{id}` | |
| GET/POST | `/api/v1/events?start=&end=` | list / create (validated: end > start) |
| GET | `/api/v1/events/{id}` | |
| GET | `/api/v1/invitations?status=` | |
| POST | `/api/v1/invitations/{id}/respond` | `{status: accepted\|declined}` — updates invitation + linked event; accepting an invitation with no event creates one |
| POST | `/api/v1/operator/message` | operator injects a message (X-Operator-Token) |

## Test data

- 3 messages carry deadlines: test internship application (2026-12-05),
  test event invitation (2026-12-12), test utility bill ₹1,250 (2026-12-01).
- Thread `t-intern` has 2 messages; attachments are synthetic `.txt` files.
- Seeded draft + 2 pending invitations (one linked to event `e2`, one
  standalone — accepting it creates a new calendar event).

## What this is NOT

- Not Gmail, not Google Calendar, not Outlook. Real Gmail/Calendar remain
  **BLOCKED** without a configured test account — this sandbox proves the
  connector + demo flow against realistic data, nothing more.
- No payment, no sending, no real accounts.
