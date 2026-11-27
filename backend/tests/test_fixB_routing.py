"""FIX-B (NAVIGATOR routing fix, round 2): regression tests for M3, M4,
M5, M6 and M10.

Every prompt/product here is novel — none is copied from the
FINAL-VERIFY-REPORT reproductions (no Sony XM6, no Kindle, no Tokyo
booking). The report's exact shapes were probed manually during the
fix and are recorded in fixB-REPORT.md; these tests pin the same
behaviour with fresh inputs so a future refactor cannot silently
reintroduce it.
"""
import asyncio
import json

import pytest

from app import db
from app.agentLoop import _deliver_pending_ask_user_answer, _plausible_ask_user_answer
from app.continuous_monitoring_companion import (
    _extract_monitored_product,
    _is_retailer_or_place_product,
    _parse_interval_seconds,
    handle_continuous_monitoring_instruction,
    is_continuous_monitoring_directive,
)
from app.hub import TaskRecord, hub as real_hub
from app.intent_router import classify_intent
from app.objective import build_objective


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "fixb.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db(path)
    return path


def _run(coro):
    return asyncio.run(coro)


def _rows(database, where="1=1"):
    with db.get_connection(database) as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM monitoring_missions WHERE {where} ORDER BY created_at"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# M3 — bare-name monitor creation is routing-deterministic
# ---------------------------------------------------------------------------

def test_bare_name_keep_an_eye_on_routes_and_creates_directly(database):
    """A bare product name with a threshold + a misspelled cadence
    creates a watch via the store search URL — no URL is ever
    demanded for a named product."""
    ask = (
        "keep an eye on OnePlus Nord Buds 3, ping me if it drops below \u20b92200. "
        "check evry 25 mins"
    )
    assert is_continuous_monitoring_directive(ask) is True
    obj = build_objective(ask)
    assert obj.primary_intent == "monitor_product"
    assert obj.interval_seconds == 1500
    assert (obj.entities or {}).get("product") == "OnePlus Nord Buds 3"
    assert classify_intent(ask, context={"conversation_history": []}).reason == (
        "Continuous background monitoring directive"
    )
    reply = _run(handle_continuous_monitoring_instruction(
        ask, chat_id="m3", db_path=database))
    assert "privagent-watch:" in reply
    rows = _rows(database)
    assert len(rows) == 1
    assert "Nord Buds" in rows[0]["title"]
    assert rows[0]["interval_seconds"] == 1500
    assert rows[0]["condition_type"] == "below_price"
    assert json.loads(rows[0]["condition_params"])["threshold"] == 2200.0
    assert rows[0]["target_url"].startswith("https://www.flipkart.com/search?q=")
    assert "Nord+Buds" in rows[0]["target_url"]


def test_ping_verb_and_evry_typo_route(database):
    assert is_continuous_monitoring_directive(
        "ping me if the Noise ColorFit Pro 6 drops below \u20b93000") is True
    assert is_continuous_monitoring_directive(
        "watch the Samsung Galaxy Buds 3, check evry 30 mins") is True
    assert _parse_interval_seconds("check evry 30 mins") == 1800
    assert _parse_interval_seconds("check evry 45 mins") == 2700
    assert build_objective(
        "live monitor Galaxy Buds 3, check evry 30 mins").interval_seconds == 1800


def test_plain_statements_still_do_not_route():
    assert is_continuous_monitoring_directive(
        "the Noise ColorFit Pro 6 price is below 3000") is False
    assert is_continuous_monitoring_directive(
        "check the Noise ColorFit Pro 6") is False
    assert is_continuous_monitoring_directive("keep an eye on the kids") is False
    assert is_continuous_monitoring_directive(
        "what is the price of the Noise ColorFit Pro 6") is False


# ---------------------------------------------------------------------------
# M4 — no phantom watch from the assistant's own text
# ---------------------------------------------------------------------------

def test_assistant_price_mention_never_yields_a_product():
    """The assistant's own prose ("launched around \u20b924,990 ... share
    the Amazon.in / Flipkart / Croma / Noise India page") must never
    become the product — the user's own named product wins."""
    history = [
        {"role": "user", "content": (
            "keep an eye on Noise ColorFit Pro 6, ping me if it drops below "
            "\u20b93000. check evry 20 mins")},
        {"role": "assistant", "content": (
            "The ColorFit Pro 6 launched in India around \u20b924,990. To set this up "
            "I need the product URL \u2014 share the Amazon.in / Flipkart / Croma / "
            "Noise India page for it and I will watch it.")},
        {"role": "user", "content": "also tell me if it goes out of stock"},
        {"role": "assistant", "content": (
            "Sure \u2014 once the watch exists I will also flag out of stock. "
            "I still need the product URL.")},
    ]
    title, url = _extract_monitored_product("change it to every 45 mins", history=history)
    assert "noise" in title.lower() and "colorfit" in title.lower(), title
    assert "india" not in title.lower(), title
    assert "ColorFit" in url or "colorfit" in url.lower()


@pytest.mark.parametrize("title,expected", [
    ("Noise India", True),
    ("Noise India Page", True),
    ("Sony India", True),
    ("Noise ColorFit Pro 6", False),
    ("OnePlus Nord Buds 3", False),
    ("Samsung Galaxy Buds 3", False),
])
def test_retailer_or_place_titles_rejected(title, expected):
    assert _is_retailer_or_place_product(title) is expected


def test_follow_up_with_no_watch_asks_instead_of_creating(database):
    """A cadence follow-up with no watch in the DB must ask — never
    create a watch, and never one named after a retailer."""
    history = [
        {"role": "user", "content": (
            "keep an eye on the Aurora X9 earbuds, ping me if it drops below \u20b91800")},
        {"role": "assistant", "content": (
            "The Aurora X9 sells around \u20b92400. Share the Amazon.in / Flipkart / "
            "Croma / Aurora India page and I will watch it.")},
    ]
    reply = _run(handle_continuous_monitoring_instruction(
        "change it to every 45 mins", chat_id="m4", history=history, db_path=database))
    assert _rows(database) == []
    assert "don't have a watch running" in reply
    assert "Aurora X9" in reply
    assert "India" not in reply


def test_create_stock_update_chain_keeps_one_watch(database):
    """Create (bare name) -> stock follow-up -> interval follow-up:
    one row, threshold kept, stock flag stored and preserved."""
    chat, hist = "m4chain", []

    def turn(text):
        reply = _run(handle_continuous_monitoring_instruction(
            text, chat_id=chat, history=list(hist), db_path=database))
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": reply})
        return reply

    reply = turn(
        "keep an eye on Samsung Galaxy Buds 3, ping me if it drops below "
        "\u20b99000. check evry 20 mins")
    assert "privagent-watch:" in reply
    reply = turn("also tell me if it goes out of stock")
    assert "out of stock" in reply.lower()
    assert "did not save" not in reply
    rows = _rows(database)
    assert len(rows) == 1
    params = json.loads(rows[0]["condition_params"])
    assert params.get("stock_alert") is True
    assert params.get("threshold") == 9000.0
    reply = turn("change it to every 45 mins")
    assert "45 minutes" in reply
    rows = _rows(database)
    assert len(rows) == 1
    assert rows[0]["interval_seconds"] == 2700
    assert rows[0]["title"] == "Price Watch: Samsung Galaxy Buds 3"
    params = json.loads(rows[0]["condition_params"])
    assert params.get("threshold") == 9000.0
    assert params.get("stock_alert") is True


def test_stock_asked_at_creation_is_stored(database):
    reply = _run(handle_continuous_monitoring_instruction(
        "watch the OnePlus Nord Buds 3, alert me if it drops below \u20b92200 "
        "and tell me if it goes out of stock, check every hour",
        chat_id="m4stock", db_path=database))
    assert "privagent-watch:" in reply
    rows = _rows(database)
    assert len(rows) == 1
    assert json.loads(rows[0]["condition_params"]).get("stock_alert") is True


# ---------------------------------------------------------------------------
# M5 — list scope: all chats, all statuses
# ---------------------------------------------------------------------------

def test_list_shows_paused_and_other_chat_watches(database):
    _run(handle_continuous_monitoring_instruction(
        "live monitor Galaxy Buds 3 when it is 10% off, check every 10 minutes",
        chat_id="chat-a", db_path=database))
    _run(handle_continuous_monitoring_instruction(
        "watch the Noise ColorFit Pro 6, alert me if it drops below \u20b93000, "
        "check every hour",
        chat_id="chat-b", db_path=database))
    with db.get_connection(database) as conn:
        conn.execute(
            "UPDATE monitoring_missions SET status='paused' WHERE conversation_id='chat-a'")
        conn.commit()
    reply = _run(handle_continuous_monitoring_instruction(
        "show my watches", chat_id="chat-c", history=[], db_path=database))
    assert "Galaxy Buds" in reply
    assert "ColorFit" in reply
    assert "paused" in reply
    assert "active" in reply
    assert "all chats" in reply


def test_list_with_no_watches_states_scope(database):
    reply = _run(handle_continuous_monitoring_instruction(
        "show my watches", chat_id="chat-empty", history=[], db_path=database))
    assert "No watches are set up yet" in reply


# ---------------------------------------------------------------------------
# M6 — summary must not deny a request the user made
# ---------------------------------------------------------------------------

def test_summary_names_unfulfilled_request_from_history(database):
    """A watch ask that never became a watch (clarification-pending)
    is named as asked-but-not-set-up — never denied."""
    chat = db.create_chat(title="m6", db_path=database)["id"]
    db.add_message(chat, "user",
                   "watch the price of the new Fire-Boltt smartwatch and also "
                   "remind me about my 4pm call", db_path=database)
    db.add_message(chat, "assistant",
                   "Which Fire-Boltt model did you mean? I need the model before "
                   "I can set up the watch.", db_path=database)
    _run(handle_continuous_monitoring_instruction(
        "live monitor Galaxy Buds 3 when it is 10% off, check every 10 minutes",
        chat_id=chat, db_path=database))
    assert build_objective("what did I ask you to watch earlier").primary_intent == "list_monitors"
    reply = _run(handle_continuous_monitoring_instruction(
        "what did I ask you to watch earlier", chat_id=chat, history=[], db_path=database))
    assert "Fire-Boltt" in reply
    assert "no watch is set up" in reply
    assert "Galaxy Buds" in reply
    assert "never mentioned" not in reply
    assert "You never" not in reply


def test_summary_with_nothing_is_scoped_not_a_denial(database):
    reply = _run(handle_continuous_monitoring_instruction(
        "what did I ask you to watch earlier", chat_id="m6empty",
        history=[], db_path=database))
    assert "don't see any watch request" in reply
    assert "never mentioned" not in reply
    assert "You never" not in reply


# ---------------------------------------------------------------------------
# M10 — a pending ask-user answer only consumes a plausible answer
# ---------------------------------------------------------------------------

def test_plausible_answer_date_time_novel_pair():
    pending = {"question_id": "f1",
               "question": "Which date should I book the flight for?",
               "kind": "date_time"}
    # Unrelated question asked while the flight date is pending.
    assert _plausible_ask_user_answer(
        pending, "what's the weather in Pune today") is False
    assert _plausible_ask_user_answer(
        pending, "who won the match last night?") is False
    assert _plausible_ask_user_answer(
        pending, "what time is it in London right now") is False
    # Plausible answers to the same pending question.
    assert _plausible_ask_user_answer(pending, "next Monday") is True
    assert _plausible_ask_user_answer(pending, "12 Dec") is True
    assert _plausible_ask_user_answer(pending, "tomorrow") is True
    assert _plausible_ask_user_answer(pending, "what about Friday?") is True


def test_plausible_answer_budget_and_options():
    budget = {"question_id": "b1",
              "question": "What budget should I stay within?",
              "kind": "budget"}
    assert _plausible_ask_user_answer(
        budget, "what is a good budget for a laptop?") is False
    assert _plausible_ask_user_answer(budget, "under \u20b95000") is True
    choice = {"question_id": "c1", "question": "pick one",
              "kind": "preference", "options": ["tomorrow", "next week"]}
    assert _plausible_ask_user_answer(choice, "next week") is True
    assert _plausible_ask_user_answer(
        choice, "what time is it in London?") is False


@pytest.mark.asyncio
async def test_intercept_declines_unrelated_question_and_keeps_pending_open(
        monkeypatch, tmp_path):
    """End-to-end at the intercept: a new question is NOT delivered
    to the paused task, the paused task stays awaiting_input, and the
    new task is told the question is still open."""
    path = str(tmp_path / "m10.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db(path)
    chat_id = db.create_chat(title="m10", db_path=path)["id"]
    paused = TaskRecord(task_id="paused-1",
                        instruction="Book me a flight to Delhi",
                        chat_id=chat_id)
    paused.state = "awaiting_input"
    paused.pending_ask_user = {
        "question_id": "f1",
        "question": "Which date should I book the flight for?",
        "kind": "date_time",
    }
    paused.ask_user_event = asyncio.Event()
    incoming = TaskRecord(task_id="msg-1",
                          instruction="what's the weather in Pune today",
                          chat_id=chat_id)
    real_hub.tasks["paused-1"] = paused
    real_hub.tasks["msg-1"] = incoming
    try:
        consumed = await _deliver_pending_ask_user_answer(incoming)
        assert consumed is False
        assert paused.state == "awaiting_input"
        assert getattr(paused, "ask_user_answer", None) is None
        assert getattr(incoming, "deferred_ask_user", None) is not None
        assert "Which date should I book the flight for?" in (
            incoming.deferred_ask_user.get("question") or "")
        # A plausible answer IS still consumed.
        incoming2 = TaskRecord(task_id="msg-2", instruction="next Monday",
                               chat_id=chat_id)
        real_hub.tasks["msg-2"] = incoming2
        consumed = await _deliver_pending_ask_user_answer(incoming2)
        assert consumed is True
        assert incoming2.state == "completed"
        assert paused.ask_user_answer == "next Monday"
    finally:
        for tid in ("paused-1", "msg-1", "msg-2"):
            real_hub.tasks.pop(tid, None)
