"""Stream 4 regression tests: form-result reading + authorized-host override.

All fixtures are novel (a fictional "Harbour City -> Lake Town" rail
search and a 127.0.0.1:8199 demo host) — never the operator's literal
prompts or the YatraDemo ground-truth fares.
"""
import asyncio

import pytest

from app import agentLoop
from app.agentLoop import (
    _apply_authorized_host_override,
    _extract_result_summary,
    _is_form_refill_action,
    _record_results_page_as_research_source,
    _results_state_in_text,
    _search_submitted_in_history,
    _task_authorized_host_url,
)

# A fictional results page: two priced result blocks, one unpriced heading.
RESULTS_TEXT = """### Results

Harbour City → Lake Town · 2027-01-15

Coastal Line 204
Harbour Rail · Train · ★ 4.3
07:10 → 12:40
AC Chair Car, Pantry
₹812
41 of 48 seats left
Select seats

Lake Express 118
Harbour Rail · Train · ★ 4.1
09:25 → 15:05
Sleeper, Pantry
₹655
41 of 48 seats left
Select seats"""

FORM_ONLY_TEXT = """Book trains & buses

From
To
Travel date
Search"""


def test_results_state_detects_priced_results():
    assert _results_state_in_text(RESULTS_TEXT) == "results"


def test_results_state_detects_empty_results():
    assert _results_state_in_text("No options found for this route and date. Try another date.") == "empty"


def test_results_state_ignores_form_only_page():
    assert _results_state_in_text(FORM_ONLY_TEXT) is None
    assert _results_state_in_text("") is None


def test_results_state_ignores_lone_price_without_result_wording():
    # A fare on a booking summary is not a results list.
    assert _results_state_in_text("Total payable ₹1,240 (payment disabled in demo)") is None


def test_search_submitted_in_history():
    assert _search_submitted_in_history(
        [{"result_ok": True, "action": {"type": "click", "target": {"name": "Search"}}}]
    ) is True
    assert _search_submitted_in_history(
        [{"result_ok": True, "action": {"type": "type", "params": {"text": "x", "submit": True}}}]
    ) is True
    # Failed submits and result clicks do not count.
    assert _search_submitted_in_history(
        [{"result_ok": False, "action": {"type": "click", "target": {"name": "Search"}}}]
    ) is False
    assert _search_submitted_in_history(
        [{"result_ok": True, "action": {"type": "click", "target": {"name": "Select seats"}}}]
    ) is False
    assert _search_submitted_in_history([]) is False


def test_is_form_refill_action():
    assert _is_form_refill_action({"type": "type", "target": {"name": "From"}}) is True
    assert _is_form_refill_action({"type": "set_date", "target": {"name": "Travel date"}}) is True
    assert _is_form_refill_action({"type": "click", "target": {"name": "Search"}}) is True
    # Acting on a result is never a refill.
    assert _is_form_refill_action({"type": "click", "target": {"name": "Select seats"}}) is False
    assert _is_form_refill_action({"type": "click", "target": {"name": "View details"}}) is False
    assert _is_form_refill_action({"type": "navigate", "params": {"url": "http://x/"}}) is False


def test_extract_result_summary_picks_cheapest_block():
    summary = _extract_result_summary(RESULTS_TEXT)
    assert summary is not None
    assert "Lake Express 118" in summary
    assert "₹655" in summary
    assert "1 other priced option" in summary


def test_extract_result_summary_returns_none_without_prices():
    assert _extract_result_summary(FORM_ONLY_TEXT) is None
    assert _extract_result_summary("") is None


def test_authorized_host_url_from_own_instruction():
    url = _task_authorized_host_url(
        "Navigate to http://127.0.0.1:8199/ and search for trains",
        ["127.0.0.1", "localhost"],
    )
    assert url == "http://127.0.0.1:8199/"


def test_authorized_host_url_from_parent_instruction():
    # A compound part carries no URL of its own; the parent's does.
    url = _task_authorized_host_url(
        "search for trains from Harbour City to Lake Town",
        ["127.0.0.1"],
        parent_instruction="Navigate to http://127.0.0.1:8199/ and search for trains",
    )
    assert url == "http://127.0.0.1:8199/"


def test_authorized_host_url_rejects_host_outside_allow_list():
    assert _task_authorized_host_url(
        "go to http://untrusted.example.com/x", ["127.0.0.1"]
    ) is None
    assert _task_authorized_host_url("search for trains", ["127.0.0.1"]) is None


def test_authorized_host_override_reseeds_blocked_discovery():
    blocked = {
        "scenario": "B",
        "primary_navigation_url": None,
        "discovery_blocked_reason": "No research source is on the domain allow-list",
        "selected_sources": [],
    }
    out = _apply_authorized_host_override(blocked, "http://127.0.0.1:8199/")
    assert out["scenario"] == "A"
    assert out["primary_navigation_url"] == "http://127.0.0.1:8199/"
    assert out["discovery_blocked_reason"] is None
    assert out["selected_sources"][0]["domain"] == "127.0.0.1"


def test_discovery_plus_override_composition():
    """End-to-end shape of the run_task call site, without run_task:
    discovery on a novel sandbox host yields no primary URL, and the
    override reseeds it from the task's own authorized host."""
    from app.website_resolver import website_resolver

    instruction = (
        "On the demo rail page at http://127.0.0.1:8199/, use the search form "
        "to look for trains from Harbour City to Lake Town on 2027-01-15, "
        "then tell me the cheapest option and its price."
    )
    disc = website_resolver.discover_websites_for_task(
        instruction, allowed_domains=["127.0.0.1", "localhost"]
    )
    assert not disc.get("primary_navigation_url")
    host_url = _task_authorized_host_url(instruction, ["127.0.0.1", "localhost"])
    assert host_url == "http://127.0.0.1:8199/"
    out = _apply_authorized_host_override(disc, host_url)
    assert out["primary_navigation_url"] == "http://127.0.0.1:8199/"
    assert out["discovery_blocked_reason"] is None


# ---------------------------------------------------------------------------
# Loop integration: a planner that re-fills the form after submitting it
# must be stopped with the visible results, not loop to the step cap.
# ---------------------------------------------------------------------------

class _FakeHub:
    def __init__(self):
        self.executed: list[str] = []

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        self.executed.append(action["type"])
        return {"ok": True}


def _obs(page_text: str):
    return {
        "tabId": 1,
        "url": "http://127.0.0.1:8199/",
        "title": "Demo Rail",
        "interactive": [],
        "scroll": {},
        "dpr": 1,
        "pageText": page_text,
    }


def test_record_results_page_registers_source():
    # The results page the loop just read becomes the verifier-visible
    # research source, with the collector's own reliability scoring.
    mem: dict = {}
    ok = _record_results_page_as_research_source(
        mem,
        "s4-pkg-test",
        "search for trains from Harbour City to Lake Town and tell me the cheapest option",
        "http://127.0.0.1:8199/",
        "Demo Rail",
        RESULTS_TEXT,
    )
    assert ok is True
    pkg = mem.get("research_knowledge_package") or {}
    sources = pkg.get("sources") or {}
    assert sources, pkg
    src = next(iter(sources.values()))
    assert src.get("url") == "http://127.0.0.1:8199/"
    assert (src.get("reliability_score") or 0) > 0
    from app.research.collector import is_search_results_page

    assert not is_search_results_page(src.get("url") or "")


def test_record_results_page_refuses_search_results_page():
    # A real search-index page is never registered as a substantive source.
    mem: dict = {}
    ok = _record_results_page_as_research_source(
        mem,
        "s4-pkg-test",
        "search for trains and tell me the cheapest option",
        "https://example-search.test/search?q=trains",
        "Search results for trains",
        "Search results for trains\nshowing 1-10 of 42\nRefine your search",
    )
    assert ok is False
    pkg = mem.get("research_knowledge_package") or {}
    assert not (pkg.get("sources") or {})


def test_record_results_page_never_raises():
    mem: dict = {}
    assert _record_results_page_as_research_source(mem, "s4-pkg-test", "x", "", "", "") is False
    assert _record_results_page_as_research_source(mem, "s4-pkg-test", "", "", "", "") is False


def test_seeded_package_passes_research_verifier():
    # Live failure (2026-11-27): the loop returned the correct cheapest
    # summary, but the verifier classifies "search for ... tell me the
    # cheapest" as RESEARCH and failed it for having no package source.
    # With the results page recorded, the same answer verifies.
    from app.completion_verifier import verify_task_completion

    instruction = (
        "search for trains from Harbour City to Lake Town on 2027-01-15, "
        "then tell me the cheapest option and its price."
    )
    summary = _extract_result_summary(RESULTS_TEXT) or ""
    assert "Lake Express 118" in summary
    mem: dict = {
        "autonomous_discovery": {"scenario": "A", "specified_website": "127.0.0.1"},
        "collected_research": {
            "127.0.0.1:8199": {"domain": "127.0.0.1:8199", "url": "http://127.0.0.1:8199/", "price": "₹655"}
        },
    }
    failed = verify_task_completion(instruction, "completed", summary, working_memory=mem)
    assert failed.state == "failed"
    assert "No substantive research sources" in (failed.message or "")
    assert _record_results_page_as_research_source(
        mem, "s4-pkg-test", instruction, "http://127.0.0.1:8199/", "Demo Rail", RESULTS_TEXT
    )
    passed = verify_task_completion(instruction, "completed", summary, working_memory=mem)
    assert passed.state == "completed", passed


def test_loop_reads_results_instead_of_refilling_form():
    hub = _FakeHub()
    state = {"calls": 0}
    mem: dict = {}

    def obs_factory():
        # Results appear once the Search click has executed.
        return _obs(RESULTS_TEXT if "click" in hub.executed else FORM_ONLY_TEXT)

    def planner(instruction, payload, history):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"goal": "search", "steps": [{"why": "submit the search form", "action": {
                "type": "click", "risk": "write", "verify": "clicked Search",
                "timeoutMs": 10000, "target": {"targetId": "search-btn", "name": "Search"}}}]}
        # The buggy behaviour: re-type the From field forever.
        return {"goal": "search", "steps": [{"why": "fill the From field again", "action": {
            "type": "type", "risk": "write", "verify": "typed From",
            "timeoutMs": 10000, "target": {"targetId": "from-input", "name": "From"},
            "params": {"text": "Harbour City", "submit": False}}}]}

    result = asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=planner,
        obs_factory=obs_factory,
        task_id="s4-form-loop-test",
        instruction=(
            "On the demo rail page, search for trains from Harbour City to Lake Town "
            "on 2027-01-15 and tell me the cheapest option and its price."
        ),
        max_steps=50,
        initial_memory=mem,
    ))
    assert result["final"] == "completed", result
    assert "Lake Express 118" in (result.get("summary") or "")
    assert "₹655" in (result.get("summary") or "")
    # Only the Search click ever executed — the refills were intercepted.
    assert hub.executed == ["click"]
    assert result.get("steps") == 1
    # The results page is recorded as the research source behind the
    # answer, so the completion verifier's RESEARCH rule can see it.
    pkg = mem.get("research_knowledge_package") or {}
    sources = pkg.get("sources") or {}
    assert sources, mem
    assert any(
        (s.get("url") or "") == "http://127.0.0.1:8199/"
        and (s.get("reliability_score") or 0) > 0
        for s in sources.values()
    )
