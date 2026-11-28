"""Stream 1 (CONTRACTS5): semantic-wiring tests.

The production path is: agentLoop builds a provider-backed
classifier (``make_semantic_classifier``) per task and passes it
into ``classify_intent`` -> ``build_objective``. These tests mock
the PROVIDER at the boundary (a fake provider factory), never the
classifier itself — the real ``make_semantic_classifier`` and the
real ``classify_semantic`` run in every test.

Prompts here are novel wordings; the Failure A SHAPE (product
discount watch + interval + remind) is what is pinned, not the
user's literal example.
"""
import json
import logging
import time

import pytest

from app.intent_router import classify_intent, should_route_email_interval
from app.objective import build_objective
from app.semantic_classifier import (
    MockProviderRefused,
    clear_cache,
    is_mock_provider,
    make_semantic_classifier,
)
from app.providers.mock import MockProvider


@pytest.fixture(autouse=True)
def _clear_semantic_cache():
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# Fake provider boundary: a real provider-shaped object whose chat_call
# returns canned JSON. The classifier under test is always real.
# ---------------------------------------------------------------------------

class FakeProvider:
    """OpenAI-compatible-shaped fake: name() carries the model, and
    chat_call records prompts and returns one canned JSON reply."""

    def __init__(self, reply: dict | str, model: str = "deepseek-v4.1-flash"):
        self._reply = reply
        self._model = model
        self.calls: list[str] = []

    def name(self) -> str:
        return f"freepi:{self._model}"

    def chat_call(self, prompt, history=None, system_prompt=None) -> str:
        self.calls.append(prompt)
        if isinstance(self._reply, str):
            return self._reply
        return json.dumps(self._reply)


def _monitor_reply() -> dict:
    # NOTE: secondary_intents must be canonical INTENTS members —
    # "notify" is a secondary flag inside build_objective, not an
    # intent, so a model reply naming it is rejected by
    # classify_semantic's schema validation.
    return {
        "primary_intent": "monitor_product",
        "secondary_intents": [],
        "entities": {"product": "Sony WH-1000XM5 headphones", "brand": "Sony"},
        "references": [],
        "interval_seconds": 420,
        "condition": {"kind": "percent_discount", "value": 15.0, "operator": ">="},
        "target_url": None,
        "missing": [],
    }


def _classifier_for(provider, task_id="t-test"):
    return make_semantic_classifier(
        task_id=task_id, provider_factory=lambda: provider
    )


FAILURE_A_SHAPE = (
    "live monitor iphone 17 when it 10% off then remind, "
    "check every 10 minutes"
)
COMPOUND_MONITOR_SCHEDULE = (
    "monitor the price of the Sony WH-1000XM5 headphones "
    "and schedule a summary every day"
)
AMBIGUOUS_PRONOUN = "hmm, what about that thing"
AMBIGUOUS_PRONOUN_2 = "what about that one"


# ---------------------------------------------------------------------------
# 1. Production path: the classifier is passed in, and the provider
#    boundary (not the classifier) is what is mocked.
# ---------------------------------------------------------------------------

def test_production_path_passes_classifier_via_classify_intent():
    provider = FakeProvider(_monitor_reply())
    cls = classify_intent(
        AMBIGUOUS_PRONOUN,
        semantic_classifier=_classifier_for(provider, task_id="t-prod"),
    )
    assert cls.objective is not None
    assert cls.objective.primary_intent == "monitor_product"
    assert cls.objective.entities.get("product") == "Sony WH-1000XM5 headphones"
    assert cls.objective.interval_seconds == 420
    # Exactly one model call: the semantic prompt went to the provider.
    assert len(provider.calls) == 1
    assert "Instruction: " in provider.calls[0]


def test_classifier_resolves_provider_via_agentloop_factory(monkeypatch):
    """make_semantic_classifier with no factory resolves the provider
    through agentLoop._provider_for_config (the production wiring)."""
    import app.agentLoop as agent_loop

    provider = FakeProvider(_monitor_reply())
    monkeypatch.setattr(agent_loop, "_provider_for_config", lambda **kw: provider)
    clf = make_semantic_classifier(task_id="t-factory")
    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj.primary_intent == "monitor_product"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# 2. Precedence: ambiguous/compound -> semantic; unambiguous -> never.
# ---------------------------------------------------------------------------

def test_ambiguous_input_triggers_callable():
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_monitor_reply())

    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=spy)
    assert len(calls) == 1
    assert obj.primary_intent == "monitor_product"


def test_ambiguous_pronoun_variants_trigger_callable():
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_monitor_reply())

    for prompt in (AMBIGUOUS_PRONOUN_2, "do that thing again"):
        calls.clear()
        obj = build_objective(prompt, semantic_classifier=spy)
        assert len(calls) == 1, prompt
        assert obj.primary_intent == "monitor_product"


def test_unambiguous_input_never_triggers_callable():
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_monitor_reply())

    for prompt in (
        "search for Fairphone",
        "hi",
        "check my email every 15 minutes",
        "watch the price of the Bose QC45 headphones and check every 20 minutes",
    ):
        calls.clear()
        obj = build_objective(prompt, semantic_classifier=spy)
        assert calls == [], prompt
        assert "intent_ambiguous" not in obj.missing, prompt


def test_failure_a_shape_is_deterministic_monitor_not_semantic():
    """Failure A shape: monitor verb + product + discount + remind +
    interval is ONE deterministic rule (the remind is the monitor's
    own notification secondary), so it never spends a model call —
    and never routes to email polling."""
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_monitor_reply())

    obj = build_objective(FAILURE_A_SHAPE, semantic_classifier=spy)
    assert calls == []
    assert obj.primary_intent == "monitor_product"
    assert obj.interval_seconds == 600
    assert obj.condition == {"kind": "percent_discount", "value": 10.0, "operator": ">="}
    assert should_route_email_interval(obj, FAILURE_A_SHAPE, True) is False
    assert should_route_email_interval(obj, FAILURE_A_SHAPE, False) is False


def test_compound_monitor_plus_schedule_triggers_semantic():
    """A monitor PLUS a real schedule (non-notification schedule verb
    + its own cadence) is TWO deterministic rules -> semantic."""
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(_monitor_reply())

    obj = build_objective(COMPOUND_MONITOR_SCHEDULE, semantic_classifier=spy)
    assert len(calls) == 1
    assert obj.primary_intent == "monitor_product"


def test_compound_search_plus_email_triggers_semantic():
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({
            "primary_intent": "simple_search",
            "secondary_intents": [],
            "entities": {"query": "Fairphone"},
            "references": [],
            "interval_seconds": None,
            "condition": None,
            "target_url": None,
            "missing": [],
        })

    obj = build_objective(
        "search for Fairphone and check my email every 15 minutes",
        semantic_classifier=spy,
    )
    assert len(calls) == 1
    assert obj.primary_intent == "simple_search"


# ---------------------------------------------------------------------------
# 3. Invalid model output -> honest fallback, never a guess.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_reply",
    [
        "not json at all",
        json.dumps({"primary_intent": "teleport_the_phone", "entities": {}}),
        json.dumps({"primary_intent": "monitor_product", "secondary_intents": ["teleport"]}),
        "",
    ],
)
def test_invalid_model_json_falls_back_honestly(bad_reply):
    def bad(prompt: str) -> str:
        return bad_reply

    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=bad)
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing


def test_raising_classifier_falls_back_honestly():
    def raises(prompt: str) -> str:
        raise RuntimeError("provider down")

    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=raises)
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing


def test_no_classifier_falls_back_honestly():
    obj = build_objective(AMBIGUOUS_PRONOUN)
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing
    obj2 = build_objective(COMPOUND_MONITOR_SCHEDULE)
    assert obj2.primary_intent == "conversational"
    assert "intent_ambiguous" in obj2.missing


# ---------------------------------------------------------------------------
# 4. Mock provider -> flagged/refused, never presented as real.
# ---------------------------------------------------------------------------

def test_mock_provider_is_detected():
    assert is_mock_provider(MockProvider()) is True
    assert is_mock_provider(FakeProvider(_monitor_reply())) is False


def test_mock_provider_refused_before_any_call():
    calls: list[str] = []

    class CountingMock(MockProvider):
        def chat_call(self, prompt, history=None, system_prompt=None) -> str:
            calls.append(prompt)
            return super().chat_call(prompt, history, system_prompt)

    clf = make_semantic_classifier(
        task_id="t-mock", provider_factory=lambda: CountingMock()
    )
    with pytest.raises(MockProviderRefused):
        clf("You classify a user's instruction for a browser agent. Instruction: hi")
    assert calls == [], "the mock's chat_call must never run for classification"


def test_mock_provider_in_build_objective_falls_back_honestly():
    clf = make_semantic_classifier(
        task_id="t-mock2", provider_factory=lambda: MockProvider()
    )
    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing


# ---------------------------------------------------------------------------
# 5. One model call per ambiguous turn (per-turn cache); timeout bound.
# ---------------------------------------------------------------------------

def test_one_model_call_per_ambiguous_turn():
    provider = FakeProvider(_monitor_reply())
    clf = _classifier_for(provider, task_id="t-cache")
    obj1 = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    obj2 = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj1.primary_intent == "monitor_product"
    assert obj2.primary_intent == "monitor_product"
    assert len(provider.calls) == 1, "second resolve must hit the per-turn cache"


def test_cache_is_per_task():
    provider = FakeProvider(_monitor_reply())
    clf1 = _classifier_for(provider, task_id="t-a")
    clf2 = _classifier_for(provider, task_id="t-b")
    build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf1)
    build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf2)
    assert len(provider.calls) == 2


def test_classifier_call_is_timeout_bounded():
    class SlowProvider:
        def name(self) -> str:
            return "slow:never"

        def chat_call(self, prompt, history=None, system_prompt=None) -> str:
            time.sleep(5)
            return json.dumps(_monitor_reply())

    clf = make_semantic_classifier(
        task_id="t-slow", provider_factory=lambda: SlowProvider(), timeout_s=0.2
    )
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        clf("prompt")
    assert time.monotonic() - t0 < 3, "the call must fail fast on timeout, not hang"


def test_rate_limit_retries_then_succeeds(monkeypatch):
    """A transient 429 (the shared one-at-a-time gateway) is retried
    a bounded number of times; the turn still gets one classification."""
    monkeypatch.setattr(
        "app.semantic_classifier._RETRY_BACKOFF_S", (0.01, 0.01)
    )

    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        def name(self) -> str:
            return "freepi:deepseek-v4.1-flash"

        def chat_call(self, prompt, history=None, system_prompt=None) -> str:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError(
                    "Conversational call failed: HTTP 429 (Rate Limit / "
                    "Quota Exceeded) — one request at a time."
                )
            return json.dumps(_monitor_reply())

    provider = FlakyProvider()
    clf = _classifier_for(provider, task_id="t-retry")
    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj.primary_intent == "monitor_product"
    assert provider.calls == 3


def test_non_rate_limit_error_does_not_retry():
    class FailingProvider:
        def __init__(self):
            self.calls = 0

        def name(self) -> str:
            return "broken:never"

        def chat_call(self, prompt, history=None, system_prompt=None) -> str:
            self.calls += 1
            raise RuntimeError("connection refused")

    provider = FailingProvider()
    clf = _classifier_for(provider, task_id="t-noretry")
    obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj.primary_intent == "conversational"
    assert "intent_ambiguous" in obj.missing
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# 6. Logging: SEMANTIC_CLASSIFY + DETERMINISTIC_ROUTE line formats.
# ---------------------------------------------------------------------------

def test_semantic_invocation_logs_semantic_classify(caplog):
    provider = FakeProvider(_monitor_reply())
    clf = _classifier_for(provider, task_id="t-log-sem")
    with caplog.at_level(logging.INFO, logger="app.semantic_classifier"):
        obj = build_objective(AMBIGUOUS_PRONOUN, semantic_classifier=clf)
    assert obj.primary_intent == "monitor_product"
    lines = [r.getMessage() for r in caplog.records if "SEMANTIC_CLASSIFY" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "task_id=t-log-sem" in line
    assert "model=deepseek-v4.1-flash" in line
    assert "latency_ms=" in line
    assert "result=monitor_product" in line


def test_deterministic_hit_logs_deterministic_route(caplog):
    with caplog.at_level(logging.INFO, logger="app.objective"):
        obj = build_objective("hi", context={"task_id": "t-log-det"})
    assert obj.primary_intent == "greet"
    lines = [r.getMessage() for r in caplog.records if "DETERMINISTIC_ROUTE" in r.getMessage()]
    assert len(lines) == 1
    assert "task_id=t-log-det" in lines[0]
    assert "rule=greet" in lines[0]


def test_unambiguous_never_logs_semantic_classify(caplog):
    provider = FakeProvider(_monitor_reply())
    clf = _classifier_for(provider, task_id="t-log-none")
    with caplog.at_level(logging.INFO, logger="app.semantic_classifier"):
        build_objective("search for Fairphone", semantic_classifier=clf)
    assert not [r for r in caplog.records if "SEMANTIC_CLASSIFY" in r.getMessage()]
    assert provider.calls == []
