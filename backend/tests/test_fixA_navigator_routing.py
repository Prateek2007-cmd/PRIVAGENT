"""FixA — NAVIGATOR routing fix, round 2 (M1, M2, M7, price baseline).

Drives the real handler + verifier against a throwaway SQLite DB —
no live server, no model calls. Every prompt and product here is
NOVEL: none copies the user's 12-step examples, and none reuses
Stream 5's test products (Aurora X9, Vertex Pro, Nimbus Trail,
Kestrel 4K, Halcyon, Galaxy Tab S11/Buds 3, boAt Airdopes 141,
iPhone 17, Galaxy S26, Sony WH-1000XM6). Products here (Quartz
Desk Lamp, Cobalt Trail shoes, Cinder speaker, Juniper desk,
Zephyr One earbuds) are invented for these tests.

* M1 — a successful pause must verify ``completed``: the verifier
  used to re-resolve the watch from the regex capture (ref='.')
  instead of the working context, so the pause persisted while
  the user was told it failed.
* M2 — "Resume it" after a pause must target the PAUSED watch,
  not the most-recent (still active) one.
* M7 — missing-account / missing-capability turns end ``blocked``
  (not failed/completed); a correct no-op cancel ends
  ``completed``; a clarification ends ``awaiting_input``.
* Price — a search page's filter minimum (₹10,000) must never
  become the baseline price; when no real price can be read, the
  stored price is null and the message says so.
"""

import asyncio
import hashlib
import json
import re
import time
import uuid

import pytest

from app import db
from app.change_detector import _extract_price, _triggered, evaluate_observation
from app.completion_verifier import verify_task_completion
from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction
from app.working_context import (
    WorkingContext,
    record_watch,
    resolve_watch_target,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "fixa.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _run(instruction, db_path, chat_id="fixa-chat", history=None):
    return asyncio.run(handle_continuous_monitoring_instruction(
        instruction, chat_id=chat_id, history=history, db_path=db_path))


def _history_for(turns):
    return [{"role": r, "content": c} for r, c in turns]


def _create_watch(db_path, instruction, chat_id="fixa-chat"):
    answer = _run(instruction, db_path, chat_id=chat_id)
    match = re.search(r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->", answer)
    assert match, "watch creation returned no watch marker: " + answer[:300]
    return match.group(1), answer


def _missions(db_path):
    with db.get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at ASC")]


def _watch_row(db_path, mission_id):
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    return dict(row) if row else None


def _insert_mission(db_path, *, title, condition_type="price_drop",
                    condition_params=None, status="active",
                    last_state=None, chat_id="fixa-chat"):
    mission_id = str(uuid.uuid4())
    state_json = json.dumps(last_state) if last_state is not None else None
    state_hash = (hashlib.sha256(state_json.encode()).hexdigest()
                  if state_json else None)
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, conversation_id, title, target_url, trigger_type,
                schedule_cron, interval_seconds, condition_type,
                condition_params, status, created_at, next_run_at,
                last_state_hash, last_state_json)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",
            (mission_id, chat_id, title, "https://example.org/product",
             "interval", None, 600, condition_type,
             json.dumps(condition_params or {"target": title}),
             time.time(), time.time(), state_hash, state_json),
        )
        conn.commit()
    return mission_id


# ---------------------------------------------------------------------------
# M1 — pause verifies completed through the working context
# ---------------------------------------------------------------------------

def test_pause_first_watch_verifies_completed_not_failed(database):
    """Repro (novel products): two watches, an interval change,
    then "Pause the first watch." The pause persists AND the
    verifier reports completed — no "no watch row found for '.'"."""
    t1 = "Monitor the price of Quartz Desk Lamp every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Live monitor Cobalt Trail running shoes when it is 10% off. Check every 15 minutes."
    _, a2 = _create_watch(database, t2)
    history = _history_for([("user", t1), ("assistant", a1),
                            ("user", t2), ("assistant", a2)])
    _run("Change that watch to every 15 minutes.", database, history=history)
    pause_answer = _run("Pause the first watch.", database, history=history)
    rows = _missions(database)
    paused = [r for r in rows if r["status"] == "paused"]
    assert len(paused) == 1 and "Quartz Desk Lamp" in paused[0]["title"], pause_answer[:300]
    result = verify_task_completion(
        instruction="Pause the first watch.", candidate_state="completed",
        message=pause_answer, db_path=database, chat_id="fixa-chat")
    assert result.state == "completed", result.message
    assert "no watch row found" not in result.message
    assert "Quartz Desk Lamp" in result.message


def test_pause_by_brand_verifies_through_working_context(database):
    """A brand referent ("Pause the Quartz watch.") has no regex
    post-"watch" capture at all — the verifier must still find the
    row through the working context, not fail on an empty ref."""
    t1 = "Monitor the price of Quartz Desk Lamp every 10 minutes."
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    pause_answer = _run("Pause the Quartz watch.", database, history=history)
    assert _missions(database)[0]["status"] == "paused", pause_answer[:300]
    result = verify_task_completion(
        instruction="Pause the Quartz watch.", candidate_state="completed",
        message=pause_answer, db_path=database, chat_id="fixa-chat")
    assert result.state == "completed", result.message


def test_resume_after_pause_verifies_completed(database):
    """The verifier's resume check reads the row the working
    context resolves — the resumed watch, not the most recent one."""
    t1 = "Monitor the price of Quartz Desk Lamp every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Cobalt Trail running shoes every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _history_for([("user", t1), ("assistant", a1),
                            ("user", t2), ("assistant", a2)])
    _run("Pause the first watch.", database, history=history)
    resume_answer = _run("Resume it.", database, history=history)
    rows = _missions(database)
    assert all(r["status"] == "active" for r in rows), resume_answer[:300]
    assert "Quartz Desk Lamp" in resume_answer
    result = verify_task_completion(
        instruction="Resume it.", candidate_state="completed",
        message=resume_answer, db_path=database, chat_id="fixa-chat")
    assert result.state == "completed", result.message


# ---------------------------------------------------------------------------
# M2 — resume pronoun resolution is status-weighted
# ---------------------------------------------------------------------------

def _ctx_two_watches(first_state="paused", second_state="active"):
    ctx = WorkingContext(chat_id="fixa-w")
    record_watch(ctx, {
        "watch_id": "aaaaaaaa-1111-2222-3333-444444444444",
        "label": "Cinder Bluetooth speaker",
        "title": "Price Watch: Cinder Bluetooth Speaker",
        "product": "Cinder Bluetooth speaker",
        "interval_seconds": 600, "condition": "below_price",
        "state": first_state, "target_url": "https://example.org/cinder",
        "created_at": 1000.0,
    })
    record_watch(ctx, {
        "watch_id": "bbbbbbbb-1111-2222-3333-444444444444",
        "label": "Juniper standing desk",
        "title": "Price Watch: Juniper Standing Desk",
        "product": "Juniper standing desk",
        "interval_seconds": 900, "condition": "price_drop",
        "state": second_state, "target_url": "https://example.org/juniper",
        "created_at": 2000.0,
    })
    return ctx


def test_resume_pronoun_prefers_the_paused_watch():
    result = resolve_watch_target(_ctx_two_watches(), "Resume it.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Cinder Bluetooth speaker"


def test_resume_pronoun_with_nothing_paused_keeps_most_recent():
    result = resolve_watch_target(
        _ctx_two_watches(first_state="active"), "Resume it.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Juniper standing desk"


def test_pause_pronoun_prefers_an_active_watch():
    result = resolve_watch_target(_ctx_two_watches(), "Pause it.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Juniper standing desk"


def test_resume_the_paused_watch_by_state_word():
    result = resolve_watch_target(_ctx_two_watches(), "Resume the paused watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Cinder Bluetooth speaker"


def test_resume_it_after_pause_resumes_the_paused_watch(database):
    """Handler level, novel products: pause the first of two
    watches, then "Resume it." must resume the paused one — not
    report the other (already active) watch."""
    t1 = "Monitor the price of Cinder Bluetooth speaker every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Juniper standing desk every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _history_for([("user", t1), ("assistant", a1),
                            ("user", t2), ("assistant", a2)])
    _run("Pause the first watch.", database, history=history)
    resume_answer = _run("Resume it.", database, history=history)
    rows = _missions(database)
    assert all(r["status"] == "active" for r in rows), resume_answer[:300]
    assert "Cinder Bluetooth Speaker" in resume_answer
    assert "already active" not in resume_answer


# ---------------------------------------------------------------------------
# M7 — state taxonomy at the recording point (agentLoop mapping)
# ---------------------------------------------------------------------------

from app.agentLoop import _map_outcome_state  # noqa: E402


def test_missing_mail_account_failed_becomes_blocked():
    msg = ("Monitoring incomplete: no watch was created and no watch id was returned.\n\n"
           "### Mail Account Not Connected\n\nNo active mail account was found.")
    assert _map_outcome_state(
        "check my inbox every 30 minutes and flag anything urgent",
        "failed", msg) == "blocked"


def test_missing_mail_account_completed_becomes_blocked():
    msg = ("Can't do it yet — no mail account is connected, so I have no inbox to read.")
    assert _map_outcome_state(
        "summarise my unread emails", "completed", msg) == "blocked"


def test_missing_file_tool_completed_becomes_blocked():
    msg = ("No file tool here — I can't open or list your laptop's filesystem. "
           "Which one do you mean — Google Drive, or the browser?")
    assert _map_outcome_state(
        "use the file tool to list my documents", "completed", msg) == "blocked"


def test_correct_noop_cancel_failed_becomes_completed():
    msg = ("You're all set — nothing was ever created, so there's nothing to undo. "
           "No reminder was made, so no cancellation was needed.")
    assert _map_outcome_state("cancel that reminder", "failed", msg) == "completed"


def test_clarification_failed_becomes_awaiting_input():
    msg = ("Two things there. I need the actual target first — which model is it? "
           "Send me the store link or tell me the exact model, and I'll create the watch.")
    assert _map_outcome_state(
        "watch the price of the new reader and remind me about my meeting",
        "failed", msg) == "awaiting_input"


def test_genuine_failure_stays_failed_and_completion_stays_completed():
    assert _map_outcome_state(
        "what is the latest phone", "failed",
        "Research incomplete: no substantive research sources were examined.") == "failed"
    assert _map_outcome_state(
        "remember my keyboard is a Juniper K2", "completed",
        "Noted — your keyboard is a Juniper K2.") == "completed"


# ---------------------------------------------------------------------------
# Price — a filter minimum is never the baseline price
# ---------------------------------------------------------------------------

# A search-results page: a filter sidebar ("Price … Min ₹10000
# ₹15000 …") above real listings whose first price is ₹4,299.
_SEARCH_PAGE_TEXT = (
    "Zephyr One Earbuds - Buy Products Online at Best Price in India | Flipkart.com "
    "Explore Plus Login Filters CATEGORIES Audio Price . . . . . . Min ₹10000 ₹15000 "
    "₹20000 ₹30000 to ₹10000 ₹15000 ₹20000 ₹30000 ₹30000+ Brand Zephyr Discount "
    "10% or more Showing 1 – 24 of 132 results for \" Zephyr One \" Sort By Relevance "
    "Zephyr One True Wireless Earbuds (Midnight Black) 4.4 12,408 Ratings 2 Year "
    "Warranty Bluetooth 5.3 ₹4,299 ₹5,999 28% off Upto ₹1,200 Off on Exchange "
    "Zephyr One Pro Earbuds (Pearl White) 4.5 8,112 Ratings ₹5,499 ₹6,999 21% off"
)


def test_filter_minimum_is_not_the_baseline_price():
    price, source = _extract_price({}, _SEARCH_PAGE_TEXT)
    assert price is None, f"filter minimum became the price: {price} ({source})"
    assert source is None


def test_genuine_contextual_price_still_extracts():
    price, source = _extract_price({}, "Zephyr One earbuds — Selling price ₹4,199. In stock.")
    assert price == 4199.0 and source == "contextual"


def test_single_unambiguous_amount_still_extracts():
    price, source = _extract_price({}, "₹100")
    assert price == 100.0 and source == "single-amount"


def test_structured_price_still_wins():
    price, source = _extract_price(
        {"price": 4299.0, "price_info": {"source": "json-ld"}},
        _SEARCH_PAGE_TEXT)
    assert price == 4299.0 and source == "json-ld"


def test_percent_drop_never_fires_against_a_missing_baseline(database):
    """A %-drop watch whose page yields no real price stores a null
    baseline — and the condition evaluates 'Price unavailable',
    never an alert against a fabricated ₹10,000."""
    mission_id = _insert_mission(
        database, title="Price Watch: Zephyr One Earbuds",
        condition_type="price_drop", condition_params={"target": "Zephyr One Earbuds", "min_percent": 10.0})
    event = evaluate_observation(
        mission_id,
        {"text": _SEARCH_PAGE_TEXT, "elements": []},
        db_path=database)
    assert event is None
    row = _watch_row(database, mission_id)
    state = json.loads(row["last_state_json"])
    assert state.get("price") is None
    assert "price_source" not in state
    # A second observation, even with a changed text, cannot fire a
    # %-drop off a null baseline.
    fired, summary = _triggered(
        "price_drop", {"min_percent": 10.0},
        {"price": None}, {"price": 3899.0})
    assert fired is False
    assert summary == "Price unavailable"
    with db.get_connection(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM monitoring_events WHERE mission_id=?",
            (mission_id,)).fetchone()[0] == 0


def test_verifier_says_when_no_baseline_price_could_be_read(database):
    """A price-based watch with a baseline but no price completes
    honestly — the message says price alerts stay off until a real
    price is recorded, instead of implying a price was captured."""
    mission_id = _insert_mission(
        database, title="Price Watch: Zephyr One Earbuds",
        condition_type="price_drop",
        condition_params={"target": "Zephyr One Earbuds", "min_percent": 10.0},
        last_state={"text": _SEARCH_PAGE_TEXT, "elements": [], "price": None})
    reply = ("Watch active for **Zephyr One Earbuds** — checking every 10 minutes.\n"
             f"<!-- privagent-watch: {mission_id} -->")
    result = verify_task_completion(
        instruction="Live monitor Zephyr One earbuds when it is 10% off. Check every 10 minutes.",
        candidate_state="completed", message=reply,
        db_path=database, chat_id="fixa-chat")
    assert result.state == "completed", result.message
    assert "no baseline price could be read" in result.message
    assert "10000" not in result.message
