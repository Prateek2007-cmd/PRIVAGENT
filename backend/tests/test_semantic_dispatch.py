"""Stream B (CONTRACTS6): semantic objective controls dispatch.

Precedence rule under test (intent_router.resolve_dispatch):
  1. Semantic failed + legacy browser intent -> ASK (await_input),
     never run a browser/research workflow on a guess.
  2. Semantic objective whose task type differs from the legacy
     intent -> the semantic type wins (either direction), logged as
     SEMANTIC_OVERRIDE with the legacy keyword rule.
  3. Otherwise the legacy intent stands. A deterministic objective
     NEVER overrules a legacy browser intent (compound find+track
     policy, how-to guard, monitoring-directive correctness).
  4. CONTINUE_BROWSER_TASK / USE_EXISTING_USER_TAB are never
     overridden (the classifier has no notion of sessions/tabs).

Plus the within-objective conflict (objective.build_objective):
when >= 2 deterministic families fire and the model disagrees with
what the deterministic cascade would have picked, a
SEMANTIC_OVERRIDE (reason=deterministic_cascade) is logged.

Fake providers only — the real classifier and the real
classify_semantic run in every test; only the provider boundary
behind them is faked (same pattern as test_semantic_wiring.py).
"""
import json
import logging

import pytest

from app.intent_router import (
    TaskIntent,
    classify_intent,
    fallback_second_failure,
    log_dispatch_decision,
    resolve_dispatch,
)
from app.objective import build_objective
from app.semantic_classifier import clear_cache, make_semantic_classifier


@pytest.fixture(autouse=True)
def _clear_semantic_cache():
    clear_cache()
    yield
    clear_cache()


class FakeProvider:
    """Provider-shaped fake: chat_call returns one canned JSON reply."""

    def __init__(self, reply, model="deepseek-v4.1-flash"):
        self._reply = reply
        self._model = model
        self.calls = []

    def name(self):
        return f"freepi:{self._model}"

    def chat_call(self, prompt, history=None, system_prompt=None):
        self.calls.append(prompt)
        if isinstance(self._reply, str):
            return self._reply
        return json.dumps(self._reply)


class RaisingProvider:
    """Provider-shaped fake whose chat_call always raises."""

    def name(self):
        return "freepi:deepseek-v4.1-flash"

    def chat_call(self, prompt, history=None, system_prompt=None):
        raise RuntimeError("provider unavailable")


def _sem(primary, **kw):
    d = {
        "primary_intent": primary,
        "secondary_intents": [],
        "entities": {},
        "references": [],
        "interval_seconds": None,
        "condition": None,
        "target_url": None,
        "missing": [],
    }
    d.update(kw)
    return d


def _classify(text, reply=None, task_id="t", context=None):
    ctx = {"task_id": task_id, "extension_connected": False}
    ctx.update(context or {})
    classifier = None
    if reply is not None:
        provider = reply if not isinstance(reply, dict) else FakeProvider(reply)
        classifier = make_semantic_classifier(
            task_id=task_id, provider_factory=lambda: provider
        )
    return classify_intent(text, context=ctx, semantic_classifier=classifier)


# ---------------------------------------------------------------------------
# Rule 2: semantic wins on conflict (both directions)
# ---------------------------------------------------------------------------


def test_semantic_browser_task_overrides_legacy_non_browser():
    # "what is latest from this": legacy sees no browsing directive
    # (NON_BROWSER, conversational); the model says browser_task.
    # Before Stream B this turn never browsed — it fell to the
    # conversational sink. Now dispatch follows the objective.
    c = _classify(
        "what is latest from this", _sem("browser_task"), task_id="b-1"
    )
    assert c.intent == TaskIntent.NON_BROWSER
    assert c.objective.primary_intent == "browser_task"
    assert c.objective.source == "semantic"
    d = resolve_dispatch(c, task_id="b-1")
    assert d.overridden is True
    assert d.is_browser is True
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK
    assert d.source == "semantic"
    assert d.await_input is False


def test_semantic_weather_overrides_legacy_conversational():
    c = _classify(
        "what is the weather in Hyderabad", _sem("simple_search"), task_id="b-2"
    )
    assert c.intent == TaskIntent.NON_BROWSER
    d = resolve_dispatch(c, task_id="b-2")
    assert d.overridden is True
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_semantic_flight_booking_overrides_wrong_monitoring_keyword():
    # "book a flight ..." matches the legacy monitoring-directive
    # keyword ("Continuous background monitoring directive") — a
    # confidently incorrect keyword match. The model says
    # browser_task; semantic wins and no monitoring workflow runs.
    c = _classify(
        "book a flight from Hyderabad to Delhi tomorrow",
        _sem("browser_task"),
        task_id="b-3",
    )
    assert c.intent == TaskIntent.NON_BROWSER
    assert "monitoring" in c.reason.lower()
    d = resolve_dispatch(c, task_id="b-3")
    assert d.overridden is True
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK
    assert d.keyword_rule == c.reason


def test_semantic_connector_task_overrides_legacy_browser():
    # Messaging send: legacy says NEW_BROWSER_TASK; the model says
    # connector_task. Dispatch goes NON_BROWSER (connector block
    # first, then the conversational chain) — no browser tab opens.
    c = _classify(
        "send the report to prateek in a chat app",
        _sem("connector_task"),
        task_id="b-4",
    )
    assert c.intent == TaskIntent.NEW_BROWSER_TASK
    d = resolve_dispatch(c, task_id="b-4")
    assert d.overridden is True
    assert d.is_browser is False
    assert d.effective_intent == TaskIntent.NON_BROWSER


def test_semantic_research_overrides_legacy_new_browser_task():
    c = _classify(
        "search for the history of the internet and write a report",
        _sem("research"),
        task_id="b-5",
    )
    # Legacy may already say RESEARCH_TASK here; the assertion that
    # matters is the effective intent follows the semantic objective.
    d = resolve_dispatch(c, task_id="b-5")
    assert d.effective_intent == TaskIntent.RESEARCH_TASK
    assert d.is_browser is True


def test_semantic_simple_search_downgrades_legacy_research_task():
    # Compound (research + latest-release check) so the model IS
    # consulted; it says simple_search while legacy says
    # RESEARCH_TASK. Dispatch follows the model: a one-off price
    # check runs in the browser loop, not the research pipeline.
    c = _classify(
        "research the iPhone 17 price and check the latest Pixel phone out",
        _sem("simple_search"),
        task_id="b-6",
    )
    assert c.intent == TaskIntent.RESEARCH_TASK
    d = resolve_dispatch(c, task_id="b-6")
    assert d.overridden is True
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_semantic_override_logs_keyword_rule(caplog):
    c = _classify(
        "what is latest from this", _sem("browser_task"), task_id="b-7"
    )
    with caplog.at_level(logging.INFO, logger="app.intent_router"):
        resolve_dispatch(c, task_id="b-7")
    lines = [r.getMessage() for r in caplog.records if "SEMANTIC_OVERRIDE" in r.getMessage()]
    assert len(lines) == 1
    assert "task_id=b-7" in lines[0]
    assert "semantic_result=browser_task" in lines[0]
    assert "reason=legacy_keyword_intent" in lines[0]
    assert "keyword_rule=" in lines[0]


def test_semantic_agreement_logs_no_override(caplog):
    # Legacy browser + semantic browser_task: same family, no conflict.
    c = _classify("open gmail", _sem("browser_task"), task_id="b-8")
    assert c.intent == TaskIntent.NEW_BROWSER_TASK
    with caplog.at_level(logging.INFO, logger="app.intent_router"):
        d = resolve_dispatch(c, task_id="b-8")
    assert d.overridden is False
    assert not [r for r in caplog.records if "SEMANTIC_OVERRIDE" in r.getMessage()]


# ---------------------------------------------------------------------------
# Within-objective conflict: semantic vs deterministic cascade
# ---------------------------------------------------------------------------


def test_semantic_overrides_deterministic_cascade_logs_override(caplog):
    # >= 2 families fire (search + schedule); the cascade would pick
    # simple_search, the model picks monitor_product.
    with caplog.at_level(logging.INFO, logger="app.objective"):
        obj = build_objective(
            "check the price of iPhone 17 on amazon every hour and remind me if it drops",
            context={"task_id": "b-9"},
            semantic_classifier=make_semantic_classifier(
                task_id="b-9",
                provider_factory=lambda: FakeProvider(_sem("monitor_product")),
            ),
        )
    assert obj.primary_intent == "monitor_product"
    assert obj.source == "semantic"
    lines = [r.getMessage() for r in caplog.records if "SEMANTIC_OVERRIDE" in r.getMessage()]
    assert len(lines) == 1
    assert "keyword_rule=simple_search" in lines[0]
    assert "semantic_result=monitor_product" in lines[0]
    assert "reason=deterministic_cascade" in lines[0]


def test_semantic_agrees_with_cascade_logs_no_override(caplog):
    # >= 2 families fire; cascade and model both say monitor_product.
    with caplog.at_level(logging.INFO, logger="app.objective"):
        obj = build_objective(
            "monitor the price of iPhone 17 and send me a summary every day",
            context={"task_id": "b-10"},
            semantic_classifier=make_semantic_classifier(
                task_id="b-10",
                provider_factory=lambda: FakeProvider(_sem("monitor_product")),
            ),
        )
    assert obj.primary_intent == "monitor_product"
    assert not [r for r in caplog.records if "SEMANTIC_OVERRIDE" in r.getMessage()]


def test_zero_family_semantic_logs_no_override(caplog):
    # Zero families: no deterministic match existed, so there is
    # nothing to override — no SEMANTIC_OVERRIDE line.
    with caplog.at_level(logging.INFO, logger="app.objective"):
        obj = build_objective(
            "what is latest from this",
            context={"task_id": "b-11"},
            semantic_classifier=make_semantic_classifier(
                task_id="b-11",
                provider_factory=lambda: FakeProvider(_sem("browser_task")),
            ),
        )
    assert obj.primary_intent == "browser_task"
    assert not [r for r in caplog.records if "SEMANTIC_OVERRIDE" in r.getMessage()]


# ---------------------------------------------------------------------------
# Rule 1: semantic failed -> ask, don't guess
# ---------------------------------------------------------------------------


def test_fallback_with_legacy_browser_asks_instead_of_guessing():
    c = _classify("open gmail", RaisingProvider(), task_id="b-12")
    assert c.intent == TaskIntent.NEW_BROWSER_TASK
    assert c.objective.primary_intent == "conversational"
    assert c.objective.source == "fallback"
    assert c.objective.semantic_failed is True
    d = resolve_dispatch(c, task_id="b-12")
    assert d.await_input is True
    assert d.is_browser is False
    assert d.effective_intent == TaskIntent.NON_BROWSER
    assert d.source == "fallback"
    assert d.overridden is True


def test_second_failure_goes_conversational_and_applies():
    # Regression (2026-11-27, found live): after the user answered
    # the ask gate and the semantic call failed a SECOND time, the
    # dispatch site replaced the decision with overridden=False —
    # so the "apply override" step skipped it and the task
    # dispatched on the legacy NEW_BROWSER_TASK while the
    # DISPATCH_DECISION line recorded non-browser. The helper keeps
    # overridden=True, so the dispatch site's application rule lands
    # the conversational intent and the log agrees with dispatch.
    from app.intent_router import fallback_second_failure

    c = _classify("open gmail", RaisingProvider(), task_id="b-12b")
    first = resolve_dispatch(c, task_id="b-12b")
    assert first.await_input is True
    # The user answered; re-classify with the clarified text (the
    # exact format agentLoop builds: original + the gate question +
    # the answer) and the provider fails again. NOTE: the short form
    # "[Operator's answer: open gmail.com]" does NOT reproduce this
    # — the deterministic cascade matches the bare URL and returns
    # browser_task without ever consulting semantic. The full live
    # text is what reaches the fallback.
    _q = (
        "I couldn't work out what you meant by 'open gmail' — "
        "the automatic check didn't return a clear answer. "
        "What should I do with it?"
    )
    c2 = _classify(
        f"open gmail\n[Operator's answer to '{_q}': open gmail.com]",
        RaisingProvider(),
        task_id="b-12b",
    )
    second = resolve_dispatch(c2, task_id="b-12b")
    assert second.await_input is True
    assert c2.intent == TaskIntent.NEW_BROWSER_TASK
    final = fallback_second_failure(second)
    assert final.await_input is False
    assert final.overridden is True
    assert final.is_browser is False
    assert final.effective_intent == TaskIntent.NON_BROWSER
    assert final.source == "fallback"
    # The dispatch site's application rule, verbatim:
    intent = c2.intent
    if final.overridden:
        intent = final.effective_intent
    assert intent == TaskIntent.NON_BROWSER
    # And the DISPATCH_DECISION line the site logs for it is honest:
    assert final.is_browser is False
    assert final.effective_intent == intent


def test_fallback_with_legacy_non_browser_does_not_ask():
    # Nothing would have run anyway (legacy is conversational), so
    # there is no guessed workflow to stop — the turn flows to the
    # conversational sink as before.
    c = _classify("hmm, what about that thing", RaisingProvider(), task_id="b-13")
    assert c.intent == TaskIntent.NON_BROWSER
    d = resolve_dispatch(c, task_id="b-13")
    assert d.await_input is False
    assert d.overridden is False


def test_invalid_semantic_json_is_a_fallback():
    c = _classify(
        "open gmail", FakeProvider("this is not json at all"), task_id="b-14"
    )
    assert c.objective.source == "fallback"
    assert c.objective.semantic_failed is True
    d = resolve_dispatch(c, task_id="b-14")
    assert d.await_input is True


def test_no_classifier_fallback_does_not_ask():
    # No classifier was ever consulted (old callers): the fallback is
    # honest conversational, not a failed semantic call, so dispatch
    # keeps the legacy intent instead of asking.
    c = classify_intent(
        "open gmail",
        context={"task_id": "b-15", "extension_connected": False},
        semantic_classifier=None,
    )
    assert c.objective.source == "fallback"
    assert c.objective.semantic_failed is False
    d = resolve_dispatch(c, task_id="b-15")
    assert d.await_input is False
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_mock_provider_is_unavailable_not_failed():
    # Structural unavailability (mock provider — no real model
    # exists) is NOT a failure: dispatch runs the legacy intent
    # instead of parking the turn in the ask gate. Regression:
    # before this distinction, every mock-provider ambiguous
    # browser turn blocked 300 s in the gate (compound sub-tasks
    # hung the whole suite).
    from app.providers.mock import MockProvider

    c = classify_intent(
        "open gmail",
        context={"task_id": "b-15m", "extension_connected": False},
        semantic_classifier=make_semantic_classifier(
            task_id="b-15m", provider_factory=lambda: MockProvider()
        ),
    )
    assert c.objective.source == "fallback"
    assert c.objective.semantic_unavailable is True
    assert c.objective.semantic_failed is False
    d = resolve_dispatch(c, task_id="b-15m")
    assert d.await_input is False
    assert d.overridden is False
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_classify_semantic_propagates_mock_refusal():
    # The refusal propagates out of classify_semantic (other
    # failures still return None) so build_objective can tell
    # unavailability apart from transient failure.
    from app.objective import classify_semantic
    from app.semantic_classifier import MockProviderRefused

    def _refuse(prompt):
        raise MockProviderRefused("mock")

    with pytest.raises(MockProviderRefused):
        classify_semantic("open gmail", _refuse)

    def _boom(prompt):
        raise RuntimeError("boom")

    assert classify_semantic("open gmail", _boom) is None


def test_compound_part_never_asks():
    # A compound sub-task runs autonomously inside its parent
    # mission: even a transient semantic failure must not park it
    # (and its parent) in the ask gate — it falls through to legacy
    # dispatch, which fails honestly downstream.
    c = _classify("open gmail", RaisingProvider(), task_id="b-15c")
    assert c.objective.semantic_failed is True
    d = resolve_dispatch(c, task_id="b-15c", allow_ask=False)
    assert d.await_input is False
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


# ---------------------------------------------------------------------------
# Rule 3: deterministic never overrules legacy
# ---------------------------------------------------------------------------


def test_deterministic_monitor_does_not_overrule_compound_browser():
    # "find the cheapest laptop ... and watch its price": the
    # deterministic objective says monitor_product, but the compound
    # find+track policy deliberately routes it to the browser task
    # (search now, offer the watch). Legacy stands.
    c = _classify(
        "find the cheapest laptop under 50000 and watch its price",
        None,
        task_id="b-16",
    )
    assert c.intent == TaskIntent.NEW_BROWSER_TASK
    assert c.objective.primary_intent == "monitor_product"
    assert c.objective.source == "deterministic"
    d = resolve_dispatch(c, task_id="b-16")
    assert d.overridden is False
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_deterministic_search_does_not_overrule_monitoring_legacy():
    # "check every 10 minutes for a price drop ...": the cascade says
    # simple_search, the legacy monitoring directive says watch. The
    # legacy answer is right — dispatch keeps the watch.
    c = _classify(
        "check every 10 minutes for a price drop on the Sony headphones",
        None,
        task_id="b-17",
    )
    assert c.intent == TaskIntent.NON_BROWSER
    assert c.objective.primary_intent == "simple_search"
    d = resolve_dispatch(c, task_id="b-17")
    assert d.overridden is False
    assert d.is_browser is False


def test_how_to_guard_stands_over_deterministic_search():
    # "how do I check my email?" is advice, not a search, even
    # though the deterministic cascade says simple_search.
    c = _classify("how do I check my email?", None, task_id="b-18")
    assert c.intent == TaskIntent.NON_BROWSER
    assert c.objective.primary_intent == "simple_search"
    d = resolve_dispatch(c, task_id="b-18")
    assert d.overridden is False
    assert d.is_browser is False


def test_semantic_monitor_does_not_hijack_compound_find_and_track():
    # >= 2 families (monitor + research) so the model IS consulted;
    # it says monitor_product — but the instruction is a compound
    # find+track ask, so the browser route stands and the find half
    # is not silently dropped.
    c = _classify(
        "find the cheapest MacBook Air and alert me when it drops, and research the best cases for it",
        _sem("monitor_product"),
        task_id="b-19",
    )
    assert c.objective.source == "semantic"
    assert c.objective.primary_intent == "monitor_product"
    d = resolve_dispatch(c, task_id="b-19")
    assert d.overridden is False
    assert d.is_browser is True


# ---------------------------------------------------------------------------
# Rule 4: session / tab intents are never overridden
# ---------------------------------------------------------------------------


def test_continue_browser_task_never_overridden():
    c = _classify(
        "continue",
        _sem("browser_task"),
        task_id="b-20",
        context={"active_session": object()},
    )
    assert c.intent == TaskIntent.CONTINUE_BROWSER_TASK
    d = resolve_dispatch(c, task_id="b-20")
    assert d.overridden is False
    assert d.effective_intent == TaskIntent.CONTINUE_BROWSER_TASK


def test_use_existing_tab_never_overridden():
    c = _classify(
        "use the existing tab",
        _sem("conversational"),
        task_id="b-21",
        context={"active_session": object()},
    )
    assert c.intent == TaskIntent.USE_EXISTING_USER_TAB
    d = resolve_dispatch(c, task_id="b-21")
    assert d.overridden is False
    assert d.await_input is False


# ---------------------------------------------------------------------------
# DISPATCH_DECISION log line + provenance
# ---------------------------------------------------------------------------


def test_dispatch_decision_log_line(caplog):
    c = _classify(
        "what is latest from this", _sem("browser_task"), task_id="b-22"
    )
    d = resolve_dispatch(c, task_id="b-22")
    with caplog.at_level(logging.INFO, logger="app.intent_router"):
        log_dispatch_decision("b-22", d)
    lines = [r.getMessage() for r in caplog.records if "DISPATCH_DECISION" in r.getMessage()]
    assert len(lines) == 1
    assert "task_id=b-22" in lines[0]
    assert "source=semantic" in lines[0]
    assert "objective_type=browser_task" in lines[0]
    assert "legacy_intent=NON_BROWSER" in lines[0]


def test_no_objective_means_legacy_dispatch():
    c = classify_intent(
        "open gmail",
        context={"task_id": "b-23", "extension_connected": False},
        semantic_classifier=None,
    )
    c.objective = None
    d = resolve_dispatch(c, task_id="b-23")
    assert d.source == "legacy"
    assert d.objective_type is None
    assert d.overridden is False
    assert d.effective_intent == TaskIntent.NEW_BROWSER_TASK


def test_objective_source_values():
    det = build_objective("watch the Sony WH-1000XM5 price", context={"task_id": "b-24"})
    assert det.source == "deterministic"
    assert det.semantic_failed is False
    sem = build_objective(
        "what is latest from this",
        context={"task_id": "b-25"},
        semantic_classifier=make_semantic_classifier(
            task_id="b-25",
            provider_factory=lambda: FakeProvider(_sem("browser_task")),
        ),
    )
    assert sem.source == "semantic"
    assert sem.semantic_failed is False
    fb = build_objective(
        "what is latest from this",
        context={"task_id": "b-26"},
        semantic_classifier=make_semantic_classifier(
            task_id="b-26", provider_factory=lambda: RaisingProvider()
        ),
    )
    assert fb.source == "fallback"
    assert fb.semantic_failed is True
    assert fb.primary_intent == "conversational"
    assert "intent_ambiguous" in fb.missing
