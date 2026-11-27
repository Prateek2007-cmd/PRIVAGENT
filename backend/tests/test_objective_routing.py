"""Stream 1 (CONTRACTS4 §1): objective-understanding regression tests.

Every prompt here is NOVEL — none is the user's literal example.
Failure analogues use different products, brands, intervals and
wordings (headphones + 7 minutes, Pixel + 5 minutes, Fairphone,
OnePlus) so the tests pin the RULE, not the example.
"""
import json

import pytest

from app.intent_router import classify_intent, should_route_email_interval
from app.objective import (
    INTENTS,
    Objective,
    build_objective,
    classify_semantic,
)


# ---------------------------------------------------------------------------
# Failure A analogue: a product discount watch must never become an
# email-polling change, however strong its interval token is.
# ---------------------------------------------------------------------------

A_ANALOG = (
    "keep an eye on the Sony WH-1000XM5 headphones and ping me "
    "if they go 15% off, check every 7 minutes"
)


def test_headphones_discount_watch_routes_to_monitor_product():
    obj = build_objective(A_ANALOG)
    assert obj.primary_intent == "monitor_product"
    assert obj.entities.get("product") == "Sony WH-1000XM5 headphones"
    assert obj.interval_seconds == 420
    assert obj.condition == {"kind": "percent_discount", "value": 15.0, "operator": ">="}
    assert "notify" in obj.secondary_intents
    assert obj.primary_intent != "configure_email_polling"


def test_headphones_discount_watch_never_routes_to_email_polling():
    obj = build_objective(A_ANALOG)
    # Even with an active email mission in the chat, the email gate
    # must stay closed: the object is a product, not email.
    assert should_route_email_interval(obj, A_ANALOG, True) is False
    assert should_route_email_interval(obj, A_ANALOG, False) is False


def test_classify_headphones_watch_is_monitoring_not_email():
    cls = classify_intent(A_ANALOG)
    assert cls.intent.value == "NON_BROWSER"
    assert "monitoring" in cls.reason.lower()
    assert cls.objective is not None
    assert cls.objective.primary_intent == "monitor_product"


def test_email_polling_requires_email_noun_and_no_product_noun():
    # Genuine email polling change: email noun, no product noun.
    obj = build_objective("check my email every 15 minutes")
    assert obj.primary_intent == "configure_email_polling"
    assert obj.interval_seconds == 900
    assert should_route_email_interval(obj, "check my email every 15 minutes", True) is True

    # A product noun anywhere vetoes email polling, even with an
    # interval token and an active email mission.
    obj2 = build_objective(
        "watch the price of the Bose QC45 headphones and check every 20 minutes"
    )
    assert obj2.primary_intent == "monitor_product"
    assert should_route_email_interval(obj2, "watch the price of the Bose QC45 headphones", True) is False


def test_bare_interval_is_ambiguous_without_mission_context():
    obj = build_objective("check every 5 minutes")
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing
    # ... but with an active email mission it IS the polling change
    # (the old bare-interval behaviour, preserved deliberately).
    obj2 = build_objective(
        "check every 5 minutes", {"has_active_email_mission": True}
    )
    assert obj2.primary_intent == "configure_email_polling"
    assert obj2.interval_seconds == 300
    assert should_route_email_interval(obj2, "check every 5 minutes", True) is True


# ---------------------------------------------------------------------------
# Failure B analogue: a "check the latest ..." release ask is a live
# search, not a knowledge question.
# ---------------------------------------------------------------------------

B_ANALOG = "so can you check the latest OnePlus phone thats launched"


def test_latest_release_check_is_simple_search():
    obj = build_objective(B_ANALOG)
    assert obj.primary_intent == "simple_search"
    assert "OnePlus" in (obj.entities.get("query") or "")
    assert obj.completion_criteria == [
        ">=1 real search result with title+url, returned to the user"
    ]


def test_classify_latest_release_check_is_browser_task():
    cls = classify_intent(B_ANALOG)
    assert cls.intent.value == "NEW_BROWSER_TASK"
    assert cls.objective is not None
    assert cls.objective.primary_intent == "simple_search"


# ---------------------------------------------------------------------------
# Failure C analogue: a plain "search for <brand>" is a simple search,
# never comprehensive research.
# ---------------------------------------------------------------------------

C_ANALOG = "search for Fairphone"


def test_plain_brand_search_is_simple_search_not_research():
    obj = build_objective(C_ANALOG)
    assert obj.primary_intent == "simple_search"
    assert obj.entities.get("query") == "Fairphone"
    assert obj.primary_intent != "research"


def test_classify_plain_brand_search_is_browser_not_research():
    cls = classify_intent(C_ANALOG)
    assert cls.intent.value == "NEW_BROWSER_TASK"
    assert not cls.is_research
    assert cls.objective is not None
    assert cls.objective.primary_intent == "simple_search"
    # The simple-search bar is one real result — not the research
    # bar (>=3 substantive sources + evidence table + artifact).
    assert cls.objective.completion_criteria == [
        ">=1 real search result with title+url, returned to the user"
    ]


def test_real_research_still_routes_to_research():
    obj = build_objective(
        "research solid-state batteries and give me a comprehensive pdf report"
    )
    assert obj.primary_intent == "research"
    assert obj.entities.get("query") == "solid-state batteries"


# ---------------------------------------------------------------------------
# Failure E analogue: pronouns and intervals must never leak into the
# product name (the objective is what the watch/query is built from).
# ---------------------------------------------------------------------------

E_ANALOG = "Pixel 9 Pro so can u watch the price of this for every 5 mins"


def test_pronoun_and_interval_never_leak_into_product():
    obj = build_objective(E_ANALOG)
    assert obj.primary_intent == "monitor_product"
    assert obj.entities.get("product") == "Pixel 9 Pro"
    assert "5 mins" not in (obj.entities.get("product") or "")
    assert "this" not in (obj.entities.get("product") or "").lower().split()
    assert "every" not in (obj.entities.get("product") or "").lower()
    assert obj.references == ["this"]
    assert obj.interval_seconds == 300
    # No threshold was named: any price change is the condition, and
    # the gap is recorded honestly instead of invented.
    assert obj.condition == {"kind": "price_change", "value": None, "operator": None}
    assert "notification_condition" in obj.missing


def test_product_before_verb_beats_clause_after_verb():
    obj = build_objective(
        "Nothing Phone 3a so can u track the price of it for every 10 mins"
    )
    assert obj.primary_intent == "monitor_product"
    assert obj.entities.get("product") == "Nothing Phone 3a"
    assert obj.interval_seconds == 600


def test_below_price_condition_parses():
    obj = build_objective(
        "watch the OnePlus 13 price and alert me if it drops below 50000, "
        "check every 30 minutes"
    )
    assert obj.primary_intent == "monitor_product"
    assert obj.entities.get("product") == "OnePlus 13"
    assert obj.condition == {"kind": "absolute_price", "value": 50000.0, "operator": "<"}
    assert obj.interval_seconds == 1800


# ---------------------------------------------------------------------------
# Objective schema / vocabulary (CONTRACTS4 §1)
# ---------------------------------------------------------------------------


def test_objective_schema_round_trip():
    obj = build_objective(A_ANALOG)
    data = obj.to_dict()
    assert set(data) == {
        "raw", "primary_intent", "secondary_intents", "entities",
        "references", "interval_seconds", "condition", "target_url",
        "missing", "deliverables", "completion_criteria", "tools",
        "permissions",
    }
    assert Objective.from_dict(data) == obj
    assert data["primary_intent"] in INTENTS
    assert data["deliverables"] == ["persisted_watch", "baseline"]
    assert data["tools"] == ["monitoring.create_watch"]
    assert data["permissions"] == ["monitoring.create"]


def test_all_primary_intents_are_canonical():
    assert "simple_search" in INTENTS
    assert "monitor_product" in INTENTS
    assert "configure_email_polling" in INTENTS
    assert "research" in INTENTS


def test_monitor_website_and_listings_intents():
    web = build_objective("monitor https://example.org/notes every 15 minutes")
    assert web.primary_intent == "monitor_website"
    assert web.target_url == "https://example.org/notes"
    assert web.interval_seconds == 900

    listings = build_objective(
        "track new SDE internships and alert me when a new one appears"
    )
    assert listings.primary_intent == "monitor_new_listings"
    assert listings.entities.get("listing") == "SDE internships"


def test_greeting_and_schedule_intents():
    assert build_objective("hi").primary_intent == "greet"
    sched = build_objective("summarise this daily at 8am")
    assert sched.primary_intent == "schedule_task"
    assert sched.interval_seconds == 86400


# ---------------------------------------------------------------------------
# Semantic classification: only for genuinely ambiguous instructions,
# and only through a validated schema.
# ---------------------------------------------------------------------------


def _fake_model_ok(prompt: str) -> str:
    assert "Instruction: " in prompt
    return json.dumps({
        "primary_intent": "simple_search",
        "secondary_intents": [],
        "entities": {"query": "Fairphone 6 release date"},
        "references": [],
        "interval_seconds": None,
        "condition": None,
        "target_url": None,
        "missing": [],
    })


def test_semantic_classifier_used_only_when_deterministic_is_ambiguous():
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return _fake_model_ok(prompt)

    # Deterministic rules fire: the model is never consulted.
    obj = build_objective(C_ANALOG, semantic_classifier=spy)
    assert obj.primary_intent == "simple_search"
    assert calls == []

    # Genuinely ambiguous: the model decides, schema-validated.
    obj2 = build_objective("hmm, what about that thing", semantic_classifier=spy)
    assert len(calls) == 1
    assert obj2.primary_intent == "simple_search"
    assert obj2.entities.get("query") == "Fairphone 6 release date"


def test_semantic_classifier_rejects_invalid_model_output():
    def bad_intent(prompt: str) -> str:
        return json.dumps({"primary_intent": "teleport_the_phone", "entities": {}})

    def malformed(prompt: str) -> str:
        return "not json at all"

    def raises(prompt: str) -> str:
        raise RuntimeError("provider down")

    assert classify_semantic("hmm, what about that thing", bad_intent) is None
    assert classify_semantic("hmm, what about that thing", malformed) is None
    assert classify_semantic("hmm, what about that thing", raises) is None
    # ... and without any classifier the instruction stays honestly ambiguous.
    obj = build_objective("hmm, what about that thing")
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing


def test_connector_route_maps_to_canonical_intent():
    from app.connector_router import route_instruction, route_to_intent

    route = route_instruction("check my inbox")
    assert route is not None
    assert route_to_intent(route) == "connector_task"
    assert route_to_intent(None) is None

# ---------------------------------------------------------------------------
# S5 gen B5 / D10: pronoun-only watch-interval updates must route to
# update_monitor — never to the export/research sink, never to a
# search, and never to a bogus scheduled job. Prompts are novel
# wordings of the same SHAPE (different verbs, intervals, referents).
# ---------------------------------------------------------------------------

B5_ANALOGS = [
    # (prompt, expected interval_seconds or None for relative)
    ("switch that to every 45 minutes", 2700),
    ("change this to every 20 mins", 1200),
    ("update it to check every 45 minutes", 2700),
    ("change that watch to every 15 minutes", 900),
]


@pytest.mark.parametrize("prompt,interval", B5_ANALOGS)
def test_pronoun_interval_update_routes_to_update_monitor(prompt, interval):
    from app.intent_router import is_export_deliverable_directive

    obj = build_objective(prompt)
    assert obj.primary_intent == "update_monitor"
    assert obj.interval_seconds == interval
    assert obj.references, "the pronoun referent must be recorded"
    assert "watch_ref" in obj.missing, "referent resolves from working context"
    # The legacy router must not sink the turn into the export path:
    # that is what produced the empty Technical_Analysis.pdf live.
    assert is_export_deliverable_directive(prompt)[0] is False
    cls = classify_intent(prompt)
    assert cls.operation_type is None or cls.operation_type.name != "EXPORT_EXISTING_PACKAGE"
    assert cls.intent.value == "NON_BROWSER"


def test_relative_interval_update_twice_as_often():
    obj = build_objective("make that check twice as often")
    assert obj.primary_intent == "update_monitor"
    assert obj.entities.get("relative_interval") == "twice_as_often"
    assert obj.interval_seconds is None, "no number may be guessed"
    assert "watch_ref" in obj.missing


def test_relative_interval_update_half_as_often():
    obj = build_objective("cut that check to half as often")
    assert obj.primary_intent == "update_monitor"
    assert obj.entities.get("relative_interval") == "half_as_often"


def test_export_guard_keeps_genuine_reformats():
    # The B5 guard must not break real exports: a named format or
    # deliverable noun still routes to the export directive.
    from app.intent_router import is_export_deliverable_directive

    assert is_export_deliverable_directive("change it to 12 slides")[0] is True
    assert is_export_deliverable_directive("make the report into a pdf")[0] is True
    assert is_export_deliverable_directive("change it to every 45 mins")[0] is False


def test_objective_update_monitor_beats_schedule_misfire():
    # detect_schedule_intent reads "update it to check every 45
    # minutes" as create_schedule("update it to check"); the
    # agentLoop chain orders the objective monitor-control branch
    # BEFORE the schedule branch, so the objective is the decision
    # that must win. Pin the objective side of that ordering here.
    from app.intent_router import detect_schedule_intent

    prompt = "update it to check every 45 minutes"
    assert detect_schedule_intent(prompt) is not None  # the misfire exists
    obj = build_objective(prompt)
    assert obj.primary_intent == "update_monitor"
    assert obj.primary_intent in {
        "update_monitor", "pause_monitor", "resume_monitor",
        "cancel_monitor", "list_monitors",
    }


# ---------------------------------------------------------------------------
# Failure A, prove-it-cannot-happen: a REAL active email mission row in
# a temp DB, and the product-watch analogue still cannot route to
# email polling. The mission's existence is asserted first, so the
# test is not vacuous.
# ---------------------------------------------------------------------------


def test_email_polling_gate_holds_with_real_active_mission(tmp_path, monkeypatch):
    from app import db
    from app.email_intelligence.mission_setup import get_active_email_mission

    path = str(tmp_path / "s1-email.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    mission = db.create_email_mission(
        "tester@example.com",
        provider="gmail",
        frequency_seconds=600,
        conversation_id="s1-chat",
        db_path=path,
    )
    assert mission and mission.get("status") == "active"
    assert get_active_email_mission("s1-chat", db_path=path) is not None

    obj = build_objective(
        "live monitor Sony WH-1000XM5 headphones when it is 15% off, "
        "check every 7 minutes",
        context={"has_active_email_mission": True},
    )
    assert obj.primary_intent == "monitor_product"
    assert obj.interval_seconds == 420
    assert should_route_email_interval(obj, "live monitor Sony WH-1000XM5 headphones when it is 15% off, check every 7 minutes", True) is False

    # The same real mission present, a genuine email-polling change
    # still routes True — the gate is selective, not dead.
    obj2 = build_objective(
        "check my email every 15 minutes",
        context={"has_active_email_mission": True},
    )
    assert obj2.primary_intent == "configure_email_polling"
    assert should_route_email_interval(obj2, "check my email every 15 minutes", True) is True


def test_a_analog_creates_product_watch_via_handler(tmp_path, monkeypatch):
    """Dispatch-level proof for Failure A: once routed, the handler
    creates the product watch (420 s, 15% condition) and touches no
    email mission — the email interval stays at its own 600 s."""
    import asyncio

    from app import db
    from app.continuous_monitoring_companion import (
        handle_continuous_monitoring_instruction,
    )
    from app.email_intelligence.mission_setup import get_active_email_mission

    path = str(tmp_path / "s1-dispatch.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    db.create_email_mission(
        "tester@example.com",
        provider="gmail",
        frequency_seconds=600,
        conversation_id="s1-chat",
        db_path=path,
    )
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "live monitor Sony WH-1000XM5 headphones when it is 15% off, "
        "check every 7 minutes",
        chat_id="s1-chat",
        db_path=path,
    ))
    assert "WH-1000XM5" in answer
    with db.get_connection(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM monitoring_missions WHERE title LIKE '%WH-1000XM5%'")]
    assert len(rows) == 1
    assert rows[0]["interval_seconds"] == 420
    assert json.loads(rows[0]["condition_params"])["min_percent"] == 15.0
    # The email mission is untouched: still 600 s, still active.
    email = get_active_email_mission("s1-chat", db_path=path)
    assert email is not None and email["frequency_seconds"] == 600


@pytest.mark.xfail(strict=False, reason="Stream 3: companion fallback regex "
                  "leaks the pronoun into the Flipkart query ('This For "
                  "Every 5 Mins'); routing half is fixed (objective "
                  "extracts the real product), handler half still open")
def test_e_analog_handler_still_leaks_pronoun_into_query(tmp_path, monkeypatch):
    """Failure E, handler half: the objective now extracts the product
    correctly, but the companion still creates the garbage watch.
    This xfail pins Stream 3's remaining fix."""
    import asyncio

    from app import db
    from app.continuous_monitoring_companion import (
        handle_continuous_monitoring_instruction,
    )

    path = str(tmp_path / "s1-e-leak.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    obj = build_objective(
        "Galaxy S26 Ultra 5G so can u watch the price of this for every 5 mins"
    )
    assert obj.primary_intent == "monitor_product"
    assert obj.entities.get("product") == "Galaxy S26 Ultra 5G"
    asyncio.run(handle_continuous_monitoring_instruction(
        "Galaxy S26 Ultra 5G so can u watch the price of this for every 5 mins",
        chat_id="s1-chat",
        db_path=path,
    ))
    with db.get_connection(path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM monitoring_missions")]
    assert len(rows) == 1
    assert "S26" in rows[0]["title"], rows[0]
    assert "this+for+every+5+mins" not in rows[0]["target_url"], rows[0]
