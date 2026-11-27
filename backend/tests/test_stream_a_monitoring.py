"""Stream A (monitoring + price) — regression tests for the Demo 2
known failures (evidence/08-real-sites.md, DEMO2_REAL_WORLD.md §1–2):

* the 1,751,390-byte ISRO opportunities page failed EVERY check
  (1 MiB hard body cap) — the fetch now streams and truncates;
* new-listings monitoring refused isro.gov.in (six-domain allowlist);
* the price was the first currency-looking number on the page
  (banner/MRP) instead of structured product metadata;
* timestamps/ads/counters re-alerted on unchanged pages.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import db
from app.change_detector import (
    _extract_price,
    _state,
    broadcast_event,
    evaluate_observation,
)
from app.continuous_monitoring_companion import (
    _create_new_listings_watch,
    _extract_tracked_listing,
)
from app.monitor_check import check_monitor_http, extract_visible, fetch_observation
from app.monitoring_scheduler import (
    count_unread_events,
    ensure_stream_c_schema,
    list_unread_events,
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "stream-a.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _insert_mission(db_path, target_url, condition="text_change", params=None,
                    trigger="interval", cron=None, interval=60):
    mission_id = str(uuid.uuid4())
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
               (id, title, target_url, trigger_type, schedule_cron, interval_seconds,
                condition_type, condition_params, status, created_at, next_run_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (mission_id, "Stream A watch", target_url, trigger, cron, interval,
             condition, json.dumps(params or {}), "active", time.time(), time.time() - 1))
    return mission_id


def _events(db_path, mission_id):
    with db.get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at",
            (mission_id,)).fetchall()


def _mission_row(db_path, mission_id):
    with db.get_connection(db_path) as conn:
        return dict(conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone())


class _StreamAHandler(BaseHTTPRequestHandler):
    """Serves mutable HTML + JSON fixture pages."""

    pages: dict[str, tuple[str, str]] = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence stderr noise
        pass

    def do_GET(self):
        body, ctype = self.pages.get(self.path, (b"not found", "text/plain"))
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200 if self.path in self.pages else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


_BIG_MAIN = "<p>Recruitment to the posts of Technician-B, URSC:04:2026.</p>"
_BIG_PAGE = (
    "<html><head><title>Current Opportunities</title></head><body>"
    "<div class=\"cookie-banner\">We use cookies. Accept all.</div>"
    "<div class=\"ad-banner\">Sponsored: Buy watches now! Only ₹999!</div>"
    "<nav>Home | Careers | Contact</nav>"
    "<main><h1>Current Opportunities</h1>"
    "<p>Sorry, No Recruitment right now at Antrix.</p>"
    + _BIG_MAIN * 20000 +
    "</main>"
    "<aside class=\"sidebar-promo\">Special offer ₹499!</aside>"
    "<footer>Copyright 2026</footer></body></html>"
)
# ~1.9 MB of HTML — over the old 1 MiB refusal cap, under the 8 MiB stream cap.
assert len(_BIG_PAGE.encode("utf-8")) > 1_048_576

_JSON_LD_PAGE = (
    "<html><head><title>Amazfit Bip 5</title>"
    "<script type=\"application/ld+json\">"
    "{\"@context\": \"http://schema.org\", \"@type\": \"Product\","
    " \"name\": \"Amazfit Bip 5 Smart Watch\","
    " \"offers\": {\"@type\": \"Offer\", \"price\": \"4199\", \"priceCurrency\": \"INR\"}}"
    "</script></head><body><main>"
    "<div class=\"deal-strip\">Festive deal — headphones only ₹999!</div>"
    "<h1>Amazfit Bip 5 Smart Watch</h1>"
    "<p>MRP ₹8,999. Current price in India is ₹4,199.</p>"
    "</main></body></html>"
)

_SHOPSPHERE_JSON = {
    "ok": True,
    "product": {
        "id": 1, "name": "Amazfit Bip 5", "base_price": 5499,
        "description": "1.91-inch HD display smartwatch.",
        "sellers": [
            {"id": 4, "name": "PrimeDeals", "price": 5059, "stock": 2},
            {"id": 2, "name": "GadgetHub India", "price": 5279, "stock": 9},
            {"id": 1, "name": "TechKart Retail", "price": 5499, "stock": 15},
        ],
        "lowest_price": 5059,
    },
}


@pytest.fixture
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamAHandler)
    _StreamAHandler.pages = {
        "/big": (_BIG_PAGE, "text/html; charset=utf-8"),
        "/tracker": (_JSON_LD_PAGE, "text/html; charset=utf-8"),
        "/api/v1/products/1": (json.dumps(_SHOPSPHERE_JSON), "application/json"),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# --- Fix 2: bounded streaming + main-content extraction ------------

def test_large_page_streams_instead_of_refusing(database, fixture_server):
    """The 1.75 MB ISRO page failed every check under the 1 MiB hard
    cap. A ~1.9 MB fixture page now checks fine: main content is
    extracted, ads/cookie/nav/footer never enter the text."""
    mission = _insert_mission(database, fixture_server + "/big")
    assert check_monitor_http(mission, fixture_server + "/big", db_path=database) is None
    row = _mission_row(database, mission)
    assert row["consecutive_failures"] == 0
    assert row["last_state_json"] is not None
    assert check_monitor_http(mission, fixture_server + "/big", db_path=database) is None
    assert _events(database, mission) == []
    assert row["next_run_at"] is not None and row["next_run_at"] > time.time()


def test_large_page_extraction_ignores_ads_and_boilerplate(fixture_server):
    obs = fetch_observation(fixture_server + "/big")
    assert obs["used_main_content"] is True
    assert "URSC:04:2026" in obs["text"]
    assert "Sorry, No Recruitment right now" in obs["text"]
    assert "We use cookies" not in obs["text"]
    assert "Buy watches now" not in obs["text"]
    assert "Special offer" not in obs["text"]
    assert "Home | Careers" not in obs["text"]
    assert "Copyright 2026" not in obs["text"]
    assert obs["truncated"] is False


def test_oversize_stream_truncates_instead_of_refusing(fixture_server):
    """Past the stream cap the fetch truncates and still observes —
    it never refuses a page for size alone."""
    obs = fetch_observation(fixture_server + "/big", max_bytes=10_000)
    assert obs["truncated"] is True
    assert "Current Opportunities" in obs["text"]


# --- Fix 3: product-specific price extraction ----------------------

def test_json_ld_price_beats_first_currency_match(fixture_server):
    """The tracker page's first currency number is a ₹999 promo
    banner; the validated price is the JSON-LD Offer price ₹4,199
    for the named product — not the banner."""
    obs = fetch_observation(fixture_server + "/tracker")
    assert obs["price"] == 4199.0
    assert obs["price_info"]["source"] == "json-ld"
    assert "Amazfit Bip 5" in (obs.get("product_name") or "")
    assert "₹999" in obs["text"]  # the banner is still visible text…
    assert obs["price"] != 999.0  # …but it is never the price


def test_json_product_api_uses_lowest_seller_price(fixture_server):
    """ShopSphere's JSON product endpoint: the price is the validated
    lowest_price / cheapest seller, the identity is the name field."""
    obs = fetch_observation(fixture_server + "/api/v1/products/1")
    assert obs["price"] == 5059.0
    assert obs["price_info"]["source"] == "json-api"
    assert obs["product_name"] == "Amazfit Bip 5"
    assert "PrimeDeals" in obs["text"]


def test_price_never_first_currency_match_when_amounts_compete():
    """Several competing amounts and no structure/context: the price
    is unknown (None), not the first number on the page."""
    text = "Banner ₹999 · was ₹8,999 · now ₹5,499 · clearance ₹499"
    price, _source = _extract_price({}, text)
    assert price is None


def test_contextual_price_wins_over_banner():
    text = "Banner deal ₹999 only. Selling price ₹4,199 with free delivery."
    price, source = _extract_price({}, text)
    assert price == 4199.0
    assert source == "contextual"


def test_single_unambiguous_amount_still_extracts():
    """A one-line observation with exactly one amount (the old
    browser-path case) still yields its price."""
    price, _source = _extract_price({}, "₹100")
    assert price == 100.0


def test_structured_price_wins_over_text():
    """An observation carrying structured metadata wins even when
    the visible text starts with a different (banner) amount."""
    obs = {"text": "₹999 headphones deal. Amazfit Bip 5 Smart Watch.",
           "price": 4199.0,
           "price_info": {"price": 4199.0, "currency": "INR",
                          "name": "Amazfit Bip 5 Smart Watch", "source": "json-ld"}}
    state = _state(obs)
    assert state["price"] == 4199.0
    assert state["price_source"] == "json-ld"
    assert state["product_name"] == "Amazfit Bip 5 Smart Watch"


def test_shopsphere_below_price_chain_end_to_end(database, fixture_server):
    """Unit mirror of the live ShopSphere chain: baseline ₹5,059
    (no alert under ₹5,000), operator drop to ₹4,799 (one alert),
    repeat tick (no duplicate)."""
    mission = _insert_mission(database, fixture_server + "/api/v1/products/1",
                              condition="below_price", params={"threshold": 5000})
    assert check_monitor_http(mission, fixture_server + "/api/v1/products/1",
                              db_path=database) is None
    assert _mission_row(database, mission)["consecutive_failures"] == 0
    assert _events(database, mission) == []
    dropped = json.loads(json.dumps(_SHOPSPHERE_JSON))
    dropped["product"]["sellers"][0]["price"] = 4799
    dropped["product"]["lowest_price"] = 4799
    _StreamAHandler.pages["/api/v1/products/1"] = (
        json.dumps(dropped), "application/json")
    event = check_monitor_http(mission, fixture_server + "/api/v1/products/1",
                               db_path=database)
    assert event is not None
    assert event["diff_data"]["old_price"] == 5059
    assert event["diff_data"]["new_price"] == 4799
    assert len(_events(database, mission)) == 1
    assert check_monitor_http(mission, fixture_server + "/api/v1/products/1",
                              db_path=database) is None
    assert len(_events(database, mission)) == 1


# --- Fix 1: no hardcoded domain list for new listings -------------

def test_track_new_accepts_isro_url():
    title, url = _extract_tracked_listing(
        "Track new ISRO recruitment openings on "
        "https://www.isro.gov.in/CurrentOpportunities.html "
        "and alert me when a new one appears")
    assert url == "https://www.isro.gov.in/CurrentOpportunities.html"
    assert "ISRO" in title


def test_create_new_listings_watch_accepts_generic_site(database, monkeypatch):
    """isro.gov.in is watchable now: the SSRF guard decides, not a
    six-domain list. The proxy env makes the guard skip the local
    DNS check (this VM's DNS answers every name with CGNAT)."""
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    reply = _create_new_listings_watch(
        "ISRO recruitment openings",
        "https://www.isro.gov.in/CurrentOpportunities.html",
        "chat-A", database)
    assert "New-listings watch active" in reply
    assert "not on the allowed-domains list" not in reply
    with db.get_connection(database) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE target_url LIKE '%isro.gov.in%'"
        ).fetchone()
    assert row is not None
    assert row["condition_type"] == "new_listings"


def test_create_new_listings_watch_still_refuses_private_hosts(database):
    """The SSRF guard stays: a private/internal URL is refused
    honestly instead of creating a watch that can never fire."""
    reply = _create_new_listings_watch(
        "Internal page", "http://169.254.169.254/latest/meta-data/",
        "chat-A", database)
    assert "can't watch" in reply
    with db.get_connection(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM monitoring_missions").fetchone()[0] == 0


# --- Fix 4: false-change suppression --------------------------------

def test_ad_and_timestamp_noise_does_not_alert(database, fixture_server):
    """Only ads/timestamps/counters change on the big page: no alert,
    and the baseline hash does not move."""
    mission = _insert_mission(database, fixture_server + "/big")
    assert check_monitor_http(mission, fixture_server + "/big", db_path=database) is None
    before = _mission_row(database, mission)["last_state_hash"]
    _StreamAHandler.pages["/big"] = (
        _BIG_PAGE.replace("Sponsored: Buy watches now! Only ₹999!",
                          "Sponsored: Buy headphones now! Only ₹799!")
        .replace("Copyright 2026", "Copyright 2026 · Updated 2 minutes ago"),
        "text/html; charset=utf-8")
    assert check_monitor_http(mission, fixture_server + "/big", db_path=database) is None
    assert _events(database, mission) == []
    assert _mission_row(database, mission)["last_state_hash"] == before


def test_real_listing_change_still_alerts(database, fixture_server):
    mission = _insert_mission(database, fixture_server + "/big")
    assert check_monitor_http(mission, fixture_server + "/big", db_path=database) is None
    _StreamAHandler.pages["/big"] = (
        _BIG_PAGE.replace("Sorry, No Recruitment right now at Antrix.",
                          "New opening: URSC:05:2026 Technician-B, last date 10 December 2026."),
        "text/html; charset=utf-8")
    event = check_monitor_http(mission, fixture_server + "/big", db_path=database)
    assert event is not None
    assert "URSC:05:2026" in event["diff_data"]["after"]
    assert len(_events(database, mission)) == 1


def test_volatile_posted_today_and_ad_markers_do_not_alert(database):
    mission = _insert_mission(database, "https://example.org/notes")
    assert evaluate_observation(
        mission, {"text": "Jobs posted today · Sponsored · 5 min read"},
        observed_at=100, db_path=database) is None
    assert evaluate_observation(
        mission, {"text": "Jobs posted yesterday · Advertisement · 9 min read"},
        observed_at=160, db_path=database) is None
    assert _events(database, mission) == []


# --- Notification outbox row for monitor alerts --------------------

def test_broadcast_event_writes_outbox_row_and_pull_dedups(database):
    """A monitor alert gets a durable notification_outbox row (kind
    monitor) AND the unified unread pull lists the alert exactly
    once — the outbox row mirrors the event, it does not double it."""
    mission = _insert_mission(database, "https://example.org/notes")
    event = evaluate_observation(
        mission, {"text": "First version"}, observed_at=100, db_path=database)
    assert event is None
    event = evaluate_observation(
        mission, {"text": "Second version, changed"}, observed_at=160, db_path=database)
    assert event is not None
    ensure_stream_c_schema(database)
    asyncio.run(broadcast_event(event))
    with db.get_connection(database) as conn:
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE kind='monitor'").fetchall()
    assert len(rows) == 1
    assert rows[0]["ref_id"] == event["id"]
    assert rows[0]["title"] == event["title"]
    unread = list_unread_events(unread_only=True, db_path=database)
    assert [e["id"] for e in unread].count(event["id"]) == 1
    assert count_unread_events(db_path=database) == 1
