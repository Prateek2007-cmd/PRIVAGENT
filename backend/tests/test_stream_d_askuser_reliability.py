"""Stream D (CONTRACTS3): ask-user-before-extension, no false calendar
claims, verified-only skill saves, {"text": ...} chat messages, and
terminal task records that survive a backend restart.

Live-provider tests are NOT here — they live in D-REPORT.md (isolated
backend copy + FreePi). These tests pin the deterministic behaviour
with fakes, and drive run_task end to end where no browser is needed.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import agentLoop, db
from app.agentLoop import (
    _detect_trip_missing_info,
    _parse_reminder,
    answer_ask_user,
    run_task,
)
from app.hub import TaskRecord, hub as real_hub
from app.long_memory import (
    create_executable_skill,
    extract_skill_inputs,
    replay_skill_steps,
)
from app.main import app


# ---------------------------------------------------------------------------
# Pure detectors
# ---------------------------------------------------------------------------

def test_trip_plan_without_date_asks_for_dates_and_origin():
    missing = _detect_trip_missing_info("Plan a trip to Bengaluru.", home_origin="DEL")
    assert missing is not None
    assert missing["kind"] == "date_time"
    assert "DEL" in missing["question"]
    assert "dates" in missing["question"].lower()


def test_trip_plan_with_date_does_not_ask():
    assert _detect_trip_missing_info("Plan a trip to Bengaluru 12-14 Dec.") is None
    assert _detect_trip_missing_info("Plan a trip to Bengaluru tomorrow.") is None


def test_book_train_without_date_asks():
    missing = _detect_trip_missing_info("Book me a train to Bengaluru")
    assert missing is not None
    assert "date" in missing["question"].lower()
    assert _detect_trip_missing_info("Book me a train to Bengaluru tomorrow") is None


def test_reminder_parse_once_and_daily():
    once = _parse_reminder("Remind me to call mom tomorrow 9am")
    assert once is not None and once["what"] == "call mom"
    assert once["time"]["kind"] == "once"
    assert once["time"]["delay_minutes"] >= 60
    rel = _parse_reminder("Remind me to call mom in 30 minutes")
    assert rel is not None and rel["time"] == {"kind": "once", "delay_minutes": 30,
                                               "label": "in 30 minutes"}
    daily = _parse_reminder("Remind me to stretch daily at 9am")
    assert daily is not None and daily["what"] == "stretch"
    assert daily["time"]["kind"] == "daily"
    assert daily["time"]["utc_hhmm"] == "03:30"  # 09:00 IST == 03:30 UTC
    assert _parse_reminder("Remind me to call mom") is None  # no time yet
    assert _parse_reminder("What is my home airport?") is None


def test_reminder_parse_uses_answer_not_question_examples():
    """Live bug 2026-11-27: the ask-user question contains the example
    'in 30 minutes', and the parser matched it instead of the
    operator's 'in 2 minutes' answer, creating a 30-minute job."""
    instruction = (
        "Remind me to call mom\n"
        "[Operator's answer to 'When should I remind you? For example "
        "'tomorrow 9am' or 'in 30 minutes'.': in 2 minutes]"
    )
    parsed = _parse_reminder(instruction)
    assert parsed is not None
    assert parsed["what"] == "call mom"
    assert parsed["time"]["delay_minutes"] == 2


def test_skill_inputs_extraction_is_deterministic():
    assert extract_skill_inputs("open https://example.com/flights", ["url"]) == {
        "url": "https://example.com/flights"}
    assert extract_skill_inputs("search flights", ["origin", "destination"]) == {}


# ---------------------------------------------------------------------------
# Skill save gate: never from a failed run
# ---------------------------------------------------------------------------

def _skill_steps():
    return [
        {"action": "navigate", "value": "https://example.com/flights", "verify": "url changed"},
        {"action": "click", "ref": "e0", "verify": "element present"},
    ]


def test_skill_save_refused_from_failed_run(tmp_path, monkeypatch):
    path = str(tmp_path / "skills.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db(path)
    db.save_task_record("task-failed", instruction="do a thing", state="failed",
                        steps_done=1, message="broke", db_path=path)
    with pytest.raises(ValueError, match="no verified completed run"):
        create_executable_skill("do a thing", _skill_steps(), source_task_id="task-failed",
                                db_path=path)
    assert db.get_task_record("task-failed", db_path=path)["state"] == "failed"


def test_skill_save_allowed_from_completed_run_and_params_round_trip(tmp_path, monkeypatch):
    path = str(tmp_path / "skills.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db(path)
    db.save_task_record("task-ok", instruction="do a thing", state="completed",
                        steps_done=2, message="done", db_path=path)
    created = create_executable_skill(
        "do a thing",
        [{"action": "navigate", "value": "{url}", "verify": "url changed"}],
        source_task_id="task-ok", params=["url"],
        preconditions=["operator is on the flights site"], db_path=path)
    assert created["executable"] is True
    assert created["params"] == ["url"]
    with pytest.raises(ValueError, match="undeclared parameter"):
        create_executable_skill(
            "do a thing",
            [{"action": "navigate", "value": "{nope}", "verify": "url changed"}],
            source_task_id="task-ok", db_path=path)


@pytest.mark.asyncio
async def test_replay_substitutes_inputs_and_fails_fast_without_them():
    steps = [
        {"action": "navigate", "value": "{url}", "verify": "url changed"},
        {"action": "type", "ref": "e1", "value": "{query}", "verify": "element present"},
    ]
    seen_actions: list[dict] = []
    obs_calls = {"n": 0}
    nav = {"url": "about:blank"}

    async def fake_send_action(action):
        seen_actions.append(action)
        if action.get("type") == "navigate":
            nav["url"] = action.get("params", {}).get("url", "")
        return {"ok": True, "observation": {"url": nav["url"],
                                            "interactive": [{"elementRef": "e1", "role": "textbox"}]}}

    async def fake_observe():
        obs_calls["n"] += 1
        return {"url": nav["url"],
                "interactive": [{"elementRef": "e1", "role": "textbox"}]}

    # Missing input: nothing executes, fallback is the planner.
    missing = await replay_skill_steps(steps, send_action=fake_send_action,
                                       observe=fake_observe)
    assert missing["ok"] is False
    assert missing["fallback"] == "planner"
    assert missing["planner_calls"] == 0
    assert seen_actions == []
    # Supplied inputs: the substituted values are what executes.
    done = await replay_skill_steps(
        steps, send_action=fake_send_action, observe=fake_observe,
        inputs={"url": "https://example.com/flights", "query": "Goa"})
    assert done["ok"] is True
    assert done["steps_done"] == 2
    assert done["planner_calls"] == 0
    assert seen_actions[0]["params"]["url"] == "https://example.com/flights"
    assert seen_actions[1]["params"]["text"] == "Goa"
    assert obs_calls["n"] >= 2  # observe-before + observe-after per step


# ---------------------------------------------------------------------------
# Chat API: {"text": ...} must not 500
# ---------------------------------------------------------------------------

def test_chat_message_text_field_returns_task(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "chat.db"))
    db.init_db()
    # NOTE: `with TestClient` keeps the app portal alive so the
    # background run_task spawned by the POST can actually run; a bare
    # TestClient drops it when the request returns.
    with TestClient(app) as client:
        chat = client.post("/api/chat", json={"title": "t"}).json()
        res = client.post(f"/api/chat/{chat['chatId']}/message", json={"text": "hi"})
        assert res.status_code == 200, res.text
        assert res.json()["taskId"]
        # The greeting fast path completes without a provider call.
        task_id = res.json()["taskId"]
        deadline = time.time() + 10
        while time.time() < deadline:
            got = client.get(f"/api/task/{task_id}")
            if got.status_code == 200 and got.json()["taskState"] == "completed":
                break
            time.sleep(0.2)
        else:
            raise AssertionError("greeting task did not complete")
        assert "Hello" in client.get(f"/api/task/{task_id}").json()["message"]


def test_chat_message_blank_is_422_not_500(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "chat.db"))
    db.init_db()
    client = TestClient(app)
    chat = client.post("/api/chat", json={"title": "t"}).json()
    for body in ({"text": "   "}, {}, {"text": 123}, {"instruction": "  "}):
        res = client.post(f"/api/chat/{chat['chatId']}/message", json=body)
        assert res.status_code == 422, (body, res.status_code, res.text)
        assert res.status_code != 500
        assert "text" in res.json()["detail"].lower()


def test_terminal_task_record_survives_restart(monkeypatch, tmp_path):
    """RC-LIVE-05: wipe the in-memory hub (a restart) — the completed
    task must still be readable via GET /api/task/{id}."""
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "chat.db"))
    db.init_db()
    with TestClient(app) as client:
        chat = client.post("/api/chat", json={"title": "t"}).json()
        task_id = client.post(f"/api/chat/{chat['chatId']}/message", json={"text": "hi"}).json()["taskId"]
        deadline = time.time() + 10
        while time.time() < deadline:
            got = client.get(f"/api/task/{task_id}")
            if got.status_code == 200 and got.json()["taskState"] == "completed":
                break
            time.sleep(0.2)
        else:
            raise AssertionError("greeting task did not complete")
        # Simulate a backend restart: the hub registry is empty.
        real_hub.tasks.clear()
        got = client.get(f"/api/task/{task_id}")
    assert got.status_code == 200, got.text
    assert got.json()["taskState"] == "completed"
    assert got.json()["taskId"] == task_id


# ---------------------------------------------------------------------------
# run_task end to end (no browser, no provider)
# ---------------------------------------------------------------------------

def _isolated_chat(monkeypatch, tmp_path, name):
    path = str(tmp_path / name)
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db(path)
    return db.create_chat(title="stream-d", db_path=path)["id"]


@pytest.mark.asyncio
async def test_trip_plan_pauses_then_reports_blocked(monkeypatch, tmp_path):
    """DEMO2 §11b: 'Plan a trip to Bengaluru.' pauses for dates BEFORE
    the extension check, resumes the SAME task on the answer, and then
    reports blocked (not failed) when Chrome is not connected."""
    chat_id = _isolated_chat(monkeypatch, tmp_path, "trip.db")
    assert real_hub.extension_sockets == []
    record = TaskRecord(task_id="trip-1", instruction="Plan a trip to Bengaluru.",
                        chat_id=chat_id)
    real_hub.tasks["trip-1"] = record
    try:
        running = asyncio.create_task(run_task("trip-1"))
        deadline = time.time() + 10
        while time.time() < deadline and record.state != "awaiting_input":
            await asyncio.sleep(0.1)
        assert record.state == "awaiting_input", record.state
        assert "dates" in (record.message or "").lower()
        assert answer_ask_user("trip-1", "Delhi, 12-14 Dec, under 10000") is True
        await asyncio.wait_for(running, timeout=15)
        assert record.task_id == "trip-1"
        assert record.state == "blocked", (record.state, record.message)
        assert "extension" in record.message.lower()
        assert "not connected" in record.message.lower()
        # Durable record: the blocked outcome survives a restart.
        row = db.get_task_record("trip-1")
        assert row is not None and row["state"] == "blocked"
        cp = db.get_task_checkpoint("trip-1")
        assert cp is not None
        assert cp["working_memory"].get("final_state") == "blocked"
    finally:
        real_hub.tasks.pop("trip-1", None)
        real_hub.extension_sockets.clear()


@pytest.mark.asyncio
async def test_browser_task_without_missing_info_is_blocked_not_failed(monkeypatch, tmp_path):
    chat_id = _isolated_chat(monkeypatch, tmp_path, "open.db")
    record = TaskRecord(task_id="open-1", instruction="Open google.com",
                        chat_id=chat_id)
    real_hub.tasks["open-1"] = record
    try:
        await asyncio.wait_for(run_task("open-1"), timeout=15)
        assert record.state == "blocked", (record.state, record.message)
        assert "EXTENSION_DISCONNECTED" not in record.message
    finally:
        real_hub.tasks.pop("open-1", None)


@pytest.mark.asyncio
async def test_reminder_no_time_pauses_then_creates_job_not_calendar(monkeypatch, tmp_path):
    """DEMO2 §11d: the reminder pauses for the time, then creates a
    real scheduled job with a receipt — and never claims a calendar
    write."""
    chat_id = _isolated_chat(monkeypatch, tmp_path, "remind.db")
    record = TaskRecord(task_id="rem-1", instruction="Remind me to call mom",
                        chat_id=chat_id)
    real_hub.tasks["rem-1"] = record
    try:
        running = asyncio.create_task(run_task("rem-1"))
        deadline = time.time() + 10
        while time.time() < deadline and record.state != "awaiting_input":
            await asyncio.sleep(0.1)
        assert record.state == "awaiting_input", record.state
        assert answer_ask_user("rem-1", "tomorrow 9am") is True
        await asyncio.wait_for(running, timeout=15)
        assert record.state == "completed", (record.state, record.message)
        assert "scheduled job" in record.message
        assert "No calendar event" in record.message
        assert "Google Calendar" not in record.message
        assert "calendar" in record.message.lower()
        from app.scheduler import list_jobs

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["schedule_kind"] == "once"
        assert jobs[0]["instruction"].startswith("[scheduled-reminder] call mom")
        assert jobs[0]["id"] in record.message
        row = db.get_task_record("rem-1")
        assert row is not None and row["state"] == "completed"
    finally:
        real_hub.tasks.pop("rem-1", None)


@pytest.mark.asyncio
async def test_reminder_with_time_never_calls_provider(monkeypatch, tmp_path):
    chat_id = _isolated_chat(monkeypatch, tmp_path, "remind2.db")

    def _boom():
        raise AssertionError("provider must not be called for a deterministic reminder")

    monkeypatch.setattr(agentLoop, "_provider_for_config", _boom)
    record = TaskRecord(task_id="rem-2", instruction="Remind me to call mom in 2 minutes",
                        chat_id=chat_id)
    real_hub.tasks["rem-2"] = record
    try:
        await asyncio.wait_for(run_task("rem-2"), timeout=15)
        assert record.state == "completed", (record.state, record.message)
        assert "scheduled job" in record.message
        from app.scheduler import list_jobs

        jobs = list_jobs()
        assert len(jobs) == 1 and jobs[0]["schedule_kind"] == "once"
    finally:
        real_hub.tasks.pop("rem-2", None)


@pytest.mark.asyncio
async def test_fired_reminder_delivers_notification_receipt(monkeypatch, tmp_path):
    """The marker job instruction delivers an outbox notification and
    completes — it never re-asks for the time."""
    _isolated_chat(monkeypatch, tmp_path, "fire.db")
    record = TaskRecord(task_id="fire-1",
                        instruction="[scheduled-reminder] call mom",
                        chat_id=None, background=True)
    real_hub.tasks["fire-1"] = record
    try:
        await asyncio.wait_for(run_task("fire-1"), timeout=15)
        assert record.state == "completed", (record.state, record.message)
        assert "Notification receipt" in record.message
        from app.monitoring_scheduler import list_unread_events

        events = list_unread_events()
        assert any(e.get("kind") == "reminder" for e in events), events
    finally:
        real_hub.tasks.pop("fire-1", None)
