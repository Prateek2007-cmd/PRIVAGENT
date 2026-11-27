#!/usr/bin/env python3
"""Smoke-check the demo booking fixture (stdlib only, no deps).

Serves demo/booking/ on 127.0.0.1:8123 in-process and fetches every page,
asserting HTTP 200 plus the markers each page must contain. Run from the
repo root or from this directory:

    python3 demo/booking/smoke-check.py        # Linux / macOS
    py demo\\booking\\smoke-check.py            # Windows

Exit 0 = all pages serve with their markers. This checks the fixture
serves and renders as static HTML; it does NOT run a browser task and
does NOT prove the agent can drive it — that needs the Windows package
(see navigator-audit/reports/LOCAL_BROWSER_VERIFICATION.md).
"""
import http.server
import functools
import socket
import sys
import threading
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHECKS = [
    ("index.html", ["Search trips", 'id="date"', 'id="from"', 'id="to"', "DEMO FIXTURE"]),
    ("results.html", ["Choose an option", 'id="options"', "DEMO FIXTURE"]),
    ("passengers.html", ["Passenger details", 'id="full_name"', 'id="berth"', "DEMO FIXTURE"]),
    ("summary.html", ["Booking summary", "PREPARED", "Approval boundary", "disabled in this demo"]),
    ("fixture.css", ["--accent"]),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    failures = []
    try:
        for page, markers in CHECKS:
            url = f"http://127.0.0.1:{port}/{page}"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    status = resp.status
            except Exception as exc:  # noqa: BLE001 — report, don't crash
                failures.append(f"{page}: fetch failed: {exc}")
                print(f"FAIL {page}: fetch failed: {exc}")
                continue
            missing = [m for m in markers if m not in body]
            if status != 200 or missing:
                failures.append(f"{page}: status={status} missing={missing}")
                print(f"FAIL {page}: status={status} missing={missing}")
            else:
                print(f"PASS {page}: 200, {len(markers)} markers present")
    finally:
        httpd.shutdown()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        return 1
    print(f"\nAll {len(CHECKS)} pages OK on http://127.0.0.1:{port}/ (server stopped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
