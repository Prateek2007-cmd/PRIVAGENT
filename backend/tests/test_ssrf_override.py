"""Stream 3 (security) — SSRF guard + authorized-host override, probed
2026-11-27 after the v2 wiring.

What was found by probe (see ``security-REPORT.md``):

* Non-canonical IPv4 forms (decimal ``2130706433``, hex ``0x7f000001``,
  octal ``0177.0.0.1``, short ``127.1``) sailed past the IP-literal
  check — ``ipaddress.ip_address`` only accepts canonical dotted quads
  — and were only caught by the DNS-rebinding check, which never runs
  on Linux (``urllib.request.proxy_bypass`` returns False for every
  host except ``no_proxy`` entries, and False for all hosts when no
  proxy is configured). Decimal/hex forms of private, loopback and
  cloud-metadata addresses were ALLOWED pre-fix. The validator now
  normalizes every numeric host with ``socket.inet_aton`` before the
  IP check, and strips a trailing dot before the internal-hostname
  check (``localhost.`` was ALLOWED pre-fix).
* The authorized-host override admitted ANY URL the task named when no
  domain allow-list was configured (``url_allowed`` allows everything
  on an empty list) — including ``169.254.169.254`` and private IPs the
  resolver deliberately rejects. The override now admits nothing
  without a non-empty allow-list.

All probes here are unit-level (validator called in-process) or
against LOCAL servers on 127.0.0.1 that the test itself runs. No probe
ever issues a request to a private IP, a metadata address, or an
external host: a BLOCKED verdict means the validator rejected the URL
without fetching it, and the redirect tests assert the handler raises
before urllib opens the target.
"""
from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agentLoop import _apply_authorized_host_override, _task_authorized_host_url
from app.change_detector import _state
from app.monitor_check import (
    _ValidatingRedirectHandler,
    _validate_monitor_url,
    fetch_observation,
)


# ---------------------------------------------------------------------------
# The validator: canonical private / loopback / metadata literals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://10.0.0.1/",
    "http://10.255.255.255/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://192.168.0.1:8080/admin",
    "http://169.254.169.254/",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.2/",
    "http://127.0.0.1%09@example.com/",  # userinfo hides nothing; still refused
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[64:ff9b::a9fe:a9fe]/",       # NAT64 cloud metadata
    "http://[::ffff:10.0.0.1%25eth0]/",   # mapped private + zone id
    "http://[64:ff9b::a9fe:a9fe%25eth0]/",
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost./",                # trailing dot (was ALLOWED pre-fix)
    "http://foo.localhost/",
    "http://metadata.google.internal/",
    "http://metadata/",
    "http://instance-data/",
    "http://foo.internal/",
    "http://foo.local/",
    "http://10.0.0.1.nip.io/",            # embedded private IP in a hostname
    "http://192.168.1.1.example.com/",
    "ftp://example.com/",
    "file:///etc/passwd",
    "gopher://example.com/",
    "http://user:pass@example.com/",
    "http://example.com@169.254.169.254/",
    "http://%31%32%37.0.0.1/",            # percent-encoded numeric host
    "",
    "http://",
])
def test_validator_blocks_private_and_unsafe(url):
    ok, reason = _validate_monitor_url(url)
    assert not ok, f"{url!r} was ALLOWED ({reason})"


# ---------------------------------------------------------------------------
# The validator: non-canonical IPv4 forms (the pre-fix bypass)
# ---------------------------------------------------------------------------

# Each entry is (url, canonical address it denotes).
NON_CANONICAL_PRIVATE = [
    ("http://167772161/", "10.0.0.1"),          # decimal
    ("http://3232235521/", "192.168.0.1"),      # decimal
    ("http://2852039166/", "169.254.169.254"),  # decimal metadata
    ("http://0x0a000001/", "10.0.0.1"),         # hex
    ("http://0x7f000002/", "127.0.0.2"),        # hex loopback (non-fixture)
    ("http://0xc0.0x00.0x02.0xeb/", "192.0.2.235"),  # hex-dotted, TEST-NET-1
    ("http://0300.0250.0001.0001/", "192.168.1.1"),  # octal
    ("http://0/", "0.0.0.0"),                   # short: unspecified
    ("http://4294967295/", "255.255.255.255"),  # decimal broadcast
]


@pytest.mark.parametrize("url,canonical", NON_CANONICAL_PRIVATE)
def test_validator_blocks_noncanonical_private(url, canonical):
    ok, reason = _validate_monitor_url(url)
    assert not ok, f"{url!r} (={canonical}) was ALLOWED ({reason})"
    assert canonical in (reason or ""), f"reason should name {canonical}: {reason}"


# Forms that denote the 127.0.0.1 fixture address stay allowed — the
# fix normalizes, it does not change the fixture policy. (Pre-fix,
# "127.0.0.1." was BLOCKED only by accident of a regex substring
# match; post-fix it is ALLOWED because it IS the fixture address.)
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:8501/api/v1/products/1",
    "http://127.0.0.1./",        # trailing dot on the fixture
    "http://127.1/",             # short form = 127.0.0.1
    "http://2130706433/",        # decimal = 127.0.0.1
    "http://0x7f000001/",        # hex = 127.0.0.1
    "http://0x7f.0.0.1/",        # hex-dotted = 127.0.0.1
    "http://0x7f.1/",            # short hex = 127.0.0.1
    "http://0177.0.0.1/",        # octal = 127.0.0.1
    "http://127.0.0.01/",        # leading zero = 127.0.0.1
    "http://2130706433./",       # decimal + trailing dot = fixture
    "http://[::ffff:127.0.0.1]/",        # IPv4-mapped fixture
    "http://[0:0:0:0:0:ffff:127.0.0.1]/",  # expanded mapped fixture
    "http://[::ffff:7f00:1]/",           # hex mapped fixture
    "http://[64:ff9b::7f00:1]/",         # NAT64 fixture
    "http://[::ffff:127.0.0.1%25eth0]/",  # mapped fixture + zone id
])
def test_validator_allows_fixture_in_any_spelling(url):
    ok, reason = _validate_monitor_url(url)
    assert ok, f"{url!r} was BLOCKED ({reason})"


# Public hostnames are allowed on the proxied path (the DNS check is
# skipped when a proxy resolves the host). proxy_bypass is forced
# False so the verdict does not depend on the test runner's env.
@pytest.mark.parametrize("url", [
    "https://example.com/",
    "http://example.com:8080/x",
    "https://www.isro.gov.in/",
    "https://www.isro.gov.in/opportunities",
])
def test_validator_allows_public_hosts(url, monkeypatch):
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    ok, reason = _validate_monitor_url(url)
    assert ok, f"{url!r} was BLOCKED ({reason})"


# The DNS-rebinding check itself works when it runs: a hostname
# resolving to a private address is refused, a public one passes, and
# an unresolvable one fails closed. (In production the check only runs
# for no_proxy hosts — see security-REPORT.md — so this is tested with
# a stubbed resolver and a forced-direct bypass.)
def test_dns_rebinding_check_blocks_private_resolution(monkeypatch):
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)

    def _gai_private(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai_private)
    ok, reason = _validate_monitor_url("http://rebind.example/")
    assert not ok and "10.9.9.9" in (reason or "")

    def _gai_public(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _gai_public)
    ok, _ = _validate_monitor_url("http://rebind.example/")
    assert ok

    def _gai_nxdomain(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _gai_nxdomain)
    ok, reason = _validate_monitor_url("http://rebind.example/")
    assert not ok and "does not resolve" in (reason or "")


# ---------------------------------------------------------------------------
# Redirect handler: every hop is re-validated, before any fetch
# ---------------------------------------------------------------------------

def _redirect_verdict(target: str) -> str:
    handler = _ValidatingRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:9/start")
    try:
        out = handler.redirect_request(req, None, 302, "Found", {}, target)
    except urllib.error.HTTPError:
        return "BLOCKED"
    return f"ALLOWED -> {out.full_url}"


@pytest.mark.parametrize("target", [
    "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://167772161/",     # decimal 10.0.0.1 (was ALLOWED pre-fix)
    "http://2852039166/",    # decimal metadata (was ALLOWED pre-fix)
    "http://0x7f000002/",    # hex 127.0.0.2 (was ALLOWED pre-fix)
    "http://localhost./",    # trailing dot (was ALLOWED pre-fix)
])
def test_redirect_handler_blocks_private_targets(target):
    assert _redirect_verdict(target) == "BLOCKED"


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:9/ok",
    "https://www.isro.gov.in/",
])
def test_redirect_handler_allows_safe_targets(target):
    assert _redirect_verdict(target).startswith("ALLOWED")


# ---------------------------------------------------------------------------
# Redirect chains against LOCAL servers (live, fixture only)
# ---------------------------------------------------------------------------

_PAGE = (b"<html><head><title>Local Fixture</title></head><body><main>"
         b"<h1>OK</h1><p>price Rs. 499</p></main></body></html>")
_PII_PAGE = (b"<html><body><main><h1>Headphones</h1><p>Price Rs. 499</p>"
             b"<p>Your code 482910 is ready</p>"
             b"<p>Contact buyer@example.com</p></main></body></html>")


class _FixtureHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def do_GET(self):
        status, headers, body = self.routes.get(
            self.path, (404, {}, b"not found"))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_servers():
    page_srv = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=page_srv.serve_forever, daemon=True).start()
    page_base = f"http://127.0.0.1:{page_srv.server_address[1]}"
    redir_srv = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=redir_srv.serve_forever, daemon=True).start()
    redir_base = f"http://127.0.0.1:{redir_srv.server_address[1]}"
    _FixtureHandler.routes = {
        "/page": (200, {"Content-Type": "text/html"}, _PAGE),
        "/pii": (200, {"Content-Type": "text/html"}, _PII_PAGE),
        "/ok": (302, {"Location": f"{page_base}/page"}, b""),
        "/private": (302, {"Location": "http://10.0.0.1/"}, b""),
        "/decimal": (302, {"Location": "http://167772161/"}, b""),
        "/metadata": (302, {"Location": "http://2852039166/"}, b""),
        "/localhost": (302, {"Location": "http://localhost./"}, b""),
        "/hexloop": (302, {"Location": "http://0x7f000002/"}, b""),
    }
    try:
        yield page_base, redir_base
    finally:
        redir_srv.shutdown()
        page_srv.shutdown()


def test_live_redirect_to_fixture_succeeds(local_servers):
    _, redir_base = local_servers
    obs = fetch_observation(f"{redir_base}/ok", timeout=5)
    assert "Local Fixture" in obs["text"]
    assert "499" in obs["text"]


@pytest.mark.parametrize("path", [
    "/private", "/decimal", "/metadata", "/localhost", "/hexloop",
])
def test_live_redirect_to_private_is_rejected(local_servers, path):
    _, redir_base = local_servers
    with pytest.raises(RuntimeError, match="HTTP 302"):
        fetch_observation(f"{redir_base}{path}", timeout=5)


def test_monitor_path_masks_bare_otp_in_observation_and_state(local_servers):
    """R5-02 parity with the browser path: a bare 6-digit code on a
    monitored page must not survive into the stored baseline."""
    page_base, _ = local_servers
    obs = fetch_observation(f"{page_base}/pii", timeout=5)
    assert "482910" not in obs["text"]
    assert "buyer@example.com" in obs["text"]  # raw fetch text is unredacted
    state = _state(obs)
    assert "482910" not in str(state)
    assert "buyer@example.com" not in str(state)
    assert "499" in str(state)  # the price survives


# ---------------------------------------------------------------------------
# Authorized-host override
# ---------------------------------------------------------------------------

def test_override_admits_allowlisted_sandbox_host():
    url = _task_authorized_host_url(
        "Navigate to http://127.0.0.1:8199/ and search for trains",
        ["127.0.0.1", "localhost"],
    )
    assert url == "http://127.0.0.1:8199/"


def test_override_admits_allowlisted_public_host():
    url = _task_authorized_host_url(
        "See https://www.isro.gov.in/ for opportunities",
        ["isro.gov.in"],
    )
    assert url == "https://www.isro.gov.in/"


def test_override_admits_host_from_parent_instruction():
    url = _task_authorized_host_url(
        "search for trains from Harbour City to Lake Town",
        ["127.0.0.1"],
        parent_instruction="Navigate to http://127.0.0.1:8199/ and search for trains",
    )
    assert url == "http://127.0.0.1:8199/"


def test_override_rejects_host_outside_allow_list():
    assert _task_authorized_host_url(
        "go to http://untrusted.example.com/x", ["127.0.0.1"]
    ) is None
    assert _task_authorized_host_url(
        "Check http://169.254.169.254/latest/meta-data/", ["127.0.0.1"]
    ) is None
    assert _task_authorized_host_url(
        "Check http://10.5.5.5/admin", ["127.0.0.1"]
    ) is None


@pytest.mark.parametrize("allow", [None, []])
def test_override_admits_nothing_without_allow_list(allow):
    """Fail-closed: an unconfigured allow-list is not an authorization.
    Pre-fix, url_allowed() allowed everything on an empty list, so the
    override admitted metadata/private URLs the resolver rejects."""
    assert _task_authorized_host_url(
        "Check http://169.254.169.254/latest/meta-data/ for changes", allow
    ) is None
    assert _task_authorized_host_url(
        "Check http://10.5.5.5/admin for changes", allow
    ) is None
    assert _task_authorized_host_url(
        "See https://www.isro.gov.in/ for opportunities", allow
    ) is None


def test_override_rejects_userinfo_urls():
    assert _task_authorized_host_url(
        "go to http://user:pass@127.0.0.1:8199/", ["127.0.0.1"]
    ) is None


def test_override_reseeds_blocked_discovery():
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
