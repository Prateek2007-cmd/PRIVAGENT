"""Stream 3 — relative-interval updates + objective-driven product repair
(S5 gen D10, S1 Failure E; CONTRACTS4 §3).

Every prompt here is NOVEL — none copies the user's example prompts or the
S5/S1 live-run wordings. Products below (Aurora Trail Headphones, Nimbus /
Vertex Trail Watch, Ember Desk Lamp, Cobalt Running Shoes) are invented for
these tests.

Covers:
* a relative interval ("twice as often" / "half as often") HALVES or
  DOUBLES the resolved watch's interval IN PLACE — never a second watch,
  never a garbage watch titled "Twice As Often";
* the companion consumes the structured Objective's ``relative_interval``
  entity (passed in, or built from the instruction) instead of guessing
  a number — the new interval is derived from the row's current interval;
* Failure E: a pronoun-led instruction ("<Product> so can u watch the
  price of this for every 5 mins") creates the watch for the objective's
  product, never for the garbage name "This For Every 5 Mins";
* the verifier accepts a true relative-update reply (the new cadence is
  the one the reply states) and still fails a false one (row unchanged).
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app import db
from app.completion_verifier import verify_task_completion
from app.continuous_monitoring_companion import (
    _is_garbage_product_title,
    handle_continuous_monitoring_instruction,
    upsert_monitor_mission,
)
from app.monitoring_orchestration import run_objective


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "s3-relative.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _missions(database):
    with db.get_connection(database) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at ASC")]


def _run_handler(instruction, database, chat_id="s3-rel", history=None, objective=None):
    return asyncio.run(handle_continuous_monitoring_instruction(
        instruction, chat_id=chat_id, history=history,
        db_path=database, objective=objective))


def _history_for(turns):
    return [{"role": r, "content": c} for r, c in turns]


def _create_watch(database, instruction, chat_id="s3-rel"):
    answer = _run_handler(instruction, database, chat_id=chat_id)
    match = re.search(r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->", answer)
    assert match, "watch creation returned no watch marker: " + answer[:300]
    return match.group(1), answer


def _relative_objective(factor):
    """The structured Objective (Stream 1) for a relative update, in the
    shape build_objective returns: update_monitor, entities carry only
    the factor — never a guessed number."""
    return {
        "primary_intent": "update_monitor",
        "entities": {"relative_interval": factor},
        "references": ["that"],
        "missing": ["watch_ref"],
    }


# ---------------------------------------------------------------------------
# 1. Relative interval via the handler (D10)
# ---------------------------------------------------------------------------

def test_relative_update_halves_interval_in_place(database):
    t1 = ("watch Aurora Trail Headphones, alert me under \u20b93000, "
          "check every hour")
    _, a1 = _create_watch(database, t1)
    assert _missions(database)[0]["interval_seconds"] == 3600
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run_handler("check that twice as often", database,
                          history=history,
                          objective=_relative_objective("twice_as_often"))
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert rows[0]["interval_seconds"] == 1800, answer[:300]
    assert "every 30 minutes" in answer
    assert "privagent-watch:" in answer


def test_relative_update_doubles_interval_half_as_often(database):
    t1 = ("watch Cobalt Running Shoes, alert me under \u20b94000, "
          "check every 30 minutes")
    _, a1 = _create_watch(database, t1)
    assert _missions(database)[0]["interval_seconds"] == 1800
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run_handler("check it half as often", database,
                          history=history,
                          objective=_relative_objective("half_as_often"))
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert rows[0]["interval_seconds"] == 3600, answer[:300]
    assert "every 60 minutes" in answer or "every 1 hour" in answer


def test_relative_update_no_watch_asks_instead_of_creating(database):
    answer = _run_handler("make that check twice as often", database,
                          objective=_relative_objective("twice_as_often"))
    assert _missions(database) == []
    assert "don't have a watch" in answer.lower()
    assert "Twice As Often" not in answer


def test_relative_update_ambiguous_asks_which_watch(database):
    upsert_monitor_mission(
        product_title="Ember Desk Lamp", watch_title="Price Watch: Ember Desk Lamp",
        target_url="https://www.flipkart.com/search?q=ember+desk+lamp",
        trigger_type="interval", schedule_cron=None, interval_seconds=3600,
        condition_type="price_drop", condition_params={"target": "Ember Desk Lamp"},
        below=None, percent=None, chat_id="s3-rel", db_path=database)
    upsert_monitor_mission(
        product_title="Cobalt Running Shoes", watch_title="Price Watch: Cobalt Running Shoes",
        target_url="https://www.flipkart.com/search?q=cobalt+running+shoes",
        trigger_type="interval", schedule_cron=None, interval_seconds=3600,
        condition_type="price_drop", condition_params={"target": "Cobalt Running Shoes"},
        below=None, percent=None, chat_id="s3-rel", db_path=database)
    answer = _run_handler("check that twice as often", database,
                          objective=_relative_objective("twice_as_often"))
    rows = _missions(database)
    assert len(rows) == 2
    assert all(r["interval_seconds"] == 3600 for r in rows), answer[:300]
    assert "more than one watch" in answer


# ---------------------------------------------------------------------------
# 2. Failure E — objective product repairs the pronoun garbage
# ---------------------------------------------------------------------------

def test_objective_product_repairs_pronoun_garbage(database):
    objective = {
        "primary_intent": "monitor_product",
        "entities": {"product": "Nimbus Trail Watch 3"},
        "references": ["this"],
    }
    answer = _run_handler(
        "Nimbus Trail Watch 3 so can u watch the price of this for every 5 mins",
        database, objective=objective)
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert "Nimbus" in rows[0]["title"], rows[0]
    assert "this+for+every" not in rows[0]["target_url"], rows[0]


def test_pronoun_garbage_creates_no_watch_without_objective(database):
    # Merged build: the caller passes no objective, but the handler
    # builds one from the instruction via Stream 1's objective layer,
    # which resolves the product named in the instruction ("Vertex
    # Trail"). The watch is created for the RESOLVED product — never
    # for the garbage name "This For Every 5 Mins".
    answer = _run_handler(
        "Vertex Trail Watch 3 so can u watch the price of this for every 5 mins",
        database)
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert "Trail" in rows[0]["title"], rows[0]
    assert not any("This For Every" in r["title"] for r in rows)
    assert "this+for+every" not in rows[0]["target_url"], rows[0]


def test_pure_pronoun_with_no_product_anywhere_asks(database):
    # No product named in the instruction, no history, no objective:
    # the handler must ask for the product instead of minting a watch
    # titled "This For Every 5 Mins".
    answer = _run_handler(
        "so can u watch the price of this for every 5 mins",
        database)
    rows = _missions(database)
    assert rows == [], answer[:300]
    assert not any("This For Every" in r["title"] for r in rows)
    assert "don't have a product" in answer.lower()


def test_garbage_title_detection():
    assert _is_garbage_product_title("This For Every 5 Mins")
    assert _is_garbage_product_title("Twice As Often")
    assert _is_garbage_product_title("That Watch")
    assert not _is_garbage_product_title("Samsung Galaxy Fit3")
    assert not _is_garbage_product_title("Aurora Trail Headphones")
    assert not _is_garbage_product_title("Everyday Carry Watch")


# ---------------------------------------------------------------------------
# 3. Relative interval via the structured orchestration pipeline
# ---------------------------------------------------------------------------

def _run_objective(objective, database):
    return asyncio.run(run_objective(objective, None, db_path=database))


def test_orchestration_relative_update_halves(database):
    t1 = ("watch Aurora Trail Headphones, alert me under \u20b93000, "
          "check every hour")
    _create_watch(database, t1)
    result = _run_objective(
        {"primary_intent": "update_monitor",
         "entities": {"relative_interval": "twice_as_often"}},
        database)
    assert result.state == "completed", result.message
    rows = _missions(database)
    assert len(rows) == 1
    assert rows[0]["interval_seconds"] == 1800
    assert result.evidence.get("interval_seconds") == 1800


def test_orchestration_relative_update_doubles(database):
    t1 = ("watch Cobalt Running Shoes, alert me under \u20b94000, "
          "check every 30 minutes")
    _create_watch(database, t1)
    result = _run_objective(
        {"primary_intent": "update_monitor",
         "entities": {"relative_interval": "half_as_often"}},
        database)
    assert result.state == "completed", result.message
    assert _missions(database)[0]["interval_seconds"] == 3600


def test_orchestration_relative_update_no_watch_awaiting_input(database):
    result = _run_objective(
        {"primary_intent": "update_monitor",
         "entities": {"relative_interval": "twice_as_often"}},
        database)
    assert result.state == "awaiting_input"
    assert _missions(database) == []


# ---------------------------------------------------------------------------
# 4. Verifier: a true relative-update reply completes, a false one fails
# ---------------------------------------------------------------------------

def test_verifier_relative_update_reply_completes(database):
    t1 = ("watch Aurora Trail Headphones, alert me under \u20b93000, "
          "check every hour")
    _, a1 = _create_watch(database, t1)
    history = _history_for([("user", t1), ("assistant", a1)])
    answer = _run_handler("check that twice as often", database,
                          history=history,
                          objective=_relative_objective("twice_as_often"))
    verdict = verify_task_completion("check that twice as often", "completed",
                                     answer, db_path=database)
    assert verdict.state == "completed", verdict.message


def test_verifier_relative_update_false_claim_fails(database):
    t1 = ("watch Aurora Trail Headphones, alert me under \u20b93000, "
          "check every hour")
    mission_id, _ = _create_watch(database, t1)
    assert _missions(database)[0]["interval_seconds"] == 3600
    # A reply claiming the 30-minute cadence while the row is unchanged.
    fake = (f"Updated the watch for **Aurora Trail Headphones** — it was checking "
            f"every 1 hour, now checking every 30 minutes.\n\n"
            f"<!-- privagent-watch: {mission_id} -->")
    verdict = verify_task_completion("check that twice as often", "completed",
                                     fake, db_path=database)
    assert verdict.state == "failed", verdict.message
    assert "3600" in verdict.message and "1800" in verdict.message


def test_verifier_creation_without_interval_phrase_uses_reply_cadence(database):
    # Regression pin for _stated_interval_seconds: an instruction with
    # no "every N" phrase verifies against the cadence the reply states.
    mission_id, answer = _create_watch(
        database, "watch Aurora Trail Headphones, alert me under \u20b93000")
    assert _missions(database)[0]["interval_seconds"] == 300
    verdict = verify_task_completion(
        "watch Aurora Trail Headphones, alert me under \u20b93000",
        "completed", answer, db_path=database)
    assert verdict.state == "completed", verdict.message
