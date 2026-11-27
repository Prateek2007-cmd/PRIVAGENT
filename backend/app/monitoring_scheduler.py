"""Bounded, non-blocking persistent mission scheduler (UTC cron / interval)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import time
import uuid

from . import db
from .hub import hub

logger = logging.getLogger(__name__)

# Stream C: India Standard Time is the user's timezone (UTC+5:30,
# no daylight saving). Contract §3/§6: store UTC in the DB, display
# IST in the UI with the offset stated. Every user-facing time in this
# stream goes through format_ist.
IST = timezone(timedelta(hours=5, minutes=30))


def format_ist(ts: float | None) -> str | None:
    """Epoch seconds -> 'Fri 27 Nov 2026, 2:30 PM IST' (None stays None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), IST).strftime("%a %d %b %Y, %-I:%M %p IST")


def format_ist_short(ts: float | None) -> str | None:
    """Epoch seconds -> '27 Nov 2026, 2:30 PM IST' (None stays None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), IST).strftime("%d %b %Y, %-I:%M %p IST")


# Stream C: additive schema for the notification outbox + job outcomes
# + alert cooldown. db.py is NOT owned by this stream, so the columns
# / table are added idempotently here (PRAGMA check + ALTER TABLE) the
# first time any Stream C path touches a DB. Nothing existing is
# altered; a DB without these columns keeps working (callers read them
# defensively).
_STREAM_C_COLUMNS = {
    "monitoring_events": [("delivered_at", "REAL")],
    "monitoring_missions": [("alert_cooldown_seconds", "INTEGER NOT NULL DEFAULT 60")],
    "scheduled_jobs": [
        ("last_outcome", "TEXT"),
        ("last_error", "TEXT"),
        ("last_run_outcome_at", "REAL"),
    ],
}
_SCHEMA_DONE: set[str | None] = set()


def ensure_stream_c_schema(db_path: str | None = None) -> None:
    """Create the notification outbox table and add Stream C columns.

    Idempotent and safe to call on every entry point. The outbox holds
    notifications that are NOT monitoring events (e.g. a scheduled job
    blocked on confirmation/takeover) AND — Stream A — one durable
    row per monitoring alert (kind ``monitor``, ref_id = the event
    id), so a monitor alert has a notification-outbox row the
    reconnect pull returns even when no socket was ever connected.
    ``list_unread_events`` / ``count_unread_events`` deduplicate the
    outbox monitor row against its monitoring_events row, so one
    alert is never listed or counted twice.
    """
    if db_path in _SCHEMA_DONE:
        return
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
            diff_data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
            delivered_at REAL, acknowledged INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_unread ON notification_outbox(acknowledged, created_at DESC)")
        for table, columns in _STREAM_C_COLUMNS.items():
            try:
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except Exception:
                continue
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    _SCHEMA_DONE.add(db_path)


def post_outbox_notification(kind: str, ref_id: str, title: str, summary: str,
                             *, diff_data: dict | None = None,
                             db_path: str | None = None) -> str:
    """Persist a non-monitor notification (scheduled-job blocked, ...).

    Returns the outbox row id. Delivery follows the same rule as
    monitoring events: not delivered until a client acknowledges it or
    the missed-event pull returns it.
    """
    ensure_stream_c_schema(db_path)
    now = time.time()
    note_id = str(uuid.uuid4())
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO notification_outbox
               (id, kind, ref_id, title, summary, diff_data, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (note_id, kind, ref_id, title, summary,
             json.dumps(diff_data or {}), now),
        )
    return note_id


async def broadcast_outbox_notification(note_id: str, *, db_path: str | None = None) -> None:
    """Push one outbox notification to live WS clients (best-effort).

    The row stays unread until acknowledged or pulled, so a closed
    frontend loses nothing — the pull is the durable channel.
    """
    from starlette.websockets import WebSocketDisconnect, WebSocketState

    ensure_stream_c_schema(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM notification_outbox WHERE id=?", (note_id,)
        ).fetchone()
    if row is None:
        return
    envelope = {
        "type": "SCHEDULED_JOB_BLOCKED" if row["kind"] == "scheduled_blocked" else "NOTIFICATION",
        "kind": row["kind"],
        "ref_id": row["ref_id"],
        "title": row["title"],
        "message": row["summary"],
        "event_id": row["id"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(row["created_at"]))),
    }
    for ws in list(hub.client_sockets):
        if getattr(ws, "client_state", None) == WebSocketState.DISCONNECTED or getattr(ws, "application_state", None) == WebSocketState.DISCONNECTED:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
            continue
        try:
            await ws.send_json(envelope)
        except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, OSError):
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
        except Exception:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None


def list_unread_events(*, since: float | None = None, unread_only: bool = False,
                       limit: int = 100, db_path: str | None = None) -> list[dict]:
    """Unified missed-event pull: monitoring events + outbox rows.

    Sorted newest first. Each row carries kind ('monitor' | outbox
    kind), acknowledged, notification_sent/delivered_at. Callers that
    surface these rows should mark them read via mark_events_read, or
    delivered via mark_events_delivered.
    """
    ensure_stream_c_schema(db_path)
    events: list[dict] = []
    with db.get_connection(db_path) as conn:
        query = ("SELECT id, mission_id, task_id, detected_at, title, summary, diff_data,"
                 " notification_sent, acknowledged, state_hash FROM monitoring_events")
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("detected_at >= ?")
            params.append(since)
        if unread_only:
            clauses.append("acknowledged = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY detected_at DESC LIMIT ?"
        params.append(limit)
        try:
            delivered = {r[1] for r in conn.execute("PRAGMA table_info(monitoring_events)").fetchall()}
            has_delivered = "delivered_at" in delivered
        except Exception:
            has_delivered = False
        for r in conn.execute(query, params).fetchall():
            d = dict(r)
            try:
                d["diff_data"] = json.loads(d.get("diff_data") or "{}")
            except Exception:
                d["diff_data"] = {}
            d["kind"] = "monitor"
            d["created_at"] = d.pop("detected_at")
            if has_delivered:
                row2 = conn.execute("SELECT delivered_at FROM monitoring_events WHERE id=?", (d["id"],)).fetchone()
                d["delivered_at"] = row2["delivered_at"] if row2 else None
            else:
                d["delivered_at"] = None
            events.append(d)
        # Outbox rows (scheduled-job blocked, ...).
        query2 = ("SELECT id, kind, ref_id, title, summary, diff_data, created_at,"
                  " delivered_at, acknowledged FROM notification_outbox")
        clauses2: list[str] = []
        params2: list = []
        if since is not None:
            clauses2.append("created_at >= ?")
            params2.append(since)
        if unread_only:
            clauses2.append("acknowledged = 0")
        if clauses2:
            query2 += " WHERE " + " AND ".join(clauses2)
        query2 += " ORDER BY created_at DESC LIMIT ?"
        params2.append(limit)
        for r in conn.execute(query2, params2).fetchall():
            d = dict(r)
            try:
                d["diff_data"] = json.loads(d.get("diff_data") or "{}")
            except Exception:
                d["diff_data"] = {}
            # Stream A: an outbox row of kind "monitor" is the
            # durable notification record of a monitoring event
            # already listed above (same ref_id = event id). List
            # the alert once — skip the duplicate row.
            if d.get("kind") == "monitor" and any(
                e.get("id") == d.get("ref_id") for e in events
            ):
                continue
            d["mission_id"] = d.pop("ref_id")
            d["task_id"] = None
            d["notification_sent"] = 1 if d.get("delivered_at") else 0
            d["state_hash"] = ""
            events.append(d)
    events.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    return events[:limit]


def mark_events_delivered(event_ids: list[str], *, db_path: str | None = None) -> None:
    """Mark events delivered: the missed-event pull returned them.

    Contract §3: an event is not 'delivered' until a client
    acknowledges it OR the pull returns it. This is the pull half.
    """
    if not event_ids:
        return
    ensure_stream_c_schema(db_path)
    now = time.time()
    with db.get_connection(db_path) as conn:
        for event_id in event_ids:
            try:
                conn.execute(
                    "UPDATE monitoring_events SET notification_sent=1, delivered_at=? WHERE id=?",
                    (now, event_id),
                )
            except Exception:
                try:
                    conn.execute(
                        "UPDATE monitoring_events SET notification_sent=1 WHERE id=?",
                        (event_id,),
                    )
                except Exception:
                    pass
            conn.execute(
                "UPDATE notification_outbox SET delivered_at=? WHERE id=?",
                (now, event_id),
            )


def mark_event_read(event_id: str, *, db_path: str | None = None) -> bool:
    """Mark one event (monitor or outbox) acknowledged/read."""
    ensure_stream_c_schema(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE monitoring_events SET acknowledged=1 WHERE id=?", (event_id,)
        )
        if cur.rowcount:
            return True
        cur = conn.execute(
            "UPDATE notification_outbox SET acknowledged=1 WHERE id=?", (event_id,)
        )
        return bool(cur.rowcount)


def count_unread_events(*, db_path: str | None = None) -> int:
    ensure_stream_c_schema(db_path)
    with db.get_connection(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM monitoring_events WHERE acknowledged=0"
        ).fetchone()[0]
        # Stream A: outbox rows of kind "monitor" mirror a
        # monitoring event (same alert) — they are counted via the
        # events table above, never twice.
        m = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE acknowledged=0 AND kind != 'monitor'"
        ).fetchone()[0]
    return int(n) + int(m)


# A watch is not killed by one bad tick. A transient failure (DNS,
# connect timeout, a site hiccup) keeps the mission active so it
# retries on its normal cadence; only a permanent refusal, or this
# many transient failures in a row, ends it — and the end is recorded
# as a monitoring event so the user can see the watch died, instead
# of it vanishing silently (claim_due only ever selects 'active').
MAX_CONSECUTIVE_MONITOR_FAILURES = 5


def _is_transient_refusal(reason: str | None) -> bool:
    """Only a DNS-resolution refusal is transient; every other URL
    refusal (malformed URL, disallowed scheme, private/internal
    host) is a stable fact about the URL."""
    return bool(reason) and "does not resolve" in reason


def _record_monitor_failure(mission_id: str, reason: str, *, permanent: bool = False) -> tuple[str, dict | None]:
    """Record one failed check tick. Returns (status, death_event).

    Transient: count rises, mission stays 'active' and retries next
    tick. Permanent, or MAX_CONSECUTIVE_MONITOR_FAILURES transient
    failures in a row: status becomes 'failed' and a monitoring
    event is recorded describing why the watch stopped."""
    now = time.time()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
        prior = int(row["consecutive_failures"] or 0) if row else 0
        count = 0 if permanent else prior + 1
        if permanent or count >= MAX_CONSECUTIVE_MONITOR_FAILURES:
            conn.execute(
                "UPDATE monitoring_missions SET status='failed', last_run_at=?, consecutive_failures=? WHERE id=?",
                (now, count, mission_id),
            )
            event = {
                "id": str(uuid.uuid4()), "mission_id": mission_id, "task_id": None,
                "detected_at": now, "title": "Monitoring stopped",
                "summary": f"Watch stopped after {count} consecutive failed check(s): {reason}",
                "diff_data": {"reason": reason}, "state_hash": "",
            }
            conn.execute(
                """INSERT INTO monitoring_events (id, mission_id, detected_at, title, summary, diff_data, state_hash)
                   VALUES (?,?,?,?,?,?,?)""",
                (event["id"], mission_id, now, event["title"], event["summary"],
                 json.dumps(event["diff_data"]), ""),
            )
            return "failed", event
        conn.execute(
            "UPDATE monitoring_missions SET last_run_at=?, consecutive_failures=? WHERE id=?",
            (now, count, mission_id),
        )
        return "active", None


def _record_monitor_success(mission_id: str) -> None:
    """A clean check resets the consecutive-failure count."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE monitoring_missions SET consecutive_failures=0 WHERE id=?", (mission_id,)
        )


def _cron_part(raw: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for chunk in raw.split(","):
        base, slash, step_text = chunk.partition("/")
        step = int(step_text) if slash and step_text.isdigit() else 1
        if slash and not step_text.isdigit() or step < 1:
            raise ValueError("unsupported cron step")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            pieces = base.split("-")
            if len(pieces) != 2 or not all(p.isdigit() for p in pieces):
                raise ValueError("invalid cron range")
            start, end = map(int, pieces)
        elif base.isdigit() and not slash:
            start = end = int(base)
        else:
            raise ValueError("invalid cron field")
        if start < low or end > high or start > end:
            raise ValueError("cron field out of range")
        values.update(range(start, end + 1, step))
    return values


def next_due_at(trigger: str, cron: str | None, interval: int, now: float) -> float | None:
    if trigger == "webhook":
        return None
    if trigger == "interval":
        if not 60 <= interval <= 604800:
            raise ValueError("interval must be between 60 seconds and 7 days")
        return now + interval
    if trigger != "cron" or not cron:
        raise ValueError("cron schedule requires five UTC fields")
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("cron schedule requires five UTC fields")
    minute, hour, day, month, weekday = (
        _cron_part(value, low, high) for value, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)))
    candidate = datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        if (candidate.minute in minute and candidate.hour in hour and candidate.day in day
                and candidate.month in month and (cron_weekday in weekday or cron_weekday == 0 and 7 in weekday)):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise ValueError("cron schedule has no occurrence in the next year")


def claim_due(now: float | None = None, limit: int = 5, db_path: str | None = None) -> list[dict]:
    current = time.time() if now is None else now
    db.init_db(db_path)
    ensure_stream_c_schema(db_path)
    claims: list[dict] = []
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("""SELECT * FROM monitoring_missions
            WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
            ORDER BY next_run_at ASC LIMIT ?""", (current, limit)).fetchall()
        for row in rows:
            next_at = next_due_at(row["trigger_type"], row["schedule_cron"], row["interval_seconds"], current)
            conn.execute("UPDATE monitoring_missions SET next_run_at=? WHERE id=?", (next_at, row["id"]))
            claims.append(dict(row))

        # Also claim due email intelligence missions (avoid duplicate claims if mirrored in monitoring_missions)
        rem_limit = max(0, limit - len(claims))
        claimed_ids = {c["id"] for c in claims}
        if rem_limit > 0:
            try:
                email_rows = conn.execute("""SELECT * FROM email_monitoring_missions
                    WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
                    ORDER BY next_run_at ASC LIMIT ?""", (current, rem_limit)).fetchall()
                for erow in email_rows:
                    ed = dict(erow)
                    if ed["id"] in claimed_ids:
                        continue
                    freq = ed.get("frequency_seconds") or 300
                    next_at = current + freq
                    conn.execute("UPDATE email_monitoring_missions SET next_run_at=? WHERE id=?", (next_at, ed["id"]))
                    claims.append({
                        "id": ed["id"],
                        "target_url": f"email://{ed.get('account_email') or 'inbox'}",
                        "condition_type": "email_intelligence",
                        "conversation_id": ed.get("conversation_id"),
                        "frequency_seconds": freq,
                    })
            except Exception as exc:
                logger.error("Failed to claim email monitoring missions: %s", exc)
    return claims


async def execute_monitor(mission: dict) -> None:
    from .agentLoop import run_task
    from .change_detector import evaluate_observation, broadcast_event
    target_url = mission["target_url"]

    if target_url.startswith("email://") or mission.get("condition_type") == "email_intelligence":
        from .email_intelligence import run_email_mission_tick
        try:
            await run_email_mission_tick(mission["id"])
        except Exception as exc:
            # Stream C (R-07): a broken email mission used to stay
            # 'active' forever — the exception only updated last_sync_at,
            # with no failure counting, no death event. Route it through
            # the same 5-strike rule as web watches (the mirrored
            # monitoring_missions row carries the same id).
            logger.warning("email monitor %s: tick failed: %s", mission["id"], exc)
            _status, death_event = _record_monitor_failure(mission["id"], str(exc))
            if death_event:
                from .change_detector import broadcast_event

                await broadcast_event(death_event)
            with db.get_connection() as conn:
                conn.execute("UPDATE email_monitoring_missions SET last_sync_at=? WHERE id=?", (time.time(), mission["id"]))
        return

    # Task 2 live monitoring: a plain public http(s) watch does not
    # need a live Chrome extension. When no extension is connected,
    # check the page over HTTP instead of failing with
    # EXTENSION_DISCONNECTED; when one IS connected, keep the
    # existing browser path (it sees rendered/JS content the HTTP
    # fetch cannot). Either way the check is read-only.
    if target_url.startswith(("http://", "https://")) and hub.extension_ws is None:
        # Stream C (RC-LIVE-04): the fetch uses the ONE monitoring URL
        # gate (change_detector.validate_monitor_url) — the same gate
        # watch creation uses — so a URL the fetch accepts can always
        # be created, including the 127.0.0.1 fixture host.
        from .change_detector import validate_monitor_url
        from .monitor_check import check_monitor_http

        ok, reason = validate_monitor_url(target_url)
        if not ok:
            logger.warning("monitor %s: HTTP check refused (%s)", mission["id"], reason)
            # A DNS refusal is transient — the watch survives and
            # retries next tick. A structural refusal (private host,
            # bad scheme) ends it, with a recorded event.
            _status, death_event = _record_monitor_failure(
                mission["id"], reason or "URL refused",
                permanent=not _is_transient_refusal(reason),
            )
            if death_event:
                await broadcast_event(death_event)
            return
        try:
            event = await asyncio.to_thread(check_monitor_http, mission["id"], target_url)
            _record_monitor_success(mission["id"])
            if event:
                await broadcast_event(event)
        except Exception as exc:
            logger.warning("monitor %s: HTTP check failed: %s", mission["id"], exc)
            _status, death_event = _record_monitor_failure(mission["id"], str(exc))
            if death_event:
                await broadcast_event(death_event)
        return

    record = hub.new_task(f"Open {target_url} and observe its public visible page text and price. Report only observed facts.",
                          chat_id=mission["conversation_id"])
    record.state = "planning"
    await hub.broadcast_progress(record)
    try:
        await run_task(record.task_id)
        observation = record.last_observation
        if record.state != "completed" or not observation:
            raise RuntimeError("monitor observation was not verified")
        from urllib.parse import urlsplit
        observed_url = observation.get("url", "") if isinstance(observation, dict) else getattr(observation, "url", "")
        if urlsplit(observed_url).hostname != urlsplit(target_url).hostname:
            raise RuntimeError("monitor observed the wrong domain")
        text = observation.get("pageText", "") if isinstance(observation, dict) else getattr(observation, "page_text", "")
        interactive = observation.get("interactive", []) if isinstance(observation, dict) else getattr(observation, "interactive", [])
        names = [str(el.get("name", "") if isinstance(el, dict) else getattr(el, "name", "")) for el in interactive]
        _record_monitor_success(mission["id"])
        event = evaluate_observation(mission["id"], {"text": text, "elements": names}, task_id=record.task_id)
        if event:
            await broadcast_event(event)
    except Exception as exc:
        _status, death_event = _record_monitor_failure(mission["id"], str(exc))
        if death_event:
            await broadcast_event(death_event)


async def scheduler_loop(stop: asyncio.Event, tick_seconds: float = 10.0) -> None:
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        # Stream C (R-02): the claim used to run unguarded — one
        # claim_due exception (DB locked/corrupt) propagated out of
        # the loop and ALL monitoring stopped silently for the rest of
        # the process. Mirror the scheduler loop: log and keep ticking.
        try:
            due = claim_due(limit=max(0, 5 - len(running)))
        except Exception as exc:  # noqa: BLE001 — the loop must survive a bad claim
            logger.warning("monitoring tick failed: %s", exc)
            due = []
        for mission in due:
            task = asyncio.create_task(execute_monitor(mission))
            running.add(task)
            task.add_done_callback(running.discard)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
    if running:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def monitoring_lifespan(_app):
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await task
