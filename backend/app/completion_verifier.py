"""PrivAgent Strict Completion Verification Module.

Enforces strict verification rules:
- Navigation tasks require observing the intended destination.
- Research tasks require the requested research evidence.
- Comparison tasks require all required comparisons.
- Refusals, explanations, empty responses, or provider errors never count as successful execution.
- Distinguishes completed, partially completed, blocked, waiting for user, and failed states.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

from .verification_layers import ActionEvidence, verify_layers

_AUTHORITATIVE = re.compile(
    r"\b(?:order (?:placed|confirmed)|payment (?:successful|received)|message sent|"
    r"email sent|invitation sent|booking confirmed|account created|signed in successfully|"
    r"file uploaded|changes saved)\b", re.IGNORECASE,
)
_EXTERNAL_WRITE = re.compile(
    r"\b(?:pay|purchase|buy|book|send|login|log in|sign in|save|submit|post|upload|"
    r"order|checkout|transfer|register|create account|schedule|invite)\b", re.I,
)


def verify_action_receipt(action: dict[str, Any], result: dict[str, Any], *,
                          before: Any = None, goal: str = "") -> dict[str, Any]:
    """Evaluate six explicit signals. An OK dispatch or optimistic text is not authority."""
    receipt = result.get("verification") or {}
    after = result.get("observation") or {}
    before_url = before.get("url") if isinstance(before, dict) else getattr(before, "url", "")
    after_url = after.get("url") if isinstance(after, dict) else getattr(after, "url", "")
    before_text = before.get("pageText", "") if isinstance(before, dict) else getattr(before, "page_text", "")
    after_text = after.get("pageText", "") if isinstance(after, dict) else getattr(after, "page_text", "")
    changed = receipt.get("surfaceChanged")
    if changed is None and after:
        changed = (before_url != after_url or before_text != after_text)
    authority = receipt.get("authoritativeConfirmation")
    if authority is None and after_text and after_text != before_text:
        status = receipt.get("transportStatus")
        authority = bool(_AUTHORITATIVE.search(after_text)) if isinstance(status, int) and 200 <= status < 400 else None
    target = action.get("target") or {}
    target_confidence = receipt.get("targetConfidence")
    if target_confidence is None and result.get("targetId") and target.get("targetId") == result.get("targetId"):
        target_confidence = .85
    # A browser API return says only the script ran. Trusted CDP acknowledgment
    # must be provided by the executor; never infer it from result.ok.
    evidence = ActionEvidence(
        target_confidence=target_confidence,
        dispatch_acknowledged=receipt.get("dispatchAcknowledged"),
        surface_changed=changed,
        transport_status=receipt.get("transportStatus"),
        authoritative_confirmation=authority,
        goal_aligned=receipt.get("goalAligned"),
    )
    write_effect = bool(action.get("type") == "upload_file" or
        action.get("type") == "type" and (action.get("params") or {}).get("submit") or
        _EXTERNAL_WRITE.search(goal))
    # For a sent message, the receipt must show both the site's explicit
    # affirmation and the exact submitted text in the post-action surface.
    # Other goals with unknown constraints remain unknown rather than guessed.
    if evidence.goal_aligned is None and action.get("type") == "type" and (action.get("params") or {}).get("submit"):
        submitted = (action.get("params") or {}).get("text") or ""
        if submitted and after_text and authority and re.search(r"\b(?:send|message|email)\b", goal, re.I):
            evidence = ActionEvidence(evidence.target_confidence, evidence.dispatch_acknowledged,
                                      evidence.surface_changed, evidence.transport_status,
                                      evidence.authoritative_confirmation,
                                      submitted.casefold() in after_text.casefold())
    return verify_layers(evidence, mutating=write_effect)


def requires_authoritative_confirmation(instruction: str, history: list[dict[str, Any]]) -> bool:
    if not _EXTERNAL_WRITE.search(instruction):
        return False
    return bool(history) and any(h.get("result_ok") and
        (h.get("action") or {}).get("type") in ("click", "type", "press", "upload_file") for h in history)


@dataclass
class VerificationResult:
    state: str  # "completed", "partially_completed", "blocked", "waiting_user", "failed"
    message: str
    is_verified: bool
    reason: str


# Patterns matching model refusals or capability denials
REFUSAL_PATTERNS = [
    re.compile(r"\b(?:i\s+(?:can(?:not|'t)|am\s+unable\s+to|do\s+not\s+have\s+(?:access|the\s+ability))\b)", re.IGNORECASE),
    re.compile(r"\b(?:as\s+an\s+ai(?:\s+language\s+model)?|i'm\s+an\s+ai)\b.*?\b(?:cannot|can't|unable)\b", re.IGNORECASE),
    re.compile(r"\b(?:sorry|apologies)[,\s]+(?:but\s+)?i\s+(?:cannot|can't|do\s+not)\b", re.IGNORECASE),
    re.compile(r"\b(?:cannot\s+access\s+browsers|cannot\s+access\s+external\s+apps|cannot\s+open\s+email)\b", re.IGNORECASE),
    re.compile(r"\bi\s+don't\s+have\s+real-time\s+access\s+to\s+the\s+internet\b", re.IGNORECASE),
    re.compile(r"\bi\s+cannot\s+browse\s+the\s+web\b", re.IGNORECASE),
    # "I don't have the details/context of the previous task…" — a request for
    # missing information is a denial of capability, never a completed result.
    re.compile(r"\bi\s+(?:do\s+n[o']t\s+have|lack|am\s+missing|have\s+no)\s+(?:the\s+)?(?:details?|context|information|info|data|record|memory|knowledge)\s+(?:of|about|regarding|for)\b", re.IGNORECASE),
    re.compile(r"\b(?:please\s+specify|could\s+you\s+(?:please\s+)?specify|kindly\s+specify)\b", re.IGNORECASE),
]

# Patterns detecting authentication/login required or waiting for user
AUTH_WAITING_PATTERNS = [
    re.compile(r"\b(?:sign-in|login|authentication|credentials|qr\s*code|mfa|2fa|captcha)\s+(?:required|blocking|needed)\b", re.IGNORECASE),
    re.compile(r"\b(?:please\s+sign\s+in|please\s+log\s+in|solve\s+the\s+captcha)\b", re.IGNORECASE),
]


def is_refusal_message(message: str) -> bool:
    """True if message represents an LLM refusal or capability denial."""
    if not message:
        return False
    clean = message.strip()
    return any(p.search(clean) for p in REFUSAL_PATTERNS)


def is_substantive_conversational_answer(message: str, instruction: str) -> bool:
    """True for a real answer that only mentions a limitation in passing.

    Capability answers legitimately say things like "I cannot continuously
    monitor sites 24/7" while still delivering a full, useful result. Requiring
    a conversational instruction plus a long, structured answer keeps bare
    refusals ("I cannot access browsers.") failing as before.
    """
    if classify_instruction_type(instruction) != "CONVERSATIONAL":
        return False
    text = (message or "").strip()
    if len(text) < 240:
        return False
    return bool(re.search(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\|)", text))


def is_waiting_for_user(message: str, state: str) -> bool:
    """True if task is waiting for user action (auth, CAPTCHA, approval)."""
    if state in ("awaiting_confirmation", "awaiting_input", "waiting_user"):
        return True
    if not message:
        return False
    return any(p.search(message) for p in AUTH_WAITING_PATTERNS)


# A whole instruction that is only an acknowledgment / go-ahead, with
# no new content of its own: "yeah", "yes please", "retry". Such a
# turn continues whatever the conversation was doing — it never
# states a new task that could complete on its own.
_BARE_CONTINUATIONS = {
    "yeah", "yes", "yep", "yup", "ok", "okay", "sure", "go ahead",
    "retry", "try again", "do it", "continue", "proceed", "please do",
    "go on", "yeah retry", "yes retry", "yes please", "yeah please",
    "ok retry", "yes go ahead", "yeah go ahead", "sure go ahead",
}


def _is_bare_continuation(instruction: str) -> bool:
    """True when the instruction is only an acknowledgment / go-ahead."""
    text = (instruction or "").strip().lower()
    text = re.sub(r"[.!?,\s]+$", "", text).strip()
    return text in _BARE_CONTINUATIONS


# Execution-failure markers produced by the agent loop itself. A summary
# carrying any of these is an honest failure record, never a completion.
_EXECUTION_FAILURE_MARKERS = (
    "planner unavailable",
    "planner timed out",
    "loop detected",
    "non-recoverable error",
    "privacy violation",
    "session init failed",
    "budget_exceeded",
)

_EXECUTION_FAILURE_PREFIXES = (
    "Error:",
    "error:",
    "Failed:",
    "failed:",
)

# UPPERCASE error codes emitted by the loop/executor, e.g.
# "SESSION_INIT_FAILED: no tab", "TASK_TIMEOUT: ...".
_EXECUTION_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{3,}:\s")


def is_execution_failure(message: str) -> bool:
    """True if the message reports an execution failure rather than a result."""
    m = (message or "").strip()
    if not m:
        return False
    low = m.lower()
    if any(m.startswith(p) for p in _EXECUTION_FAILURE_PREFIXES):
        return True
    if _EXECUTION_FAILURE_CODE.match(m):
        return True
    return any(marker in low for marker in _EXECUTION_FAILURE_MARKERS)



def classify_instruction_type(instruction: str) -> str:
    """Classify the user instruction into a verification category.

    Categories:
    - "NAVIGATION": Direct site/service opening ("open mail", "go to google.com").
    - "COMPARISON": Multi-site research ("compare X on Amazon and Flipkart").
    - "RESEARCH": Single-site product research or extraction ("find price of ...").
    - "CONVERSATIONAL": Q&A, coding, explanations, greetings.
    - "UNSUPPORTED": Desktop OS control, rebooting machine.
    """
    text = (instruction or "").strip().lower()

    from .capabilities import check_supported_capability
    supported, _ = check_supported_capability(text)
    if not supported:
        return "UNSUPPORTED"

    from .extractors.research import is_multi_site_instruction
    if is_multi_site_instruction(instruction):
        return "COMPARISON"

    # Navigation patterns
    nav_pattern = re.compile(
        r"^(?:open|go\s+to|navigate\s+to|visit|browse\s+to)\s+"
        r"(?:https?://|mail|email|gmail|inbox|google|amazon|flipkart|youtube|leetcode|wikipedia|[a-z0-9-]+\.[a-z]{2,})\b",
        re.IGNORECASE,
    )
    if nav_pattern.search(text):
        return "NAVIGATION"

    # Companion-routed instructions (email monitoring, calendar,
    # tracking, memory, dispatch) are handled by their own companions,
    # which either do the thing or say honestly they cannot — their
    # answers are not research products. Without this, the word "find"
    # in "find the Microsoft interview invitation" labels an email
    # mission setup as RESEARCH, and Rule 6 prepends "Research
    # incomplete: No substantive research sources were examined" to a
    # mission confirmation that never asked for research.
    from .connectors.calendar_companion import is_calendar_query
    from .email_intelligence.companion_queries import is_email_companion_query
    from .email_intelligence.mission_setup import is_email_mission_setup_instruction
    from .memory_companion import is_remember_directive
    if (
        is_email_mission_setup_instruction(instruction)
        or is_email_companion_query(instruction)
        or is_calendar_query(instruction)
        or is_remember_directive(instruction)
    ):
        return "CONVERSATIONAL"

    # Stream 3 (CONTRACTS4 §3 — the price-watch wart): a monitoring
    # instruction ("watch the price of X ... below ₹5000") is NOT a
    # research product. Without this, the word "price" in the research
    # regex below labels every price-watch ask RESEARCH, and Rule 6
    # then prepends "Research incomplete: No substantive research
    # sources were examined" to a watch confirmation that never asked
    # for research — the NL task ends "failed" while the watch itself
    # works (FINAL_DELIVERY §4.3). Only a genuine monitor directive
    # is carved out (MONITORING_DIRECTIVE_PATTERN / watch control),
    # never the broad companion detector — that detector also matches
    # schedule/job-control instructions, which keep their own paths.
    # A compound find-now + track ask stays research: it asks for
    # findings first, and the track is a follow-up.
    from .continuous_monitoring_companion import (
        MONITORING_DIRECTIVE_PATTERN,
        _WATCH_CONTROL_RE,
        _is_compound_find_and_track,
    )
    if not _is_compound_find_and_track(instruction) and (
        MONITORING_DIRECTIVE_PATTERN.search(instruction)
        or _WATCH_CONTROL_RE.search(instruction)
    ):
        return "CONVERSATIONAL"

    # Research patterns
    research_pattern = re.compile(
        r"\b(?:price|search\s+for|find|look\s+up|extract|check\s+price|specs|compare)\b",
        re.IGNORECASE,
    )
    if research_pattern.search(text):
        return "RESEARCH"

    return "CONVERSATIONAL"


def verify_navigation_destination(
    instruction: str,
    current_url: str,
    history: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Verify that a navigation task actually observed the intended destination."""
    text = (instruction or "").strip().lower()
    curr_host = urllib.parse.urlsplit(current_url or "").hostname or ""
    curr_host = curr_host.lower().replace("www.", "")

    visited_hosts: set[str] = set()
    if curr_host:
        visited_hosts.add(curr_host)
    for h in history or []:
        act = (h.get("action") or {})
        if act.get("type") == "navigate":
            u = act.get("params", {}).get("url") or ""
            host = urllib.parse.urlsplit(u).hostname or ""
            if host:
                visited_hosts.add(host.lower().replace("www.", ""))

    # Target host resolution
    target_host = ""
    if any(k in text for k in ("mail", "gmail", "email", "inbox")):
        target_hosts = {"mail.google.com", "accounts.google.com", "google.com"}
        if any(th in vh for th in target_hosts for vh in visited_hosts):
            return True, "Observed email service destination (mail.google.com / Google authentication)."
        return False, f"Navigation to mail not observed. Visited hosts: {visited_hosts or 'none'}."

    if "google" in text:
        target_host = "google.com"
    elif "amazon" in text:
        target_host = "amazon"
    elif "flipkart" in text:
        target_host = "flipkart.com"
    elif "youtube" in text:
        target_host = "youtube.com"
    elif "leetcode" in text:
        target_host = "leetcode.com"
    elif "wikipedia" in text:
        target_host = "wikipedia.org"

    if target_host:
        if any(target_host in vh for vh in visited_hosts):
            return True, f"Observed intended destination matching '{target_host}'."
        return False, f"Did not observe intended destination '{target_host}'. Visited: {visited_hosts or 'none'}."

    # Direct URL navigation (integration fix, E defect D3): strip the
    # port — urlsplit().hostname already excludes it for visited hosts,
    # so a target like 127.0.0.1:8124 never matched visited 127.0.0.1
    # and every sandbox loop task failed verification after 50 steps.
    url_m = re.search(r"https?://([^\s/]+)", text)
    if url_m:
        raw_target = (
            urllib.parse.urlsplit(url_m.group(0)).hostname or ""
        ).lower().replace("www.", "")
        if any(raw_target in vh for vh in visited_hosts):
            return True, f"Observed target host '{raw_target}'."
        return False, f"Did not observe target host '{raw_target}'. Visited: {visited_hosts or 'none'}."

    # If navigation action was successfully executed in history
    if history and any(h.get("result_ok") and (h.get("action") or {}).get("type") == "navigate" for h in history):
        return True, "Navigation action completed successfully."

    return False, "No navigation action was executed or observed."


# ---------------------------------------------------------------------------
# Stream 3 (CONTRACTS4 §3): ONE objective-type completion verifier.
#
# Decision (documented per CONTRACTS4 §3): the single canonical
# verifier is THIS module — ``backend/app/completion_verifier.py`` —
# already called by agentLoop at its three completion sites.
# ``backend/app/completion.py`` is a thin alias that re-exports it;
# there is no second implementation. Every objective type below is
# checked against the evidence CONTRACTS4 §3 names, by re-reading the
# persisted row / file / receipt — never by trusting a reply sentence.
# ---------------------------------------------------------------------------

# Machine-readable watch marker the monitoring companion appends to
# creation / update / already-running confirmations.
_WATCH_MARKER_RE = re.compile(r"privagent-watch:\s*([0-9a-fA-F-]{8,})")

# Task states (CONTRACTS4 §4). The backend record is the only source
# of truth; the frontend derives its display from it (see
# src/agentApp.ts — no frontend-side completion inference).
TASK_STATES = ("running", "awaiting_input", "awaiting_approval",
               "completed", "partially_completed", "blocked", "failed",
               "cancelled")


def suggest_next_actions(reason: str, intent: str = "") -> list[str]:
    """Next actions generated from the ACTUAL failure reason.

    CONTRACTS4 §3: after a failure the suggested actions must come
    from the failure reason, not from a static list. Each branch
    quotes the specific missing piece the reason names; the fallback
    carries the reason verbatim so the user can act on it.
    """
    reason = reason or "unknown failure"
    low = reason.lower()
    if "no product" in low or "don't have a product" in low or "which product" in low:
        return ["Tell me the product name, or paste its product page link."]
    if "can't watch" in low or "url refused" in low or "disallowed scheme" in low:
        return ["Give me a public http(s) product page URL for the product."]
    if "does not resolve" in low or "dns" in low:
        return ["Check the link is spelled right, then ask me to watch it again — "
                "a page that does not resolve yet is retried on schedule."]
    if "identity mismatch" in low or "not the product" in low:
        return ["Check the link is the right product page, or correct the product name."]
    if "no active email" in low:
        return ["Set up email monitoring first, e.g. 'scan my mails and notify me for important mails'."]
    if "no scheduled job" in low or "no watch" in low or "couldn't find a watch" in low:
        return ["List the current watches/jobs to see their names or IDs, then retry by ID."]
    if "no substantive research sources" in low or "no comparison results" in low:
        return ["Ask me to search for it, naming the sites or sources to check."]
    if "deliverable" in low and ("not generated" in low or "does not exist" in low):
        return ["Ask me to generate the file again, naming the format (PDF, Excel, Word)."]
    if "no verified browser observation" in low or "i'll search" in low:
        return ["Make sure the browser extension is connected, then retry the task."]
    if "baseline pending" in low:
        return ["Wait for the first scheduled check — it records the baseline automatically.",
                "Or say 'check it now' to take the baseline immediately."]
    if "receipt" in low:
        return ["Retry the action — it did not produce an execution receipt, so nothing was written."]
    return [f"Retry with more detail — the failure was: {reason}",
            "If it keeps failing, tell me what you expected to happen."]


def _extract_watch_id(message: str) -> str | None:
    m = _WATCH_MARKER_RE.search(message or "")
    return m.group(1) if m else None


def _is_monitor_completion_case(instruction: str, message: str) -> bool:
    """True when this turn is a monitoring outcome to verify against
    the watch table — a confirmation carrying the watch marker, a
    genuine monitor directive, or a watch-control ask. Schedule/job
    instructions and compound find-now + track asks are excluded:
    they have their own verification paths."""
    if _extract_watch_id(message or ""):
        return True
    try:
        from .continuous_monitoring_companion import (
            MONITORING_DIRECTIVE_PATTERN,
            _WATCH_CONTROL_RE,
            _is_compound_find_and_track,
        )
    except Exception:
        return False
    if _is_compound_find_and_track(instruction or ""):
        return False
    return bool(MONITORING_DIRECTIVE_PATTERN.search(instruction or "")
                or _WATCH_CONTROL_RE.search(instruction or ""))


def _read_watch_row(mission_id: str, db_path: str | None) -> dict | None:
    from . import db

    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    return dict(row) if row else None


# Stream 3 (S5 F6): only a price taken from a structured product
# field counts as a VERIFIED baseline price. A "contextual" price is
# _extract_price's guess from prose near a price word on a
# multi-listing page (e.g. ₹10,000 read off a Flipkart search page
# whose real listings were ₹89,100+); "single-amount" is the only
# amount anywhere in the text. Either may be stored as the row's
# observed price, but neither may be PRESENTED as the verified
# baseline price — the completion message must say so honestly.
_STRUCTURED_PRICE_SOURCES = {"json-ld", "microdata", "meta", "price-element", "json-api"}


def _baseline_price(row: dict | None) -> tuple[Any, str | None, bool]:
    """(price, source, verified) for a monitor row's stored baseline."""
    if not row:
        return None, None, False
    try:
        state = json.loads(row.get("last_state_json") or "{}")
    except Exception:
        state = {}
    price = state.get("price")
    source = state.get("price_source")
    verified = price is not None and source in _STRUCTURED_PRICE_SOURCES
    return price, source, verified


def _unverified_price_note(row: dict | None) -> str:
    """Honest note when the stored baseline price is unverified, else ''."""
    price, source, verified = _baseline_price(row)
    if price is None or verified:
        return ""
    return (f" Note: the recorded baseline price (₹{price:g}) is unverified — "
            f"it was read from page text ({source}), not from structured "
            f"product data. The first structured check will confirm it.")


_PRICE_CONDITIONS = ("price_drop", "below_price", "daily_price")


def _no_baseline_price_note(row: dict | None) -> str:
    """Honest note when a price-based watch has a baseline but no
    price could be read from the page: the %-drop / below-price
    condition can never evaluate, so the message must say so
    instead of leaving the user assuming a price was recorded.
    A text-change watch has no price to record, so it gets no note."""
    if not row:
        return ""
    if str(row.get("condition_type") or "") not in _PRICE_CONDITIONS:
        return ""
    price, _source, _verified = _baseline_price(row)
    if price is not None:
        return ""
    return (" Note: no baseline price could be read from the page, so "
            "price alerts stay off until a real price is recorded — "
            "the next scheduled check will try again.")


def _stated_interval_seconds(text: str, *, last: bool = False) -> int | None:
    """Seconds of the (first|last) 'every N minutes/hours' phrase in
    *text*, or None when it states no absolute interval. Unlike
    ``_parse_interval_seconds`` this does NOT invent the 300 s default:
    a relative update ("twice as often") states no absolute interval
    in the instruction, and the default would falsely fail the row
    (S5 D10 — the fixed handler's reply states the new cadence, "now
    checking every 30 minutes", which is what the row is verified
    against)."""
    from .continuous_monitoring_companion import _INTERVAL_RE, _WORD_NUMBERS

    matches = list(_INTERVAL_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1] if last else matches[0]
    num_s, unit = m.group(1), m.group(2).lower()
    num = 1 if num_s is None else (int(num_s) if num_s.isdigit() else _WORD_NUMBERS.get(num_s.lower(), 5))
    seconds = num * 3600 if unit.startswith("hour") else num * 60
    return max(60, seconds)


# The watch title a control reply names ("Paused the watch
# **Price Watch: Iphone 17**", "The watch **X** is already
# paused") — the reply is the handler's own record of which watch
# it acted on.
_CONTROL_REPLY_TITLE_RE = re.compile(
    r"(?:Paused|Resumed|Cancelled) the watch \*\*(.+?)\*\*"
    r"|The watch \*\*(.+?)\*\* is already (?:paused|active)",
    re.IGNORECASE,
)


def _control_reply_title(message: str) -> str | None:
    m = _CONTROL_REPLY_TITLE_RE.search(message or "")
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _resolve_control_watch(instruction: str, ref: str | None, message: str,
                           chat_id: str | None,
                           db_path: str | None) -> tuple[dict | None, str]:
    """The watch row a control turn acted on, as a plain dict.

    Returns ``(row, status)`` with status "resolved" |
    "clarification" | "none" | "unresolved". Resolution order, per
    action:

    * pause / resume: the working context (``resolve_watch_target``
      over the chat's synced watches — the exact path the handler
      uses), then the title the reply itself names, then the regex
      referent. The regex capture alone used to be the only path,
      so "Pause the first watch." resolved ref='.' and a successful
      pause was reported failed (M1).
    * cancel: the reply title first — after a delete the working
      context no longer holds the cancelled watch, so resolving
      through it would point at a surviving watch — then the regex
      referent. A cancel whose target cannot be found at all still
      returns ("resolved", None): the row being gone IS the
      verified outcome for a cancel.
    """
    from .continuous_monitoring_companion import _resolve_watch

    action_m = re.search(r"\b(pause|resume|cancel|delete|stop)\b",
                         instruction or "", re.IGNORECASE)
    action = action_m.group(1).lower() if action_m else ""
    clean_ref = (ref or "").strip().strip("\"'").rstrip(".,;:!?").strip() or None

    def _by_title(title: str | None) -> dict | None:
        if not title:
            return None
        found, _amb = _resolve_watch(title, None, db_path)
        return dict(found) if found is not None else None

    def _by_ref() -> dict | None:
        if not clean_ref:
            return None
        found, _amb = _resolve_watch(clean_ref, None, db_path)
        return dict(found) if found is not None else None

    def _by_context() -> tuple[dict | None, str]:
        if not chat_id:
            return None, "unresolved"
        from .working_context import load_working_context, resolve_watch_target

        ctx = load_working_context(chat_id, db_path)
        result = resolve_watch_target(ctx, instruction or "")
        if result["status"] == "resolved" and result.get("watch"):
            row = _read_watch_row(str(result["watch"].get("watch_id") or ""), db_path)
            return row, "resolved"
        return None, result["status"]  # "clarification" | "none"

    if action in ("cancel", "delete", "stop"):
        row = _by_title(_control_reply_title(message))
        if row is not None:
            return row, "resolved"
        row = _by_ref()
        if row is not None:
            return row, "resolved"
        return None, "resolved"  # no row: the cancel outcome is "gone"
    row, status = _by_context()
    if status == "resolved":
        return row, "resolved"
    if status in ("clarification", "none"):
        return None, status
    row = _by_title(_control_reply_title(message))
    if row is not None:
        return row, "resolved"
    row = _by_ref()
    if row is not None:
        return row, "resolved"
    return None, "unresolved"


def _verify_monitor_completion(instruction: str, message: str,
                               db_path: str | None = None,
                               chat_id: str | None = None) -> VerificationResult:
    """CONTRACTS4 §3 monitor criteria, verified by re-reading the DB.

    Completed requires: a persisted watch row with the correct
    product/url, interval_seconds and condition — plus a baseline
    observation row, or an honest ``baseline_pending`` state (never a
    claimed baseline that does not exist). A watch-control ask is
    verified the same way: pause/resume by the row's status on
    re-read, cancel by the row being gone.
    """
    from .continuous_monitoring_companion import (
        _WATCH_CONTROL_RE,
        parse_below_price,
        parse_daily_time,
        parse_percent_drop,
    )

    msg = (message or "").strip()
    intent = "monitor_product"
    # -- Watch-control asks (pause / resume / cancel): no marker in
    # the reply; resolve the watch the instruction names and verify
    # the row's state on re-read. Cancel DELETES the row, so a missing
    # row after a "Cancelled" reply IS the verified outcome.
    ctrl = _WATCH_CONTROL_RE.search(instruction or "")
    if ctrl and not _extract_watch_id(msg):
        action = ctrl.group(1).lower()
        ref = (ctrl.group(2) or "").strip() or None
        if "couldn't find a watch" in msg or msg.startswith("Which watch?") \
                or "more than one watch" in msg:
            return VerificationResult(
                state="awaiting_input", message=msg, is_verified=False,
                reason="Watch control needs the watch's name or id.",
            )
        # M1: resolve through the working context — the same path the
        # handler uses — never from the regex capture alone. The old
        # code fed the raw capture ("Pause the first watch." -> '.')
        # to _resolve_watch, so a successful pause was reported
        # failed with a self-contradictory message. Rows are plain
        # dicts here: _resolve_watch returns sqlite3.Row, which has
        # no .get() and crashed this branch whenever a row WAS found.
        row, resolve_status = _resolve_control_watch(
            instruction, ref, msg, chat_id, db_path)
        if resolve_status in ("clarification", "none"):
            return VerificationResult(
                state="awaiting_input", message=msg, is_verified=False,
                reason="Watch control needs the watch's name or id.",
            )
        if action in ("cancel", "delete", "stop"):
            if row is None:
                return VerificationResult(
                    state="completed", message=msg, is_verified=True,
                    reason="Watch row is gone on re-read — cancel verified.")
            return VerificationResult(
                state="failed",
                message=f"Cancel did not persist: the watch row still exists.\n\n{msg}",
                is_verified=False,
                reason="Watch row still present after cancel.",
            )
        if row is None:
            display_ref = (ref or "").strip().strip("\"'").rstrip(".,;:!?").strip()
            return VerificationResult(
                state="failed",
                message=f"Watch control failed: no watch row found for {display_ref or 'the watch you named'!r}.\n\n{msg}",
                is_verified=False,
                reason="No watch row found for the named watch.",
            )
        want = "paused" if action == "pause" else "active"
        if row.get("status") == want:
            return VerificationResult(
                state="completed", message=msg, is_verified=True,
                reason=f"Watch status is '{want}' on re-read.")
        return VerificationResult(
            state="failed",
            message=f"Watch {action} did not persist: status is {row.get('status')!r}, not {want!r}.\n\n{msg}",
            is_verified=False,
            reason=f"Watch status is {row.get('status')!r} on re-read.",
        )

    # -- Creation / already-running / updated confirmations.
    watch_id = _extract_watch_id(msg)
    if not watch_id:
        # The handler asked for what is missing, or refused the URL —
        # that is a clarification / honest failure, never a completion.
        if "don't have a product to watch yet" in msg:
            return VerificationResult(
                state="awaiting_input", message=msg, is_verified=False,
                reason="No product to watch — clarification needed.",
            )
        if "can't watch" in msg:
            return VerificationResult(
                state="failed", message=msg, is_verified=False,
                reason="Monitor URL refused by the URL gate.",
            )
        return VerificationResult(
            state="failed",
            message=f"Monitoring incomplete: no watch was created and no watch id was returned.\n\n{msg}",
            is_verified=False,
            reason="No watch marker and no watch row to verify.",
        )
    row = _read_watch_row(watch_id, db_path)
    if row is None:
        return VerificationResult(
            state="failed",
            message=f"Watch confirmation carried id {watch_id}, but no persisted watch row exists.\n\n{msg}",
            is_verified=False,
            reason="Watch row missing on re-read.",
        )
    if row.get("status") != "active":
        return VerificationResult(
            state="failed",
            message=f"Watch {watch_id} exists but its status is {row.get('status')!r}, not active.\n\n{msg}",
            is_verified=False,
            reason=f"Watch status is {row.get('status')!r}.",
        )
    # Interval: a daily cron watch stores interval 86400 + a cron;
    # anything else stores the parsed "every N" cadence. A relative
    # update ("twice as often") states NO absolute interval in the
    # instruction — the new cadence is the one the reply states ("now
    # checking every 30 minutes"); verify the row against that, never
    # against the 300 s default the parser returns for "no interval
    # found", or every true relative update would fail (S5 D10).
    # The reply's LAST interval phrase is the new cadence ("was
    # checking every hour, now checking every 30 minutes").
    daily = parse_daily_time(instruction)
    if daily is not None:
        cron, _display = daily
        if (row.get("schedule_cron") or "") != cron or row.get("trigger_type") != "cron":
            return VerificationResult(
                state="failed",
                message=f"Watch schedule mismatch: stored cron {row.get('schedule_cron')!r}, expected {cron!r}.\n\n{msg}",
                is_verified=False,
                reason="Stored cron does not match the requested daily time.",
            )
    else:
        want_interval = _stated_interval_seconds(instruction) \
            or _stated_interval_seconds(msg, last=True)
        if want_interval is None:
            return VerificationResult(
                state="failed",
                message=f"Watch interval cannot be verified: neither the instruction nor the reply states the new cadence.\n\n{msg}",
                is_verified=False,
                reason="No interval stated to verify against the stored row.",
            )
        if row.get("interval_seconds") != want_interval:
            return VerificationResult(
                state="failed",
                message=f"Watch interval mismatch: stored every {row.get('interval_seconds')}s, "
                        f"requested every {want_interval}s.\n\n{msg}",
                is_verified=False,
                reason="Stored interval does not match the requested cadence.",
            )
    # Condition: an absolute threshold stays absolute, a percent stays
    # a percent — the stored row must carry the requested one.
    below = parse_below_price(instruction)
    percent = parse_percent_drop(instruction)
    params: dict = {}
    try:
        params = json.loads(row.get("condition_params") or "{}")
    except Exception:
        params = {}
    if below is not None:
        if row.get("condition_type") != "below_price" or params.get("threshold") != below:
            return VerificationResult(
                state="failed",
                message=f"Watch condition mismatch: stored {row.get('condition_type')} {params}, "
                        f"requested below ₹{below:g}.\n\n{msg}",
                is_verified=False,
                reason="Stored condition is not the requested absolute-price threshold.",
            )
    elif percent is not None:
        if row.get("condition_type") != "price_drop" or (params.get("min_percent") or 0) != percent:
            return VerificationResult(
                state="failed",
                message=f"Watch condition mismatch: stored {row.get('condition_type')} {params}, "
                        f"requested a {percent:g}% drop.\n\n{msg}",
                is_verified=False,
                reason="Stored condition is not the requested percent drop.",
            )
    # URL: when the instruction names a URL, the row must watch it.
    url_m = re.search(r"https?://[^\s\"'<>)]+", instruction or "")
    if url_m and (row.get("target_url") or "").rstrip("/") != url_m.group(0).rstrip("/"):
        return VerificationResult(
            state="failed",
            message=f"Watch target mismatch: stored {row.get('target_url')!r}, requested {url_m.group(0)!r}.\n\n{msg}",
            is_verified=False,
            reason="Stored target URL does not match the requested URL.",
        )
    # Baseline: a recorded state hash is a real baseline; otherwise
    # the task still completes (the watch is live and the first tick
    # records it) but the message must say baseline_pending honestly.
    # S5 F6: a stored price from page text ("contextual") is NOT a
    # verified baseline price — the message says so, never presenting
    # the guessed price as confirmed.
    if row.get("last_state_hash"):
        return VerificationResult(
            state="completed", message=f"{msg}{_unverified_price_note(row)}{_no_baseline_price_note(row)}", is_verified=True,
            reason="Watch row active with correct product/url, interval and condition; baseline recorded.",
        )
    return VerificationResult(
        state="completed",
        message=f"{msg}\n\nBaseline pending — the first scheduled check has not run yet, so no price is recorded for this watch so far.",
        is_verified=True,
        reason="Watch row active with correct product/url, interval and condition; baseline_pending (honest).",
    )


def verify_objective_completion(objective: Any, evidence: dict | None = None,
                                db_path: str | None = None) -> VerificationResult:
    """Verify one structured objective against CONTRACTS4 §3.

    ``objective`` is Stream 1's Objective dataclass or a dict with
    the §1 fields (``primary_intent``, ``entities``, ``condition``,
    ``interval_seconds``, ``target_url``). ``evidence`` carries what
    the pipeline produced — watch/job ids, search results, sources,
    artifact paths, receipts — and every claim in it is checked by
    re-reading the persisted row / file / receipt. A missing or
    unverifiable claim is ``failed`` (or ``blocked`` with the
    blocker), never ``completed``. A bare promise sentence
    ("I'll search") is never completion.
    """
    from pathlib import Path as _Path

    ev = evidence or {}
    intent = ""
    if isinstance(objective, dict):
        intent = str(objective.get("primary_intent") or "")
    else:
        intent = str(getattr(objective, "primary_intent", "") or "")
    if not intent:
        return VerificationResult(
            state="failed", message="No objective intent to verify.",
            is_verified=False, reason="Objective has no primary_intent.")

    def _fail(msg: str, reason: str) -> VerificationResult:
        return VerificationResult(state="failed", message=msg,
                                  is_verified=False, reason=reason)

    # -- Search: ≥1 real result with title+url, returned to the user.
    if intent == "simple_search":
        results = ev.get("results") or []
        real = [r for r in results if isinstance(r, dict)
                and r.get("title") and r.get("url")]
        if not real:
            return _fail("Search incomplete: no real search results with title and URL were returned.",
                         "No search results.")
        return VerificationResult(
            state="completed",
            message=f"Found {len(real)} result(s).",
            is_verified=True, reason=f"{len(real)} search result(s) with title+url.")

    # -- Research: ≥3 accepted sources, evidence table, artifact file
    # read-back verified when one was requested.
    if intent == "research":
        sources = ev.get("sources") or []
        accepted = [s for s in sources if isinstance(s, dict) and s.get("url")]
        if len(accepted) < 3:
            return _fail(f"Research incomplete: only {len(accepted)} accepted source(s); at least 3 are required.",
                         f"Only {len(accepted)} accepted source(s).")
        if not ev.get("evidence_table"):
            return _fail("Research incomplete: no evidence table was produced.",
                         "No evidence table.")
        art = ev.get("artifact_path")
        if ev.get("artifact_requested") and art:
            p = _Path(str(art))
            if not p.exists() or p.stat().st_size == 0:
                return _fail(f"Research deliverable incomplete: {art} does not exist or is empty.",
                             "Artifact file missing or empty.")
            if ev.get("artifact_must_contain") and ev["artifact_must_contain"] not in p.read_text(errors="replace"):
                return _fail("Research deliverable incomplete: the file does not contain the requested content.",
                             "Artifact lacks the requested content.")
        return VerificationResult(
            state="completed",
            message=f"Research completed across {len(accepted)} accepted sources.",
            is_verified=True, reason=f"{len(accepted)} accepted sources + evidence table.")

    # -- Monitoring: persisted watch row, correct config, baseline or
    # honest baseline_pending.
    if intent in ("monitor_product", "monitor_website", "monitor_new_listings", "monitor_inbox"):
        watch_id = ev.get("watch_id")
        if not watch_id:
            return _fail("Monitoring incomplete: no watch was created (no watch id in evidence).",
                         "No watch id.")
        row = _read_watch_row(str(watch_id), db_path)
        if row is None:
            return _fail(f"Monitoring incomplete: no persisted watch row for {watch_id}.",
                         "Watch row missing on re-read.")
        if row.get("status") != "active":
            return _fail(f"Monitoring incomplete: watch {watch_id} status is {row.get('status')!r}, not active.",
                         f"Watch status is {row.get('status')!r}.")
        expected = ev.get("expected") or {}
        if expected.get("target_url") and (row.get("target_url") or "") != expected["target_url"]:
            return _fail("Monitoring incomplete: stored target URL does not match the objective.",
                         "Target URL mismatch.")
        if expected.get("interval_seconds") is not None and \
                row.get("interval_seconds") != expected["interval_seconds"]:
            return _fail("Monitoring incomplete: stored interval does not match the objective.",
                         "Interval mismatch.")
        if expected.get("condition_type") and row.get("condition_type") != expected["condition_type"]:
            return _fail("Monitoring incomplete: stored condition does not match the objective.",
                         "Condition mismatch.")
        baseline = ev.get("baseline")
        if isinstance(baseline, dict) and baseline.get("status") == "baseline_pending":
            return VerificationResult(
                state="completed",
                message=f"Watch {watch_id} is active with the correct config. "
                        f"Baseline pending ({baseline.get('reason') or 'first check not run yet'}).",
                is_verified=True,
                reason="Watch row correct on re-read; baseline_pending (honest).")
        if row.get("last_state_hash") or (isinstance(baseline, dict) and baseline.get("price") is not None):
            # S5 F6: a stored price from page text ("contextual") is
            # not a verified baseline price — say so, never present
            # the guessed price as confirmed.
            return VerificationResult(
                state="completed",
                message=f"Watch {watch_id} is active with a recorded baseline.{_unverified_price_note(row)}{_no_baseline_price_note(row)}",
                is_verified=True, reason="Watch row correct on re-read; baseline recorded.")
        return VerificationResult(
            state="completed",
            message=f"Watch {watch_id} is active with the correct config. Baseline pending — the first scheduled check has not run yet.",
            is_verified=True,
            reason="Watch row correct on re-read; baseline_pending (honest).")

    # -- Monitor updates: the named watch row actually changed.
    if intent in ("update_monitor", "pause_monitor", "resume_monitor"):
        watch_id = ev.get("watch_id")
        want = {"pause_monitor": "paused", "resume_monitor": "active"}.get(intent)
        if not watch_id:
            return _fail("Monitor update incomplete: no watch id in evidence.", "No watch id.")
        row = _read_watch_row(str(watch_id), db_path)
        if row is None:
            return _fail("Monitor update incomplete: watch row missing on re-read.", "Watch row missing.")
        if want and row.get("status") != want:
            return _fail(f"Monitor update incomplete: status is {row.get('status')!r}, not {want!r}.",
                         f"Watch status is {row.get('status')!r}.")
        if intent == "update_monitor" and ev.get("interval_seconds") is not None and \
                row.get("interval_seconds") != ev["interval_seconds"]:
            return _fail("Monitor update incomplete: stored interval unchanged on re-read.",
                         "Interval unchanged.")
        return VerificationResult(
            state="completed", message=f"Watch {watch_id} updated and verified by re-read.",
            is_verified=True, reason="Watch row matches on re-read.")
    if intent == "cancel_monitor":
        watch_id = ev.get("watch_id")
        if not watch_id:
            return _fail("Monitor cancel incomplete: no watch id in evidence.", "No watch id.")
        row = _read_watch_row(str(watch_id), db_path)
        if row is None or row.get("status") == "cancelled":
            return VerificationResult(
                state="completed", message=f"Watch {watch_id} is cancelled.",
                is_verified=True, reason="Watch row gone/cancelled on re-read.")
        return _fail(f"Monitor cancel incomplete: watch {watch_id} is still {row.get('status')!r}.",
                     f"Watch still {row.get('status')!r}.")

    # -- Scheduled tasks: persisted job row with next_run_at + receipt.
    if intent in ("schedule_task", "update_schedule", "cancel_schedule"):
        from .scheduler import get_job

        job_id = ev.get("job_id")
        if not job_id:
            return _fail("Scheduling incomplete: no job id in evidence.", "No job id.")
        job = get_job(str(job_id), db_path=db_path)
        if job is None:
            return _fail(f"Scheduling incomplete: no job row for {job_id}.", "Job row missing.")
        if intent == "cancel_schedule":
            if job.get("status") != "cancelled":
                return _fail(f"Cancel incomplete: job {job_id} is still {job.get('status')!r}.",
                             f"Job still {job.get('status')!r}.")
            return VerificationResult(
                state="completed", message=f"Scheduled job {job_id} is cancelled.",
                is_verified=True, reason="Job row cancelled on re-read.")
        if not job.get("next_run_at"):
            return _fail(f"Scheduling incomplete: job {job_id} has no next run time.",
                         "Job has no next_run_at.")
        return VerificationResult(
            state="completed", message=f"Scheduled job {job_id} is active with next run {job['next_run_at']}.",
            is_verified=True, reason="Job row with next_run_at on re-read.")

    # -- Email polling: the setting row actually changed, re-read.
    if intent == "configure_email_polling":
        from . import db as _db

        mission_id = ev.get("mission_id")
        want_freq = ev.get("frequency_seconds")
        if not mission_id or want_freq is None:
            return _fail("Email polling incomplete: no mission id or frequency in evidence.",
                         "No mission id / frequency.")
        mission = _db.get_email_mission(str(mission_id), db_path=db_path)
        if mission is None:
            return _fail("Email polling incomplete: email mission row missing.", "Email mission missing.")
        if mission.get("frequency_seconds") != want_freq:
            return _fail("Email polling incomplete: stored frequency does not match the requested one.",
                         "Frequency mismatch on re-read.")
        return VerificationResult(
            state="completed",
            message=f"Email check interval is now every {want_freq} seconds (verified by re-read).",
            is_verified=True, reason="Email polling setting matches on re-read.")

    # -- Browser task: ≥1 verified browser observation + the outcome.
    # A promise sentence ("I'll search") is NEVER completion.
    if intent == "browser_task":
        observations = ev.get("observations") or []
        if not observations:
            return _fail("Browser task incomplete: no verified browser observation was recorded.",
                         "No verified browser observation.")
        return VerificationResult(
            state="completed", message="Browser task completed with a verified observation.",
            is_verified=True, reason=f"{len(observations)} verified observation(s).")

    # -- Artifact: the file exists, opens, and contains the content.
    if intent == "artifact_request":
        art = ev.get("artifact_path")
        if not art:
            return _fail("Artifact incomplete: no file path in evidence.", "No artifact path.")
        p = _Path(str(art))
        if not p.exists() or p.stat().st_size == 0:
            return _fail(f"Artifact incomplete: {art} does not exist or is empty.",
                         "Artifact file missing or empty.")
        if ev.get("must_contain"):
            try:
                content = p.read_text(errors="replace")
            except Exception as exc:
                return _fail(f"Artifact incomplete: {art} cannot be read ({exc}).", "Artifact unreadable.")
            if ev["must_contain"] not in content:
                return _fail("Artifact incomplete: the file does not contain the requested content.",
                             "Artifact lacks the requested content.")
        return VerificationResult(
            state="completed", message=f"Artifact ready: {art}.",
            is_verified=True, reason="Artifact file exists and contains the requested content.")

    # -- Connector / MCP: execution receipt (id, status).
    if intent in ("connector_task", "mcp_task"):
        receipt = ev.get("receipt") or {}
        if not receipt.get("id") or not receipt.get("status"):
            return _fail(f"{intent} incomplete: no execution receipt (id, status) was returned.",
                         "No execution receipt.")
        return VerificationResult(
            state="completed",
            message=f"{intent} executed (receipt {receipt['id']}, {receipt['status']}).",
            is_verified=True, reason="Execution receipt present.")

    # -- Memory: store needs the row on re-read; recall needs the fact.
    if intent == "memory_store":
        if not ev.get("stored_row"):
            return _fail("Memory store incomplete: no stored row was read back.",
                         "No stored row on re-read.")
        return VerificationResult(
            state="completed", message="Fact stored and read back.",
            is_verified=True, reason="Stored row present on re-read.")
    if intent == "memory_recall":
        if not ev.get("fact"):
            return _fail("Memory recall incomplete: no fact was returned from the store.",
                         "No fact returned.")
        return VerificationResult(
            state="completed", message="Fact recalled from memory.",
            is_verified=True, reason="Fact returned from the store.")

    # -- Conversational / greeting / capability: a substantive,
    # non-refusal answer completes; refusals and empty answers never do.
    if intent in ("greet", "capability_query", "conversational", "ask_user_answer"):
        answer = (ev.get("answer") or "").strip()
        if not answer or is_refusal_message(answer):
            return _fail("No substantive answer was produced.", "Empty answer or refusal.")
        return VerificationResult(
            state="completed", message=answer, is_verified=True,
            reason="Substantive conversational answer.")

    return _fail(f"Unknown objective intent {intent!r} — no completion criteria exist for it.",
                 f"Unknown intent {intent!r}.")


def verify_task_completion(
    instruction: str,
    candidate_state: str,
    message: str,
    observation: Any = None,
    working_memory: Optional[dict[str, Any]] = None,
    history: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> VerificationResult:
    """Strictly verify task outcome against evidence and requirements.

    Returns VerificationResult with normalized state and honest message.
    """
    msg = (message or "").strip()
    mem = working_memory or {}
    hist = history or []
    current_url = getattr(observation, "url", "") if observation else ""
    if not current_url and isinstance(observation, dict):
        current_url = observation.get("url") or ""

    category = classify_instruction_type(instruction)

    # Rule 1: A refusal, explanation, empty response, or provider error NEVER
    # counts as completed. A substantive conversational answer may mention a
    # limitation ("I cannot monitor sites 24/7") and still be a real result.
    if is_refusal_message(msg) and not is_substantive_conversational_answer(msg, instruction):
        # Stream 3: a deterministic companion outcome ("I can't watch
        # <url> — <gate reason>") is an honest monitor refusal with a
        # specific reason, not a model capability denial. It is NOT
        # failed here with the generic refusal reason — the monitor
        # branch below (before Rule 6) verifies it against the watch
        # table and reports the specific reason. A model refusal for
        # a monitor ask still ends failed there ("no watch created").
        if not _is_monitor_completion_case(instruction, msg):
            return VerificationResult(
                state="failed",
                message=msg,
                is_verified=False,
                reason="Model refusal or capability denial cannot produce successful completion.",
            )

    if not msg:
        return VerificationResult(
            state="failed",
            message="Task failed: empty response received from agent.",
            is_verified=False,
            reason="Empty response cannot produce successful completion.",
        )

    if msg.startswith("Error:") or "Planner unavailable" in msg or "Planner timed out" in msg:
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Provider error or timeout cannot produce successful completion.",
        )

    # Rule 1b: Any execution-failure summary is failed, regardless of category.
    if is_execution_failure(msg):
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Execution error cannot produce successful completion.",
        )

    # Rule 2: Distinguish waiting for user (authentication / CAPTCHA handoff)
    if is_waiting_for_user(msg, candidate_state):
        return VerificationResult(
            state="awaiting_confirmation",
            message=msg,
            is_verified=True,
            reason="Task paused awaiting user authentication or input.",
        )

    if candidate_state in ("completed", "running") and requires_authoritative_confirmation(instruction, hist):
        confirmed = any((h.get("verification") or {}).get("layers", {}).get("authority") == "passed"
                        and (h.get("verification") or {}).get("verified") for h in hist)
        if not confirmed:
            return VerificationResult("failed", f"Action not confirmed by the service. {msg}", False,
                                      "No verified authoritative affirmation for the requested write.")

    # Rule 3: Unsupported operations must fail honestly
    if category == "UNSUPPORTED":
        return VerificationResult(
            state="failed",
            message=msg if msg else "Requested desktop OS operation is unsupported.",
            is_verified=False,
            reason="Operation requires unsupported desktop OS capabilities.",
        )

    # Rule 4: Navigation tasks require observing the intended destination
    if category == "NAVIGATION":
        nav_ok, nav_reason = verify_navigation_destination(instruction, current_url, hist)
        if not nav_ok:
            return VerificationResult(
                state="failed",
                message=f"Navigation incomplete: {nav_reason}",
                is_verified=False,
                reason=nav_reason,
            )
        return VerificationResult(
            state="completed",
            message=msg,
            is_verified=True,
            reason=nav_reason,
        )

    # Rule 5: Comparison tasks require all required comparisons
    if category == "COMPARISON":
        ms_tracker = mem.get("multi_site_research")
        if ms_tracker:
            from .extractors.research import (
                is_multi_site_task_complete,
                get_completed_sites,
                get_pending_sites,
                get_unavailable_sites,
                claims_cheapest_on_single_listing,
            )
            completed = get_completed_sites(ms_tracker)
            pending = get_pending_sites(ms_tracker)
            unavailable = get_unavailable_sites(ms_tracker)

            # Check single-listing cheapest fallacy
            if claims_cheapest_on_single_listing(msg, len(completed)):
                msg = re.sub(
                    r"\b(?:is\s+the\s+cheapest\s+option|cheapest\s+available|best\s+price)\b",
                    "was the only verified listing found",
                    msg,
                    flags=re.IGNORECASE,
                )

            if is_multi_site_task_complete(ms_tracker):
                if len(completed) >= 2 or (len(completed) == 1 and len(unavailable) > 0):
                    return VerificationResult(
                        state="completed",
                        message=msg,
                        is_verified=True,
                        reason="All requested comparison sites were either verified or confirmed unavailable.",
                    )
            elif len(completed) > 0:
                # Some completed, but pending sites remain
                return VerificationResult(
                    state="failed",
                    message=f"Partially completed ({len(completed)}/{len(completed) + len(pending)} sites). Pending: {', '.join(s['name'] for s in pending)}.\n\n{msg}",
                    is_verified=False,
                    reason=f"{len(pending)} comparison sites remain pending.",
                )
            else:
                return VerificationResult(
                    state="failed",
                    message=f"Comparison incomplete: no requested sites verified. Pending: {', '.join(s['name'] for s in pending)}.",
                    is_verified=False,
                    reason="No comparison results collected.",
                )

    # Stream 3 (CONTRACTS4 §3 — the price-watch wart): a monitoring
    # outcome is verified against the persisted watch row, never
    # against research evidence. This runs before Rule 6 so a watch
    # confirmation is never failed for having no research sources.
    if _is_monitor_completion_case(instruction, msg):
        checked = _verify_monitor_completion(instruction, msg, db_path=db_path,
                                             chat_id=chat_id)
        if candidate_state == "failed":
            # The loop reported failure: the verifier never launders
            # that into success — but it says plainly whether the
            # watch itself is live, so the user is not left guessing.
            note = (" The watch itself is live and verified."
                    if checked.state == "completed"
                    else f" The watch itself is not verified: {checked.reason}.")
            return VerificationResult(
                state="failed",
                message=f"{msg}{note}",
                is_verified=False,
                reason="Execution reported failure; verifier does not override it.",
            )
        return checked

    # Rule 6: Research tasks require evidence and verification across 6 distinct stages
    if category == "RESEARCH" or mem.get("is_research"):
        if candidate_state == "failed":
            return VerificationResult(
                state="failed",
                message=msg,
                is_verified=False,
                reason="Execution reported failure; verifier does not override failure into success.",
            )

        pkg_data = mem.get("research_knowledge_package") or {}
        sources = pkg_data.get("sources", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "sources", {})

        # Stage 1: Discovery stage
        discovery_verified = bool(mem.get("autonomous_discovery") or mem.get("specified_website"))

        # Stage 2: Collection stage - reject search results pages, verify independent sources
        from .research.collector import is_search_results_page
        substantive_sources = {}
        for sid, s in sources.items():
            s_url = s.get("url") if isinstance(s, dict) else getattr(s, "url", "")
            s_score = s.get("reliability_score", 0) if isinstance(s, dict) else getattr(s, "reliability_score", 0)
            if s_score > 0 and not is_search_results_page(s_url):
                substantive_sources[sid] = s

        inst_lower = instruction.lower()
        requires_multi = any(k in inst_lower for k in ["multiple", "compare", "credible sources", "independent sources", "different sources", "cross-reference"])

        distinct_domains = set()
        for s in substantive_sources.values():
            dom = s.get("domain") if isinstance(s, dict) else getattr(s, "domain", "")
            if dom:
                parts = dom.lower().split(".")
                root = ".".join(parts[-2:]) if len(parts) >= 2 else dom.lower()
                distinct_domains.add(root)

        coll_reason = ""
        collection_verified = bool(substantive_sources)
        if requires_multi and len(distinct_domains) < 2:
            collection_verified = False
            coll_reason = f"Prompt requested multiple credible sources, but only {len(distinct_domains)} independent source(s) were examined."
        elif not substantive_sources:
            collection_verified = False
            coll_reason = "No substantive research sources were examined (search results pages are not sources)."

        # Stage 3: Synthesis stage - claims must have verified evidence linkage
        claims = pkg_data.get("claims", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "claims", {})
        evidence_items = pkg_data.get("evidence", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "evidence", {})
        verified_claims = [
            c for c in claims.values()
            if (c.get("evidence_item_ids") if isinstance(c, dict) else getattr(c, "evidence_item_ids", []))
        ]
        synthesis_verified = bool(verified_claims or evidence_items or mem.get("collected_research"))

        # Stage 4: Artifact generation & validation stage
        artifacts = mem.get("artifacts") or []
        requested_fmt = mem.get("requested_format") or mem.get("deliverable_type")
        artifact_needed = bool(requested_fmt or any(fmt in inst_lower for fmt in ["pdf", "docx", "word", "excel", "report", "csv"]))
        artifact_verified = True
        artifact_reason = ""
        if artifact_needed:
            if not artifacts:
                artifact_verified = False
                artifact_reason = "Deliverable document was requested but not generated."
            else:
                for art in artifacts:
                    fpath = Path(art.get("filepath", ""))
                    if not fpath.exists() or fpath.stat().st_size == 0:
                        artifact_verified = False
                        artifact_reason = f"Deliverable {art.get('filename')} does not exist or is empty on disk."
                        break

        # Verification enforcement
        if not collection_verified:
            return VerificationResult(
                state="failed",
                message=f"Research incomplete: {coll_reason}\n\n{msg}",
                is_verified=False,
                reason=coll_reason,
            )

        if not synthesis_verified:
            return VerificationResult(
                state="failed",
                message=f"Research incomplete: Synthesized findings lack verified evidence traceability.\n\n{msg}",
                is_verified=False,
                reason="Synthesized claims lack verifiable evidence items.",
            )

        if not artifact_verified:
            return VerificationResult(
                state="failed",
                message=f"Research deliverable incomplete: {artifact_reason}\n\n{msg}",
                is_verified=False,
                reason=artifact_reason,
            )

        return VerificationResult(
            state="completed",
            message=msg,
            is_verified=True,
            reason=f"Research mission completed across {len(distinct_domains)} independent source(s) with verified evidence and deliverable generation.",
        )

    # Conversational or general tasks. A failed execution candidate stays
    # failed: the conversational fallback may only confirm completion of
    # tasks that did not fail, never launder a failure into success.
    if candidate_state == "failed":
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Execution reported failure; verifier does not override it.",
        )

    # A bare acknowledgment ("yeah", "yes, retry") after a failed
    # mission is a continuation of that mission, not a new
    # conversational task. If the previous assistant message in the
    # conversation is an execution failure and the failed mission's
    # deliverable is still outstanding (requested_format /
    # deliverable_type in working memory), this turn cannot complete:
    # no browsing happened and no artifact was produced. Live case
    # (2026-11-18 02:07 IST): the price hunt failed on a planner DNS
    # blip, the user said "yeah" to retry, the LLM answered with a
    # diagnosis — and the mission was recorded as "Mission Completed"
    # with no prices and no Excel.
    if _is_bare_continuation(instruction):
        conv_hist = mem.get("conversation_history") or []
        last_assistant = next(
            (m for m in reversed(conv_hist) if isinstance(m, dict) and m.get("role") == "assistant"),
            None,
        )
        if last_assistant and is_execution_failure(str(last_assistant.get("content") or "")):
            session_mem = mem.get("session_memory") or {}
            outstanding = (
                mem.get("requested_format")
                or mem.get("deliverable_type")
                or (session_mem.get("requested_format") if isinstance(session_mem, dict) else None)
                or (session_mem.get("deliverable_type") if isinstance(session_mem, dict) else None)
            )
            if outstanding and "Deliverable Ready" not in msg:
                return VerificationResult(
                    state="failed",
                    message=(
                        f"Previous mission failed and its {outstanding} deliverable is still "
                        f"outstanding — this answer does not retry or deliver it.\n\n{msg}"
                    ),
                    is_verified=False,
                    reason="Bare continuation after a failed mission with an undelivered deliverable.",
                )

    final_state = "completed" if candidate_state in ("completed", "running") else candidate_state
    return VerificationResult(
        state=final_state,
        message=msg,
        is_verified=True,
        reason="Task concluded normally.",
    )

