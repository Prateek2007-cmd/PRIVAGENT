"""Stream 5 — generalisation tests (NAVIGATOR intent-routing fix, base d423388).

Offline handler-level tests: they drive
``handle_continuous_monitoring_instruction`` (the deterministic companion
path) against a throwaway SQLite DB — no live server, no model calls.
Every prompt here is NOVEL (none copies the user's 12-step examples).

Tests marked xfail document BASELINE failures found by the live
12-step + 36-prompt runs (evidence/09-conversation-verification.md and
evidence/10-generalisation.md). An xfail that starts passing on the
merged build is the fix working; xfail(strict=False) so XPASS does not
break the suite.
"""
import asyncio
import re

import pytest

from app import db
from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "s5-gen.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _run(instruction, db_path, chat_id="s5-chat", history=None):
    return asyncio.run(handle_continuous_monitoring_instruction(
        instruction, chat_id=chat_id, history=history, db_path=db_path))


def _history_for(turns):
    """Live-path history shape: [{"role": ..., "content": ...}]."""
    return [{"role": r, "content": c} for r, c in turns]


def _missions(db_path):
    with db.get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at ASC")]


def _email_missions(db_path):
    return db.list_email_missions(db_path)


def _create_watch(db_path, instruction):
    answer = _run(instruction, db_path)
    match = re.search(r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->", answer)
    assert match, "watch creation returned no watch marker: " + answer[:300]
    return match.group(1), answer


# --- Baseline-correct behaviour (must keep passing) -----------------------

def test_novel_product_interval_absolute_price(database):
    """Novel product + novel interval (7 min) + absolute-price condition."""
    mission_id, _ = _create_watch(
        database,
        "watch the Sony WH-1000XM5 headphones, alert me if it drops below "
        "\u20b920000, check every 7 minutes")
    rows = _missions(database)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == mission_id
    assert row["interval_seconds"] == 420
    assert row["status"] == "active"
    assert "WH-1000XM5" in row["title"] or "WH-1000XM5" in row["target_url"]
    assert _email_missions(database) == []


def test_bare_interval_creates_no_watch_and_no_email_change(database):
    """Failure A probe: a bare interval with NO object must not create a
    watch and must never touch email monitoring."""
    answer = _run("check every 10 minutes", database)
    assert _missions(database) == []
    assert _email_missions(database) == []
    assert "10" in answer or "which" in answer.lower()


def test_email_interval_creates_no_product_watch(database):
    """An email object routes to email, never to a product watch."""
    _run("check my email every 10 minutes", database)
    assert _missions(database) == []
    assert _email_missions(database) == []


def test_update_named_watch_interval(database):
    """'Change that watch to every 15 minutes' after a named creation.

    Live baseline L8 resolves this via the chat history the live path
    passes in; the handler needs that same history (regression guard:
    without it the handler answers 'no product to watch yet')."""
    create_text = "Monitor the price of Galaxy S26 Ultra 5G every 5 minutes."
    mission_id, create_answer = _create_watch(database, create_text)
    history = _history_for([("user", create_text), ("assistant", create_answer)])
    answer = _run("Change that watch to every 15 minutes.", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["id"] == mission_id
    assert rows[0]["interval_seconds"] == 900, answer[:300]


# --- Baseline failures (live-verified; xfail until Streams 1/2 fix them) --

@pytest.mark.xfail(strict=False, reason="S5 baseline L10: ordinal 'first watch' "
                  "resolves to ref '.' in _WATCH_CONTROL_RE; no watch paused")
def test_pause_first_watch_ordinal(database):
    t1 = "Monitor the price of Galaxy S26 Ultra 5G every 5 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Live monitor iPhone 17 when it is 10% off. Check every 10 minutes."
    _, a2 = _create_watch(database, t2)
    history = _history_for([("user", t1), ("assistant", a1),
                            ("user", t2), ("assistant", a2)])
    answer = _run("Pause the first watch.", database, history=history)
    rows = _missions(database)
    paused = [r for r in rows if r["status"] == "paused"]
    assert len(paused) == 1, answer[:300]
    assert "Galaxy S26" in paused[0]["title"]


@pytest.mark.xfail(strict=False, reason="S5 baseline L12 / gen B10: brand noun "
                  "BEFORE 'watch' is never captured by _WATCH_CONTROL_RE (ref '.')")
def test_cancel_watch_by_brand_before_watch(database):
    t1 = "Monitor the price of Galaxy S26 Ultra 5G every 5 minutes."
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run("Cancel the Samsung watch.", database, history=history)
    assert _missions(database) == [], answer[:300]


@pytest.mark.xfail(strict=False, reason="S5 gen D11: pronoun-only 'cancel it' "
                  "bypasses the watch-control handler; model fabricates a "
                  "cancellation while the DB row stays active")
def test_cancel_watch_by_pronoun(database):
    t1 = "watch boAt Airdopes 141, alert me under \u20b91200, check every hour"
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run("cancel it", database, history=history)
    assert _missions(database) == [], answer[:300]


def test_update_interval_by_pronoun(database):
    """'Change it to every 45 mins' with chat history: the deterministic
    handler DOES update the interval (2700s). The live gen-B5 failure was
    UPSTREAM of this handler — the live agentLoop/model path misrouted
    the turn into the research/artifact pipeline (empty
    Technical_Analysis.pdf) and never called the handler. That live
    misroute is documented in evidence/10-generalisation.md (B5); this
    test guards the handler half of the fix."""
    t1 = ("keep an eye on Sony WH-1000XM6 headphones, ping me if it "
          "drops below ₹25000. check every 20 mins")
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run("change it to every 45 mins", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 2700, answer[:300]


@pytest.mark.xfail(strict=False, reason="S5 gen D10: relative update 'twice as "
                  "often' is claimed in prose (30 min) but the DB row stays "
                  "at 3600s")
def test_relative_interval_twice_as_often(database):
    t1 = "watch boAt Airdopes 141, alert me under \u20b91200, check every hour"
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run("make that check twice as often", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 1800, answer[:300]


@pytest.mark.xfail(strict=False, reason="S5 baseline L9: 'show both watches' "
                  "reports only the most recently touched watch in full; "
                  "the earlier watch's id/interval/target are 'not confirmed'")
def test_list_both_watches_with_ids(database):
    t1 = "Live monitor iPhone 17 when it is 10% off. Check every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Galaxy S26 Ultra 5G every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _history_for([("user", t1), ("assistant", a1),
                            ("user", t2), ("assistant", a2)])
    answer = _run("Show me both product watches.", database, history=history)
    rows = _missions(database)
    assert len(rows) == 2
    for row in rows:
        assert row["id"] in answer, "list omits watch " + row["title"]
        assert str(row["interval_seconds"] // 60) in answer
