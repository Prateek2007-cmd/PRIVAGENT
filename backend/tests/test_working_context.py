"""Stream 2 — working context (CONTRACTS4 §2).

Novel prompts only: none of these use the user's literal example
prompts. Products here (Aurora X9 earbuds, Vertex Pro laptop,
Nimbus Trail shoes, Kestrel 4K drone, Halcyon keyboard) are invented
for the tests.
"""

import json
import time
import uuid

import pytest

from app import db
from app.working_context import (
    WorkingContext,
    find_mission_row,
    load_working_context,
    record_entity,
    record_result,
    record_watch,
    remove_watch,
    resolve_reference,
    resolve_watch_target,
    save_working_context,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "wc.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _ctx_with_two_products():
    """Most-recent-first: Vertex Pro laptop (latest), Aurora X9 earbuds."""
    ctx = WorkingContext(chat_id="chat-1")
    record_entity(ctx, "product", "Aurora X9 earbuds",
                  {"price": 3499.0, "url": "https://example.org/aurora-x9"})
    record_entity(ctx, "product", "Vertex Pro laptop",
                  {"price": 89999.0, "url": "https://example.org/vertex-pro"})
    return ctx


# --- shape & persistence -------------------------------------------------

def test_entities_are_most_recent_first_and_deduped():
    ctx = WorkingContext(chat_id="c")
    record_entity(ctx, "product", "Aurora X9 earbuds")
    record_entity(ctx, "product", "Vertex Pro laptop")
    record_entity(ctx, "product", "Aurora X9 earbuds")
    assert [e["label"] for e in ctx.entities] == [
        "Aurora X9 earbuds", "Vertex Pro laptop"]
    assert ctx.entities[0]["kind"] == "product"
    assert ctx.entities[0]["turn"] >= 1


def test_round_trip_through_working_context_table(database):
    # The watch must exist as a mission row: load re-syncs watches
    # from the missions table (source of truth), so a blob-only watch
    # is not resurrected.
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, conversation_id, title, target_url, trigger_type,
                condition_type, condition_params, interval_seconds,
                schedule_cron, status, created_at, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
            ("w-1", "chat-1", "Price Watch: Aurora X9 earbuds",
             "https://example.org/aurora-x9", "interval", "price_drop",
             json.dumps({"target": "Aurora X9 earbuds", "min_percent": 10}),
             300, None, time.time(), time.time()))
        conn.commit()
    ctx = _ctx_with_two_products()
    record_result(ctx, "Aurora X9 review", "https://example.org/review-aurora")
    save_working_context(ctx)
    loaded = load_working_context("chat-1", database)
    assert [e["label"] for e in loaded.entities] == [
        "Vertex Pro laptop", "Aurora X9 earbuds"]
    assert loaded.last_results[0]["url"] == "https://example.org/review-aurora"
    assert loaded.active_watches[0]["watch_id"] == "w-1"
    assert loaded.active_watches[0]["label"] == "Aurora X9 earbuds"
    assert loaded.chat_id == "chat-1"


def test_load_unknown_chat_is_empty_and_corrupt_blob_is_empty(database):
    assert load_working_context("no-such-chat", database).entities == []
    with db.get_connection(database) as conn:
        conn.execute("INSERT INTO working_context (chat_id, data, updated_at)"
                     " VALUES (?, ?, ?)", ("bad", "{not json", time.time()))
        conn.commit()
    assert load_working_context("bad", database).entities == []


def test_working_context_never_touches_persistent_memory_tables(database):
    """Working context is separate from user_semantic_facts /
    long_memory_facts by design: recording turns must not create a
    row in either durable store."""
    ctx = _ctx_with_two_products()
    save_working_context(ctx)
    load_working_context("chat-1", database)
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_semantic_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM long_memory_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM working_context").fetchone()[0] == 1


def test_model_view_sends_at_most_two_entity_labels():
    """CONTRACTS4 §2: only the resolved objective + last 2 entity
    labels go to a remote model — never full history or details."""
    ctx = _ctx_with_two_products()
    record_entity(ctx, "product", "Nimbus Trail running shoes")
    view = ctx.model_view(objective="watch the price of the second one")
    assert view["entity_labels"] == ["Nimbus Trail running shoes", "Vertex Pro laptop"]
    assert view["objective"] == "watch the price of the second one"
    # No price VALUES, no URLs, no details — the word "price" may
    # appear inside the objective text itself, but no number from a
    # detail and no link may leak.
    dumped = json.dumps(view)
    assert "3499" not in dumped and "89999" not in dumped
    assert "example.org" not in dumped
    assert "detail" not in dumped
    assert set(view) == {"entity_labels", "active_watch_labels", "objective"}


def test_model_view_without_objective_has_no_objective_key():
    ctx = _ctx_with_two_products()
    view = ctx.model_view()
    assert "objective" not in view
    assert view["entity_labels"] == ["Vertex Pro laptop", "Aurora X9 earbuds"]


# --- deictic resolution ----------------------------------------------------

def test_bare_this_resolves_to_most_recent_entity():
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "watch this for me")
    assert res["status"] == "resolved"
    assert res["entity"]["label"] == "Vertex Pro laptop"
    assert res["score"] >= 0.5


def test_second_one_resolves_to_second_most_recent():
    """Multi-turn chain: two products discussed, then "the second one"."""
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "watch the price of the second one")
    assert res["status"] == "resolved"
    assert res["entity"]["label"] == "Aurora X9 earbuds"


def test_first_and_latest_resolve_to_most_recent():
    ctx = _ctx_with_two_products()
    assert resolve_reference(ctx, "open the first one")["entity"]["label"] == "Vertex Pro laptop"
    assert resolve_reference(ctx, "show me the latest one")["entity"]["label"] == "Vertex Pro laptop"


def test_previous_product_is_the_one_before_the_latest():
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "what about the previous product?")
    assert res["status"] == "resolved"
    assert res["entity"]["label"] == "Aurora X9 earbuds"


def test_cheapest_picks_lowest_priced_entity():
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "watch the cheapest one")
    assert res["status"] == "resolved"
    assert res["entity"]["label"] == "Aurora X9 earbuds"


def test_cheapest_without_prices_asks_instead_of_guessing():
    ctx = WorkingContext(chat_id="c")
    record_entity(ctx, "product", "Aurora X9 earbuds")
    record_entity(ctx, "product", "Vertex Pro laptop")
    res = resolve_reference(ctx, "watch the cheapest one")
    assert res["status"] == "clarification"
    assert "cheapest" in res["question"]


def test_empty_context_asks_for_clarification():
    ctx = WorkingContext(chat_id="c")
    res = resolve_reference(ctx, "watch this for me")
    assert res["status"] == "clarification"
    assert "Which one" in res["question"]


def test_ordinal_beyond_what_was_discussed_asks():
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "watch the price of the fourth one")
    assert res["status"] == "clarification"
    assert "2 item" in res["question"]


def test_control_follow_up_prefers_the_active_watch():
    ctx = _ctx_with_two_products()
    record_watch(ctx, {"watch_id": "w-9", "label": "Aurora X9 earbuds",
                       "title": "Price Watch: Aurora X9 earbuds",
                       "product": "Aurora X9 earbuds", "interval_seconds": 300,
                       "condition": {"type": "price_drop", "params": {"min_percent": 10}},
                       "state": "active"})
    res = resolve_reference(ctx, "change it to every twelve minutes", prefer="watch")
    assert res["status"] == "resolved"
    assert res["watch"]["watch_id"] == "w-9"
    assert res["entity"] is None


def test_cancel_that_watch_resolves_the_watch():
    ctx = _ctx_with_two_products()
    record_watch(ctx, {"watch_id": "w-9", "label": "Aurora X9 earbuds",
                       "title": "Price Watch: Aurora X9 earbuds",
                       "product": "Aurora X9 earbuds", "interval_seconds": 300,
                       "condition": {"type": "price_drop", "params": {"min_percent": 10}},
                       "state": "active"})
    res = resolve_reference(ctx, "cancel that watch")
    assert res["status"] == "resolved"
    assert res["watch"]["watch_id"] == "w-9"
    remove_watch(ctx, "w-9")
    assert ctx.active_watches == []


def test_do_the_same_for_this_marks_repeat():
    ctx = _ctx_with_two_products()
    res = resolve_reference(ctx, "do the same for this one too")
    assert res["status"] == "resolved"
    assert res["repeat"] is True
    assert res["entity"]["label"] == "Vertex Pro laptop"


# --- turn recording ---------------------------------------------------------

def test_note_completed_turn_records_watch_marker_and_entity(database):
    """A reply carrying the privagent-watch marker proves a watch was
    created: the mission row is read back and recorded as watch +
    product entity."""
    from app.working_context import note_completed_turn

    mission_id = str(uuid.uuid4())
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, conversation_id, title, target_url, trigger_type,
                condition_type, condition_params, interval_seconds,
                schedule_cron, status, created_at, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (mission_id, "chat-1", "Price Watch: Kestrel 4K Drone",
             "https://example.org/kestrel-4k", "interval", "below_price",
             json.dumps({"target": "Kestrel 4K Drone", "threshold": 40000}),
             300, None, time.time(), time.time()))
        conn.commit()
    ctx = WorkingContext(chat_id="chat-1")
    note_completed_turn(
        ctx,
        instruction="watch the kestrel 4k drone below 40000",
        message="### 🎯 Continuous Price Monitoring Activated\n\n"
                f"<!-- privagent-watch: {mission_id} -->",
        state="completed",
        db_path=database,
    )
    assert ctx.active_watches[0]["watch_id"] == mission_id
    assert ctx.active_watches[0]["label"] == "Kestrel 4K Drone"
    assert ctx.entities[0]["label"] == "Kestrel 4K Drone"
    assert ctx.entities[0]["kind"] == "product"


def test_note_completed_turn_records_urls_as_results(database):
    from app.working_context import note_completed_turn

    ctx = WorkingContext(chat_id="chat-1")
    note_completed_turn(
        ctx,
        instruction="find the halcyon mechanical keyboard",
        message="Completed — cheapest listing:\n"
                "- Halcyon Keyboard Store https://example.org/halcyon-kb\n"
                "- Second seller https://example.org/halcyon-2",
        state="completed",
        db_path=database,
    )
    # Most-recent-first: the second URL recorded is listed first.
    assert [r["url"] for r in ctx.last_results] == [
        "https://example.org/halcyon-2", "https://example.org/halcyon-kb"]
    assert ctx.entities[0]["label"] == "halcyon mechanical keyboard"


def test_note_completed_turn_ignores_garbage_instruction_labels(database):
    """A follow-up that sanitises to nothing ("watch this for every
    5 mins" pattern) must not store a garbage entity."""
    from app.working_context import note_completed_turn

    ctx = WorkingContext(chat_id="chat-1")
    note_completed_turn(
        ctx,
        instruction="watch this for every twelve minutes",
        message="I don't have a product to watch yet.",
        state="completed",
        db_path=database,
    )
    assert ctx.entities == []
    assert ctx.last_results == []


def test_agentloop_recording_hook_writes_working_context(database):
    """agentLoop._record_working_context_turn: the run_task finally
    hook records a finished turn into the chat's working context."""
    from app import agentLoop
    from app.hub import hub

    mission_id = str(uuid.uuid4())
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, conversation_id, title, target_url, trigger_type,
                condition_type, condition_params, interval_seconds,
                schedule_cron, status, created_at, next_run_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (mission_id, "chat-hook", "Price Watch: Kestrel 4K Drone",
             "https://example.org/kestrel-4k", "interval", "below_price",
             json.dumps({"target": "Kestrel 4K Drone", "threshold": 40000}),
             300, None, time.time(), time.time()))
        conn.commit()
    rec = hub.new_task("watch the kestrel 4k drone below 40000", chat_id="chat-hook")
    rec.state = "completed"
    rec.message = ("### 🎯 Continuous Price Monitoring Activated\n\n"
                   f"<!-- privagent-watch: {mission_id} -->")
    try:
        agentLoop._record_working_context_turn(rec.task_id)
    finally:
        hub.tasks.pop(rec.task_id, None)
    loaded = load_working_context("chat-hook", database)
    assert loaded.active_watches[0]["watch_id"] == mission_id
    assert loaded.entities[0]["label"] == "Kestrel 4K Drone"

# --- resolve_watch_target (Stream 5 F2 follow-up) -------------------------
#
# Novel products only. The two-watch context is built oldest-first
# through record_watch (which prepends), so active_watches is
# most-recent-first: Nimbus Trail shoes (latest), Aurora X9 earbuds
# (first / oldest).

def _ctx_with_two_watches():
    ctx = WorkingContext(chat_id="chat-w")
    record_watch(ctx, {
        "watch_id": "aaaaaaaa-1111-2222-3333-444444444444",
        "label": "Aurora X9 earbuds",
        "title": "Price Watch: Aurora X9 Earbuds",
        "product": "Aurora X9 earbuds",
        "interval_seconds": 600, "condition": "below_price",
        "state": "active", "target_url": "https://example.org/aurora-x9",
        "created_at": 1000.0,
    })
    record_watch(ctx, {
        "watch_id": "bbbbbbbb-1111-2222-3333-444444444444",
        "label": "Nimbus Trail shoes",
        "title": "Price Watch: Nimbus Trail Shoes",
        "product": "Nimbus Trail shoes",
        "interval_seconds": 900, "condition": "price_drop",
        "state": "active", "target_url": "https://example.org/nimbus-trail",
        "created_at": 2000.0,
    })
    return ctx


def test_first_watch_is_the_oldest_by_creation():
    result = resolve_watch_target(_ctx_with_two_watches(), "Pause the first watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Aurora X9 earbuds"


def test_latest_watch_is_the_most_recent():
    result = resolve_watch_target(_ctx_with_two_watches(), "Cancel the latest watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Nimbus Trail shoes"


def test_ordinal_out_of_range_asks_instead_of_guessing():
    result = resolve_watch_target(_ctx_with_two_watches(), "Pause the third watch.")
    assert result["status"] == "clarification"
    assert "Aurora X9" in result["question"] and "Nimbus Trail" in result["question"]


def test_brand_before_watch_resolves_by_maker_family():
    ctx = _ctx_with_two_watches()
    record_watch(ctx, {
        "watch_id": "cccccccc-1111-2222-3333-444444444444",
        "label": "Galaxy Tab S11",
        "title": "Price Watch: Galaxy Tab S11",
        "product": "Galaxy Tab S11",
        "interval_seconds": 300, "condition": "below_price",
        "state": "active", "target_url": "https://example.org/galaxy-tab-s11",
        "created_at": 3000.0,
    })
    result = resolve_watch_target(ctx, "Cancel the Samsung watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Galaxy Tab S11"


def test_product_token_resolves_the_headphones_watch():
    ctx = _ctx_with_two_watches()
    record_watch(ctx, {
        "watch_id": "dddddddd-1111-2222-3333-444444444444",
        "label": "Halcyon keyboard",
        "title": "Price Watch: Halcyon Keyboard",
        "product": "Halcyon keyboard",
        "interval_seconds": 300, "condition": "below_price",
        "state": "active", "target_url": "https://example.org/halcyon",
        "created_at": 3000.0,
    })
    result = resolve_watch_target(ctx, "Pause the earbuds one.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Aurora X9 earbuds"


def test_pronoun_resolves_to_the_most_recent_watch():
    result = resolve_watch_target(_ctx_with_two_watches(), "Resume it.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Nimbus Trail shoes"


def test_bare_the_watch_resolves_to_the_most_recent():
    result = resolve_watch_target(_ctx_with_two_watches(), "Cancel the watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Nimbus Trail shoes"


def test_named_referent_with_no_match_clarifies_never_guesses():
    result = resolve_watch_target(_ctx_with_two_watches(), "Cancel the Zebra watch.")
    assert result["status"] == "clarification"
    assert "Zebra" in result["question"]


def test_unknown_token_before_one_clarifies():
    result = resolve_watch_target(_ctx_with_two_watches(), "Pause the zebra one.")
    assert result["status"] == "clarification"


def test_no_watches_reports_none():
    result = resolve_watch_target(WorkingContext(chat_id="empty"), "Cancel it.")
    assert result["status"] == "none"


def test_single_watch_resolves_a_bare_control():
    ctx = WorkingContext(chat_id="one")
    record_watch(ctx, {
        "watch_id": "eeeeeeee-1111-2222-3333-444444444444",
        "label": "Kestrel 4K drone",
        "title": "Price Watch: Kestrel 4K Drone",
        "product": "Kestrel 4K drone",
        "interval_seconds": 300, "condition": "below_price",
        "state": "active", "target_url": "https://example.org/kestrel-4k",
        "created_at": 1000.0,
    })
    result = resolve_watch_target(ctx, "Pause it.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Kestrel 4K drone"


def test_generic_page_token_resolves_a_page_watch():
    ctx = WorkingContext(chat_id="page")
    record_watch(ctx, {
        "watch_id": "ffffffff-1111-2222-3333-444444444444",
        "label": "example.org changelog",
        "title": "Page Watch: example.org changelog",
        "product": "example.org changelog",
        "interval_seconds": 3600, "condition": "page_change",
        "state": "active", "target_url": "https://example.org/changelog",
        "created_at": 1000.0,
    })
    record_watch(ctx, {
        "watch_id": "aaaaaaaa-1111-2222-3333-444444444444",
        "label": "Aurora X9 earbuds",
        "title": "Price Watch: Aurora X9 Earbuds",
        "product": "Aurora X9 earbuds",
        "interval_seconds": 600, "condition": "below_price",
        "state": "active", "target_url": "https://example.org/aurora-x9",
        "created_at": 2000.0,
    })
    result = resolve_watch_target(ctx, "Cancel that website watch.")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "example.org changelog"


def test_explicit_id_prefix_resolves():
    ctx = _ctx_with_two_watches()
    result = resolve_watch_target(ctx, "cancel watch bbbbbbbb")
    assert result["status"] == "resolved"
    assert result["watch"]["label"] == "Nimbus Trail shoes"
