"""Deterministic fast path + hybrid router (Stream D, RC-1.1 / Atticus T1, T4).

Everything in this module is deterministic — regexes and keyword
scoring only, zero LLM tokens — and every decision it makes is
returned as data (``path`` + ``reason``) so the caller can log it on
the task record (``record.route``) instead of silently swallowing it.

Routing tiers (cheapest first, escalate once on failure):

* ``fast`` — greetings and other canned deterministic replies. 0
  planner calls, 0 provider tokens. The reply is a plain canned
  greeting, never presented as model output.
* ``skill_replay`` — the instruction matches a stored *executable*
  skill (see :mod:`app.long_memory`); the stored steps run with
  per-step verification and no planner call. On any failure the
  caller falls back to ``planner`` exactly once.
* ``ask_user`` — a required slot is missing (date/time, budget,
  file, ...). The caller pauses the task and asks (see the ask-user
  gate in :mod:`app.agentLoop`) instead of guessing a default.
* ``planner`` — everything else: the normal planner / provider path.

Also here: the research-package freshness check (R4-6, partial —
same-conversation reuse already exists; this adds the freshness
verdict so a stale package is re-verified instead of silently
reused) and the sensitive-question guard (CONTRACTS §2: passwords,
OTPs, recovery codes and card details are never asked in chat).
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

__all__ = [
    "ASK_USER_KINDS",
    "ASK_USER_TIMEOUT_S",
    "DEFAULT_PACKAGE_FRESH_HOURS",
    "answer_greeting",
    "detect_missing_info",
    "escalate_once",
    "is_greeting",
    "is_sensitive_question",
    "package_freshness",
    "route_instruction",
]

ASK_USER_KINDS = frozenset({
    "missing_info",
    "date_time",
    "budget",
    "preference",
    "file",
    "account",
    "domain_permission",
    "auth",
    "confirmation",
})

# CONTRACTS §2: default time a task waits for an answer before it
# fails honestly with ASK_USER_TIMEOUT. Never guesses, never silently
# completes.
ASK_USER_TIMEOUT_S = 300

# A same-topic research package older than this is "stale": reuse it
# only with a staleness warning and a re-verification of the top
# sources (R4-6). Time-sensitive claims are re-verified regardless.
DEFAULT_PACKAGE_FRESH_HOURS = 24

# ---------------------------------------------------------------------------
# Greetings (fast path, 0 tokens)
# ---------------------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^\s*(hi|hii+|hello+|hey+|hiya|howdy|good\s+(morning|afternoon|evening)|"
    r"namaste|namaskar|yo|sup|hola)\b[.!?]?\s*$",
    re.IGNORECASE,
)

_GREETING_REPLY = (
    "Hello! I'm PrivAgent — I can browse the web for you, research a "
    "topic and make a document, watch a page or a price, or help with "
    "email and calendar. What would you like me to do?"
)


def is_greeting(instruction: str) -> bool:
    """True for a bare greeting — nothing else in the message."""
    return bool(_GREETING_RE.match((instruction or "").strip()))


def answer_greeting() -> str:
    """The deterministic greeting reply (canned, not model output)."""
    return _GREETING_REPLY


# ---------------------------------------------------------------------------
# Sensitive-question guard (CONTRACTS §2)
# ---------------------------------------------------------------------------

_SENSITIVE_ASK_RE = re.compile(
    r"\b(passw(or)?d|otp|one[- ]?time\s+(code|password)|verification\s+code|"
    r"recovery\s+code|card\s+(number|details|pin|cvv)|cvv|pin\s*(number|code)?|"
    r"bank\s+account|secret\s+key|api\s+key|2fa\s+code|authenticator\s+code)\b",
    re.IGNORECASE,
)

_SENSITIVE_KINDS = frozenset({"auth", "account"})


def is_sensitive_question(question: str, kind: str = "missing_info") -> bool:
    """True when the question must NOT be asked in chat.

    Sensitive kinds (auth/account) are always takeover-only, and any
    question whose wording asks for a password, OTP, recovery code or
    card detail is sensitive whatever its kind. Such questions go
    through takeover only — never transmitted to a remote planner and
    never stored in logs.
    """
    if kind in _SENSITIVE_KINDS:
        return True
    return bool(_SENSITIVE_ASK_RE.search(question or ""))


# ---------------------------------------------------------------------------
# Missing-information detection (ask-user trigger)
# ---------------------------------------------------------------------------

# A time expression is enough to proceed without asking: an explicit
# clock time, a relative delay, a named day, or a recurring schedule.
_TIME_RE = re.compile(
    r"\b("
    r"\d{1,2}:\d{2}\s*(am|pm)?|\d{1,2}\s*(am|pm)|"
    r"in\s+\d+\s*(seconds?|minutes?|mins?|hours?|hrs?|days?)|"
    r"after\s+\d+\s*(seconds?|minutes?|mins?|hours?|hrs?|days?)|"
    r"today|tomorrow|tonight|yesterday|next\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this\s+(morning|afternoon|evening|week|weekend)|"
    r"daily|every\s+(day|morning|evening|week|hour|minute|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"morning|afternoon|evening|noon|midnight|"
    r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|"
    r"\d{4}-\d{2}-\d{2}|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r")\b",
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r"\b("
    r"today|tomorrow|next\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|\d{4}-\d{2}-\d{2}|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r")\b",
    re.IGNORECASE,
)

_REMIND_RE = re.compile(r"\bremind\s+me\b", re.IGNORECASE)
_BOOK_RE = re.compile(
    r"\b(book|reserve|schedule)\b.{0,60}\b(flight|hotel|ticket|appointment|table|room|cab|taxi)\b",
    re.IGNORECASE,
)
_FILE_REF_RE = re.compile(
    r"\b(summari[sz]e|analyse|analyze|read|review|extract|check)\b.{0,40}\b(the|this|my|that)\s+(?:\w+\s+)?(file|document|attachment|pdf|report|spreadsheet|ppt|presentation)\b",
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(
    r"\b(buy|purchase|book|order)\b.{0,80}\b(under|below|within|budget|at most|up to|less than)\b",
    re.IGNORECASE,
)


def detect_missing_info(
    instruction: str, *, has_attachment: bool = False
) -> Optional[dict[str, Any]]:
    """Detect a deterministically missing slot, or None.

    Deliberately narrow: only patterns where the missing slot is
    unambiguous (a reminder with no time, a booking with no date, a
    file task with no attached file, a buy with a budget phrase but no
    amount). Anything fuzzy goes to the planner, which asks via the
    same gate when it can (planner SYSTEM_FRAME §15 still mandates
    assume-defaults — that rule change belongs to Stream B; see the
    Stream D report).
    """
    text = (instruction or "").strip()
    if not text:
        return None

    if _REMIND_RE.search(text) and not _TIME_RE.search(text):
        return {
            "kind": "date_time",
            "question": "When should I remind you? For example 'tomorrow 9am' or 'in 30 minutes'.",
            "options": None,
        }
    if _BOOK_RE.search(text) and not _DATE_RE.search(text):
        return {
            "kind": "date_time",
            "question": "Which date should I book for? For example 'tomorrow' or '12 Dec'.",
            "options": None,
        }
    if _FILE_REF_RE.search(text) and not has_attachment:
        return {
            "kind": "file",
            "question": "Which file should I use? Attach it to this chat and I'll pick it up.",
            "options": None,
        }
    if _BUDGET_RE.search(text) and not re.search(
        r"(₹|rs\.?|inr|\$|usd|€|£)\s?[\d,]+|[\d,]+\s*(rupees|dollars|rs\.?)",
        text,
        re.IGNORECASE,
    ):
        return {
            "kind": "budget",
            "question": "What budget should I stay within? For example 'under ₹5,000'.",
            "options": None,
        }
    return None


# ---------------------------------------------------------------------------
# Research-package freshness (R4-6, partial)
# ---------------------------------------------------------------------------

def package_freshness(
    package_record: Optional[dict[str, Any]],
    *,
    max_age_hours: float = DEFAULT_PACKAGE_FRESH_HOURS,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Freshness verdict for a stored research package.

    ``fresh`` means the package is recent enough to reuse as-is;
    ``stale`` means reuse it only with a staleness warning and a
    re-verification of the top sources; time-sensitive claims are
    re-verified either way (the caller decides that from the topic).
    Returns ``{"fresh": bool, "stale": bool, "age_hours": float|None,
    "reason": str}``. A missing package is neither fresh nor stale —
    it is absent.
    """
    if not package_record:
        return {"fresh": False, "stale": False, "age_hours": None,
                "reason": "no stored package for this conversation"}
    ts = package_record.get("updated_at") or package_record.get("created_at")
    try:
        age_hours = ((now if now is not None else time.time()) - float(ts)) / 3600.0
    except (TypeError, ValueError):
        return {"fresh": False, "stale": True, "age_hours": None,
                "reason": "stored package has no usable timestamp — treat as stale and re-verify"}
    if age_hours <= max_age_hours:
        return {"fresh": True, "stale": False, "age_hours": round(age_hours, 2),
                "reason": f"stored package is {age_hours:.1f}h old (fresh within {max_age_hours}h)"}
    return {"fresh": False, "stale": True, "age_hours": round(age_hours, 2),
            "reason": f"stored package is {age_hours:.1f}h old (older than {max_age_hours}h) — re-verify before reuse"}


# ---------------------------------------------------------------------------
# Hybrid router
# ---------------------------------------------------------------------------

def route_instruction(
    instruction: str,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Route one instruction to the cheapest tier that can do it.

    Order: greeting (fast) → missing info (ask_user) → learned
    executable skill (skill_replay) → planner. ``context`` may carry
    ``has_attachment`` (bool) and ``skills`` (a pre-recalled list of
    executable-skill dicts from ``long_memory.match_executable_skill``;
    when absent the router does not hit the DB itself — the caller
    supplies the match so routing stays a pure function).
    """
    ctx = context or {}
    text = (instruction or "").strip()
    if is_greeting(text):
        return {"path": "fast", "reason": "bare greeting — deterministic reply, 0 tokens"}
    missing = detect_missing_info(text, has_attachment=bool(ctx.get("has_attachment")))
    if missing is not None:
        return {"path": "ask_user", "reason": f"missing {missing['kind']} — ask instead of guessing", "question": missing}
    skill = ctx.get("skill")
    if isinstance(skill, dict) and skill.get("id"):
        return {"path": "skill_replay", "reason": f"learned executable skill '{skill.get('name')}' matches", "skill": skill}
    return {"path": "planner", "reason": "no deterministic fast path applies"}


def escalate_once(attempts: list[dict[str, Any]]) -> Optional[str]:
    """Next tier after a failed attempt, or None when already escalated.

    ``attempts`` is a list of ``{"path": ..., "ok": bool}`` in order.
    One escalation only: skill_replay failure → planner; a planner
    failure does not escalate further (it fails honestly). A fast-path
    failure never escalates — it was deterministic, so retrying it
    through the planner is the caller's choice, not this helper's.
    """
    if not attempts:
        return None
    last = attempts[-1]
    if last.get("ok"):
        return None
    if last.get("path") == "skill_replay":
        if any(a.get("path") == "planner" for a in attempts):
            return None
        return "planner"
    return None
