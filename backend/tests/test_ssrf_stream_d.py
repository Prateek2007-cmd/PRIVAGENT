"""Stream D (round 6) — network security across execution environments.

Probes the SSRF boundary in every place the backend reaches the network:

* ``env=backend`` — :func:`app.monitor_check._validate_monitor_url` (the
  HTTP monitor/fetch path) plus live redirect chains against local
  servers. Round 3 fixed this validator (``_canonical_ipv4``,
  trailing-dot strip, percent-encoded rejection, fail-closed
  override); this file re-probes the whole matrix to prove no
  regression, including the redirect chains (d).
* ``env=browser`` — :func:`app.website_resolver.is_safe_public_url`
  (the browser/research fetch path) and
  :func:`app.website_resolver.validate_redirect_chain`. The gaps
  found here live in ``website_resolver.py`` / ``research/collector.py``,
  which Stream D does NOT own (CONTRACTS6) — the failing cases are
  therefore marked ``xfail`` with the owning file named, and the
  ready-to-apply patch is in ``D-REPORT.md``. An xfail turning into an
  xpass is the signal the coordinator's patch landed.
* ``env=mcp-stdio`` — proxy env forwarding to spawned MCP stdio
  servers (:func:`app.mcp.discovery.proxy_env_for_stdio`).
* ``env=backend-proxy`` — validator behaviour with the egress proxy
  env active.

No probe ever issues a request to a private IP, a metadata address,
or an external host, except the one live proxied control fetch of
``https://example.com/`` through the sandbox egress proxy (public
documentation host) and the collector repro, where the "private"
server is a second localhost listener the test itself runs. A BLOCKED
verdict means the validator rejected the URL without fetching it.
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.agentLoop import _task_authorized_host_url
from app.mcp.discovery import proxy_env_for_stdio
from app.monitor_check import (
    _ValidatingRedirectHandler,
    _validate_monitor_url,
    fetch_observation,
)
from app.website_resolver import is_safe_public_url, validate_redirect_chain

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


def _stub_public(host, port, proto=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _stub_private(host, port, proto=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


def _stub_dns_fail(host, port, proto=0):
    raise socket.gaierror("Name or service not known")


# ---------------------------------------------------------------------------
# (a) Backend monitor validator — full matrix, must hold (no regression)
# ---------------------------------------------------------------------------

BACKEND_BLOCKED = [
    "http://localhost/",
    "http://localhost./",
    "http://LOCALHOST/",
    "http://[::1]/",
    "http://[::]/",
    "http://0.0.0.0/",
    "http://0/",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254./",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://2852039166/",          # decimal 169.254.169.254
    "http://0xa9fea9fe/",          # hex 169.254.169.254
    "http://0251.0376.0251.0376/",  # octal 169.254.169.254
    "http://3232235521/",          # decimal 192.168.0.1
    "http://%31%32%37.0.0.1/",     # percent-encoded 127.0.0.1
    "http://%31%32%37%2e%30%2e%30%2e%31/",
    "http://%6c%6f%63%61%6c%68%6f%73%74/",  # percent-encoded localhost
    "http://[fe80::1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 cloud metadata
    "http://user:pass@127.0.0.1/",
    "http://example.com@127.0.0.1/",
    "file:///etc/passwd",
    "ftp://example.com/",
    "http://127.0.0.1.nip.io/",
    "http://192.168.1.1.sslip.io/",
    "http://metadata.google.internal/",
    "http://metadata/",
    "http://foo.internal/",
    "http://foo.local/",
]

# The 127.0.0.1 fixture is allowed in EVERY spelling by design: the
# canonical address is the fixture address, and the fixture server is
# the same host in any spelling. These pin that equivalence.
BACKEND_FIXTURE_SPELLINGS = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8124/x",
    "http://127.0.0.1./",
    "http://2130706433/",
    "http://2130706433./",
    "http://0x7f000001/",
    "http://0x7f.0.0.1/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://127.0.1/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[::ffff:8.8.8.8]/",  # public via v4-mapped
    "http://8.8.8.8/",
    "http://93.184.216.34/",
]


@pytest.mark.parametrize("url", BACKEND_BLOCKED)
def test_backend_validator_blocks_private(url):
    ok, reason = _validate_monitor_url(url)
    assert ok is False, f"{url} allowed: {reason}"


@pytest.mark.parametrize("url", BACKEND_FIXTURE_SPELLINGS)
def test_backend_validator_allows_fixture_spellings(url):
    ok, reason = _validate_monitor_url(url)
    assert ok is True, f"{url} blocked: {reason}"


def test_backend_validator_allows_public_hostname(monkeypatch):
    # Under a proxy the DNS check is skipped, so this holds in any env.
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    ok, reason = _validate_monitor_url("https://example.com/")
    assert ok is True, reason


# ---------------------------------------------------------------------------
# (b) Browser/research validator — gaps found (xfail: unowned files)
# ---------------------------------------------------------------------------

# GAP-D1 (website_resolver.py): percent-encoded hosts are allowed.
# getaddrinfo fails on "%31%32%37.0.0.1" and the except-pass branch
# returns True — while Chrome (WHATWG URL) percent-decodes the host
# to 127.0.0.1 (verified with node: new URL("http://%31%32%37.0.0.1/")
# .hostname === "127.0.0.1"). monitor_check.py already rejects "%".
@pytest.mark.parametrize("url", [
    "http://%31%32%37.0.0.1/",
    "http://%31%32%37%2e%30%2e%30%2e%31/",
    "http://%6c%6f%63%61%6c%68%6f%73%74/",
    "http://%31%36%39.254.169.254/",
])
@pytest.mark.xfail(reason="GAP-D1 website_resolver.py: no percent-encoded-host rejection", strict=False)
def test_browser_validator_blocks_percent_encoded_hosts(url):
    ok, reason = is_safe_public_url(url, resolver=_stub_public)
    assert ok is False, f"{url} allowed: {reason}"


# GAP-D2 (website_resolver.py): no _canonical_ipv4 normalization and
# no trailing-dot strip. Non-canonical forms are blocked today ONLY
# because glibc getaddrinfo happens to parse them (inet_aton) — with
# any resolver that does not (stub here, or a proxy-side resolver),
# or with a trailing dot ("2130706433."), they are allowed. Chrome
# parses "2130706433." to 127.0.0.1 (verified with node).
@pytest.mark.parametrize("url", [
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://2130706433./",
    "http://2852039166/",
    "http://0xa9fea9fe/",
])
@pytest.mark.xfail(reason="GAP-D2 website_resolver.py: no canonical-IPv4 normalization", strict=False)
def test_browser_validator_blocks_noncanonical_without_dns_luck(url):
    ok, reason = is_safe_public_url(url, resolver=_stub_public)
    assert ok is False, f"{url} allowed: {reason}"


@pytest.mark.parametrize("url", [
    "http://localhost./",
    "http://2130706433./",
])
@pytest.mark.xfail(reason="GAP-D2 website_resolver.py: no trailing-dot strip", strict=False)
def test_browser_validator_blocks_trailing_dot_hosts(url):
    # Note: "http://127.0.0.1./" IS blocked today — the embedded-IP
    # regex catches the dotted quad — but only canonical dotted quads
    # are saved that way; "localhost." and "2130706433." are not.
    ok, reason = is_safe_public_url(url, resolver=_stub_public)
    assert ok is False, f"{url} allowed: {reason}"


def test_browser_validator_rebinding_blocks_private_resolution():
    ok, _ = is_safe_public_url("https://example.com/", resolver=_stub_private)
    assert ok is False


def test_browser_dns_failure_is_fail_open_documented():
    """KNOWN residual (coordinator decision, not an xfail): when local
    DNS fails, is_safe_public_url passes the host — the fetcher would
    fail DNS too, EXCEPT under a proxy, where the proxy resolves the
    host and local DNS failure means nothing. Changing this to
    fail-closed would break the offline-test design the function
    documents; the egress proxy is the boundary for proxied fetches.
    Pinned so the behaviour cannot change silently."""
    ok, _ = is_safe_public_url("https://example.com/", resolver=_stub_dns_fail)
    assert ok is True


# GAP-D4 (research/collector.py): validate_redirect_chain itself is
# correct per hop, but it has no production callers, and the collector
# follows redirects unchecked. The chain cases below fail only where
# the per-hop check itself is blind (D1/D2); the live httpx repro in
# test_collector_redirect_follows_into_private_space proves the
# follow itself.
@pytest.mark.xfail(reason="GAP-D1/D2 website_resolver.py: per-hop check blind to pct/non-canonical hosts", strict=False)
@pytest.mark.parametrize("hops", [
    ["http://%31%32%37.0.0.1/"],
    ["https://example.com/", "http://2130706433/"],
])
def test_browser_redirect_chain_blocks_obscured_private_hops(hops):
    ok, reason = validate_redirect_chain("https://example.com/", hops, resolver=_stub_public)
    assert ok is False, f"chain {hops} allowed: {reason}"


def test_browser_redirect_chain_blocks_plain_private_hops():
    ok, _ = validate_redirect_chain(
        "https://example.com/", ["https://example.com/a", "http://169.254.169.254/"],
        resolver=_stub_public)
    assert ok is False


# ---------------------------------------------------------------------------
# (a/d) Backend live redirect chains against local servers
# ---------------------------------------------------------------------------


class _Redirector(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/ok":
            body = b"<html><head><title>OK</title></head><body>hello</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/to-priv":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/chain":
            self.send_response(302)
            self.send_header("Location", "/step2")
            self.end_headers()
        elif self.path == "/step2":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.5/")
            self.end_headers()
        elif self.path == "/to-pct":
            self.send_response(302)
            self.send_header("Location", "http://%31%32%37.0.0.1/")
            self.end_headers()
        elif self.path == "/to-https-priv":
            self.send_response(302)
            self.send_header("Location", "https://169.254.169.254/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def redirector():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_backend_live_redirect_to_private_blocked(redirector):
    with pytest.raises((RuntimeError, ValueError)):
        fetch_observation(redirector + "/to-priv", timeout=5)


def test_backend_live_redirect_chain_to_private_blocked(redirector):
    with pytest.raises((RuntimeError, ValueError)):
        fetch_observation(redirector + "/chain", timeout=5)


def test_backend_live_redirect_to_percent_encoded_blocked(redirector):
    with pytest.raises((RuntimeError, ValueError)):
        fetch_observation(redirector + "/to-pct", timeout=5)


def test_backend_live_redirect_to_https_private_blocked(redirector):
    with pytest.raises((RuntimeError, ValueError)):
        fetch_observation(redirector + "/to-https-priv", timeout=5)


def test_backend_live_redirect_control_fetches(redirector):
    obs = fetch_observation(redirector + "/ok", timeout=5)
    assert "hello" in obs["text"]


def test_backend_redirect_handler_blocks_https_private_hop():
    """http -> https -> private: the handler validates the https hop
    before any connection is attempted (no TLS server needed)."""
    handler = _ValidatingRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:9/start", method="GET")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        handler.redirect_request(req, None, 302, "Found", {}, "https://169.254.169.254/")
    assert "Redirect rejected" in str(excinfo.value.msg)


# ---------------------------------------------------------------------------
# (d) Collector httpx redirect repro — GAP-D4 (research/collector.py)
# ---------------------------------------------------------------------------


class _Secret(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append(self.path)
        body = b"SECRET-PRIVATE-CONTENT"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ToSecret(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()


@pytest.mark.xfail(reason="GAP-D4 research/collector.py: httpx follow_redirects=True with no per-hop validation", strict=False)
def test_collector_redirect_follows_into_private_space():
    """Reproduces collector.py:1178 exactly (same Client construction)
    against a local redirector whose target is a second localhost
    listener serving private content. Fails while the collector
    follows the redirect unchecked."""
    import httpx

    _Secret.hits.clear()
    srv_b = ThreadingHTTPServer(("127.0.0.1", 0), _Secret)
    threading.Thread(target=srv_b.serve_forever, daemon=True).start()
    _ToSecret.target = f"http://127.0.0.1:{srv_b.server_address[1]}/secret"
    srv_a = ThreadingHTTPServer(("127.0.0.1", 0), _ToSecret)
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    try:
        with httpx.Client(timeout=4.0, follow_redirects=True, trust_env=False) as client:
            resp = client.get(f"http://127.0.0.1:{srv_a.server_address[1]}/go")
        assert _Secret.hits == [], f"private server hit: {_Secret.hits}"
        assert "SECRET-PRIVATE-CONTENT" not in resp.text
    finally:
        srv_a.shutdown()
        srv_b.shutdown()


# ---------------------------------------------------------------------------
# (c) MCP stdio proxy-env forwarding — the round-3 fix must hold
# ---------------------------------------------------------------------------

_PROXY_NAMES = {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "NODE_USE_ENV_PROXY"}


def test_mcp_stdio_forwards_proxy_env(monkeypatch):
    """Spawn /usr/bin/env with exactly the env the MCP SDK builds
    (get_default_environment() | server.env) and the env
    discovery._session passes. Proxy var NAMES only — never values."""
    from mcp.client.stdio import get_default_environment

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    forwarded = proxy_env_for_stdio()
    assert _PROXY_NAMES & set(forwarded), "no proxy vars forwarded at all"
    env = get_default_environment() | (forwarded or {})
    out = subprocess.run(["/usr/bin/env"], capture_output=True, text=True, env=env, timeout=15)
    seen = {line.split("=", 1)[0] for line in out.stdout.splitlines() if "=" in line}
    assert _PROXY_NAMES & set(forwarded) <= seen


def test_mcp_stdio_without_forwarding_drops_proxy_env(monkeypatch):
    """Control: the SDK default env carries no proxy vars, so the fix
    is load-bearing — without proxy_env_for_stdio the spawned server
    cannot reach the internet (the original Stream C failure)."""
    from mcp.client.stdio import get_default_environment

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    env = get_default_environment()
    out = subprocess.run(["/usr/bin/env"], capture_output=True, text=True, env=env, timeout=15)
    seen = {line.split("=", 1)[0] for line in out.stdout.splitlines() if "=" in line}
    assert not (_PROXY_NAMES & seen)


def test_mcp_proxy_env_never_forwards_secrets(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "tok-secret")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    forwarded = proxy_env_for_stdio()
    assert "MY_API_KEY" not in forwarded and "MY_TOKEN" not in forwarded
    assert set(forwarded) <= _PROXY_NAMES


# ---------------------------------------------------------------------------
# (e) Proxy-env validator behaviour
# ---------------------------------------------------------------------------


def test_proxy_env_literal_blocks_still_apply(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    for url in ["http://169.254.169.254/", "http://10.0.0.5/",
                "http://%31%32%37.0.0.1/", "http://localhost/"]:
        ok, _ = _validate_monitor_url(url)
        assert ok is False, url


def test_proxy_bypass_skips_dns_check_for_proxied_hosts(monkeypatch):
    """Documents the proxy branch: proxy_bypass is False for ordinary
    hosts (so no local DNS check runs — the egress proxy resolves),
    True for no_proxy hosts. Unresolvable hosts are therefore allowed
    under a proxy; the rebinding defence for proxied fetches rests
    on the egress proxy (see D-REPORT.md)."""
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    assert urllib.request.proxy_bypass("example.com") is False
    assert urllib.request.proxy_bypass("127.0.0.1") is True
    assert urllib.request.proxy_bypass("localhost") is True
    ok, _ = _validate_monitor_url("http://rebind.example/")
    assert ok is True


# ---------------------------------------------------------------------------
# Authorized-host override: canonical spellings (Stream D fix, agentLoop.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://2130706433:8199/x",
    "http://0x7f000001:8199/x",
    "http://0177.0.0.1:8199/x",
    "http://127.1:8199/x",
    "http://%31%32%37.0.0.1:8199/x",
])
def test_override_admits_canonical_spellings_of_allowlisted_host(url):
    got = _task_authorized_host_url(f"go to {url}", ["127.0.0.1"])
    assert got == "http://127.0.0.1:8199/x", got


@pytest.mark.parametrize("url", [
    "http://2852039166/",   # decimal 169.254.169.254 — NOT the allow-listed host
    "http://0xa9fea9fe/",   # hex 169.254.169.254
    "http://169.254.169.254/",
    "http://10.5.5.5/admin",
])
def test_override_still_rejects_non_allowlisted_private_hosts(url):
    assert _task_authorized_host_url(f"go to {url}", ["127.0.0.1"]) is None
    assert _task_authorized_host_url(f"go to {url}", ["example.com"]) is None


def test_override_canonical_spelling_of_other_host_still_rejected():
    # 2130706433 IS 127.0.0.1, so it is admitted under a 127.0.0.1
    # list — but under an unrelated list it must still be rejected.
    assert _task_authorized_host_url("go to http://2130706433/x", ["example.com"]) is None


def test_override_canonical_spelling_rejects_userinfo():
    assert _task_authorized_host_url(
        "go to http://user:pass@2130706433:8199/", ["127.0.0.1"]) is None


def test_override_canonical_spelling_needs_allow_list():
    assert _task_authorized_host_url("go to http://2130706433/x", None) is None
    assert _task_authorized_host_url("go to http://2130706433/x", []) is None
