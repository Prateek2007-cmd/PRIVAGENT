"""Stream 2 (price + form safety): price disambiguation and loop-guard behaviour.

Price: a product page shows five different currency amounts at once
(current / MRP-original / discount / taxes / shipping-or-other). The
fixture (fixtures/price_disambiguation.html) puts all five on one
page, MRP first in DOM order, and asserts the extractor returns the
current price and labels the other four correctly — never the first
currency match.

Loop guard: driven through the real loop (agentLoop.run_with_injection)
with a scripted planner and a fake extension hub. Legitimate repeat
fills (distinct passenger fields) must execute; a true infinite loop
(same action forever) must still be stopped. The refinement case —
a new search after a first search — is a KNOWN BUG in the production
guard (agentLoop.py form-results interceptor, see the xfail below)
and is documented, not hidden.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app import agentLoop
from app.change_detector import _extract_price, _state
from app.extractors.research import disambiguate_prices, extract_page_research

FIXTURE = Path(__file__).parent / "fixtures" / "price_disambiguation.html"
FIXTURE_HTML = FIXTURE.read_text(encoding="utf-8")

# The flattened visible text the monitor / research paths actually see.
# Derived through the production HTML parser (monitor_check, read-only
# use here) so the fixture text is what the loop would observe, not a
# hand-typed approximation.
from app.monitor_check import extract_visible  # noqa: E402

VISIBLE = extract_visible(FIXTURE_HTML)
FIXTURE_TEXT = VISIBLE["text"]
FIXTURE_TITLE = VISIBLE.get("page_title") or "Acme Wireless Headphones X200"


# ---------------------------------------------------------------------------
# Price disambiguation
# ---------------------------------------------------------------------------

def test_fixture_has_all_five_amounts():
    """Guard the fixture itself: all five amounts must be present."""
    for amount in ("₹9,999", "₹4,199", "₹5,800", "₹756", "₹99", "₹2,499"):
        assert amount in FIXTURE_TEXT, f"fixture lost {amount}: {FIXTURE_TEXT!r}"


def test_disambiguate_labels_all_five_amounts():
    d = disambiguate_prices(FIXTURE_TEXT)
    assert d["current"] == 4199.0
    assert d["current_source"] == "labelled"
    assert d["original"] == 9999.0
    assert d["discount_amount"] == 5800.0
    assert d["discount_percent"] == 50.0
    assert d["taxes"] == 756.0
    assert [o["value"] for o in d["other"]] == [99.0, 2499.0]


def test_extractor_returns_current_price_not_mrp():
    fact = extract_page_research(
        "https://examplestore.example/p/acme-x200", FIXTURE_TITLE, FIXTURE_TEXT)
    assert fact is not None
    assert fact["price"] == "₹4,199"
    assert fact["price_source"] == "labelled"
    prices = fact["prices"]
    assert prices["current"] == 4199.0
    assert prices["original"] == 9999.0
    assert prices["discount_amount"] == 5800.0
    assert prices["taxes"] == 756.0


def test_extractor_structured_price_wins():
    """Structured metadata (JSON-LD / microdata / meta) beats text."""
    fact = extract_page_research(
        "https://examplestore.example/p/acme-x200", FIXTURE_TITLE, FIXTURE_TEXT,
        structured_price={"price": 4099.0, "currency": "INR", "source": "json-ld"})
    assert fact is not None
    assert fact["price"] == "₹4,099"
    assert fact["price_source"] == "json-ld"


def test_extractor_no_current_price_returns_none():
    """Only an MRP and a shipping amount: no current price exists,
    so the extractor must return None, not quote the MRP."""
    assert extract_page_research(
        "https://examplestore.example/p/acme-x200", "Acme X200",
        "Acme X200\nMRP ₹9,999\nShipping ₹99") is None


def test_change_detector_fixture_price_is_current():
    price, source = _extract_price({}, FIXTURE_TEXT)
    assert price == 4199.0 and source == "contextual"
    state = _state({"text": FIXTURE_TEXT, "elements": []})
    assert state["price"] == 4199.0


def test_change_detector_mrp_first_never_wins():
    price, _ = _extract_price({}, "MRP ₹9,999\nSelling Price ₹4,199")
    assert price == 4199.0
    price, _ = _extract_price({}, "Price: ₹4,199 (MRP ₹9,999)")
    assert price == 4199.0
    price, _ = _extract_price({}, "Was ₹9,999, now ₹4,199")
    assert price == 4199.0


def test_change_detector_exclusion_amounts_are_not_prices():
    """A lone MRP / discount / tax / shipping amount is not a price."""
    assert _extract_price({}, "MRP ₹9,999") == (None, None)
    assert _extract_price({}, "Upto ₹1,200 Off on Exchange") == (None, None)
    assert _extract_price({}, "Taxes ₹756 included") == (None, None)
    assert _extract_price({}, "Shipping ₹99") == (None, None)


def test_monitor_structured_path_jsonld_wins():
    """Read-only verification of the structured path (monitor_check,
    Stream 3's file — not changed here): JSON-LD beats body text."""
    assert VISIBLE.get("price") == 4199.0
    assert VISIBLE.get("price_info", {}).get("source") == "json-ld"


def test_monitor_structured_path_class_prices_cheapest():
    """Without JSON-LD and without og:price meta, the price-classed
    elements still yield the selling price (cheapest class price),
    not the MRP."""
    no_ld = re.sub(r'<script type="application/ld\+json">.*?</script>',
                   "", FIXTURE_HTML, flags=re.S)
    no_ld = re.sub(r'<meta property="og:price[^>]*>\n?', "", no_ld)
    visible = extract_visible(no_ld)
    assert visible.get("price") == 4199.0
    assert visible.get("price_info", {}).get("source") == "price-element"


# ---------------------------------------------------------------------------
# Loop guard (through the real loop with a scripted planner + fake hub)
# ---------------------------------------------------------------------------

class _FakeHub:
    """Minimal extension stand-in: records executed actions, always ok."""
    def __init__(self):
        self.executed: list[dict] = []

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        await asyncio.sleep(0.001)
        self.executed.append(action)
        return {"ok": True}


def _obs(url="http://127.0.0.1:8124/", text="", title="YatraDemo"):
    return {"tabId": 1, "url": url, "title": title, "page_text": text,
            "interactive": [], "scroll": {}, "dpr": 1}


def _type_step(text, target_id, name, submit=False):
    return {"why": f"type {name}",
            "action": {"type": "type", "risk": "write", "verify": "v",
                       "timeoutMs": 100,
                       "target": {"targetId": target_id, "name": name},
                       "params": {"text": text, "submit": submit}}}


class _ScriptedPlanner:
    """Emits a queued list of steps, then empty steps (loop completes)."""
    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    def __call__(self, instruction, penalties, history):
        self.calls += 1
        if self._steps:
            return {"goal": "scripted", "steps": [self._steps.pop(0)]}
        return {"goal": "scripted", "steps": []}


def test_legit_distinct_passenger_fills_execute():
    """Two passengers x name/age/gender: six distinct fills must all
    execute — the loop guard must not treat repeat form filling as a
    loop just because the action type repeats."""
    hub = _FakeHub()
    fills = [
        _type_step("Asha Verma", "p1-name", "Passenger 1 Name"),
        _type_step("28", "p1-age", "Passenger 1 Age"),
        _type_step("Female", "p1-gender", "Passenger 1 Gender"),
        _type_step("Rohan Verma", "p2-name", "Passenger 2 Name"),
        _type_step("31", "p2-age", "Passenger 2 Age"),
        _type_step("Male", "p2-gender", "Passenger 2 Gender"),
    ]
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_ScriptedPlanner(fills),
        obs_factory=lambda: _obs(text="Passenger details form"),
        instruction="Fill passenger details for 2 passengers",
        max_steps=20,
    ))
    assert state["final"] == "completed"
    assert len(hub.executed) == 6
    assert "Loop detected" not in (state.get("summary") or "")


def test_true_infinite_loop_still_stopped():
    """Same click on the same target forever: the guard must stop
    the task instead of burning the whole step budget."""
    hub = _FakeHub()

    class _Looper:
        def __call__(self, instruction, penalties, history):
            return {"goal": "loop", "steps": [
                {"why": "again",
                 "action": {"type": "click", "risk": "write", "verify": "v",
                            "timeoutMs": 100,
                            "target": {"targetId": "b1", "name": "Close"}}}]}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_Looper(),
        obs_factory=lambda: _obs(text="Popup"),
        instruction="close the popup",
        max_steps=12,
    ))
    assert state["final"] == "budget_exceeded"
    assert "Loop detected" in (state.get("summary") or "")
    assert len(hub.executed) <= 4


def test_refinement_search_after_first_search_executes():
    """Search HYD->BLR 2026-12-05, see results, then refine to
    2026-12-06 and re-search: the refinement must EXECUTE."""
    hub = _FakeHub()
    form_text = "Search buses From To Date Search"
    results_text = ("Search results: 3 options. KSRTC ₹850, VRL ₹920, "
                    "SRS ₹780. Fares include taxes.")

    def obs_factory():
        # Results appear once the first search was submitted.
        submitted = any(
            a.get("type") == "type" and (a.get("params") or {}).get("submit")
            for a in hub.executed)
        return _obs(text=results_text if submitted else form_text)

    steps = [
        _type_step("Hyderabad", "from", "From"),
        _type_step("Bengaluru", "to", "To"),
        _type_step("2026-12-05", "date", "Date", submit=True),
        # Refinement: a DIFFERENT date, then click Search again.
        _type_step("2026-12-06", "date", "Date"),
        {"why": "re-search with new date",
         "action": {"type": "click", "risk": "write", "verify": "v",
                    "timeoutMs": 100,
                    "target": {"targetId": "search-btn", "name": "Search"}}},
    ]
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_ScriptedPlanner(steps),
        obs_factory=obs_factory,
        instruction="Search buses Hyderabad to Bengaluru, then change "
                    "the date to 2026-12-06 and search again",
        max_steps=20,
    ))
    typed_texts = [(a.get("params") or {}).get("text")
                   for a in hub.executed if a.get("type") == "type"]
    assert "2026-12-06" in typed_texts, (
        f"refinement re-type never executed; executed={hub.executed} "
        f"final={state['final']} summary={state.get('summary')!r}")
    assert any(a.get("type") == "click" for a in hub.executed), (
        f"re-search click never executed; final={state['final']}")
