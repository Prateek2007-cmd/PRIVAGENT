"""Stream A (round 6) — currency parsing + structured price record.

Issue 1: the old currency regex truncated Indian-grouped prices
(``₹1,29,999`` matched as ``1``), ungrouped 4–5 digit prices
(``₹89999`` matched as ``899``) and European decimals
(``€1.299,99`` matched as ``1.29``). The parser is now
grouping-aware and generic — Indian / Western grouping, ungrouped
digits, dot decimals and comma decimals, four currency symbols plus
ISO codes — with malformed grouping rejected instead of guessed.

Issue 4: the browser observation path now produces one structured
price record (product + variant + currency + availability + current
selling price together, nulls with a reason when genuinely absent)
via :func:`build_price_record`, attached to the research fact as
``price_record`` so the planner sees it in <collected-research>.
Variant prices stay tied to their variant (128GB vs 256GB).
"""
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.extractors.research import (
    build_price_record,
    disambiguate_prices,
    extract_page_research,
    extract_variant_prices,
)
from app.monitor_check import _amount_from_text, _parse_amount, extract_visible

FIXTURE = Path(__file__).parent / "fixtures" / "price_indian_grouping.html"
FIXTURE_HTML = FIXTURE.read_text(encoding="utf-8")
VISIBLE = extract_visible(FIXTURE_HTML)
FIXTURE_TEXT = VISIBLE["text"]
FIXTURE_TITLE = VISIBLE.get("page_title") or "Acme Phone X"


# ---------------------------------------------------------------------------
# Issue 1 — parametrized currency cases (disambiguator, the live path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Price: ₹1,29,999", 129999.0),       # Indian grouping
    ("Price: ₹89999", 89999.0),           # ungrouped
    ("Price: ₹4,199.50", 4199.50),        # decimals
    ("Price: $1,299.99", 1299.99),
    ("Price: €1.299,99", 1299.99),        # European dot-thousands, comma-decimal
    ("Price: €1,299.99", 1299.99),
    ("Price: £999", 999.0),
    ("Price: ₹1,00,000", 100000.0),       # Indian grouping, lakh
    ("Price: ₹99", 99.0),
    ("Price: ₹12,34,567", 1234567.0),     # Indian grouping, 7 digits
    ("Price: ₹1,29,999.50", 129999.50),   # Indian grouping + decimals
    ("Price: Rs. 1,29,999", 129999.0),    # Rs. code
    ("Price: INR 89999", 89999.0),        # INR code, ungrouped
    ("Price: USD 1,299.99", 1299.99),
    ("Price: €12,99", 12.99),             # European decimal comma
    ("Price: €1.299", 1299.0),            # European lone-dot thousands
    ("Price: $1.299", 1.299),             # same digits, $ = decimal
    ("Price: ₹9,999", 9999.0),            # Western grouping still works
    ("Price: ₹1,999", 1999.0),
])
def test_currency_parses_completely(text, expected):
    assert disambiguate_prices(text)["current"] == expected


@pytest.mark.parametrize("text", [
    "Price: ₹1,2,3",       # malformed grouping — rejected, not guessed
    "Price: ₹1,29,99",     # Indian final group must be 3 digits
    "Price: ₹12,345,67",   # mixed grouping
])
def test_malformed_grouping_is_rejected_not_guessed(text):
    assert disambiguate_prices(text)["current"] is None


@pytest.mark.parametrize("text,expected,currency", [
    ("₹1,29,999", 129999.0, "INR"),
    ("€1.299,99", 1299.99, "EUR"),
    ("₹4,199.50", 4199.50, "INR"),
    ("$1,299.99", 1299.99, "USD"),
    ("£999", 999.0, "GBP"),
])
def test_monitor_amount_from_text(text, expected, currency):
    assert _amount_from_text(text) == (expected, currency)


@pytest.mark.parametrize("raw,expected", [
    ("1,29,999", 129999.0),
    ("1.299,99", 1299.99),
    ("4199.00", 4199.0),
    (129999, 129999.0),      # JSON-LD numeric price passes through
    ("1,2,3", None),         # malformed grouping rejected
])
def test_monitor_parse_amount(raw, expected):
    assert _parse_amount(raw) == expected


def test_indian_fixture_labels_all_five_amounts():
    """The controlled fixture's five amounts, Indian-grouped."""
    d = disambiguate_prices(FIXTURE_TEXT)
    assert d["current"] == 129999.0
    assert d["current_source"] == "labelled"
    assert d["original"] == 149999.0
    assert d["discount_amount"] == 20000.0
    assert d["discount_percent"] == 13.0
    assert d["taxes"] == 18000.0
    assert [o["value"] for o in d["other"]] == [99.0, 89999.0]


# ---------------------------------------------------------------------------
# Issue 4 — variant prices stay tied to their variant
# ---------------------------------------------------------------------------

def test_variant_prices_stay_tied():
    pairs = extract_variant_prices(
        "Acme Phone X 128GB ₹1,29,999 256GB ₹1,49,999 in stock")
    assert pairs == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]


def test_variant_prices_exclude_mrp_and_shipping():
    pairs = extract_variant_prices(
        "Acme Phone X MRP ₹1,49,999 128GB ₹1,29,999 Shipping ₹99")
    assert pairs == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
    ]


def test_colour_variant_tied_to_its_price():
    pairs = extract_variant_prices(
        "Acme Phone X Colour: Midnight Black Selling Price ₹1,29,999")
    assert pairs == [
        {"variant": "Midnight Black", "price": 129999.0, "currency": "INR"},
    ]


def test_ambiguous_variants_current_is_null_but_pairs_survive():
    """Two variants, no labelled current: current is honestly null
    (never the first match), but both variant→price pairs survive
    in the record so the planner can still tell them apart."""
    record = build_price_record(
        "https://examplestore.example/p/x", "Acme Phone X",
        "Acme Phone X 128GB ₹1,29,999 256GB ₹1,49,999 in stock")
    assert record["current"] is None
    assert record["reason"] == "ambiguous-multiple-unlabelled"
    assert record["variant"] is None
    assert record["variant_prices"] == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]
    assert record["currency"] == "INR"


def test_variant_tied_to_labelled_current():
    record = build_price_record(
        "https://examplestore.example/p/x", "Acme Phone X",
        "Acme Phone X 128GB Selling Price ₹1,29,999 "
        "256GB ₹1,49,999 in stock")
    assert record["current"] == 129999.0
    assert record["variant"] == "128GB"
    assert record["price_source"] == "labelled"
    assert record["reason"] is None


# ---------------------------------------------------------------------------
# Issue 4 — one structured record, nulls with reasons
# ---------------------------------------------------------------------------

def test_price_record_has_all_fields_together():
    record = build_price_record(
        "https://examplestore.example/p/x", FIXTURE_TITLE, FIXTURE_TEXT,
        source="browser", task_id="task-1")
    # The title cleaner only strips registry-known retailers / domains;
    # "ExampleStore" is neither, so the full fixture title stands.
    assert record["product"] == "Acme Phone X (128GB, Midnight Black) - ExampleStore"
    assert record["currency"] == "INR"
    assert record["current"] == 129999.0
    assert record["original"] == 149999.0
    assert record["discount_amount"] == 20000.0
    assert record["discount_percent"] == 13.0
    assert record["tax"] == 18000.0
    assert record["availability"] == "in_stock"
    assert record["price_source"] == "labelled"
    assert record["reason"] is None
    assert record["variant_prices"] == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]


def test_price_record_nulls_carry_a_reason():
    record = build_price_record(
        "https://examplestore.example/p/x", "Acme Phone X",
        "Acme Phone X\nMRP ₹9,999\nShipping ₹99")
    assert record["current"] is None
    assert record["reason"] == "only-excluded-amounts"
    assert record["original"] == 9999.0
    assert record["currency"] == "INR"
    assert record["product"] == "Acme Phone X"
    assert record["availability"] is None
    assert record["variant"] is None
    assert record["variant_prices"] == []


def test_price_record_no_amounts_reason():
    record = build_price_record(
        "https://examplestore.example/p/x", "Acme Phone X",
        "Acme Phone X — no prices listed yet")
    assert record["current"] is None
    assert record["reason"] == "no-amounts-on-page"
    assert record["currency"] is None


def test_price_record_out_of_stock():
    record = build_price_record(
        "https://examplestore.example/p/x", "Acme Phone X",
        "Acme Phone X Selling Price ₹1,29,999 Currently unavailable")
    assert record["current"] == 129999.0
    assert record["availability"] == "out_of_stock"


def test_price_record_structured_price_wins():
    record = build_price_record(
        "https://examplestore.example/p/x", FIXTURE_TITLE, FIXTURE_TEXT,
        structured_price={"price": 127999.0, "currency": "INR",
                          "source": "json-ld"})
    assert record["current"] == 127999.0
    assert record["price_source"] == "json-ld"
    assert record["currency"] == "INR"


def test_price_record_logs_price_extract(caplog):
    with caplog.at_level(logging.INFO, logger="app.extractors.research"):
        build_price_record(
            "https://examplestore.example/p/x", "Acme Phone X",
            "Acme Phone X Selling Price ₹1,29,999 in stock",
            source="browser", task_id="task-9")
    lines = [r.getMessage() for r in caplog.records
             if "PRICE_EXTRACT" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "source=browser" in line
    assert "task_id=task-9" in line
    assert "product=Acme_Phone_X" in line
    assert "currency=INR" in line
    assert "current=129999.0" in line
    assert "availability=in_stock" in line


def test_research_fact_carries_price_record():
    fact = extract_page_research(
        "https://examplestore.example/p/x", FIXTURE_TITLE, FIXTURE_TEXT)
    assert fact is not None
    assert fact["price"] == "₹129,999"
    assert fact["price_source"] == "labelled"
    record = fact["price_record"]
    assert record["product"] == "Acme Phone X (128GB, Midnight Black) - ExampleStore"
    assert record["current"] == 129999.0
    assert record["currency"] == "INR"
    assert record["availability"] == "in_stock"
    assert fact["variant_prices"] == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]


def test_price_record_logs_even_when_fact_is_none(caplog):
    """MRP-only page: extract_page_research returns None (no current
    price exists), but the record is still built and logged — the
    extraction attempt must be observable, not silent."""
    with caplog.at_level(logging.INFO, logger="app.extractors.research"):
        assert extract_page_research(
            "https://examplestore.example/p/x", "Acme Phone X",
            "Acme Phone X\nMRP ₹9,999\nShipping ₹99") is None
    lines = [r.getMessage() for r in caplog.records
             if "PRICE_EXTRACT" in r.getMessage()]
    assert len(lines) == 1
    assert "current=null" in lines[0]
    assert "original=9999.0" in lines[0]


# ---------------------------------------------------------------------------
# Browser observation → planner path, at the loop level.
#
# agentLoop harvests every observation through extract_page_research
# (agentLoop.py:1225) into working_memory["collected_research"],
# which planner.build_prompt renders as <collected-research>. This
# drives that exact path with a scripted planner and a fake hub,
# feeding the loop the observation dict the extension delivers
# (url / title / page_text) for the controlled fixture page.
# ---------------------------------------------------------------------------

class _FakeHub:
    def __init__(self):
        self.executed: list[dict] = []

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        import asyncio
        await asyncio.sleep(0.001)
        self.executed.append(action)
        return {"ok": True}


class _NavigateThenDone:
    """One navigate step, then empty steps (loop completes)."""
    def __init__(self, url):
        self._url = url
        self._sent = False

    def __call__(self, instruction, penalties, history):
        if not self._sent:
            self._sent = True
            return {"goal": "scripted", "steps": [
                {"why": "open fixture page",
                 "action": {"type": "navigate", "risk": "write",
                            "verify": "navigated",
                            "timeoutMs": 100,
                            "params": {"url": self._url}}}]}
        return {"goal": "scripted", "steps": []}


def test_loop_harvests_price_record_into_planner_memory(caplog):
    import asyncio

    from app import agentLoop

    url = "http://127.0.0.1:8129/price_indian_grouping.html"

    def obs_factory():
        return {"tabId": 1, "url": url, "title": FIXTURE_TITLE,
                "page_text": FIXTURE_TEXT, "interactive": [],
                "scroll": {}, "dpr": 1}

    with caplog.at_level(logging.INFO, logger="app.extractors.research"):
        state = asyncio.run(agentLoop.run_with_injection(
            _FakeHub(), planner=_NavigateThenDone(url),
            obs_factory=obs_factory,
            instruction="Tell me the current selling price of the Acme "
                        "Phone X, which variant it is for, the MRP, and "
                        "whether it is in stock.",
            max_steps=10,
        ))
    assert state["final"] == "completed"
    collected = state["working_memory"].get("collected_research") or {}
    assert collected, "loop harvested nothing into collected_research"
    fact = next(iter(collected.values()))
    assert fact["price"] == "₹129,999"
    record = fact["price_record"]
    assert record["product"] == "Acme Phone X (128GB, Midnight Black) - ExampleStore"
    assert record["current"] == 129999.0
    assert record["original"] == 149999.0
    assert record["currency"] == "INR"
    assert record["availability"] == "in_stock"
    assert record["variant_prices"] == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]
    # And the planner prompt itself carries the record: the fact the
    # loop harvested is exactly what build_prompt serialises.
    from app.planner import build_prompt
    from app.privacy import PrivacyFilteredPayload

    payload = PrivacyFilteredPayload(
        tab_id=1, url=url, title=FIXTURE_TITLE, interactive=[],
        scroll={}, dpr=1.0, elements_original=0, elements_sent=0,
        page_text=FIXTURE_TEXT)
    prompt = build_prompt("price?", payload, [],
                          working_memory=state["working_memory"])
    assert "129999" in prompt
    assert "price_record" in prompt
    assert "256GB" in prompt
    lines = [r.getMessage() for r in caplog.records
             if "PRICE_EXTRACT" in r.getMessage()]
    assert lines, "no PRICE_EXTRACT line from the loop harvest"
    assert "current=129999.0" in lines[0]
    assert "original=149999.0" in lines[0]


# ---------------------------------------------------------------------------
# Controlled fixture served locally — monitor path AND browser
# observation path (the exact functions agentLoop calls with the
# extension's page text) must agree on the same record.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = FIXTURE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_fixture_monitor_path_structured_price(fixture_server):
    """Monitor (HTTP) path: JSON-LD structured price wins."""
    from app.monitor_check import fetch_observation

    obs = fetch_observation(fixture_server + "/")
    assert obs["price"] == 129999.0
    assert obs["price_info"]["source"] == "json-ld"
    assert obs["price_info"]["currency"] == "INR"
    assert obs["product_name"] == "Acme Phone X"


def test_fixture_monitor_path_text_only_indian_grouping(fixture_server):
    """Monitor path with structured metadata stripped: the text
    path must still read ₹1,29,999 completely, not as ₹1."""
    no_ld = re.sub(r'<script type="application/ld\+json">.*?</script>',
                   "", FIXTURE_HTML, flags=re.S)
    no_ld = re.sub(r'<meta property="og:price[^>]*>\n?', "", no_ld)
    visible = extract_visible(no_ld)
    assert visible["price"] == 129999.0
    assert visible["price_info"]["source"] == "price-element"


def test_fixture_browser_path_same_record(fixture_server):
    """Browser observation path: the extension delivers page text;
    agentLoop feeds it to extract_page_research (this is the same
    call, same arguments). The record must match the monitor path."""
    from app.monitor_check import fetch_observation

    obs = fetch_observation(fixture_server + "/")
    text = obs["text"]
    title = obs.get("page_title") or "Acme Phone X"
    record = build_price_record(fixture_server + "/", title, text,
                                source="browser", task_id="browser-1")
    assert record["current"] == 129999.0
    assert record["original"] == 149999.0
    assert record["currency"] == "INR"
    assert record["availability"] == "in_stock"
    assert record["variant_prices"] == [
        {"variant": "128GB", "price": 129999.0, "currency": "INR"},
        {"variant": "256GB", "price": 149999.0, "currency": "INR"},
    ]
    fact = extract_page_research(fixture_server + "/", title, text)
    assert fact is not None
    assert fact["price"] == "₹129,999"
    assert fact["price_record"] == record
