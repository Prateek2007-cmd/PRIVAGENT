"""Merge-phase (v4) regression tests: SSRF gaps found by Stream D.

D1: percent-encoded hosts ("http://%31%32%37.0.0.1/") were allowed —
    Chrome decodes the host to 127.0.0.1.
D2: no canonical-IPv4 normalisation / trailing-dot strip in the
    browser/research path ("2130706433", "0x7f000001", "localhost.",
    "127.0.0.1." all slipped past the blocklist).
D4: collector followed redirects unchecked (proven live returning
    SECRET-PRIVATE-CONTENT from a private localhost listener).
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.website_resolver import is_safe_public_url, validate_redirect_chain  # noqa: E402


def _public_resolver(host, port, proto=None):
    """Stub DNS: every hostname resolves to a public IP (sandbox DNS
    maps real hostnames to private IPs, so live resolution is useless
    in tests — the gate's IP/hostname logic is what is under test)."""
    return [(2, 1, 6, "", ("93.184.216.34", port))]


@pytest.mark.parametrize("url", [
    "http://%31%32%37.0.0.1/",
    "http://%31%32%37.0.0.1:8080/admin",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://localhost./",
    "http://LOCALHOST/",
    "http://127.0.0.1./",
    "http://[::1]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
])
def test_private_and_obfuscated_hosts_blocked(url):
    ok, reason = is_safe_public_url(url, resolver=_public_resolver)
    assert ok is False, f"{url} allowed: {reason}"


@pytest.mark.parametrize("url", [
    "https://www.flipkart.com/search?q=iphone",
    "https://www.amazon.in/dp/B0C123",
    "https://example.com/product",
])
def test_public_hosts_still_allowed(url):
    ok, reason = is_safe_public_url(url, resolver=_public_resolver)
    assert ok is True, f"{url} blocked: {reason}"


def test_redirect_chain_to_private_is_rejected():
    ok, _reason = validate_redirect_chain(
        "https://example.com/start",
        ["https://example.com/a", "http://127.0.0.1/secret"],
        resolver=_public_resolver,
    )
    assert ok is False


def test_redirect_chain_public_is_allowed():
    ok, reason = validate_redirect_chain(
        "https://example.com/start",
        ["https://example.com/a", "https://example.com/b"],
        resolver=_public_resolver,
    )
    assert ok is True, reason


# --- D4: the collector itself must refuse a redirect to a private host ---

def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


class _PrivateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<html><body><img src="https://cdn.example.com/secret.jpg" alt="SECRET-PRIVATE-CONTENT"></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RedirectHandler(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_collector_refuses_redirect_to_private_host(monkeypatch):
    from app import website_resolver
    from app.research import collector

    private = _serve(_PrivateHandler)
    try:
        _RedirectHandler.target = f"http://127.0.0.1:{private.server_address[1]}/secret"
        redirect = _serve(_RedirectHandler)
        try:
            start_url = f"http://127.0.0.1:{redirect.server_address[1]}/start"
            # The start URL is a test-local server: allow it explicitly.
            # The redirect TARGET must still be judged by the real gate.
            real_gate = website_resolver.is_safe_public_url
            monkeypatch.setattr(
                website_resolver, "is_safe_public_url",
                lambda u, *a, **k: (True, None) if u == start_url else real_gate(u, *a, **k),
            )

            from app.research.models import ResearchKnowledgePackage, ResearchTopic

            pkg = ResearchKnowledgePackage(
                package_id="pkg_ssrf_redirect",
                topic=ResearchTopic(title="SSRF redirect probe", objective="prove redirects are gated"),
            )
            collector.harvest_page_evidence(
                start_url,
                "A product review page",
                "A substantive article page with plenty of readable content about the product and its features and price.",
                pkg,
            )
            leaked = [v for v in pkg.visuals if "secret" in str(v).lower()]
            assert leaked == [], f"redirect to private host was followed: {leaked}"
        finally:
            redirect.shutdown()
            redirect.server_close()
    finally:
        private.shutdown()
        private.server_close()
