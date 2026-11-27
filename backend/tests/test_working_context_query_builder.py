"""Stream 2 — query builder (CONTRACTS4 §2, Failure E).

Queries are built from RESOLVED entities only. Pronouns, interval
phrases, condition phrases and notification phrases must NEVER leak
into a search query. All prompts below are novel — invented for
these tests, never the user's literal examples.
"""

import pytest

from app.query_builder import (
    build_search_query,
    build_store_search_url,
    parse_condition,
    parse_interval_seconds,
    sanitize_query_text,
)
from app.working_context import WorkingContext, record_entity

_FORBIDDEN_TOKENS = {
    "this", "that", "it", "every", "mins", "minutes", "alert", "notify",
    "tell", "below", "when", "off", "%",
}


def _ctx_two_products():
    ctx = WorkingContext(chat_id="c")
    record_entity(ctx, "product", "Aurora X9 earbuds", {"price": 3499.0})
    record_entity(ctx, "product", "Vertex Pro laptop", {"price": 89999.0})
    return ctx


def _assert_no_leak(query: str):
    tokens = {t.strip(".,;:!?").lower() for t in query.split()}
    leaked = tokens & _FORBIDDEN_TOKENS
    assert not leaked, f"leaked {leaked} into query {query!r}"


# --- sanitiser --------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("watch this for every 5 mins", ""),
    ("monitor this for every twelve minutes", ""),
    ("check this every 5 mins", ""),
    ("watch this product for every 5 mins", ""),
    ("track this, alert me every 5 mins", ""),
    ("watch the aurora x9 earbuds for every 5 mins", "aurora x9 earbuds"),
    ("monitor the vertex pro laptop when it drops 15%", "vertex pro laptop"),
    ("watch the nimbus trail shoes below 4000, alert me", "nimbus trail shoes"),
    ("track the kestrel 4k drone and notify me when it is 10% off", "kestrel 4k drone"),
    ("watch the halcyon keyboard, tell me when the price drops", "halcyon keyboard"),
    ("monitor the aurora x9 earbuds every 2 hours", "aurora x9 earbuds"),
    ("watch the vertex pro laptop for every day", "vertex pro laptop"),
    ("noise cancelling headphones below", "noise cancelling headphones"),
    ("product for", ""),
    ("alert me", ""),
    ("every 5 mins", ""),
    ("this", ""),
    ("the product", ""),
])
def test_sanitize_query_text(raw, expected):
    assert sanitize_query_text(raw) == expected


def test_sanitize_keeps_watch_in_a_product_name():
    # "Watch" as a noun inside a product name is content, not the verb.
    assert sanitize_query_text("watch the aurora watch series 5") == "aurora watch series 5"


# --- build_search_query (resolution + sanitise) ------------------------------

def test_second_one_resolves_before_query_is_built():
    ctx = _ctx_two_products()
    query = build_search_query("watch the price of the second one", ctx)
    assert query == "aurora x9 earbuds"
    _assert_no_leak(query)


def test_this_resolves_to_most_recent_then_sanitises():
    ctx = _ctx_two_products()
    query = build_search_query("watch this for every twelve minutes", ctx)
    assert query == "vertex pro laptop"
    _assert_no_leak(query)


def test_unresolvable_reference_yields_empty_query():
    ctx = WorkingContext(chat_id="c")
    assert build_search_query("watch this for every twelve minutes", ctx) == ""


@pytest.mark.parametrize("instruction", [
    "watch the aurora x9 earbuds for every seven minutes",
    "monitor the vertex pro laptop and alert me when it drops 20%",
    "track the nimbus trail shoes, notify me if the price falls below 3000",
    "watch the kestrel 4k drone every three hours and tell me when it is 15% off",
    "check this for every twelve minutes",
    "watch that one for every 5 mins when it is 10% off",
])
def test_no_leak_property_over_novel_follow_ups(instruction):
    ctx = _ctx_two_products()
    query = build_search_query(instruction, ctx)
    _assert_no_leak(query)
    # A query that names a real product keeps the product words.
    if "aurora" in instruction:
        assert "aurora x9 earbuds" in query
    if "vertex" in instruction:
        assert "vertex pro laptop" in query


def test_explicit_resolved_label_is_used():
    query = build_search_query(
        "watch this for every twelve minutes",
        resolved_label="Nimbus Trail running shoes")
    assert query == "nimbus trail running shoes"


# --- store search URLs ---------------------------------------------------------

def test_store_search_url_uses_resolved_label_only():
    url = build_store_search_url("aurora x9 earbuds")
    assert url == "https://www.flipkart.com/search?q=aurora+x9+earbuds"


def test_store_search_url_refuses_garbage_label():
    assert build_store_search_url("every 5 mins") == ""
    assert build_store_search_url("this for every 5 mins") == ""
    assert build_store_search_url("") == ""


def test_store_search_url_never_raises_on_braces_or_percent():
    # No str.format on any template: braces / % in a label cannot raise.
    url = build_store_search_url("aurora {x9} earbuds 10% off")
    assert url.startswith("https://www.flipkart.com/search?q=")
    assert "aurora" in url


# --- interval / condition parsing ----------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("change it to every twelve minutes", 720),
    ("check every 2 hours", 7200),
    ("watch it every seven minutes", 420),
    ("notify me every day", 86400),
    ("just watch it", None),
])
def test_parse_interval_seconds(text, expected):
    assert parse_interval_seconds(text) == expected


def test_parse_condition_below_price():
    cond = parse_condition("add this condition too, alert me below 40000")
    assert cond == {"kind": "below_price", "value": 40000.0, "operator": "below"}


def test_parse_condition_percent_drop():
    cond = parse_condition("add this condition too, when it is 15% off")
    assert cond == {"kind": "percent_discount", "value": 15.0, "operator": "drop_by"}


def test_parse_condition_none_when_no_condition():
    assert parse_condition("change it to every twelve minutes") is None
    assert parse_condition("cancel that watch") is None
