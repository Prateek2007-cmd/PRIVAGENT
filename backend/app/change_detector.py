"""Atomic monitoring baselines, condition checks and deduplicated events."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from . import db
from .privacy import _redact_text

_PRICE = re.compile(r"(?:₹|\b(?:Rs\.?|INR|USD)\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.I)

# Price-keyword context: a currency amount only counts as "the
# price" when it sits next to a price word. A banner, a related
# product or an MRP elsewhere on the page must not become the
# watched price (the old first-currency-match did exactly that).
_PRICE_CONTEXT_RES = [
    re.compile(r"(?:selling|current|offer|sale|deal|discounted|special|now|today'?s|our)\s+price[^₹$\d]{0,40}?"
               r"(?:₹|\b(?:Rs\.?|INR|USD)\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.I),
    re.compile(r"(?:price|mrp|buy\s+now|add\s+to\s+cart|check\s*out)[^₹$\d]{0,40}?"
               r"(?:₹|\b(?:Rs\.?|INR|USD)\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.I),
    re.compile(r"(?:₹|\b(?:Rs\.?|INR|USD)\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)[^.\n]{0,40}?"
               r"(?:selling|current|offer|sale|deal|discounted|special)\s+price", re.I),
]


def _extract_price(observation: dict[str, Any], text: str) -> tuple[float | None, str | None]:
    """The watched price for an observation — structured first,
    contextual second, unambiguous-single-amount last.

    1. Structured metadata (``observation["price"]`` from
       monitor_check: JSON-LD / microdata / meta / JSON product
       API), already validated there.
    2. A currency amount adjacent to a price word ("selling price
       ₹4,199", "Price: ₹4,199").
    3. The ONLY currency amount in the whole text (unambiguous —
       a one-line browser observation such as "₹100").
    4. None — several competing amounts and no structure means the
       price is unknown, and guessing the first one (often a banner
       or an MRP) is worse than reporting "Price unavailable".
    """
    structured = observation.get("price")
    if isinstance(structured, (int, float)) and structured > 0:
        return float(structured), (observation.get("price_info") or {}).get("source") if isinstance(observation.get("price_info"), dict) else None
    for pattern in _PRICE_CONTEXT_RES:
        match = pattern.search(text)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
            except (ValueError, IndexError):
                continue
            if 0 < value <= 10_000_000:
                return value, "contextual"
    amounts = _PRICE.findall(text)
    if len(amounts) == 1:
        try:
            value = float(amounts[0].replace(",", ""))
        except ValueError:
            return None, None
        if 0 < value <= 10_000_000:
            return value, "single-amount"
    return None, None

# Stream C (RC-LIVE-04): ONE URL gate for monitoring, used by creation
# (continuous_monitoring_companion) AND fetch (monitor_check /
# monitoring_scheduler). The fetch gate already allows the 127.0.0.1
# fixture host and keeps the SSRF guard for public hosts; the creation
# gate (website_resolver.is_safe_public_url, used by
# POST /api/control/missions) rejects 127.0.0.1, so a watch the fetch
# path would happily check could never be created. This wrapper is the
# canonical entry point — do not add a second gate.
def validate_monitor_url(url: str) -> tuple[bool, str | None]:
    """Fail-closed URL gate shared by watch creation and watch checks.

    Allows the 127.0.0.1 fixture host for controlled demos; every other
    loopback/private/link-local/reserved address and embedded-credential
    URL is refused, and public hostnames get the DNS-rebinding check
    (skipped when a proxy resolves the host, as in this sandbox).
    """
    from .monitor_check import _validate_monitor_url

    return _validate_monitor_url(url)


# Stream C (R-12): minimum seconds between two alerts from the same
# watch. A page that changes every tick (live scores, rotating banners
# that survive volatile-stripping) used to alert every tick. The
# default applies to new watches; per-watch override lives in
# monitoring_missions.alert_cooldown_seconds. A daily_price digest is
# a report, not an alert, so it is exempt.
DEFAULT_ALERT_COOLDOWN_S = 60

# Task 2 live monitoring: volatile noise that must NOT alert. Each
# match is replaced by a single space before hashing/diffing, so a
# page whose only change is a timestamp, a "5 minutes ago" label or a
# view counter keeps the same state hash. Real content and prices are
# untouched — a price is never part of these patterns, and a content
# change next to a timestamp still changes the hash.
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_VOLATILE_RES = [
    # ISO datetimes: 2026-11-17, 2026-11-17T09:28:00Z
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?,?(?!\w)"),
    # Month-name dates: "17 November 2026", "November 17, 2026"
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\s+\d{{4}},?(?!\w)", re.I),
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}},?(?!\w)", re.I),
    # Numeric dates: 17/11/2026, 11-17-2026
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4},?(?!\w)"),
    # Relative time: "5 minutes ago", "an hour ago", "just now"
    re.compile(r"\b\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago,?(?!\w)", re.I),
    re.compile(r"\b(?:a|an)\s+(?:second|minute|hour|day|week|month|year)\s+ago,?(?!\w)", re.I),
    re.compile(r"\bjust now,?(?!\w)", re.I),
    re.compile(r"\bmoments? ago,?(?!\w)", re.I),
    # Rapidly-changing counters: "1,234 views", "57 people online"
    re.compile(r"\b[\d,]+\s+(?:views?|visitors?|likes?|comments?|shares?|followers?|subscribers?|downloads?|plays?),?(?!\w)", re.I),
    re.compile(r"\b[\d,]+\s+people\s+(?:online|viewing|reading|here),?(?!\w)", re.I),
    re.compile(r"\b[\d,]+\s+online,?(?!\w)", re.I),
    # "posted today / updated yesterday" flips every day without any
    # real content change; "5 min read" is a layout label, not content.
    re.compile(r"\b(?:posted|updated|published|modified)\s+(?:today|yesterday),?(?!\w)", re.I),
    re.compile(r"\b\d+\s+min\s+read,?(?!\w)", re.I),
    # Ad markers that survive into visible text ("Sponsored", a lone
    # "Advertisement" label) are chrome, not content.
    re.compile(r"\b(?:sponsored|advertisement)\b,?(?!\w)", re.I),
    # Clock times: 09:28, 09:28:14, 9:28 AM
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M|am|pm)?,?(?!\w)"),
]


def _normalise_volatile(text: str) -> str:
    """Strip volatile timestamps/counters so noise cannot alert."""
    out = text
    for pattern in _VOLATILE_RES:
        out = pattern.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


def _state(observation: dict[str, Any]) -> dict[str, Any]:
    text = _redact_text(_normalise_volatile(re.sub(r"\s+", " ", str(observation.get("text") or "")).strip()), 4000)
    names = [_redact_text(_normalise_volatile(str(name)), 120) for name in (observation.get("elements") or [])[:100]]
    # Stream A: the price is NEVER the first currency-looking number
    # in the text (a banner/MRP/related-product price used to become
    # the watched price). Structured metadata wins, then a
    # price-keyword context, then a single unambiguous amount.
    price, price_source = _extract_price(observation, text)
    state: dict[str, Any] = {"text": text, "elements": names, "price": price}
    product_name = observation.get("product_name")
    if not product_name:
        price_info = observation.get("price_info")
        if isinstance(price_info, dict):
            product_name = price_info.get("name")
    if product_name:
        # Identity: the structured product name the price belongs
        # to, so a watch can be checked against the product it
        # names instead of trusting any number on the page.
        state["product_name"] = _redact_text(str(product_name), 200)
    if price_source:
        state["price_source"] = price_source
    return state


def _triggered(condition: str, params: dict, before: dict, after: dict) -> tuple[bool, str]:
    if condition == "daily_price":
        # Daily price digest (e.g. "every morning at 9am, tell me the
        # price"): reports the current price on EVERY scheduled run,
        # even when it has not changed since yesterday. This is a
        # report, not a change alert — so it never depends on `before`.
        new = after.get("price")
        if new is None:
            return False, "Price unavailable"
        return True, f"Price today: ₹{new:g}"
    if condition == "price_drop":
        old, new = before.get("price"), after.get("price")
        threshold = float(params.get("min_percent", 0))
        if not 0 <= threshold <= 100:
            raise ValueError("price drop threshold must be 0-100%")
        if old is None or new is None or old <= 0:
            return False, "Price unavailable"
        return new < old and (old - new) * 100 / old >= threshold, f"Price changed from {old:g} to {new:g}"
    if condition == "below_price":
        # Absolute threshold ("alert me when the price falls below
        # ₹5000"): fires whenever the observed price is under the
        # threshold — a state, not a change — so the first run can
        # already alert when the product is cheap.
        try:
            threshold = float(params.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError("below-price threshold must be a number") from None
        if threshold <= 0:
            raise ValueError("below-price threshold must be positive")
        new = after.get("price")
        if new is None:
            return False, "Price unavailable"
        return new < threshold, f"Price below ₹{threshold:g} (now ₹{new:g})"
    if condition == "new_listings":
        # New items on a listing / search page (jobs, internships,
        # products): fires when accessible names appear that were not
        # in the previous snapshot. The caller only evaluates this
        # condition when the state changed, so the first run only
        # baselines and never alerts. Reordering alone can surface a
        # name the snapshot window dropped, so the summary names the
        # new items for the human to judge.
        before_names = {item.casefold() for item in before.get("elements") or []}
        if not before_names:
            # The previous snapshot had no items (login wall, empty
            # results, failed load): do NOT treat the first non-empty
            # snapshot as "everything is new" — baseline it instead,
            # or the watch fires once on every existing listing.
            return False, "Baseline: no prior listings to compare"
        new_items = [item for item in (after.get("elements") or []) if item.casefold() not in before_names]
        if not new_items:
            return False, "No new listings"
        shown = ", ".join(new_items[:3])
        extra = f" and {len(new_items) - 3} more" if len(new_items) > 3 else ""
        return True, f"New listings: {shown}{extra}"
    if condition == "regex":
        pattern = str(params.get("pattern") or "")
        # A narrow, useful regex subset avoids catastrophic backtracking in
        # the scheduler process. Grouping, backreferences and compound
        # quantifiers are deliberately not accepted.
        if (not pattern or len(pattern) > 120 or any(ch in pattern for ch in "(){}")
                or len(re.findall(r"(?<!\\)[*+?]", pattern)) > 1 or re.search(r"\\[1-9]", pattern)):
            raise ValueError("invalid or unsafe monitor regex")
        found = bool(re.search(pattern, after["text"], re.I))
        previously = bool(re.search(pattern, before["text"], re.I))
        return found and not previously, "Requested text pattern appeared"
    if condition == "element":
        name = str(params.get("name") or "").casefold()
        if not name or len(name) > 120:
            raise ValueError("element condition requires a short accessible name")
        found = any(name in item.casefold() for item in after["elements"])
        previously = any(name in item.casefold() for item in before["elements"])
        return found and not previously, "Requested element appeared"
    return before["text"] != after["text"], "Observed public page content changed"


def evaluate_observation(mission_id: str, observation: dict[str, Any], *,
                         task_id: str | None = None, observed_at: float | None = None,
                         db_path: str | None = None) -> dict[str, Any] | None:
    now = time.time() if observed_at is None else observed_at
    state = _state(observation)
    serialized = json.dumps(state, sort_keys=True)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise ValueError("unknown mission")
        before = json.loads(row["last_state_json"] or "{}")
        changed = bool(row["last_state_hash"] and row["last_state_hash"] != digest)
        # A daily-price digest reports on every scheduled run, even when
        # nothing changed (and even on the first run, when there is no
        # baseline yet). A below-price threshold is likewise a state,
        # not a change: being under the threshold is alert-worthy on
        # the first run too (the product may already be cheap) — but
        # unlike the digest it is deduplicated by state digest, so a
        # steady below-threshold price alerts once, not every tick.
        # Every other condition only fires on a change, and is
        # deduplicated by the alert hash.
        if row["condition_type"] == "daily_price":
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert
        elif row["condition_type"] == "below_price":
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert and digest != row["last_alert_hash"]
        elif changed:
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert and digest != row["last_alert_hash"]
        else:
            should_alert, summary = False, "Unchanged"
            fire = False
        event = None
        # Stream C (R-12): alert cooldown. The dedup above already
        # suppresses repeat alerts for the SAME state; the cooldown
        # suppresses a *different* change arriving while the previous
        # alert is still fresh, so a fast-changing page cannot spam
        # the user every tick. The state is still stored, so nothing
        # is lost — only the alert is held. Daily-price digests are
        # reports, not alerts, and are exempt.
        cooldown = DEFAULT_ALERT_COOLDOWN_S
        try:
            stored_cd = row["alert_cooldown_seconds"]
            if stored_cd is not None:
                cooldown = max(0, int(stored_cd))
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        last_alert = row["last_alert_at"]
        if (fire and row["condition_type"] != "daily_price" and last_alert
                and now - float(last_alert) < cooldown):
            fire = False
        if fire:
            event = {"id": str(uuid.uuid4()), "mission_id": mission_id, "task_id": task_id,
                     "detected_at": now, "title": row["title"], "summary": summary,
                     "diff_data": {"before": before.get("text", "")[:250], "after": state["text"][:250],
                                   "old_price": before.get("price"), "new_price": state.get("price")}}
            conn.execute("""INSERT INTO monitoring_events
                (id,mission_id,task_id,detected_at,title,summary,diff_data,state_hash)
                VALUES(?,?,?,?,?,?,?,?)""", (event["id"], mission_id, task_id, now, row["title"],
                  summary, json.dumps(event["diff_data"]), digest))
            conn.execute("UPDATE monitoring_missions SET last_alert_hash=?,last_alert_at=? WHERE id=?",
                         (digest, now, mission_id))
        from .monitoring_scheduler import next_due_at
        next_run = next_due_at(row["trigger_type"], row["schedule_cron"], row["interval_seconds"], now)
        conn.execute("""UPDATE monitoring_missions SET last_state_hash=?,last_state_json=?,last_run_at=?,next_run_at=?
            WHERE id=?""", (digest, serialized, now, next_run, mission_id))
    return event


async def broadcast_event(event: dict[str, Any]) -> None:
    """Deliver one monitoring event to the user — live AND durable.

    Stream C (R-01/R-04): the old version only fanned out to open
    WebSocket clients and marked `notification_sent=1` when any socket
    accepted the JSON — "delivered" meant "a socket took it", not "the
    user saw it", and a closed frontend meant the event vanished. Now:

    1. The envelope keeps the `MISSION_EVENT_TRIGGERED` name and adds
       the contract §3 fields (mission_id, title, message, event_id,
       created_at ISO-8601 UTC) at the top level; the old taskId /
       payload shape is kept for backward compatibility.
    2. The alert is ALSO posted into the watch's conversation
       (db.add_message — the same precedent the email tick uses), so
       it is visible in chat history whenever the user reopens that
       chat, even with no socket ever connected.
    3. The alert is ALSO written to the notification outbox
       (``monitoring_scheduler.post_outbox_notification``, kind
       ``monitor``, ref_id = the event id) — the durable
       notification row the reconnect pull
       (``GET /api/monitoring/events``) returns. The unified pull
       deduplicates it against the monitoring_events row, so one
       alert is never counted or listed twice.
    4. `notification_sent` / `delivered_at` are set ONLY when at least
       one of those channels actually took the event. An event is not
       "delivered" until a client acknowledges it or the missed-event
       pull (GET /api/monitoring/events) returns it — see
       monitoring_scheduler.mark_events_delivered.
    """
    from starlette.websockets import WebSocketDisconnect, WebSocketState
    from .hub import hub
    created_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(event.get("detected_at") or time.time())))
    envelope = {
        "type": "MISSION_EVENT_TRIGGERED",
        "mission_id": event.get("mission_id"),
        "taskId": event.get("task_id"),
        "title": event.get("title") or "Watch alert",
        "message": event.get("summary") or "",
        "event_id": event.get("id"),
        "created_at": created_iso,
        "payload": event,
    }
    delivered = False
    for ws in list(hub.client_sockets):
        if getattr(ws, "client_state", None) == WebSocketState.DISCONNECTED or getattr(ws, "application_state", None) == WebSocketState.DISCONNECTED:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
            continue
        try:
            await ws.send_json(envelope)
            delivered = True
        except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, OSError):
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
        except Exception:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
    # Durable channel: post the alert into the watch's conversation.
    # The frontend loads chat history on open, so this is visible on
    # reopen with no socket involved. Best-effort — a missing chat
    # must never break the live broadcast above.
    try:
        mission_id = event.get("mission_id")
        chat_id = None
        if mission_id:
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT conversation_id FROM monitoring_missions WHERE id=?",
                    (mission_id,),
                ).fetchone()
                chat_id = row["conversation_id"] if row else None
        if chat_id:
            db.add_message(
                chat_id,
                "agent",
                f"🔔 Watch alert — {event.get('title') or 'Watch'}: {event.get('summary') or ''}",
                evidence=f"monitoring event {event.get('id')}",
                task_id=event.get("task_id"),
            )
            delivered = True
    except Exception:
        pass
    # Durable notification row: the outbox is the record the
    # reconnect pull returns, independent of any live socket. The
    # unified pull deduplicates this row against the
    # monitoring_events row (same ref_id), so the alert is listed
    # once. Best-effort — a missing outbox table must never break
    # the live broadcast above.
    try:
        from .monitoring_scheduler import post_outbox_notification

        post_outbox_notification(
            "monitor", str(event.get("id") or ""),
            str(event.get("title") or "Watch alert"),
            str(event.get("summary") or ""),
            diff_data={"event_id": event.get("id"),
                       "mission_id": event.get("mission_id")},
        )
    except Exception:
        pass
    if delivered:
        # delivered_at is added by monitoring_scheduler.ensure_stream_c_schema;
        # a DB that has not run the migration (e.g. a test temp DB) falls
        # back to the pre-existing notification_sent-only update.
        with db.get_connection() as conn:
            try:
                conn.execute(
                    "UPDATE monitoring_events SET notification_sent=1, delivered_at=? WHERE id=?",
                    (time.time(), event["id"]),
                )
            except Exception:
                conn.execute(
                    "UPDATE monitoring_events SET notification_sent=1 WHERE id=?",
                    (event["id"],),
                )
