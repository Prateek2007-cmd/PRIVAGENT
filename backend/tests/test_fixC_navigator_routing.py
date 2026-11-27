"""FixC — NAVIGATOR routing fix, round 3 (N1, N2/N4, M8).

Drives the real handlers + verifier + classifier against a throwaway
SQLite DB — no live server, no model calls. Every prompt and product
here is NOVEL: none copies the user's 12-step examples, and none
reuses Stream 5's test products (Aurora X9, Vertex Pro, Nimbus Trail,
Kestrel 4K, Halcyon, Galaxy Tab S11/Buds 3, boAt Airdopes 141,
iPhone 17, Galaxy S26, Sony WH-1000XM6) or Fix A's (Quartz Desk Lamp,
Cobalt Trail shoes, Cinder speaker, Juniper desk, Zephyr One earbuds).
Products here (Ember reader, Marble headphones, Slate phone, Pixel
phone/tablet) are invented for these tests.

* N1 — a one-off timer request must never reach the model sink: there
  is no timer implementation, and the model invented a queued one-shot
  check that would never fire (live 2026-11-27). The deterministic
  intercept answers the honest boundary (blocked, zero rows) instead.
* N2 — a capabilities listing is a delivered answer: the overview
  honestly reports unconnected pieces ("extension is not connected"),
  which the missing-capability rule misread as a blocker.
* N4 — clarifications asked without a question mark (imperatives:
  "Send me …", "Give me …", "I need …"; "Which models?") still end
  awaiting_input, not failed.
* M8 — a latest-release question ("What is the latest … released?")
  routes to the browser when the extension is connected, instead of
  answering from memory while claiming the extension cannot be
  confirmed; with no extension it stays conversational, as before.
"""

import asyncio

import pytest

from app import db
from app.agentLoop import _map_outcome_state
from app.capabilities import (
    TIMER_UNSUPPORTED_MESSAGE,
    is_one_off_timer_request,
)
from app.intent_router import TaskIntent, classify_intent


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "fixc.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _count(db_path, table):
    # A table the fresh DB never created trivially holds zero rows.
    with db.get_connection(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return 0
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# N1 — one-off timer: deterministic honest boundary, zero rows
# ---------------------------------------------------------------------------

def test_timer_request_detected_novel_phrasings():
    assert is_one_off_timer_request("start a 45-minute countdown")
    assert is_one_off_timer_request("set a timer for 7 minutes")
    assert is_one_off_timer_request("countdown in 5 minutes")
    assert is_one_off_timer_request("set a timer for 2 hours")
    assert is_one_off_timer_request("timer for 90 seconds")
    assert is_one_off_timer_request("please start a 10 minute timer")


def test_timer_request_not_triggered_by_watches_or_reminders():
    # A watch that mentions a timer is a watch, not a timer request.
    assert not is_one_off_timer_request(
        "watch the price of the Ember reader on a timer every 10 minutes")
    assert not is_one_off_timer_request(
        "monitor the Marble headphones every 15 minutes")
    # A reminder owns its own branch and still creates a real job.
    assert not is_one_off_timer_request("remind me in 30 minutes to call mom")
    assert not is_one_off_timer_request("check every 10 minutes")
    assert not is_one_off_timer_request("what time is it")
    assert not is_one_off_timer_request("")


def test_timer_unsupported_message_claims_no_job():
    low = TIMER_UNSUPPORTED_MESSAGE.lower()
    assert "can't set a one-off timer" in low
    assert "nothing was set" in low
    assert "nothing will ring" in low
    assert "queued" not in low
    assert "done" not in low.split("nothing")[0]  # no false completion


@pytest.mark.asyncio
async def test_timer_turn_never_reaches_model_and_creates_nothing(
    monkeypatch, tmp_path
):
    """End-to-end (novel phrasing): the timer turn is intercepted
    before any provider call, ends blocked, and creates zero rows in
    scheduled_jobs / notification_outbox / monitoring_events /
    monitoring_missions. The provider raises if it is ever called —
    the model must never get a chance to invent a queued check."""
    from app import agentLoop
    from app.agentLoop import real_hub, run_task
    from app.hub import TaskRecord

    temp_db = str(tmp_path / "fixc-timer.sqlite3")
    db.init_db(db_path=temp_db)
    old_init_db = db.init_db
    original_get_conn = db.get_connection
    db.init_db = lambda *args, **kwargs: None
    db.get_connection = lambda p=None: original_get_conn(temp_db)
    try:
        chat = db.create_chat(title="fixc-timer", db_path=temp_db)
        task_id = "task_fixc_timer"
        record = TaskRecord(
            task_id=task_id,
            instruction="start a 45-minute countdown",
            state="created",
            chat_id=chat["id"],
        )
        real_hub.tasks[task_id] = record

        class MustNotBeCalledProvider:
            def chat_call(self, *args, **kwargs):
                raise AssertionError(
                    "timer turn reached the model — the intercept failed")

        monkeypatch.setattr(
            agentLoop, "_provider_for_config",
            lambda: MustNotBeCalledProvider())

        await run_task(task_id)
        await asyncio.sleep(0.05)
        assert record.state == "blocked", record.message
        assert "can't set a one-off timer" in (record.message or "").lower()
        assert "queued" not in (record.message or "").lower()
        assert _count(temp_db, "scheduled_jobs") == 0
        assert _count(temp_db, "notification_outbox") == 0
        assert _count(temp_db, "monitoring_events") == 0
        assert _count(temp_db, "monitoring_missions") == 0
    finally:
        db.init_db = old_init_db
        db.get_connection = original_get_conn


# ---------------------------------------------------------------------------
# N2 — capabilities listing keeps the verifier's state
# ---------------------------------------------------------------------------

_OVERVIEW_MSG = (
    "### PrivAgent Capabilities — live status\n\n"
    "- **Planner model: antseed (model: deepseek-v4.1-flash) — configured.**\n"
    "- **Real-Browser Automation: NOT connected.** The Chrome extension "
    "is not connected, so browser tasks cannot start.\n"
    "- **Registered tools:** 54 in the tool registry.\n"
)


def test_capabilities_listing_completed_stays_completed():
    assert _map_outcome_state(
        "What can you do?", "completed", _OVERVIEW_MSG) == "completed"
    assert _map_outcome_state(
        "tell me what you can do", "completed", _OVERVIEW_MSG) == "completed"


def test_capabilities_listing_genuine_failure_stays_failed():
    # The carve-out is not a free pass: a capability turn that
    # genuinely failed keeps failed.
    assert _map_outcome_state(
        "What can you do?", "failed", _OVERVIEW_MSG) == "failed"


def test_non_capability_turn_with_same_marker_still_blocked():
    # Only capability turns are carved out; any other turn whose
    # message reports the extension as not connected is still blocked.
    assert _map_outcome_state(
        "search for the Ember reader", "completed",
        "I can't search — the extension is not connected.") == "blocked"


@pytest.mark.asyncio
async def test_capabilities_turn_end_to_end_stays_completed(
    monkeypatch, tmp_path
):
    """End-to-end: "What can you do?" runs the deterministic overview
    branch and records completed, even though the overview honestly
    reports the extension as not connected."""
    from app import agentLoop
    from app.agentLoop import real_hub, run_task
    from app.hub import TaskRecord

    temp_db = str(tmp_path / "fixc-caps.sqlite3")
    db.init_db(db_path=temp_db)
    old_init_db = db.init_db
    original_get_conn = db.get_connection
    db.init_db = lambda *args, **kwargs: None
    db.get_connection = lambda p=None: original_get_conn(temp_db)
    try:
        chat = db.create_chat(title="fixc-caps", db_path=temp_db)
        task_id = "task_fixc_caps"
        record = TaskRecord(
            task_id=task_id,
            instruction="What can you do?",
            state="created",
            chat_id=chat["id"],
        )
        real_hub.tasks[task_id] = record
        await run_task(task_id)
        await asyncio.sleep(0.05)
        assert record.state == "completed", record.message
        assert "PrivAgent Capabilities" in (record.message or "")
    finally:
        db.init_db = old_init_db
        db.get_connection = original_get_conn


# ---------------------------------------------------------------------------
# N4 — clarifications without a question mark end awaiting_input
# ---------------------------------------------------------------------------

def test_imperative_clarification_without_question_mark():
    # D4 shape (novel): imperatives, no "?" anywhere.
    msg = (
        "Two things here, and they need slightly different inputs from "
        "you before I can actually create anything.\n\n"
        "Ember reader price watch — I need a specific listing to watch. "
        "Send me either the model name plus store, or better, the product "
        "URL. Give me the link and your timezone, and I'll set both up "
        "properly and come back with a real watch ID."
    )
    assert _map_outcome_state(
        "watch the price of the new Ember reader and remind me about my 2pm standup",
        "failed", msg) == "awaiting_input"


def test_which_models_clarification():
    # C8 shape (novel): "Which models?" / "Which way …?" — plural
    # "models" and "way" were missing from the old noun list.
    msg = (
        "Happy to draft it — two things I need pinned down first.\n\n"
        "1. Which models? I don't have a record of which Marble "
        "headphones you mean, and the Slate phone could be the 13 or "
        "the 13R.\n\n"
        "2. Where should it be saved? If you'd rather not wait, say "
        "the word and I'll draft it here in chat.\n\n"
        "Which way do you want to go?"
    )
    assert _map_outcome_state(
        "write a short note comparing the Marble headphones with the Slate phone and save it",
        "failed", msg) == "awaiting_input"


def test_tell_me_your_timezone_clarification_as_statement():
    # C12 shape (novel): the ask is a statement, no "?" in the message.
    msg = (
        "I don't have a live clock, so I can't tell you the current "
        "time in Sydney. What I can tell you is that Sydney is UTC+10 "
        "with daylight saving, so if you tell me your local time and "
        "timezone I'll do the conversion exactly."
    )
    assert _map_outcome_state(
        "what time is it in Sydney right now",
        "failed", msg) == "awaiting_input"


def test_rhetorical_question_failure_stays_failed():
    # The relaxed gate must not flip a genuine failure: a rhetorical
    # question with no explicit ask for a missing input stays failed.
    msg = (
        "Research incomplete: no substantive research sources were "
        "examined. Should I try again later?"
    )
    assert _map_outcome_state(
        "what is the latest Pixel tablet", "failed", msg) == "failed"


def test_i_need_to_check_is_not_a_clarification():
    # "I need to …" is the agent describing its own next step, not
    # asking the user for a missing input.
    msg = "Research incomplete: no sources were examined. I need to check the search backend first."
    assert _map_outcome_state(
        "what is the latest Pixel tablet", "failed", msg) == "failed"


# ---------------------------------------------------------------------------
# M8 — latest-release question routes to the browser when connected
# ---------------------------------------------------------------------------

def test_latest_release_question_routes_to_browser_when_connected():
    assert classify_intent(
        "What is the latest Pixel phone released?",
        context={"extension_connected": True}).intent == TaskIntent.NEW_BROWSER_TASK
    assert classify_intent(
        "What's the newest Pixel tablet out?",
        context={"extension_connected": True}).intent == TaskIntent.NEW_BROWSER_TASK


def test_latest_release_question_stays_conversational_when_disconnected():
    # No extension: the turn stays conversational and answers honestly
    # from memory, exactly as before the fix.
    assert classify_intent(
        "What is the latest Pixel phone released?",
        context={"extension_connected": False}).intent == TaskIntent.NON_BROWSER
    assert classify_intent(
        "What is the latest Pixel phone released?",
        context={}).intent == TaskIntent.NON_BROWSER
    assert classify_intent(
        "What is the latest Pixel phone released?").intent == TaskIntent.NON_BROWSER


def test_non_release_questions_stay_conversational_even_when_connected():
    assert classify_intent(
        "What is the latest news?",
        context={"extension_connected": True}).intent == TaskIntent.NON_BROWSER
    assert classify_intent(
        "Which is the latest from these results?",
        context={"extension_connected": True}).intent == TaskIntent.NON_BROWSER
    assert classify_intent(
        "What is the capital of France?",
        context={"extension_connected": True}).intent == TaskIntent.NON_BROWSER
