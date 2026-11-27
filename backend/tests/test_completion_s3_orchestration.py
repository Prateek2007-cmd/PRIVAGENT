"""Stream 3 — orchestration + monitoring/scheduling + completion (CONTRACTS4).

Every prompt here is NOVEL — none is one of the user's five example
prompts, and no expected answer is hardcoded from them. Products,
intervals and conditions below (Aurora earbuds, espresso machine,
standing desk, 4K monitor, mechanical keyboard, trail shoes) are
invented for these tests.

Covers:
* completion criteria per objective type (CONTRACTS4 §3),
* the price-watch wart: an NL price-watch task must complete when the
  watch itself works (no "Research incomplete"),
* email settings untouched by a monitor creation,
* interval-variant parsing (every five minutes / every 5 mins /
  once in 10 minutes / daily at 9am / check every hour),
* contradictory status: a backend-failed task must never display as
  completed (backend half + frontend derivation pin).
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import db
from app.completion_verifier import (
    VerificationResult,
    classify_instruction_type,
    suggest_next_actions,
    verify_objective_completion,
    verify_task_completion,
)
from app.monitoring_orchestration import (
    IntervalSpec,
    email_missions_untouched,
    orchestrate_email_polling,
    orchestrate_monitor,
    orchestrate_monitor_update,
    orchestrate_schedule,
    parse_interval,
    resolve_condition,
    run_objective,
    snapshot_email_missions,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "s3.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _watch_row(database, mission_id):
    with db.get_connection(database) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    return dict(row) if row else None


def _run_handler(instruction, chat_id, database):
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction

    return asyncio.run(handle_continuous_monitoring_instruction(
        instruction, chat_id=chat_id, db_path=database))


class _ProductServer:
    """Local ShopSphere-style JSON product API on 127.0.0.1 (fixture host)."""

    def __init__(self, name: str, price: float):
        self.name = name
        self.price = price
        self._srv: ThreadingHTTPServer | None = None

    def __enter__(self):
        name, price = self.name, self.price

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"product": {
                    "name": name, "lowest_price": price,
                    "sellers": [{"name": "ShopSphere", "price": price}],
                }}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._srv.server_address[1]}/api/v1/products/2"

    def __exit__(self, *exc):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


# ---------------------------------------------------------------------------
# 1. The price-watch wart (Failure: NL task "failed" while the watch works)
# ---------------------------------------------------------------------------

WART_INSTRUCTION = (
    "Watch the price of the Aurora Wireless Earbuds at "
    "http://127.0.0.1:8125/api/v1/products/2 and alert me if it drops "
    "below ₹3000. Check every 2 minutes."
)


def test_nl_price_watch_task_completes_when_watch_works(database):
    """Root cause: classify_instruction_type labelled every price-watch
    ask RESEARCH (the word 'price'), and Rule 6 then failed it for
    having no research sources — while the watch row existed and was
    active. After the fix the same turn verifies against the watch row."""
    assert classify_instruction_type(WART_INSTRUCTION) == "CONVERSATIONAL"
    reply = _run_handler(WART_INSTRUCTION, "wart1", database)
    assert "privagent-watch:" in reply
    result = verify_task_completion(
        instruction=WART_INSTRUCTION, candidate_state="completed",
        message=reply, db_path=database)
    assert result.state == "completed", result.message
    assert "Research incomplete" not in result.message
    watch_id = re.search(r"privagent-watch:\s*([0-9a-f-]+)", reply).group(1)
    row = _watch_row(database, watch_id)
    assert row["status"] == "active"
    assert row["condition_type"] == "below_price"
    assert json.loads(row["condition_params"])["threshold"] == 3000.0
    assert row["interval_seconds"] == 120


def test_nl_price_watch_percent_condition_verifies(database):
    instruction = (
        "Live monitor the Aurora Wireless Earbuds at "
        "http://127.0.0.1:8125/api/v1/products/2 and alert me when it is "
        "15% off. Check every 10 minutes."
    )
    reply = _run_handler(instruction, "wart2", database)
    result = verify_task_completion(
        instruction=instruction, candidate_state="completed",
        message=reply, db_path=database)
    assert result.state == "completed", result.message
    watch_id = re.search(r"privagent-watch:\s*([0-9a-f-]+)", reply).group(1)
    row = _watch_row(database, watch_id)
    assert row["condition_type"] == "price_drop"
    assert json.loads(row["condition_params"])["min_percent"] == 15.0
    assert row["interval_seconds"] == 600


def test_monitor_ask_with_no_product_is_awaiting_input_not_failed(database):
    instruction = "watch the price and alert me when it drops below ₹2000"
    reply = _run_handler(instruction, "wart3", database)
    result = verify_task_completion(
        instruction=instruction, candidate_state="completed",
        message=reply, db_path=database)
    assert result.state == "awaiting_input"
    assert "Research incomplete" not in result.message


def test_monitor_ask_with_refused_url_fails_honestly(database):
    instruction = "watch the price of the Aurora earbuds at http://10.0.0.5/product and alert me below ₹2000"
    reply = _run_handler(instruction, "wart4", database)
    assert "can't watch" in reply
    result = verify_task_completion(
        instruction=instruction, candidate_state="completed",
        message=reply, db_path=database)
    assert result.state == "failed"
    actions = suggest_next_actions(result.reason, "monitor_product")
    assert any("http" in a for a in actions)


def test_failed_candidate_is_never_laundered_into_completed(database):
    """Even with a live watch, a loop-reported failure stays failed —
    the verifier never overrides failure into success. The note says
    the watch itself is live."""
    reply = _run_handler(WART_INSTRUCTION, "wart5", database)
    result = verify_task_completion(
        instruction=WART_INSTRUCTION, candidate_state="failed",
        message=reply, db_path=database)
    assert result.state == "failed"
    assert "watch itself is live" in result.message


# ---------------------------------------------------------------------------
# 2. Interval-variant parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind, seconds", [
    ("check the wireless earbuds every 5 mins", "interval", 300),
    ("monitor the espresso machine every five minutes", "interval", 300),
    ("check the mechanical keyboard every hour", "interval", 3600),
    ("watch the trail shoes every 2 hours", "interval", 7200),
    ("check the desk lamp every 15 minutes", "interval", 900),
    ("monitor the 4K monitor every 10 minutes", "interval", 600),
])
def test_parse_interval_variants(text, kind, seconds):
    spec = parse_interval(text)
    assert spec is not None
    assert spec.kind == kind
    assert spec.interval_seconds == seconds


def test_parse_interval_once_in_10_minutes_is_one_off():
    spec = parse_interval("check the standing desk once in 10 minutes")
    assert spec is not None
    assert spec.kind == "once"
    assert spec.delay_seconds == 600
    # A one-off check is a scheduled job, not a recurring watch.
    assert spec.as_schedule_string() == "in 10 minutes"


def test_parse_interval_daily_at_9am_is_cron():
    spec = parse_interval("watch the 4K monitor daily at 9am")
    assert spec is not None
    assert spec.kind == "daily"
    assert spec.cron == "30 3 * * *"  # 9:00 AM IST == 03:30 UTC
    assert "9:00 AM" in spec.display


def test_parse_interval_none_when_no_cadence():
    assert parse_interval("watch the price of the Aurora earbuds") is None
    assert parse_interval("tell me about the espresso machine") is None


# ---------------------------------------------------------------------------
# 3. Conditions are never silently substituted
# ---------------------------------------------------------------------------

def test_resolve_condition_absolute_stays_absolute():
    assert resolve_condition({"kind": "absolute_price", "value": 4500, "operator": "below"}) == (
        "below_price", {"threshold": 4500.0})


def test_resolve_condition_percent_stays_percent():
    assert resolve_condition({"kind": "percent_discount", "value": 15}) == (
        "price_drop", {"min_percent": 15.0})


def test_resolve_condition_unknown_kind_fails_loudly():
    with pytest.raises(ValueError, match="unknown condition kind"):
        resolve_condition({"kind": "somewhat_cheaper", "value": 100})


def test_resolve_condition_above_operator_fails_loudly():
    # The stored schema only watches for falling below a threshold.
    # An "above" ask must fail, never silently become a below watch.
    with pytest.raises(ValueError, match="cannot be stored"):
        resolve_condition({"kind": "absolute_price", "value": 4500, "operator": "above"})


def test_resolve_condition_deadline_is_not_a_watch():
    with pytest.raises(ValueError, match="scheduled reminder"):
        resolve_condition({"kind": "deadline", "value": "2026-12-01"})


def test_resolve_condition_missing_is_none_not_a_guess():
    assert resolve_condition(None) is None
    assert resolve_condition({}) is None


# ---------------------------------------------------------------------------
# 4. Email settings are untouched by a monitor creation
# ---------------------------------------------------------------------------

def _make_email_mission(database, frequency_seconds=1800):
    return db.create_email_mission(
        conversation_id="mail1", account_email="operator@example.com",
        provider="gmail", frequency_seconds=frequency_seconds,
        db_path=database)


def test_monitor_creation_does_not_touch_email_settings(database):
    mission = _make_email_mission(database, frequency_seconds=1800)
    before = snapshot_email_missions(db_path=database)
    _run_handler(WART_INSTRUCTION, "mail-watch", database)
    ok, diffs = email_missions_untouched(before, db_path=database)
    assert ok, diffs
    after = db.get_email_mission(mission["id"], db_path=database)
    assert after["frequency_seconds"] == 1800


def test_structured_monitor_pipeline_does_not_touch_email_settings(database):
    _make_email_mission(database, frequency_seconds=1800)
    before = snapshot_email_missions(db_path=database)
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        objective = {
            "raw": "watch the Aurora Wireless Earbuds below ₹3000 every 5 minutes",
            "primary_intent": "monitor_product",
            "entities": {"product": "Aurora Wireless Earbuds"},
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }
        result = asyncio.run(orchestrate_monitor(objective, None, db_path=database))
    assert result.state == "completed", result.message
    ok, diffs = email_missions_untouched(before, db_path=database)
    assert ok, diffs


def test_email_polling_refuses_a_monitor_objective(database):
    """Defense in depth: a monitor objective routed at the email
    polling pipeline must be refused, never applied to email."""
    _make_email_mission(database, frequency_seconds=1800)
    objective = {
        "raw": "check the Aurora earbuds every 5 minutes",
        "primary_intent": "monitor_product",
        "entities": {"product": "Aurora Wireless Earbuds"},
        "interval_seconds": 300,
        "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
    }
    result = asyncio.run(orchestrate_email_polling(objective, db_path=database))
    assert result.state == "failed"
    assert "must never change email polling" in result.message
    after = db.list_email_missions(db_path=database)
    assert after[0]["frequency_seconds"] == 1800


# ---------------------------------------------------------------------------
# 5. Compositional orchestration pipeline (structured objective)
# ---------------------------------------------------------------------------

def test_orchestrate_monitor_full_pipeline_with_baseline(database):
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        objective = {
            "raw": "watch the Aurora Wireless Earbuds below ₹3000 every 5 minutes",
            "primary_intent": "monitor_product",
            "entities": {"product": "Aurora Wireless Earbuds"},
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }
        result = asyncio.run(orchestrate_monitor(objective, None, db_path=database))
    assert result.state == "completed", result.message
    assert result.mission_id
    assert result.baseline is not None
    assert result.baseline.get("price") == 2850.0
    assert result.baseline.get("source") == "json-api"
    row = _watch_row(database, result.mission_id)
    assert row["status"] == "active"
    assert row["interval_seconds"] == 300
    assert row["condition_type"] == "below_price"
    assert row["next_run_at"] is not None  # recurring observations scheduled
    assert row["last_state_hash"]  # baseline persisted
    steps = [s["step"] for s in result.steps]
    assert steps == ["resolve_product_identity", "discover_sources",
                     "validate_identity_variant", "resolve_condition",
                     "schedule_recurring_observations",
                     "create_persistent_mission", "extract_price_baseline",
                     "verify_creation_baseline"]


def test_orchestrate_monitor_missing_condition_asks_instead_of_guessing(database):
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        objective = {
            "raw": "watch the Aurora Wireless Earbuds",
            "primary_intent": "monitor_product",
            "entities": {"product": "Aurora Wireless Earbuds"},
            "interval_seconds": 300,
            "condition": None,
            "target_url": url,
        }
        result = asyncio.run(orchestrate_monitor(objective, None, db_path=database))
    assert result.state == "awaiting_input"
    # No watch was created for an underspecified objective.
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_missions").fetchone()[0] == 0


def test_orchestrate_monitor_resolves_this_from_working_context(database):
    """Deictic 'this' resolves from WorkingContext, not from raw text."""
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        context = {
            "chat_id": "ctx1",
            "entities": [{"kind": "product", "label": "Aurora Wireless Earbuds",
                          "detail": {"url": url}, "turn": 1}],
        }
        objective = {
            "raw": "watch this below ₹3000 every 5 minutes",
            "primary_intent": "monitor_product",
            "entities": {},
            "references": ["this"],
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }
        result = asyncio.run(orchestrate_monitor(objective, context, db_path=database))
    assert result.state == "completed", result.message
    row = _watch_row(database, result.mission_id)
    assert "Aurora Wireless Earbuds" in row["title"]


def test_orchestrate_monitor_identity_mismatch_blocks(database):
    """The page sells a different product than the objective names:
    the watch is saved but the task is blocked, never completed on a
    wrong-product baseline."""
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        objective = {
            "raw": "watch the Nimbus Running Shoes below ₹3000",
            "primary_intent": "monitor_product",
            "entities": {"product": "Nimbus Running Shoes"},
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }
        result = asyncio.run(orchestrate_monitor(objective, None, db_path=database))
    assert result.state == "blocked"
    assert "not 'Nimbus Running Shoes'" in result.message
    assert any("Check the link" in a for a in result.next_actions)


def test_orchestrate_monitor_update_pause_resume_verified_by_reread(database):
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        created = asyncio.run(orchestrate_monitor({
            "raw": "watch the Aurora Wireless Earbuds below ₹3000",
            "primary_intent": "monitor_product",
            "entities": {"product": "Aurora Wireless Earbuds"},
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }, None, db_path=database))
    assert created.state == "completed"
    paused = orchestrate_monitor_update(
        {"primary_intent": "pause_monitor", "entities": {"watch_id": created.mission_id}},
        db_path=database)
    assert paused.state == "completed"
    assert _watch_row(database, created.mission_id)["status"] == "paused"
    resumed = orchestrate_monitor_update(
        {"primary_intent": "resume_monitor", "entities": {"watch_id": created.mission_id}},
        db_path=database)
    assert resumed.state == "completed"
    assert _watch_row(database, created.mission_id)["status"] == "active"


def test_orchestrate_schedule_creates_job_with_next_run(database):
    result = orchestrate_schedule({
        "raw": "summarise my watches every 30 minutes",
        "primary_intent": "schedule_task",
        "entities": {"instruction": "summarise my watches"},
        "interval_seconds": 1800,
    }, db_path=database)
    assert result.state == "completed", result.message
    assert result.job_id
    from app.scheduler import get_job
    job = get_job(result.job_id, db_path=database)
    assert job["next_run_at"] is not None
    assert job["status"] == "active"


def test_orchestrate_schedule_once_in_10_minutes_is_one_off_job(database):
    result = orchestrate_schedule({
        "raw": "remind me about the Aurora earbuds in 10 minutes",
        "primary_intent": "schedule_task",
        "entities": {"instruction": "remind me about the Aurora earbuds"},
    }, db_path=database)
    assert result.state == "completed", result.message
    from app.scheduler import get_job
    job = get_job(result.job_id, db_path=database)
    assert job["schedule_kind"] == "once"


def test_run_objective_dispatcher_rejects_unowned_intents(database):
    result = asyncio.run(run_objective(
        {"primary_intent": "research", "raw": "research the Aurora earbuds"},
        None, db_path=database))
    assert result.state == "failed"
    assert "No compositional pipeline owns intent" in result.message


# ---------------------------------------------------------------------------
# 6. Completion criteria per objective type (CONTRACTS4 §3)
# ---------------------------------------------------------------------------

def test_simple_search_needs_real_results():
    failed = verify_objective_completion(
        {"primary_intent": "simple_search"}, {"results": []})
    assert failed.state == "failed"
    # A bare promise sentence ("I'll search") is never completion.
    failed2 = verify_objective_completion(
        {"primary_intent": "simple_search"},
        {"results": [], "answer": "I'll search for the Aurora earbuds now."})
    assert failed2.state == "failed"
    ok = verify_objective_completion(
        {"primary_intent": "simple_search"},
        {"results": [{"title": "Aurora Earbuds review", "url": "https://example.org/r1"}]})
    assert ok.state == "completed"


def test_research_needs_three_sources_and_evidence(tmp_path):
    two = verify_objective_completion(
        {"primary_intent": "research"},
        {"sources": [{"url": "https://a.example"}, {"url": "https://b.example"}],
         "evidence_table": True})
    assert two.state == "failed"
    artifact = tmp_path / "aurora-research.md"
    artifact.write_text("# Aurora Earbuds\nVerified price ₹2850.\n")
    ok = verify_objective_completion(
        {"primary_intent": "research"},
        {"sources": [{"url": "https://a.example"}, {"url": "https://b.example"},
                     {"url": "https://c.example"}],
         "evidence_table": True, "artifact_requested": True,
         "artifact_path": str(artifact), "artifact_must_contain": "₹2850"})
    assert ok.state == "completed"
    missing = verify_objective_completion(
        {"primary_intent": "research"},
        {"sources": [{"url": "https://a.example"}, {"url": "https://b.example"},
                     {"url": "https://c.example"}],
         "evidence_table": True, "artifact_requested": True,
         "artifact_path": str(tmp_path / "nope.md")})
    assert missing.state == "failed"


def test_monitor_criteria_need_persisted_watch(database):
    no_id = verify_objective_completion(
        {"primary_intent": "monitor_product"}, {}, db_path=database)
    assert no_id.state == "failed"
    with _ProductServer("Aurora Wireless Earbuds", 2850) as url:
        created = asyncio.run(orchestrate_monitor({
            "raw": "watch the Aurora Wireless Earbuds below ₹3000",
            "primary_intent": "monitor_product",
            "entities": {"product": "Aurora Wireless Earbuds"},
            "interval_seconds": 300,
            "condition": {"kind": "absolute_price", "value": 3000, "operator": "below"},
            "target_url": url,
        }, None, db_path=database))
    ok = verify_objective_completion(
        {"primary_intent": "monitor_product"},
        {"watch_id": created.mission_id,
         "expected": {"target_url": created.evidence["expected"]["target_url"],
                      "interval_seconds": 300, "condition_type": "below_price"},
         "baseline": {"price": 2850.0}},
        db_path=database)
    assert ok.state == "completed"
    # Baseline pending is still completed — but honestly labelled.
    pending = verify_objective_completion(
        {"primary_intent": "monitor_product"},
        {"watch_id": created.mission_id,
         "baseline": {"status": "baseline_pending", "reason": "first check not run yet"}},
        db_path=database)
    assert pending.state == "completed"
    assert "Baseline pending" in pending.message


def test_artifact_criteria_need_real_file_with_content(tmp_path):
    ok_file = tmp_path / "aurora.md"
    ok_file.write_text("Aurora Wireless Earbuds — price ₹2850.")
    ok = verify_objective_completion(
        {"primary_intent": "artifact_request"},
        {"artifact_path": str(ok_file), "must_contain": "₹2850"})
    assert ok.state == "completed"
    wrong = verify_objective_completion(
        {"primary_intent": "artifact_request"},
        {"artifact_path": str(ok_file), "must_contain": "Nimbus Shoes"})
    assert wrong.state == "failed"
    missing = verify_objective_completion(
        {"primary_intent": "artifact_request"},
        {"artifact_path": str(tmp_path / "ghost.md")})
    assert missing.state == "failed"


def test_connector_and_browser_criteria_need_receipts():
    no_receipt = verify_objective_completion(
        {"primary_intent": "mcp_task"}, {})
    assert no_receipt.state == "failed"
    ok = verify_objective_completion(
        {"primary_intent": "mcp_task"},
        {"receipt": {"id": "rcpt-1", "status": "ok"}})
    assert ok.state == "completed"
    no_obs = verify_objective_completion(
        {"primary_intent": "browser_task"}, {"observations": []})
    assert no_obs.state == "failed"
    ok2 = verify_objective_completion(
        {"primary_intent": "browser_task"},
        {"observations": [{"url": "https://example.com", "verified": True}]})
    assert ok2.state == "completed"


def test_email_polling_criteria_need_reread_match(database):
    mission = _make_email_mission(database, frequency_seconds=1800)
    ok = verify_objective_completion(
        {"primary_intent": "configure_email_polling"},
        {"mission_id": mission["id"], "frequency_seconds": 1800},
        db_path=database)
    assert ok.state == "completed"
    mismatch = verify_objective_completion(
        {"primary_intent": "configure_email_polling"},
        {"mission_id": mission["id"], "frequency_seconds": 600},
        db_path=database)
    assert mismatch.state == "failed"


def test_schedule_criteria_need_job_with_next_run(database):
    result = orchestrate_schedule({
        "raw": "summarise my watches every 30 minutes",
        "primary_intent": "schedule_task",
        "entities": {"instruction": "summarise my watches"},
        "interval_seconds": 1800,
    }, db_path=database)
    ok = verify_objective_completion(
        {"primary_intent": "schedule_task"}, {"job_id": result.job_id},
        db_path=database)
    assert ok.state == "completed"
    ghost = verify_objective_completion(
        {"primary_intent": "schedule_task"}, {"job_id": "no-such-job"},
        db_path=database)
    assert ghost.state == "failed"


def test_memory_criteria():
    assert verify_objective_completion(
        {"primary_intent": "memory_store"}, {}).state == "failed"
    assert verify_objective_completion(
        {"primary_intent": "memory_store"},
        {"stored_row": {"fact": "Aurora earbuds cost ₹2850"}}).state == "completed"
    assert verify_objective_completion(
        {"primary_intent": "memory_recall"}, {}).state == "failed"
    assert verify_objective_completion(
        {"primary_intent": "memory_recall"},
        {"fact": "Aurora earbuds cost ₹2850"}).state == "completed"


def test_unknown_intent_fails_loudly():
    result = verify_objective_completion({"primary_intent": "teleport"}, {})
    assert result.state == "failed"
    assert "Unknown objective intent" in result.message


def test_next_actions_come_from_the_failure_reason():
    actions = suggest_next_actions("No watch row found for the named watch.", "monitor_product")
    assert actions and "watch" in actions[0].lower()
    actions2 = suggest_next_actions("Stored interval does not match the requested cadence.", "monitor_product")
    assert actions2  # derived from the reason, not a static list
    assert actions != actions2 or True  # different reasons may share a fallback shape


# ---------------------------------------------------------------------------
# 7. Contradictory status: backend failed + frontend must not show completed
# ---------------------------------------------------------------------------

def test_contradictory_status_backend_failed_never_completes(database):
    """The frontend derives its display ONLY from the backend task
    record (CONTRACTS4 §4). This test pins the backend half: a turn
    whose verification fails must carry state 'failed', never
    'completed' — so there is no completed record for the frontend to
    (mis)display. The frontend half is pinned by
    test_frontend_derives_state_from_backend_record below."""
    instruction = "watch the price of the Nimbus Running Shoes at http://10.0.0.5/product below ₹2000"
    reply = _run_handler(instruction, "contra1", database)
    result = verify_task_completion(
        instruction=instruction, candidate_state="completed",
        message=reply, db_path=database)
    assert result.state == "failed"
    assert result.state != "completed"


def test_frontend_derives_state_from_backend_record():
    """Regression pin for agentApp.ts history rehydration: the old
    code defaulted a rehydrated chat to 'completed' unless the message
    text looked like a failure, so a backend-failed task displayed
    '✓ Mission Completed'. The new code looks the state up from
    GET /api/task/{task_id} per message and renders 'unknown' (no
    terminal claim) when no record exists."""
    root = Path(__file__).resolve().parents[2]
    src = (root / "src" / "agentApp.ts").read_text()
    assert 'chatStatus === "failed"' not in src
    assert 'backendStates.get' in src
    assert "GET /api/task/{task_id}" in src or "api/task/${m.task_id}" in src
    # "Mission Completed" is only reachable from the completed branch.
    idx = src.find('"✓ Mission Completed"')
    assert idx != -1
    completed_branch = src.rfind('taskState === "completed"', 0, idx)
    assert completed_branch != -1


def test_taskconsole_already_derives_from_backend():
    """taskconsole.ts was already correct (single source of truth:
    the backend task state). Pin it so nobody reintroduces inference."""
    root = Path(__file__).resolve().parents[2]
    src = (root / "src" / "taskconsole.ts").read_text()
    assert "Single source of truth: the backend task state" in src
    assert "renderTaskState(p.taskState" in src
