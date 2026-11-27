"""Stream 2 follow-up — watch-control reference resolution (Stream 5 F1/F2/F4/D10).

Drives ``handle_continuous_monitoring_instruction`` against a throwaway
SQLite DB — no live server, no model calls. Every prompt here is NOVEL:
none copies the user's 12-step examples, and none reuses Stream 5's
test products (Sony WH-1000XM5/XM6, Galaxy S26, iPhone 17, boAt
Airdopes 141). Products here (Aurora X9 earbuds, Vertex Pro laptop,
Nimbus Trail shoes, Kestrel 4K drone, Halcyon keyboard, Galaxy Tab S11,
Galaxy Buds 3) are invented for these tests.

Every control test asserts the MESSAGE and the DB ROW together:
Stream 5's baseline had 5 false confirmations (message claims
success, DB unchanged). A reply that names a change the DB does not
show is a failure here, not a pass.
"""

import asyncio
import re

import pytest

from app import db
from app.continuous_monitoring_companion import (
    handle_continuous_monitoring_instruction,
    is_continuous_monitoring_directive,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "wc-watch.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _run(instruction, db_path, chat_id="wc-chat", history=None):
    return asyncio.run(handle_continuous_monitoring_instruction(
        instruction, chat_id=chat_id, history=history, db_path=db_path))


def _history_for(turns):
    """Live-path history shape: [{"role": ..., "content": ...}]."""
    return [{"role": r, "content": c} for r, c in turns]


def _missions(db_path):
    with db.get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at ASC")]


def _create_watch(db_path, instruction):
    answer = _run(instruction, db_path)
    match = re.search(r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->", answer)
    assert match, "watch creation returned no watch marker: " + answer[:300]
    return match.group(1), answer


def _watch_marker_history(*created):
    """History of (user turn, assistant answer) pairs that created watches."""
    turns = []
    for user_text, assistant_answer in created:
        turns += [("user", user_text), ("assistant", assistant_answer)]
    return _history_for(turns)


# --- ordinal control (Stream 5 F2: "Pause the first watch") ---------------

def test_pause_first_watch_ordinal_pauses_the_oldest(database):
    t1 = "Monitor the price of Aurora X9 earbuds every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Live monitor Nimbus Trail shoes when it is 10% off. Check every 15 minutes."
    _, a2 = _create_watch(database, t2)
    history = _watch_marker_history((t1, a1), (t2, a2))
    answer = _run("Pause the first watch.", database, history=history)
    rows = _missions(database)
    paused = [r for r in rows if r["status"] == "paused"]
    assert len(paused) == 1, answer[:300]
    assert "Aurora X9" in paused[0]["title"]
    assert "Aurora X9" in answer
    assert [r for r in rows if r["status"] == "active"][0]["title"].find("Nimbus Trail") >= 0


def test_cancel_latest_watch_cancels_the_most_recent(database):
    t1 = "Monitor the price of Aurora X9 earbuds every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Vertex Pro laptop every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _watch_marker_history((t1, a1), (t2, a2))
    answer = _run("Cancel the latest watch.", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert "Aurora X9" in rows[0]["title"]
    assert "Vertex Pro" in answer


# --- brand before "watch" (Stream 5 F2: "Cancel the Samsung watch") ------

def test_cancel_watch_by_brand_before_watch(database):
    t1 = "Monitor the price of Galaxy Tab S11 every 5 minutes."
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("Cancel the Samsung watch.", database, history=history)
    assert _missions(database) == [], answer[:300]
    assert "Galaxy Tab S11" in answer


def test_ambiguous_brand_clarifies_and_writes_nothing(database):
    t1 = "Monitor the price of Galaxy Tab S11 every 5 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Galaxy Buds 3 every 10 minutes."
    _, a2 = _create_watch(database, t2)
    history = _watch_marker_history((t1, a1), (t2, a2))
    answer = _run("Cancel the Samsung watch.", database, history=history)
    rows = _missions(database)
    assert len(rows) == 2 and all(r["status"] == "active" for r in rows)
    assert "?" in answer
    assert "Galaxy Tab S11" in answer and "Galaxy Buds 3" in answer


def test_unknown_brand_clarifies_and_writes_nothing(database):
    t1 = "Monitor the price of Aurora X9 earbuds every 10 minutes."
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("Cancel the Zebra watch.", database, history=history)
    assert len(_missions(database)) == 1
    assert _missions(database)[0]["status"] == "active"
    assert "Zebra" in answer and "?" in answer


# --- pronoun control (Stream 5 F1: "cancel it" fabricated the cancel) ----

def test_cancel_watch_by_pronoun_deletes_the_row(database):
    t1 = "watch the Halcyon keyboard, alert me under \u20b95000, check every hour"
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("cancel it", database, history=history)
    assert _missions(database) == [], answer[:300]
    assert "Halcyon" in answer


def test_cancel_watch_by_pronoun_without_history_still_cancels(database):
    """The cancel path resolves from the DB (synced working context),
    not from the history: a turn that arrives with no history must
    still cancel the chat's watch instead of creating a second one."""
    t1 = "watch the Halcyon keyboard, alert me under \u20b95000, check every hour"
    _create_watch(database, t1)
    answer = _run("cancel it", database, history=None)
    assert _missions(database) == [], answer[:300]
    assert "Halcyon" in answer


def test_resume_by_pronoun_after_pause(database):
    t1 = "watch the Kestrel 4K drone, alert me under \u20b940000, check every 20 minutes"
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    pause_answer = _run("Pause it.", database, history=history)
    assert _missions(database)[0]["status"] == "paused", pause_answer[:300]
    resume_answer = _run("Resume it.", database, history=history)
    assert _missions(database)[0]["status"] == "active", resume_answer[:300]
    assert "Kestrel 4K" in resume_answer


def test_pause_already_paused_watch_reports_honestly(database):
    t1 = "watch the Kestrel 4K drone, alert me under \u20b940000, check every 20 minutes"
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    _run("Pause it.", database, history=history)
    answer = _run("Pause it.", database, history=history)
    assert _missions(database)[0]["status"] == "paused"
    assert "already paused" in answer.lower()


def test_pause_the_headphones_one_resolves_by_product_token(database):
    t1 = "watch the Aurora X9 earbuds, alert me under \u20b93000, check every 10 minutes"
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Vertex Pro laptop every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _watch_marker_history((t1, a1), (t2, a2))
    answer = _run("pause the earbuds one", database, history=history)
    rows = _missions(database)
    paused = [r for r in rows if r["status"] == "paused"]
    assert len(paused) == 1 and "Aurora X9" in paused[0]["title"], answer[:300]


# --- list watches from the DB (Stream 5 F4) --------------------------------

def test_list_both_watches_with_ids_and_intervals(database):
    t1 = "Live monitor Aurora X9 earbuds when it is 10% off. Check every 10 minutes."
    _, a1 = _create_watch(database, t1)
    t2 = "Monitor the price of Vertex Pro laptop every 5 minutes."
    _, a2 = _create_watch(database, t2)
    history = _watch_marker_history((t1, a1), (t2, a2))
    answer = _run("Show me both product watches.", database, history=history)
    rows = _missions(database)
    assert len(rows) == 2
    for row in rows:
        assert row["id"] in answer, "list omits watch " + row["title"]
        assert str(row["interval_seconds"] // 60) in answer


def test_list_with_no_watches_is_honest(database):
    answer = _run("Show me both product watches.", database)
    assert "no watches" in answer.lower()
    assert _missions(database) == []


# --- relative interval (Stream 5 D10: "twice as often" claimed, DB 3600) --

def test_relative_interval_twice_as_often(database):
    t1 = "watch the Nimbus Trail shoes, alert me under \u20b96000, check every hour"
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("make that check twice as often", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 1800, answer[:300]
    assert "30 minutes" in answer


def test_relative_interval_half_as_often(database):
    t1 = "watch the Aurora X9 earbuds, alert me under \u20b93000, check every 10 minutes"
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("make that check half as often", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 1200, answer[:300]
    assert "20 minutes" in answer


def test_absolute_interval_change_by_pronoun_updates_db(database):
    t1 = ("watch the Vertex Pro laptop, alert me if it drops "
          "below \u20b980000, check every 20 minutes")
    _, a1 = _create_watch(database, t1)
    history = _watch_marker_history((t1, a1))
    answer = _run("change it to every 45 mins", database, history=history)
    rows = _missions(database)
    assert len(rows) == 1 and rows[0]["interval_seconds"] == 2700, answer[:300]


# --- routing: is_continuous_monitoring_directive ----------------------------

def _marker_history():
    return _history_for([
        ("user", "watch the Aurora X9 earbuds, check every 10 minutes"),
        ("assistant", "Watching the Aurora X9 earbuds. "
                      "<!-- privagent-watch: aaaaaaaa-1111-2222-3333-444444444444 -->"),
    ])


@pytest.mark.parametrize("text", [
    "cancel it",
    "Resume it.",
    "change it to every 45 mins",
    "make that check twice as often",
    "pause the earbuds one",
    "Pause the first watch.",
    "Cancel the Samsung watch.",
])
def test_watch_follow_ups_route_with_a_watch_marker(text):
    assert is_continuous_monitoring_directive(text, _marker_history()) is True


@pytest.mark.parametrize("text", [
    "cancel it",
    "Resume it.",
    "change it to every 45 mins",
    "make that check twice as often",
])
def test_watch_follow_ups_do_not_route_without_a_watch(text):
    assert is_continuous_monitoring_directive(text, []) is False
    assert is_continuous_monitoring_directive(text) is False


def test_list_watches_routes_without_history():
    assert is_continuous_monitoring_directive("Show me both product watches.") is True


def test_bare_watches_ask_without_product_or_marker_does_not_route():
    assert is_continuous_monitoring_directive("Show me both watches.") is False


def test_existing_false_cases_still_do_not_route():
    assert is_continuous_monitoring_directive("check the Amazfit Helio Strap") is False
    assert is_continuous_monitoring_directive(
        "what is the price of the Aurora X9 earbuds") is False
