"""LLM plan normalization: interpret intent, never fail the run on shape.

Models routinely emit `press` to mean "activate this control" and occasionally
overshoot the per-plan step budget. Both are recoverable at the normalization
boundary, long before the schema gate runs.
"""
from __future__ import annotations

import pytest

from app.planner import MAX_STEPS_PER_PLAN, _normalize_raw_plan, _validate_plan


def _plan(action: dict, steps: int = 1) -> dict:
    act = {"verify": "check", "timeoutMs": 5000}
    act.update(action)
    return {
        "goal": "do the thing",
        "steps": [
            {"why": f"step {i}", "action": dict(act)}
            for i in range(steps)
        ],
    }


def test_press_with_target_becomes_click():
    action = {
        "type": "press",
        "risk": "write",
        "target": {"name": "Interstellar main theme thumbnail"},
    }

    norm = _normalize_raw_plan(_plan(action))
    act = norm["steps"][0]["action"]

    assert act["type"] == "click"
    assert act["target"] == {"name": "Interstellar main theme thumbnail"}


def test_press_with_string_target_becomes_click():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "target": "nav-el-3"}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "click"
    assert act["target"] == {"targetId": "nav-el-3"}


def test_press_rewrite_raises_risk_to_write_floor():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "read", "target": {"targetId": "t1"}}))
    assert norm["steps"][0]["action"]["risk"] == "write"


def test_press_without_key_or_target_defaults_to_enter():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write"}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_press_with_null_key_and_no_target_defaults_to_enter():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "params": {"key": None}}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_press_with_blank_key_and_target_becomes_click():
    norm = _normalize_raw_plan(
        _plan({"type": "press", "risk": "write", "target": {"name": "Play"}, "params": {"key": "  "}})
    )
    assert norm["steps"][0]["action"]["type"] == "click"


def test_explicit_key_press_is_untouched():
    norm = _normalize_raw_plan(
        _plan({"type": "press", "risk": "write", "target": {"name": "Search"}, "params": {"key": "Enter"}})
    )
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_empty_target_object_is_not_a_click():
    norm = _normalize_raw_plan(_plan({"type": "press", "risk": "write", "target": {}}))
    act = norm["steps"][0]["action"]

    assert act["type"] == "press"
    assert act["params"]["key"] == "Enter"


def test_oversized_plan_is_clamped_instead_of_rejected():
    raw = _plan({"type": "navigate", "risk": "write", "params": {"url": "https://example.com"}},
                steps=MAX_STEPS_PER_PLAN + 2)

    norm = _normalize_raw_plan(raw)
    assert len(norm["steps"]) == MAX_STEPS_PER_PLAN

    plan = _validate_plan(raw)  # must not raise PLAN_INVALID
    assert len(plan.steps) == MAX_STEPS_PER_PLAN


def test_clamped_plan_keeps_the_leading_steps():
    raw = {
        "goal": "go",
        "steps": [
            {"why": f"step {i}", "action": {
                "type": "navigate", "risk": "write", "verify": "ok", "timeoutMs": 5000,
                "params": {"url": f"https://example.com/{i}"},
            }}
            for i in range(MAX_STEPS_PER_PLAN + 1)
        ],
    }

    plan = _validate_plan(raw)
    urls = [s.action.params.url for s in plan.steps]

    assert urls == [f"https://example.com/{i}" for i in range(MAX_STEPS_PER_PLAN)]


def test_rewritten_click_is_validated_as_a_click():
    """The rewritten action must pass the click target gate, not the press gate."""
    plan = _validate_plan(_plan({"type": "press", "risk": "write", "target": {"targetId": "t1"}}))
    assert plan.steps[0].action.type == "click"


def test_targetless_press_still_validates():
    plan = _validate_plan(_plan({"type": "press", "risk": "write"}))
    assert plan.steps[0].action.params.key == "Enter"


def test_unknown_action_type_still_rejected():
    with pytest.raises(ValueError, match="outside whitelist"):
        _validate_plan(_plan({"type": "run_js", "risk": "read"}))


def test_timeout_zero_sanitized_to_default():
    plan = _validate_plan(_plan({"type": "navigate", "risk": "write", "timeoutMs": 0, "params": {"url": "https://example.com"}}))
    assert plan.steps[0].action.timeout_ms == 10000


def test_click_with_top_level_name_lifted_to_target():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "name": "Search for Products"}))
    assert plan.steps[0].action.target["name"] == "Search for Products"


def test_click_with_target_text_mapped_to_name():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "target": {"text": "Jailer Box Office"}}))
    assert plan.steps[0].action.target["name"] == "Jailer Box Office"


def test_click_with_params_query_fallback():
    plan = _validate_plan(_plan({"type": "click", "risk": "write", "params": {"query": "Interstellar main theme"}}))
    assert plan.steps[0].action.target["name"] == "Interstellar main theme"


def test_type_without_target_defaults_to_active_element():
    plan = _validate_plan(_plan({"type": "type", "risk": "write", "params": {"text": "hello world"}}))
    assert plan.steps[0].action.target["name"] == "activeElement"

