"""Stream 2 — Failure E format-string regression.

Root cause (reproduced before the fix): ``condition_params`` is an
untyped JSON blob — the missions API stores whatever the caller
sent, so a threshold can arrive as the STRING "5000". The monitor
handler's duplicate/update branch then formatted it with ``:g``::

    continuous_monitoring_companion.py:1777
        f", alerting below ₹{stored_threshold:g}"
    ValueError: Unknown format code 'g' for object of type 'str'

so an interval-change follow-up ("change it to every twelve
minutes") on such a watch crashed the whole reply instead of
updating the watch. The same hazard existed for a string
``min_percent`` via ``_percent_threshold_text``.

All prompts/products below are novel (Kestrel 4K drone, Nimbus
Trail shoes) — never the user's literal examples.
"""

import asyncio
import json
import time
import uuid

import pytest

from app import db
from app.continuous_monitoring_companion import (
    _percent_threshold_text,
    handle_continuous_monitoring_instruction,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "fmt.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _seed_watch(database, *, title, condition_type, params, interval_seconds=300):
    mission_id = str(uuid.uuid4())
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, conversation_id, title, target_url, trigger_type,
                condition_type, condition_params, interval_seconds,
                schedule_cron, status, created_at, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (mission_id, "chat-fmt", title,
             "https://example.org/kestrel-4k", "interval", condition_type,
             json.dumps(params), interval_seconds, None,
             time.time(), time.time()))
        conn.commit()
    return mission_id


def test_string_threshold_interval_change_does_not_raise(database):
    """The reproduced crash: stored threshold "40000" (string) +
    an interval-change follow-up. Must update the watch, not raise."""
    _seed_watch(
        database,
        title="Price Watch: Kestrel 4K Drone",
        condition_type="below_price",
        params={"target": "Kestrel 4K Drone", "threshold": "40000"},
        interval_seconds=300,
    )
    history = [{"role": "user",
                "content": "watch the kestrel 4k drone below 40000"}]
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "change it to every twelve minutes",
        chat_id="chat-fmt", history=history, db_path=database))
    assert "every 12 minutes" in answer
    assert "₹40000" in answer
    assert "Updated the watch" in answer
    with db.get_connection(database) as conn:
        row = conn.execute(
            "SELECT interval_seconds FROM monitoring_missions WHERE status='active'"
        ).fetchone()
    assert row["interval_seconds"] == 720


def test_string_threshold_same_settings_is_already_running(database):
    """A string threshold must compare equal to the parsed float:
    re-asking with the same settings says "already running" instead
    of taking the update branch (which used to crash on :g)."""
    _seed_watch(
        database,
        title="Price Watch: Kestrel 4K Drone",
        condition_type="below_price",
        params={"target": "Kestrel 4K Drone", "threshold": "40000"},
        interval_seconds=720,
    )
    history = [{"role": "user",
                "content": "watch the kestrel 4k drone below 40000"}]
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "change it to every twelve minutes",
        chat_id="chat-fmt", history=history, db_path=database))
    assert "already running" in answer
    assert "every 12 minutes" in answer


def test_string_min_percent_interval_change_does_not_raise(database):
    """Same hazard for a %-drop watch whose min_percent is "15"."""
    _seed_watch(
        database,
        title="Price Watch: Nimbus Trail Shoes",
        condition_type="price_drop",
        params={"target": "Nimbus Trail Shoes", "min_percent": "15"},
        interval_seconds=300,
    )
    history = [{"role": "user",
                "content": "watch the nimbus trail shoes when they drop 15%"}]
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "change it to every twenty minutes",
        chat_id="chat-fmt", history=history, db_path=database))
    assert "every 20 minutes" in answer
    assert "15%" in answer


def test_percent_threshold_text_accepts_string_and_garbage():
    assert _percent_threshold_text("15") == ", alerting when the price drops 15% or more"
    assert _percent_threshold_text(0) == ", alerting on any price drop"
    assert _percent_threshold_text("not-a-number") == ""
    assert _percent_threshold_text(None) == ""
