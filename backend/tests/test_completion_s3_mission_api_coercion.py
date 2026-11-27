"""Stream 3 — durable fix for the Failure E format-string crash (S2).

The missions API stored condition_params verbatim, so a JSON string
threshold ("5000") reached the DB — and every downstream `:g` format
("alerting below ₹{threshold:g}") crashed on it. This file pins the
write-path coercion in control_center.py: known numeric condition keys
are floats by the time they are stored, and a non-numeric value for
one is a 422, not a stored time bomb. (S2 hardened the read sites with
_as_float for legacy rows; this stops new string numerics.)

Products below (JBL Tune 770NC, Aurora Trail Headphones) are invented
for these tests; no user example prompt is used.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from pydantic import ValidationError

from app import db
from app.control_center import MonitorInput, _coerce_condition_numerics


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "s3-coerce.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _missions(database):
    with db.get_connection(database) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at ASC")]


# ---------------------------------------------------------------------------
# 1. Coercion helper + model validator
# ---------------------------------------------------------------------------

def test_string_threshold_coerced_to_float():
    out = _coerce_condition_numerics({"target": "JBL Tune 770NC", "threshold": "5000"})
    assert out["threshold"] == 5000.0
    assert isinstance(out["threshold"], float)
    assert out["target"] == "JBL Tune 770NC"


def test_string_min_percent_coerced_to_float():
    out = _coerce_condition_numerics({"target": "JBL Tune 770NC", "min_percent": "15"})
    assert out["min_percent"] == 15.0
    assert isinstance(out["min_percent"], float)


def test_int_threshold_normalised_to_float():
    out = _coerce_condition_numerics({"threshold": 5000})
    assert out["threshold"] == 5000.0 and isinstance(out["threshold"], float)


def test_non_numeric_threshold_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        _coerce_condition_numerics({"threshold": "cheap"})
    with pytest.raises(ValueError, match="must be a number"):
        _coerce_condition_numerics({"min_percent": True})


def test_null_threshold_dropped_and_other_keys_untouched():
    out = _coerce_condition_numerics(
        {"threshold": None, "pattern": "sale", "target": "JBL Tune 770NC"})
    assert "threshold" not in out
    assert out["pattern"] == "sale"
    assert out["target"] == "JBL Tune 770NC"


def test_model_validator_coerces_on_construction():
    body = MonitorInput(
        title="Price Watch: JBL Tune 770NC",
        target_url="https://www.flipkart.com/search?q=JBL+Tune+770NC",
        condition_type="below_price",
        condition_params={"target": "JBL Tune 770NC", "threshold": "5000"},
    )
    assert body.condition_params["threshold"] == 5000.0
    assert isinstance(body.condition_params["threshold"], float)


def test_model_validator_rejects_non_numeric():
    with pytest.raises(ValidationError):
        MonitorInput(
            title="Price Watch: JBL Tune 770NC",
            target_url="https://www.flipkart.com/search?q=JBL+Tune+770NC",
            condition_type="below_price",
            condition_params={"target": "JBL Tune 770NC", "threshold": "cheap"},
        )


# ---------------------------------------------------------------------------
# 2. End to end: API seed with a string threshold, then the handler
#    follow-up that crashed on base (S2 repro shape)
# ---------------------------------------------------------------------------

def test_api_string_threshold_then_handler_followup(database):
    """POST a watch with threshold "5000" (string) through the real API
    route, then run the interval-change follow-up that crashed on base
    with `ValueError: Unknown format code 'g' for object of type 'str'`.
    The stored threshold must be a float, the follow-up must update the
    interval to 720 s, and no second watch may appear."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/control/missions", json={
            "title": "Price Watch: JBL Tune 770NC",
            "target_url": "https://www.flipkart.com/search?q=JBL+Tune+770NC",
            "conversation_id": "s3-rel",
            "interval_seconds": 300,
            "condition_type": "below_price",
            "condition_params": {"target": "JBL Tune 770NC", "threshold": "5000"},
        })
    assert resp.status_code == 201, resp.text
    mission_id = resp.json()["id"]
    rows = _missions(database)
    assert len(rows) == 1
    stored = json.loads(rows[0]["condition_params"])
    assert stored["threshold"] == 5000.0
    assert isinstance(stored["threshold"], float)
    # The API seeds watches paused; the chat dedup only sees active
    # rows, so activate it through the real PATCH route first (the
    # S2 repro seeded an active row directly).
    with TestClient(app) as client:
        activated = client.patch(f"/api/control/missions/{mission_id}",
                                 json={"status": "active"})
    assert activated.status_code == 200, activated.text
    assert _missions(database)[0]["status"] == "active"

    from app.continuous_monitoring_companion import (
        handle_continuous_monitoring_instruction,
    )

    history = [
        {"role": "user",
         "content": "Monitor the price of JBL Tune 770NC every 5 minutes."},
        {"role": "assistant",
         "content": "Watch active for JBL Tune 770NC."},
    ]
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "Change that watch to every 12 minutes.",
        chat_id="s3-rel", history=history, db_path=database))
    rows = _missions(database)
    assert len(rows) == 1, answer[:300]
    assert rows[0]["interval_seconds"] == 720, answer[:300]
    assert "every 12 minutes" in answer
    assert "5000" in answer


def test_api_non_numeric_threshold_is_422(database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/control/missions", json={
            "title": "Price Watch: JBL Tune 770NC",
            "target_url": "https://www.flipkart.com/search?q=JBL+Tune+770NC",
            "interval_seconds": 300,
            "condition_type": "below_price",
            "condition_params": {"target": "JBL Tune 770NC", "threshold": "cheap"},
        })
    assert resp.status_code == 422, resp.text
    assert _missions(database) == []
