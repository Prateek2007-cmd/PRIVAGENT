"""Merge-phase (v4) regression tests: absolute follow-up updates.

Stream D proved live that only *relative* interval updates were handled
in place: "make it check every hour instead of every day" minted a bogus
watch titled "Instead", "actually make that 10000 rupees not 5000"
minted "I Made", and "change the threshold to 5 percent" was a no-op.
These tests pin the absolute-update handler and the product-phrase
guards that stop the creation fall-through from minting garbage.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402
from app.continuous_monitoring_companion import (  # noqa: E402
    _apply_absolute_update,
    _is_garbage_product_title,
    _parse_percent_threshold,
    _parse_replacement_price,
    _parse_threshold_price,
    _product_phrase_from_text,
    handle_continuous_monitoring_instruction,
    upsert_monitor_mission,
)
from app.objective import _extract_product  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path):
    path = str(tmp_path / "abs.db")
    db.init_db(path)
    return path


def _make_anker(db_path):
    up = upsert_monitor_mission(
        product_title="Anker Soundcore Liberty 4 NC",
        watch_title="Price Watch: Anker Soundcore Liberty 4 NC",
        target_url="https://www.flipkart.com/search?q=anker+soundcore+liberty+4+nc",
        trigger_type="interval", schedule_cron=None, interval_seconds=300,
        condition_type="below_price", condition_params={"threshold": 5000.0},
        below=5000.0, percent=None, stock_alert=None,
        chat_id="test-chat", db_path=db_path,
    )
    return up["mission_id"]


def _rows(db_path):
    with db.get_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at").fetchall()]


def _params(row):
    return json.loads(row["condition_params"] or "{}")


# --- parsers ---------------------------------------------------------------

def test_percent_threshold_parses():
    assert _parse_percent_threshold("change the threshold to 5 percent") == 5.0
    assert _parse_percent_threshold("set the discount to 10%") == 10.0
    assert _parse_percent_threshold("set the drop threshold to 20 percent") == 20.0
    assert _parse_percent_threshold("watch the price of iPhone 17") is None
    assert _parse_percent_threshold("change the threshold to 150 percent") is None


def test_replacement_price_parses():
    assert _parse_replacement_price("actually make that 10000 rupees not 5000") == (10000.0, 5000.0)
    assert _parse_replacement_price("change 8000 instead of 5000") == (8000.0, 5000.0)
    assert _parse_replacement_price("make it 999 rather than 499") == (999.0, 499.0)
    assert _parse_replacement_price("watch the price of iPhone 17") == (None, None)
    # "instead of every 5 minutes" is an interval change, never a price.
    assert _parse_replacement_price("make it check every hour instead of every day") == (None, None)


def test_threshold_price_parses_and_percent_guard():
    assert _parse_threshold_price("change the threshold to 4000 rupees") == 4000.0
    assert _parse_threshold_price("change the threshold to 5 percent") is None
    assert _parse_threshold_price("watch the price of iPhone 17") is None


# --- product-phrase guards ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "make it check every hour instead of every day",
    "change that watch to every 15 minutes instead of 5 minutes",
])
def test_product_phrase_never_returns_connective(text):
    assert _product_phrase_from_text(text) == ""


@pytest.mark.parametrize("title", ["Instead", "I Made", "This For Every 5 Mins", "Twice As Often"])
def test_garbage_titles_detected(title):
    assert _is_garbage_product_title(title) is True


@pytest.mark.parametrize("title", ["iPhone 17", "Sony WH-1000XM5", "Anker Soundcore Liberty 4 NC"])
def test_real_titles_not_garbage(title):
    assert _is_garbage_product_title(title) is False


def test_extract_product_never_returns_user_clause():
    assert _extract_product("cancel the last watch I made") == ""
    assert _extract_product("cn u watch the price of Sony headphones") == "Sony headphones"
    assert _extract_product("watch the price of Sony headphones") == "Sony headphones"


# --- absolute-update handler end to end --------------------------------------

@pytest.mark.asyncio
async def test_g25_percent_threshold_applies_in_place(tmp_db):
    _make_anker(tmp_db)
    reply = await handle_continuous_monitoring_instruction(
        "change the threshold to 5 percent", chat_id="test-chat", history=[], db_path=tmp_db)
    rows = _rows(tmp_db)
    assert len(rows) == 1, f"must not mint a second watch, got {[r['title'] for r in rows]}"
    assert rows[0]["condition_type"] == "price_drop"
    assert _params(rows[0])["min_percent"] == 5.0
    assert "5%" in reply


@pytest.mark.asyncio
async def test_g26_absolute_interval_applies_in_place(tmp_db):
    _make_anker(tmp_db)
    reply = await handle_continuous_monitoring_instruction(
        "make it check every hour instead of every day", chat_id="test-chat", history=[], db_path=tmp_db)
    rows = _rows(tmp_db)
    assert len(rows) == 1, f"must not mint a second watch, got {[r['title'] for r in rows]}"
    assert rows[0]["interval_seconds"] == 3600
    assert "1 hour" in reply


@pytest.mark.asyncio
async def test_g27_replacement_price_applies_in_place(tmp_db):
    _make_anker(tmp_db)
    reply = await handle_continuous_monitoring_instruction(
        "actually make that 10000 rupees not 5000", chat_id="test-chat", history=[], db_path=tmp_db)
    rows = _rows(tmp_db)
    assert len(rows) == 1, f"must not mint a second watch, got {[r['title'] for r in rows]}"
    assert rows[0]["condition_type"] == "below_price"
    assert _params(rows[0])["threshold"] == 10000.0
    assert "10000" in reply


@pytest.mark.asyncio
async def test_same_value_update_is_honest_noop(tmp_db):
    _make_anker(tmp_db)
    await handle_continuous_monitoring_instruction(
        "change the threshold to 5 percent", chat_id="test-chat", history=[], db_path=tmp_db)
    reply = await handle_continuous_monitoring_instruction(
        "change the threshold to 5 percent", chat_id="test-chat", history=[], db_path=tmp_db)
    assert "already running" in reply
    assert len(_rows(tmp_db)) == 1


@pytest.mark.asyncio
async def test_update_with_no_watch_falls_through_without_bogus_watch(tmp_db):
    # No watch exists: the turn must not mint "Instead" / "I Made".
    reply = await handle_continuous_monitoring_instruction(
        "make it check every hour instead of every day", chat_id="test-chat", history=[], db_path=tmp_db)
    titles = [r["title"] for r in _rows(tmp_db)]
    assert not any("Instead" in t or "I Made" in t for t in titles), titles
    assert "Instead" not in reply


@pytest.mark.asyncio
async def test_creation_still_works_after_updates(tmp_db):
    _make_anker(tmp_db)
    await handle_continuous_monitoring_instruction(
        "make it check every hour instead of every day", chat_id="test-chat", history=[], db_path=tmp_db)
    await handle_continuous_monitoring_instruction(
        "watch the price of boAt Airdopes 141 below 2000 every 10 minutes",
        chat_id="test-chat", history=[], db_path=tmp_db)
    rows = _rows(tmp_db)
    assert len(rows) == 2
    anker = [r for r in rows if "Anker" in r["title"]][0]
    assert anker["interval_seconds"] == 3600
    boat = [r for r in rows if "Airdopes" in r["title"]][0]
    assert boat["interval_seconds"] == 600
    assert _params(boat)["threshold"] == 2000.0


@pytest.mark.asyncio
async def test_absolute_update_wrong_product_asks_instead_of_updating(tmp_db):
    _make_anker(tmp_db)
    reply = await handle_continuous_monitoring_instruction(
        "change the Sony watch threshold to 5 percent", chat_id="test-chat", history=[], db_path=tmp_db)
    rows = _rows(tmp_db)
    assert len(rows) == 1
    assert _params(rows[0]).get("min_percent") is None
    assert "couldn't find a watch for" in reply
