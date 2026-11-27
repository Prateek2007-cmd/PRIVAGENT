"""Measured privacy leak rate (Task 6) — regression tests over the fixture set.

The numbers come from a real run of ``backend/scripts/eval_live.py``
(2026-11-16, mock provider, offline) against
``backend/tests/fixtures/pii_cases.json``: 24 synthetic PII tokens,
6 leaked into the planner prompt — leak rate 0.25. Re-measured
2026-11-18 after the phone fix: 5 leaked — leak rate 0.2083.
Re-measured 2026-11-26 after the OTP/address fix (Stream A, R5-02):
1 leaked — leak rate 0.0417. Only the unlabelled bare password
still leaks; bare OTPs, labelled OTPs and postal addresses are now
redacted (see "Closed 2026-11-26" below). Re-measured 2026-11-27
(Stream E, checker hardening + OCR verification): still 1 leaked —
leak rate 0.0417. The bare password remains a documented gap: no
pattern can match it without guessing.

A leak = the exact token string survives ``classify_and_redact`` +
``build_prompt``. The fixture is split in two, and the split is the
point of the test:

* ``PATTERN_COVERED`` — PII types the redactor's pattern set claims to
  handle (email, card, SSN, API keys, JWT, AWS, Slack, private key,
  labelled secrets, URL tokens, bare high-entropy tokens in element
  values, phone numbers, bare/labelled OTPs, postal addresses). These
  MUST NOT leak: the test asserts a zero leak rate here. This is the
  fail-closed property.
* ``KNOWN_GAPS`` — types the redactor does not cover today, measured
  leaking: unlabelled passwords only (since 2026-11-26). These are
  asserted to be *reported* with a leak-rate ceiling (overall <= 0.05,
  i.e. at most the 1 measured leak) — a fix may close a gap (the test
  then needs updating deliberately, with the docs), but leaks may not
  silently grow.

Closed 2026-11-18: a bare high-entropy token (24+ chars, two
character classes) in an *element value* is now dropped by
``_looks_like_bare_secret`` (``pii-el-barekey-01``). The same shape
in free page text is still a known non-goal: it is indistinguishable
from a git SHA, an MD5 checksum, or a record ID, so redacting it
there would mangle legitimate pages.

Closed 2026-11-18: phone numbers. ``PATTERNS`` had no phone pattern,
so a phone in a ``tel:`` href, in free text, or in an element name
passed through unredacted (``pii-el-phone-01`` leaked). The fix adds
a ``phone`` pattern (country-code, grouped, and bare Indian mobile
forms) and makes ``redact_url`` return ``tel:[REDACTED]`` /
``sms:[REDACTED]`` — a tel:/sms: URL exists only to carry a number.
Phone leak rate is now 0.0 (3/3 cases caught). Re-measured results
are in ``evals/results/privacy_leak_2026-11-18.json`` (5/24, 0.2083).

Closed 2026-11-26: bare OTPs, labelled OTPs, and postal addresses.
The OTP/address fix (R5-02) adds an ``otp_labelled`` pattern (label +
4-8 digit code), bare-6-digit masking in page text / OCR text /
element values, OTP-named element values dropped, and street-address
+ UK-postcode patterns. Re-measured results are in
``evals/results/privacy_leak_2026-11-26.json`` (1/24, 0.0417):
bare OTP 0/2, labelled OTP 0/1, postal address 0/1, phone 0/3.
Only the unlabelled bare password still leaks — no pattern can
match it without guessing, so it stays a documented gap.

If the committed results JSON (``evals/results/privacy_leak_2026-11-26.json``)
is present, its summary must match a fresh run.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_live import run_privacy_leak

RESULTS_PATH = Path(__file__).resolve().parents[1].parent / "evals" / "results" / "privacy_leak_2026-11-27.json"

PATTERN_COVERED = {
    "email", "credit_card", "ssn", "api_key", "jwt", "aws_key",
    "slack_token", "private_key", "labelled_password", "labelled_token",
    "url_token", "bare_key", "phone",
    # Closed 2026-11-26 by the OTP/address fix (R5-02): bare OTP 0/2,
    # labelled OTP 0/1, postal address 0/1.
    "bare_otp", "otp_labelled", "postal_address",
}

# (type, context) pairs measured leaking on 2026-11-26, with the reason.
# OTPs and postal addresses were here until 2026-11-26; the OTP/address
# fix closed them (re-measured 1/24, 0.0417). Only the unlabelled bare
# password remains: no pattern can match it without guessing.
KNOWN_GAPS = {
    ("bare_password", "page_text"): "unlabelled password has no pattern to match",
}


def test_fixture_shape():
    result = run_privacy_leak()
    assert result["n_total"] == 24
    assert {r["id"] for r in result["cases"]} == {
        "pii-email-01", "pii-phone-01", "pii-phone-02", "pii-card-01",
        "pii-card-02", "pii-ssn-01", "pii-apikey-01", "pii-apikey-02",
        "pii-jwt-01", "pii-aws-01", "pii-slack-01", "pii-privkey-01",
        "pii-pwlabel-01", "pii-tokenlabel-01", "pii-otp-01", "pii-otp-02",
        "pii-pwbare-01", "pii-urltoken-01", "pii-address-01",
        "pii-el-phone-01", "pii-el-otp-01", "pii-el-email-01",
        "pii-el-card-01", "pii-el-barekey-01",
    }


def test_pattern_covered_types_never_leak():
    """Fail-closed: every PII type the redactor claims to handle must
    have a zero leak rate, in both page text and element values."""
    result = run_privacy_leak()
    for row in result["cases"]:
        if row["type"] in PATTERN_COVERED:
            assert not row["leaked"], row["id"]


def test_known_gaps_are_reported_and_bounded():
    """The gaps may not silently grow: overall leak rate stays at or
    below the measured 0.0417 (ceiling 0.05), and every known gap is
    present in the results with its leaked status recorded."""
    result = run_privacy_leak()
    assert result["leak_rate"] <= 0.05  # measured 0.0417 (1/24) after the OTP/address fix; was 0.2083 (5/24)
    by_key = {(r["type"], r["context"]): r for r in result["cases"]}
    for (pii_type, context), _reason in KNOWN_GAPS.items():
        assert (pii_type, context) in by_key
    leaked_keys = {(r["type"], r["context"]) for r in result["cases"] if r["leaked"]}
    assert leaked_keys == set(KNOWN_GAPS)


def test_committed_results_match_fresh_run():
    if not RESULTS_PATH.exists():
        return
    committed = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    fresh = run_privacy_leak()
    for key in ("n_total", "leaked", "leak_rate"):
        assert committed[key] == fresh[key], key
    assert committed["by_type"] == fresh["by_type"]
    assert committed["by_context"] == fresh["by_context"]
