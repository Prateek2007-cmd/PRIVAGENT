"""Mail/Calendar Demo Sandbox — FastAPI + SQLite, port 8126.

Synthetic test data ONLY. All senders are @demo.test, all amounts/dates
are invented test values. Nothing here sends real email or touches a real
calendar. Drafts are stored and NEVER sent (there is no send endpoint).

Auth: every /api/v1/* endpoint except /api/v1/health and
/api/v1/auth/login requires `Authorization: Bearer <demo-token>`.
Demo account: demo@demo.test (any non-empty password; demo only).
Default demo token: "demo-mailcal-token" (override with MAILCAL_DEMO_TOKEN).

Operator control (CONTRACTS2): POST /api/v1/operator/message with header
X-Operator-Token: demo-operator (override with MAILCAL_OPERATOR_TOKEN)
injects a new inbox message, persisted in SQLite and visible to
NAVIGATOR only via its normal HTTP fetch.

Run:  python -m uvicorn app:app --host 127.0.0.1 --port 8126
       (from this directory; requires fastapi + uvicorn, see requirements.txt)
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mailcal.db")
DEMO_TOKEN = os.environ.get("MAILCAL_DEMO_TOKEN", "demo-mailcal-token")
OPERATOR_TOKEN = os.environ.get("MAILCAL_OPERATOR_TOKEN", "demo-operator")
DEMO_ACCOUNT = "demo@demo.test"

app = FastAPI(title="MailCal Demo Sandbox", version="1.0.0")


# ---------------------------------------------------------------- DB ------
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                sender TEXT NOT NULL, sender_name TEXT NOT NULL,
                recipient TEXT NOT NULL, subject TEXT NOT NULL,
                snippet TEXT NOT NULL, body TEXT NOT NULL,
                received_at TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
                is_starred INTEGER NOT NULL DEFAULT 0, labels TEXT NOT NULL DEFAULT '',
                deadline TEXT, deadline_kind TEXT, has_attachments INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                filename TEXT NOT NULL, mime TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, to_addr TEXT NOT NULL, subject TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL,
                end TEXT NOT NULL, location TEXT, description TEXT,
                status TEXT NOT NULL DEFAULT 'confirmed', source TEXT NOT NULL DEFAULT 'seeded',
                invitation_id TEXT
            );
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY, event_id TEXT, sender TEXT NOT NULL,
                title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                location TEXT, status TEXT NOT NULL DEFAULT 'pending',
                message_id TEXT
            );
            """
        )
        if c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0:
            _seed(c)


def _seed(c: sqlite3.Connection) -> None:
    msgs = [
        # (id, thread, sender, name, subject, snippet, body, received, read, deadline, kind, attach)
        ("m1", "t-intern", "careers@demo.test", "Demo Careers",
         "Test Internship — Application deadline 5 Dec 2026 [TEST]",
         "Applications for the test Software Intern role close on 5 Dec 2026.",
         "Hi Demo User,\n\nThis is a SYNTHETIC test message. Applications for the test "
         "Software Intern role (Test Cohort 2027) close on Friday 5 December 2026 at "
         "11:59 PM IST.\n\nTo apply, reply to this test mailbox with your test resume. "
         "No real application exists — this deadline is invented test data for the "
         "NAVIGATOR demo.\n\n— Demo Careers Team (careers@demo.test)",
         "2026-11-25T09:12:00+05:30", 0, "2026-12-05", "application", 1),
        ("m2", "t-intern", "careers@demo.test", "Demo Careers",
         "Re: Test Internship — Application deadline 5 Dec 2026 [TEST]",
         "Reminder: the test application window closes 5 Dec 2026.",
         "Hi Demo User,\n\nQuick reminder about the test internship above — the window "
         "closes on 5 Dec 2026. This is the second message in the thread, sent as a "
         "synthetic reminder. Nothing to do: this is test data.\n\n— Demo Careers Team",
         "2026-11-26T10:05:00+05:30", 1, "2026-12-05", "application", 0),
        ("m3", "t-invite", "events@demo.test", "Demo Events",
         "You're invited: Demo Hackathon 2026 — Sat 12 Dec [TEST]",
         "You are invited to the Demo Hackathon 2026 on Saturday 12 December.",
         "Hi Demo User,\n\nYou are invited to the Demo Hackathon 2026 (a SYNTHETIC test "
         "event) on Saturday 12 December 2026, 09:00–18:00 IST, Demo Hall, Test City.\n\n"
         "Please accept or decline the invitation from the Invitations tab. Accepting "
         "adds it to your demo calendar; declining removes it. No real event exists.\n\n"
         "— Demo Events Team (events@demo.test)",
         "2026-11-24T14:40:00+05:30", 0, "2026-12-12", "event", 0),
        ("m4", "t-bill", "billing@demo.test", "Demo Billing",
         "Your demo utility bill is due 1 Dec 2026 — ₹1,250 (test amount) [TEST]",
         "Test utility bill of ₹1,250 (invented amount) is due 1 Dec 2026.",
         "Hi Demo User,\n\nYour SYNTHETIC test utility bill for November 2026 is ready.\n\n"
         "Amount due (invented test amount): ₹1,250\nDue date (test date): 1 December 2026\n"
         "Account: DEMO-UTIL-0001\n\nDo not pay anything — no real bill, no real biller, "
         "no payment link. This message exists only so the demo can show a bill deadline.\n\n"
         "— Demo Billing (billing@demo.test)",
         "2026-11-23T08:30:00+05:30", 0, "2026-12-01", "bill", 0),
        ("m5", "t-sprint", "team@demo.test", "Demo Team",
         "Sprint 14 test notes — please review [TEST]",
         "Test sprint notes are ready for review before Friday's demo.",
         "Hi Demo User,\n\nThe test notes for Sprint 14 are attached to our shared demo "
         "folder. Please review the three test items before Friday's demo run. All "
         "content is synthetic.\n\n— Demo Team",
         "2026-11-26T16:20:00+05:30", 0, None, None, 0),
        ("m6", "t-order", "noreply@demo.test", "Demo Store",
         "Your test order #DEMO-1042 has shipped [TEST]",
         "Test order #DEMO-1042 (2 test items) has shipped via Demo Courier.",
         "Hi Demo User,\n\nYour SYNTHETIC test order #DEMO-1042 has shipped.\n\nItems (test):\n"
         "- Demo Notebook (test item) x1\n- Demo Pen Set (test item) x1\n\nTracking (test): DEMO-TRACK-1042. Expected test delivery: 30 Nov 2026. "
         "No real order, no real money moved.\n\n— Demo Store",
         "2026-11-22T11:15:00+05:30", 0, None, None, 1),
        ("m7", "t-mentor", "mentor@demo.test", "Demo Mentor",
         "Feedback on your test project draft [TEST]",
         "Good structure overall — see my three test comments inside.",
         "Hi Demo User,\n\nI reviewed your test project draft. Overall structure is good.\n\n"
         "Three test comments:\n1. State the test goal in one line up front.\n"
         "2. Keep the test data table on one page.\n3. Add a one-line test conclusion.\n\n"
         "Reply in a draft when ready — drafts in this sandbox are never sent.\n\n— Demo Mentor",
         "2026-11-21T18:45:00+05:30", 0, None, None, 0),
        ("m8", "t-security", "alerts@demo.test", "Demo Alerts",
         "Security alert (test): new sign-in from Demo City [TEST]",
         "A test sign-in to your demo account was seen from Demo City.",
         "Hi Demo User,\n\nThis is a SYNTHETIC security alert. A sign-in to your demo "
         "account (demo@demo.test) was seen from Demo City on 26 Nov 2026 at 07:58 IST, "
         "device: Demo Browser on Test OS.\n\nIf this was you, no action is needed. If not, "
         "change your demo password — it is only a test credential.\n\n— Demo Alerts",
         "2026-11-26T07:58:00+05:30", 1, None, None, 0),
        ("m9", "t-news", "newsletter@demo.test", "Demo Weekly",
         "Demo Weekly — test edition #48 [TEST]",
         "Test edition: three demo links and one test tip of the week.",
         "Demo Weekly, test edition #48\n\n- Test link 1: how demo sandboxes are built\n"
         "- Test link 2: writing good test data\n- Test link 3: demo calendar tips\n\n"
         "Tip of the week (test): keep test data clearly labelled so nobody mistakes "
         "it for real data.\n\n— Demo Weekly",
         "2026-11-20T09:00:00+05:30", 1, None, None, 0),
        ("m10", "t-welcome", "welcome@demo.test", "Demo Mail",
         "Welcome to the Demo Mailbox [TEST]",
         "Your synthetic demo mailbox is ready — 10 test messages inside.",
         "Hi Demo User,\n\nWelcome to the Demo Mailbox. This inbox contains only synthetic "
         "test messages from @demo.test senders. Use search, threads, drafts and the "
         "calendar to try the demo. Nothing here is real and nothing leaves this sandbox.\n\n"
         "— Demo Mail",
         "2026-11-20T08:00:00+05:30", 1, None, None, 0),
    ]
    for (mid, tid, sender, name, subj, snip, body, recv, read, dl, kind, att) in msgs:
        c.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, tid, sender, name, DEMO_ACCOUNT, subj, snip, body, recv, read, 0, "", dl, kind, att),
        )
    c.executemany(
        "INSERT INTO attachments VALUES (?,?,?,?,?)",
        [
            ("a1", "m1", "internship-role-test.txt", "text/plain",
             "TEST FILE — SYNTHETIC\nRole: Software Intern (Test Cohort 2027)\n"
             "Test stipend: ₹10,000/month (invented)\nTest deadline: 5 Dec 2026\n"
             "This file is synthetic test data for the NAVIGATOR demo.\n"),
            ("a2", "m6", "packing-slip-test.txt", "text/plain",
             "TEST PACKING SLIP — SYNTHETIC\nOrder: #DEMO-1042\n"
             "Demo Notebook x1\nDemo Pen Set x1\nNo real order. No real shipment.\n"),
        ],
    )
    c.execute(
        "INSERT INTO drafts VALUES (?,?,?,?,?,?)",
        ("d1", "mentor@demo.test", "Re: Feedback on your test project draft [DRAFT — never sent]",
         "Hi Demo Mentor,\n\nThanks for the test feedback — I will apply the three "
         "comments and share a revised test draft.\n\n— Demo User",
         "2026-11-26T17:00:00+05:30", "2026-11-26T17:00:00+05:30"),
    )
    c.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("e1", "Demo team standup", "2026-11-27T10:00:00+05:30", "2026-11-27T10:30:00+05:30",
             "Demo Room 1", "Daily test standup (synthetic).", "confirmed", "seeded", None),
            ("e2", "Demo Hackathon 2026", "2026-12-12T09:00:00+05:30", "2026-12-12T18:00:00+05:30",
             "Demo Hall, Test City", "Synthetic test hackathon — see invitation inv1.",
             "tentative", "invitation", "inv1"),
            ("e3", "Test project review with mentor", "2026-11-28T15:00:00+05:30", "2026-11-28T16:00:00+05:30",
             "Demo Room 2", "Review the test project draft (synthetic).", "confirmed", "seeded", None),
            ("e4", "Demo Hackathon prep call", "2026-12-10T17:00:00+05:30", "2026-12-10T17:30:00+05:30",
             "Demo Meet (test link)", "Prep call for the synthetic hackathon.", "confirmed", "seeded", None),
            ("e5", "Family dinner (test)", "2026-11-29T19:00:00+05:30", "2026-11-29T20:00:00+05:30",
             "Home (test)", "Synthetic personal event.", "confirmed", "seeded", None),
        ],
    )
    c.executemany(
        "INSERT INTO invitations VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("inv1", "e2", "events@demo.test", "Demo Hackathon 2026",
             "2026-12-12T09:00:00+05:30", "2026-12-12T18:00:00+05:30",
             "Demo Hall, Test City", "pending", "m3"),
            ("inv2", None, "webinars@demo.test", "Demo webinar: Intro to Testing",
             "2026-12-03T18:00:00+05:30", "2026-12-03T19:00:00+05:30",
             "Demo Meet (test link)", "pending", None),
        ],
    )


def _row_to_message(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "thread_id": r["thread_id"], "from": r["sender"],
        "sender_name": r["sender_name"], "to": r["recipient"],
        "subject": r["subject"], "snippet": r["snippet"],
        "received_at": r["received_at"], "is_read": bool(r["is_read"]),
        "is_starred": bool(r["is_starred"]),
        "labels": [x for x in (r["labels"] or "").split(",") if x],
        "deadline": r["deadline"], "deadline_kind": r["deadline_kind"],
        "has_attachments": bool(r["has_attachments"]),
    }


def _row_to_event(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "title": r["title"], "start": r["start"], "end": r["end"],
            "location": r["location"], "description": r["description"],
            "status": r["status"], "source": r["source"],
            "invitation_id": r["invitation_id"]}


def _row_to_invitation(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "event_id": r["event_id"], "from": r["sender"],
            "title": r["title"], "start": r["start"], "end": r["end"],
            "location": r["location"], "status": r["status"],
            "message_id": r["message_id"]}


# ---------------------------------------------------------------- auth -----
def _require_auth(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != DEMO_TOKEN:
        raise HTTPException(status_code=401, detail="invalid demo token")
    return DEMO_ACCOUNT


class LoginBody(BaseModel):
    email: str
    password: str = ""


class DraftBody(BaseModel):
    to: str = Field(..., min_length=3, max_length=200)
    subject: str = Field(..., min_length=1, max_length=300)
    body: str = Field(..., min_length=1, max_length=20000)


class EventBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    start: str = Field(..., min_length=10, max_length=40)
    end: str = Field(..., min_length=10, max_length=40)
    location: str = Field("", max_length=300)
    description: str = Field("", max_length=5000)


class RespondBody(BaseModel):
    status: str  # accepted | declined


class OperatorMessageBody(BaseModel):
    sender: str = Field("operator@demo.test", max_length=200)
    sender_name: str = Field("Demo Operator", max_length=200)
    subject: str = Field(..., min_length=1, max_length=300)
    body: str = Field(..., min_length=1, max_length=20000)
    deadline: str | None = None
    deadline_kind: str | None = None


def _ok(**extra):
    return {"ok": True, **extra}


def _err(code: int, error: str):
    raise HTTPException(status_code=code, detail=error)


# ----------------------------------------------------------------- API -----
@app.on_event("startup")
def _startup():
    _init_db()


@app.get("/api/v1/health")
def health():
    _init_db()
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return _ok(service="mailcal-demo-sandbox", account=DEMO_ACCOUNT, messages=n,
                sandbox=True, note="Demo Sandbox — not the real site. All data synthetic.")


@app.post("/api/v1/auth/login")
def login(body: LoginBody):
    if body.email.strip().lower() != DEMO_ACCOUNT or not body.password:
        _err(401, "invalid demo credentials (use demo@demo.test with any non-empty password)")
    return _ok(token=DEMO_TOKEN, account=DEMO_ACCOUNT,
                note="Demo Sandbox token — works only against this sandbox.")


@app.get("/api/v1/auth/me")
def me(account: str = Depends(_require_auth)):
    return _ok(account=account, provider="demo-sandbox")


@app.get("/api/v1/messages")
def list_messages(unread: bool = False, q: str = "", limit: int = 50,
                  account: str = Depends(_require_auth)):
    limit = min(max(limit, 1), 100)
    sql = "SELECT * FROM messages"
    clauses, params = [], []
    if unread:
        clauses.append("is_read = 0")
    if q:
        clauses.append("(subject LIKE ? OR snippet LIKE ? OR body LIKE ? OR sender LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY received_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
        total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        unread_n = c.execute("SELECT COUNT(*) FROM messages WHERE is_read = 0").fetchone()[0]
    return _ok(messages=[_row_to_message(r) for r in rows],
                total=total, unread_count=unread_n)


@app.get("/api/v1/messages/{message_id}")
def get_message(message_id: str, account: str = Depends(_require_auth)):
    with _conn() as c:
        r = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not r:
            _err(404, f"message {message_id} not found")
        atts = c.execute(
            "SELECT id, filename, mime FROM attachments WHERE message_id = ?",
            (message_id,)).fetchall()
    msg = _row_to_message(r)
    msg["body"] = r["body"]
    msg["attachments"] = [{"id": a["id"], "filename": a["filename"], "mime": a["mime"]}
                          for a in atts]
    return _ok(message=msg)


@app.post("/api/v1/messages/{message_id}/read")
def mark_read(message_id: str, account: str = Depends(_require_auth)):
    with _conn() as c:
        cur = c.execute("UPDATE messages SET is_read = 1 WHERE id = ?", (message_id,))
        if cur.rowcount == 0:
            _err(404, f"message {message_id} not found")
    return _ok(message_id=message_id, is_read=True)


@app.get("/api/v1/threads/{thread_id}")
def get_thread(thread_id: str, account: str = Depends(_require_auth)):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY received_at ASC",
            (thread_id,)).fetchall()
    if not rows:
        _err(404, f"thread {thread_id} not found")
    out = []
    for r in rows:
        m = _row_to_message(r)
        m["body"] = r["body"]
        out.append(m)
    return _ok(thread_id=thread_id, messages=out)


@app.get("/api/v1/messages/{message_id}/attachments/{attachment_id}/download")
def download_attachment(message_id: str, attachment_id: str,
                        account: str = Depends(_require_auth)):
    with _conn() as c:
        r = c.execute(
            "SELECT filename, content FROM attachments WHERE id = ? AND message_id = ?",
            (attachment_id, message_id)).fetchone()
    if not r:
        _err(404, "attachment not found")
    return PlainTextResponse(
        r["content"],
        headers={"Content-Disposition": f'attachment; filename="{r["filename"]}"'})


@app.get("/api/v1/drafts")
def list_drafts(account: str = Depends(_require_auth)):
    with _conn() as c:
        rows = c.execute("SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()
    return _ok(drafts=[{"id": r["id"], "to": r["to_addr"], "subject": r["subject"],
                        "body": r["body"], "created_at": r["created_at"],
                        "updated_at": r["updated_at"]} for r in rows],
                note="Drafts are stored only. This sandbox has no send endpoint — drafts are never sent.")


@app.post("/api/v1/drafts", status_code=201)
def create_draft(body: DraftBody, account: str = Depends(_require_auth)):
    now = datetime.now(timezone.utc).isoformat()
    did = "d-" + uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?)",
                  (did, body.to, body.subject, body.body, now, now))
    return _ok(draft={"id": did, "to": body.to, "subject": body.subject,
                      "body": body.body, "created_at": now, "updated_at": now},
                note="Draft saved. It was NOT sent — this sandbox cannot send mail.")


@app.delete("/api/v1/drafts/{draft_id}")
def delete_draft(draft_id: str, account: str = Depends(_require_auth)):
    with _conn() as c:
        cur = c.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        if cur.rowcount == 0:
            _err(404, f"draft {draft_id} not found")
    return _ok(deleted=draft_id)


@app.get("/api/v1/events")
def list_events(start: str = "", end: str = "", account: str = Depends(_require_auth)):
    sql, params = "SELECT * FROM events", []
    clauses = []
    if start:
        clauses.append("end >= ?")
        params.append(start)
    if end:
        clauses.append("start <= ?")
        params.append(end)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY start ASC"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return _ok(events=[_row_to_event(r) for r in rows])


@app.get("/api/v1/events/{event_id}")
def get_event(event_id: str, account: str = Depends(_require_auth)):
    with _conn() as c:
        r = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not r:
        _err(404, f"event {event_id} not found")
    return _ok(event=_row_to_event(r))


@app.post("/api/v1/events", status_code=201)
def create_event(body: EventBody, account: str = Depends(_require_auth)):
    if body.start >= body.end:
        _err(400, "event end must be after start")
    eid = "e-" + uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                  (eid, body.title, body.start, body.end, body.location,
                   body.description, "confirmed", "created", None))
    return _ok(event={"id": eid, "title": body.title, "start": body.start,
                      "end": body.end, "location": body.location,
                      "description": body.description, "status": "confirmed",
                      "source": "created", "invitation_id": None})


@app.get("/api/v1/invitations")
def list_invitations(status: str = "", account: str = Depends(_require_auth)):
    sql = "SELECT * FROM invitations"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY start ASC"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return _ok(invitations=[_row_to_invitation(r) for r in rows])


@app.post("/api/v1/invitations/{invitation_id}/respond")
def respond_invitation(invitation_id: str, body: RespondBody,
                       account: str = Depends(_require_auth)):
    if body.status not in ("accepted", "declined"):
        _err(400, "status must be 'accepted' or 'declined'")
    with _conn() as c:
        inv = c.execute("SELECT * FROM invitations WHERE id = ?",
                        (invitation_id,)).fetchone()
        if not inv:
            _err(404, f"invitation {invitation_id} not found")
        c.execute("UPDATE invitations SET status = ? WHERE id = ?",
                  (body.status, invitation_id))
        if inv["event_id"]:
            new_status = "confirmed" if body.status == "accepted" else "cancelled"
            c.execute("UPDATE events SET status = ? WHERE id = ?",
                      (new_status, inv["event_id"]))
        elif body.status == "accepted":
            eid = "e-" + uuid.uuid4().hex[:10]
            c.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, inv["title"], inv["start"], inv["end"], inv["location"],
                 "Accepted from invitation " + invitation_id, "confirmed",
                 "invitation", invitation_id))
            c.execute("UPDATE invitations SET event_id = ? WHERE id = ?",
                      (eid, invitation_id))
    with _conn() as c:
        inv2 = c.execute("SELECT * FROM invitations WHERE id = ?",
                         (invitation_id,)).fetchone()
    return _ok(invitation=_row_to_invitation(inv2))


@app.post("/api/v1/operator/message", status_code=201)
def operator_message(body: OperatorMessageBody, request: Request):
    token = request.headers.get("X-Operator-Token", "")
    if token != OPERATOR_TOKEN:
        _err(401, "invalid operator token")
    now = datetime.now(timezone.utc).isoformat()
    mid = "m-" + uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, "t-operator", body.sender, body.sender_name, DEMO_ACCOUNT,
             body.subject, body.body[:200], body.body, now, 0, 0, "",
             body.deadline, body.deadline_kind, 0))
    return _ok(message_id=mid, note="Operator message injected; visible via normal inbox fetch.")


# -------------------------------------------------------------- frontend ---
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    _init_db()
    uvicorn.run(app, host="127.0.0.1", port=8126)
