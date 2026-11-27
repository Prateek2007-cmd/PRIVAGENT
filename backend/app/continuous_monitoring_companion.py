"""Continuous Monitoring Directive Handler.

Allows users to trigger continuous background monitoring (e.g. price drops,
page updates, stock alerts) directly from conversation.
Registers persistent monitoring missions in SQLite with the interval scheduler
and broadcasts status updates to connected workspace clients over WebSocket,
which surface in the conversation and in the Control Center monitoring view.
That is the only alert channel: there is no phone/SMS ping, and port 5173
is only the address of the workspace web UI, never the monitoring channel.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from . import db
from .hub import hub
from .query_builder import parse_relative_interval_factor, sanitize_query_text
from .working_context import load_working_context, resolve_watch_target

logger = logging.getLogger(__name__)


# Track-new-items alternatives, shared by the directive pattern and
# the handler's own ask detector so the two cannot drift apart.
# ("track SDE internships and alert me when a new one appears",
# "watch this page and tell me when a new listing shows up".)
_TRACK_NEW_ALT = (
    r"\b(?:monitor|track|watch)\b[^.\n]{0,80}\bnew\b[^.\n]{0,40}\b(?:listing|listings|job|jobs|internship|internships|role|roles|position|positions|product|products|item|items|post|posts|opportunit(?:y|ies)|opening|openings)\b|"
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\bnew\b[^.\n]{0,40}\b(?:listing|listings|job|jobs|internship|internships|role|roles|position|positions|product|products|item|items|post|posts|opportunit(?:y|ies)|opening|openings)\b|"
    r"\bwhen\s+a\s+new\s+one\b[^.\n]{0,40}\bappears?\b|"
    # Product availability ("alert me when a new iPhone 16 drops on
    # Amazon"): "new" + a thing + "drops". The plain "price drops"
    # has no "new", so it cannot match this branch.
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\bnew\b[^.\n]{0,40}\bdrops?\b"
)
_TRACK_NEW_RE = re.compile(_TRACK_NEW_ALT, re.IGNORECASE)


MONITORING_DIRECTIVE_PATTERN = re.compile(
    # FIX-B (M3): "keep an eye on <product>" is a monitor ask even
    # when the word "price" never appears ("keep an eye on Sony
    # WH-1000XM6 headphones, ping me if it drops below ₹25000").
    # The old first branch required "keep an eye on ... price", so
    # the bare-name shape fell through to the model layer, which
    # demanded a product URL — while the same shape with "Live
    # monitor iPhone 17" created directly. A signal word (price /
    # drop / below / stock / cadence / a product noun) keeps a plain
    # "keep an eye on the kids" from routing here.
    r"\bkeep\s+an\s+eye\s+on\b[^.\n]{0,80}?\b(?:price|prices|drops?|below|under|stock|every|evry|headphones|earbuds|phone|iphone|laptop|tablet|camera|tv|shoes|deal|item|product)\b|"
    # A bare price drop ("alert me when the price drops", "notify me
    # when the price drops"): "if" and "when" are the same ask. The
    # product may sit between "when" and "price" ("alert me when
    # the Apple iPhone 17 price drops"), so a short window is
    # allowed there.
    r"\b(?:continuously\s+monitor|monitor\s+this|monitor\s+(?:the\s+)?(?:product|price|page|site|webpage|item|deal)|track\s+(?:the\s+)?price|(?:alert|notify|tell)\s+me\s+(?:if|when)\s+(?:the\s+)?[^.\n]{0,40}?\bprice\s+drops?|price\s+drop\s+alert|keep\s+an\s+eye\s+on\s+(?:the\s+)?price)\b|"
    r"\b(?:monitor|track)\s+.*?\b(?:price|drops?|stock|availability|changes?)\b|"
    # Daily price report ("yes every day at 9am just notify me the
    # prices"): a notify + price(s) ask, or a price(s) ask with a daily
    # cadence. "prizes" is the common misspelling of "prices".
    r"\bnotify\s+me\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    r"\b(?:price|prize)s?\b[^.\n]{0,60}\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b|"
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    # A notify + daily cadence on its own ("notify me everyday") is a
    # monitoring cadence even when the word "price" never appears —
    # the product/threshold comes from the rest of the instruction,
    # and the handler asks which product when there is none, instead
    # of inventing one.
    r"\bnotify\s+me\b[^.\n]{0,40}\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b|"
    # Absolute threshold ("alert me when the price falls below ₹5000",
    # "notify me if it drops under 3000"): a directive verb + below /
    # under + a number. The verb requirement keeps a plain statement
    # ("the price is below 5000") from becoming a watch. "ping" is an
    # alert verb too (FIX-B M3: "ping me if it drops below ₹25000").
    r"\b(?:alert|notify|tell|let|ping)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    r"\b(?:monitor|track|watch)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    # The threshold forms users actually type, mirroring
    # parse_below_price: "reaches <5000", "drops to ₹5000", "falls
    # below 5000", "<₹5000". "check" is a verb here too ("check the
    # strap and tell me if it reaches <5000") — without it the ask
    # falls through to a browser task that only looks once.
    r"\b(?:alert|notify|tell|let|check|ping)\b[^.\n]{0,60}?(?:reaches?|drops?|falls?|goes?|gets?|dips?)\s+(?:below|under|to)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    r"\b(?:alert|notify|tell|let|check|watch|monitor|track|ping)\b[^.\n]{0,60}?<\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    # Percent-drop watch ("live monitor iPhone 17 when it 10% off",
    # "alert me when it drops by 10%"): a directive verb + a percent
    # drop, in either word order — the number first ("10% off") or
    # the drop word first ("drops by 10%"). The number may be a word
    # ("ten percent off"). Without this a bare %-drop ask (no word
    # "price", no "below", no interval) fell through to a one-shot
    # browser task instead of a watch. The drop word is required so
    # a plain statement ("the iPhone 17 is 10% off today") does not
    # route. "drops?" (not "dropped") keeps past-tense statements
    # ("it dropped 10% yesterday") from routing. No word boundary
    # after the %: "%" is a non-word character, so \b never matches
    # after it ("drops by 10%" at the end would not route).
    r"\b(?:monitor|track|watch|alert|notify|tell|let|check|ping)\b[^.\n]{0,60}?\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*(?:%|percent|per\s*cent)\s*(?:off|drops?|down|discount|less|cheaper)\b|"
    r"\b(?:monitor|track|watch|alert|notify|tell|let|check|ping)\b[^.\n]{0,60}?\b(?:drops?|falls?|goes?\s+down|discounts?|cuts?)\s+(?:by\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*(?:%|percent|per\s*cent)|"
    # An interval cadence on its own ("alert me every 5 minutes",
    # "check its price and just alert me every 5 minutes"): the
    # directive verb + a per-N-minutes/hours cadence. Without this a
    # follow-up that only changes the cadence fell through to a
    # research task and died in the completion guard ("Research
    # incomplete") — the product/threshold come from the history.
    # The number may be a word ("every five minutes"), not only a
    # digit — the handler parses both (see _parse_interval_seconds).
    # "evry" is the common misspelling of "every" (FIX-B M3: "check
    # evry 20 mins" must route and parse as 20 minutes, not fall
    # through to the model layer and default to 5 minutes).
    r"\b(?:alert|notify|tell|let|check|monitor|track|watch|ping)\b[^.\n]{0,60}?\b(?:every|evry)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?)\b|"
    + _TRACK_NEW_ALT,
    re.IGNORECASE,
)


# India Standard Time (the user's timezone) is UTC+5:30 year-round —
# no daylight saving — so a fixed offset converts a local morning time
# to the UTC cron the scheduler stores ("five UTC fields").
_IST_OFFSET_MINUTES = 330
_DAILY_TIME_RE = re.compile(
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b[^0-9\n]{0,40}?"
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
# Hour-first order ("notify me the price at 9am every day"): the time
# comes before the daily phrase. Either "at <hour>" or an am/pm
# marker is required, so a bare number (a price, a count) is never
# read as a time.
_DAILY_TIME_RE_AFTER = re.compile(
    r"(?:\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
    r"|\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b)"
    r"[^0-9\n]{0,40}?\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b",
    re.IGNORECASE,
)
# Bare daily cadence with no clock time ("notify me every day",
# "check it daily"): the time-bearing regexes above need an hour,
# so this one only proves the cadence exists; the default 9:00 AM
# IST is applied by parse_daily_time.
_BARE_DAILY_RE = re.compile(
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b",
    re.IGNORECASE,
)


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}

_INTERVAL_RE = re.compile(
    # FIX-B (M3): "evry" is the common misspelling of "every" —
    # "check evry 20 mins" parses as 1200 s, not the 300 s default.
    r"\b(?:every|evry)\s+(?:(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(minutes?|mins?|hours?)\b",
    re.IGNORECASE,
)


def _parse_interval_seconds(instruction: str) -> int:
    """Parse \"every 5 minutes\" / \"every two hours\" into seconds.

    The handler used to hardcode 300s for every interval watch, so
    \"check every 2 hours\" silently became a 5-minute watch. No
    cadence in the instruction -> the 300s default. Clamped to a
    60-second floor: a sub-minute tick would burn the scheduler.
    """
    m = _INTERVAL_RE.search(instruction or "")
    if not m:
        return 300
    num_s, unit = m.group(1), m.group(2).lower()
    num = 1 if num_s is None else (int(num_s) if num_s.isdigit() else _WORD_NUMBERS.get(num_s.lower(), 5))
    seconds = num * 3600 if unit.startswith("hour") else num * 60
    return max(60, seconds)


def _describe_interval(interval_seconds: int) -> str:
    """Human cadence for an interval watch: \"every 5 minutes\",
    \"every 2 hours\". Used in replies so the message quotes the
    watch's real cadence, never a hardcoded one."""
    seconds = interval_seconds or 300
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours} hour" + ("s" if hours != 1 else "")
    minutes = max(1, seconds // 60)
    return f"every {minutes} minute" + ("s" if minutes != 1 else "")


def parse_daily_time(instruction: str) -> tuple[str, str] | None:
    """Parse "every day at 9am" into a UTC cron + a display string.

    Returns ``(cron, "9:00 AM IST")`` or ``None`` when the instruction
    carries no daily time. A bare hour with no am/pm marker is read as
    a morning hour (the "every morning" reading the user means). The
    daily phrase may come before the hour ("every day at 9am") or
    after it ("at 9am every day").
    """
    m = _DAILY_TIME_RE.search(instruction or "") or _DAILY_TIME_RE_AFTER.search(instruction or "")
    if not m:
        # A bare daily cadence ("notify me every day", "check it daily")
        # names no clock time. Default to the 9:00 AM IST morning
        # report — the same default the demo's daily digest uses —
        # instead of dropping the cadence and falling back to a
        # 5-minute interval watch the user never asked for.
        if _BARE_DAILY_RE.search(instruction or ""):
            return "30 3 * * *", "9:00 AM IST"
        return None
    if m.re is _DAILY_TIME_RE:
        hour_s, minute_s, marker = m.group(1), m.group(2), m.group(3)
    else:
        hour_s = m.group(1) or m.group(4)
        minute_s = m.group(2) or m.group(5)
        marker = m.group(3) or m.group(6)
    hour = int(hour_s)
    minute = int(minute_s or 0)
    marker = (marker or "").lower()
    if marker == "pm" and hour != 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    display_hour = hour % 12 or 12
    display = f"{display_hour}:{minute:02d} {'PM' if hour >= 12 else 'AM'} IST"
    utc_total = (hour * 60 + minute - _IST_OFFSET_MINUTES) % (24 * 60)
    return f"{utc_total % 60} {utc_total // 60} * * *", display


# ---------------------------------------------------------------------------
# Stream C (R-05): NL scheduling intents (contract §4).
#
# "do this in 2 minutes" / "summarise this daily at 8am" / "run this
# every 15 minutes" / "continue this task tomorrow morning" create a
# REAL scheduled job via scheduler.create_job — the same function the
# API uses, not a parallel path. The intent shape is:
#   {"intent": "create_schedule", "instruction": "...",
#    "schedule": {"kind": "once"|"every"|"daily", "in_minutes": int|None,
#                 "interval_minutes": int|None, "time_ist": "HH:MM"|None}}
# Times the user says are IST (their timezone); daily jobs are stored
# as UTC cron via parse_daily_time, one-off jobs as a delay. The
# monitoring directive pattern is checked FIRST in
# is_continuous_monitoring_directive, so "monitor X every 15 minutes"
# and "every day at 9am tell me the price" stay watches, not jobs.
# ---------------------------------------------------------------------------
_SCHEDULE_TASK_VERB = (
    r"(?:do|run|perform|repeat|schedule|remind|check|tell|send|give|show|"
    r"summari[sz]e|generate|create|write|fetch|search|look|continue|start)"
)
_SCHEDULE_ONCE_RE = re.compile(
    rf"\b{_SCHEDULE_TASK_VERB}\b[^.\n]{{0,80}}?\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b|"
    rf"\b(?:this|that|it)\s+in\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b|"
    rf"\bremind\s+me\b[^.\n]{{0,80}}?\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_SCHEDULE_EVERY_RE = re.compile(
    rf"\b{_SCHEDULE_TASK_VERB}\b[^.\n]{{0,80}}?\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b|"
    rf"\b(?:this|that|it)\s+every\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
_SCHEDULE_DAILY_RE = re.compile(
    rf"\b{_SCHEDULE_TASK_VERB}\b[^.\n]{{0,80}}?\b(?:daily|every\s+day|everyday|every\s+morning|each\s+morning)\b|"
    rf"\b(?:this|that|it)\s+(?:daily|every\s+day|everyday)\b",
    re.IGNORECASE,
)
_SCHEDULE_TOMORROW_RE = re.compile(
    rf"\b{_SCHEDULE_TASK_VERB}\b[^.\n]{{0,80}}?\btomorrow\b(?:\s+(morning|afternoon|evening))?"
    rf"(?:\s+at\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?)?|"
    rf"\btomorrow\b(?:\s+(morning|afternoon|evening))?"
    rf"(?:\s+at\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)
_SCHEDULE_STRIP_RES = [
    # Remove the schedule clause itself, leaving the instruction.
    re.compile(r"\bin\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE),
    re.compile(r"\bevery\s+(?:\d+\s*)?(?:minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE),
    re.compile(r"\b(?:daily|every\s+day|everyday|every\s+morning|each\s+morning)\b(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?", re.IGNORECASE),
    re.compile(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:every\s+day|everyday|daily)\b", re.IGNORECASE),
    re.compile(r"\btomorrow\b(?:\s+(?:morning|afternoon|evening))?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?", re.IGNORECASE),
]


def _tomorrow_delay_minutes(instruction: str) -> int | None:
    """Minutes from now until tomorrow morning/afternoon/evening (IST).

    'tomorrow morning' -> 09:00 IST, afternoon -> 15:00 IST, evening
    -> 19:00 IST; an explicit 'at HH:MM' overrides the part of day.
    Returns None when the instruction names no tomorrow time.
    """
    m = _SCHEDULE_TOMORROW_RE.search(instruction or "")
    if not m:
        return None
    part = (m.group(1) or m.group(5) or "morning").lower()
    hour_s = m.group(2) or m.group(6)
    minute_s = m.group(3) or m.group(7)
    marker = (m.group(4) or m.group(8) or "").lower()
    if hour_s:
        hour = int(hour_s)
        minute = int(minute_s or 0)
        if marker == "pm" and hour != 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = {"morning": (9, 0), "afternoon": (15, 0), "evening": (19, 0)}[part]
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    from datetime import datetime, timedelta, timezone as _tz

    ist = _tz(timedelta(minutes=_IST_OFFSET_MINUTES))
    now_ist = datetime.now(ist)
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
    delay = int((target - now_ist).total_seconds() // 60)
    return delay if delay >= 1 else None


def parse_schedule_intent(text: str) -> dict[str, Any] | None:
    """Parse an NL scheduling ask into the contract §4 intent shape.

    Returns None when the text carries no schedule. The instruction
    text is NOT resolved here — the caller strips the schedule clause
    and resolves 'this' from history.
    """
    if not text:
        return None
    m = _SCHEDULE_ONCE_RE.search(text)
    if m:
        groups = [g for g in m.groups() if g is not None]
        num = int(groups[0])
        unit = groups[1].lower()
        minutes = num * 60 if unit.startswith(("hour", "hr")) else num
        return {"intent": "create_schedule", "instruction": "",
                "schedule": {"kind": "once", "in_minutes": minutes,
                             "interval_minutes": None, "time_ist": None}}
    m = _SCHEDULE_EVERY_RE.search(text)
    if m:
        groups = [g for g in m.groups() if g is not None]
        num = int(groups[0])
        unit = groups[1].lower()
        if unit.startswith("day"):
            minutes = num * 24 * 60
        elif unit.startswith(("hour", "hr")):
            minutes = num * 60
        else:
            minutes = num
        return {"intent": "create_schedule", "instruction": "",
                "schedule": {"kind": "every", "in_minutes": None,
                             "interval_minutes": minutes, "time_ist": None}}
    daily = parse_daily_time(text)
    if daily is not None and _SCHEDULE_DAILY_RE.search(text):
        _cron, display = daily
        hour_min = display.replace(" IST", "")
        return {"intent": "create_schedule", "instruction": "",
                "schedule": {"kind": "daily", "in_minutes": None,
                             "interval_minutes": None, "time_ist": hour_min}}
    if _SCHEDULE_DAILY_RE.search(text):
        return {"intent": "create_schedule", "instruction": "",
                "schedule": {"kind": "daily", "in_minutes": None,
                             "interval_minutes": None, "time_ist": "9:00 AM"}}
    delay = _tomorrow_delay_minutes(text)
    if delay is not None:
        return {"intent": "create_schedule", "instruction": "",
                "schedule": {"kind": "once", "in_minutes": delay,
                             "interval_minutes": None, "time_ist": None}}
    return None


def is_schedule_directive(text: str) -> bool:
    """True when the text asks to schedule a task (not a watch)."""
    if not text:
        return False
    return parse_schedule_intent(text) is not None


def _strip_schedule_clause(text: str) -> str:
    """Remove the schedule wording, leaving the task instruction."""
    out = text or ""
    for pattern in _SCHEDULE_STRIP_RES:
        out = pattern.sub(" ", out)
    # Leading schedule verbs ("do this", "run", "remind me to", "schedule").
    out = re.sub(r"^\s*(?:please\s+)?(?:do|run|perform|repeat|schedule|start|continue)\s+(?:this|that|it|the\s+\w+)?\s*",
                 "", out, flags=re.IGNORECASE)
    out = re.sub(r"^\s*remind\s+me\s+to\s+", "", out, flags=re.IGNORECASE)
    out = re.sub(r"^\s*(?:do|run)\s+", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip(" .,:;")
    return out


def _instruction_from_history(history: Optional[list[dict[str, Any]]],
                              current: str) -> str:
    """The task 'this' refers to: the last substantive user message.

    Skips the current ask itself, schedule/monitor asks, and bare
    affirmatives. Returns "" when history names no task.
    """
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        if (entry.get("role") or "").lower() != "user":
            continue
        content = str(entry.get("content") or entry.get("message") or "").strip()
        if not content or content == (current or "").strip():
            continue
        if _is_bare_affirmative(content):
            continue
        if MONITORING_DIRECTIVE_PATTERN.search(content) or is_schedule_directive(content):
            continue
        if len(content) < 4:
            continue
        return content
    return ""


def _schedule_to_create_job_args(intent: dict[str, Any]) -> tuple[str, str]:
    """Contract §4 intent -> (schedule string, human description).

    Daily times are IST: parse_daily_time already converted the IST
    hour to a UTC cron, so create_job gets the raw cron. One-off and
    every-N go through parse_schedule's own forms.
    """
    sched = intent["schedule"]
    kind = sched["kind"]
    if kind == "once":
        minutes = int(sched["in_minutes"])
        if minutes % 60 == 0 and minutes >= 60:
            hours = minutes // 60
            sched_str = f"in {hours} hour" + ("s" if hours != 1 else "")
        else:
            sched_str = f"in {minutes} minute" + ("s" if minutes != 1 else "")
        desc = f"once, {sched_str}"
        return sched_str, desc
    if kind == "every":
        minutes = int(sched["interval_minutes"])
        if minutes % (24 * 60) == 0:
            days = minutes // (24 * 60)
            sched_str = f"every {days}d"
            desc = f"every {days} day" + ("s" if days != 1 else "")
        elif minutes % 60 == 0:
            hours = minutes // 60
            sched_str = f"every {hours}h"
            desc = f"every {hours} hour" + ("s" if hours != 1 else "")
        else:
            sched_str = f"every {minutes}m"
            desc = f"every {minutes} minute" + ("s" if minutes != 1 else "")
        return sched_str, desc
    # daily: IST time -> UTC cron (parse_daily_time did the conversion).
    time_ist = (sched.get("time_ist") or "9:00 AM").strip()
    cron, display = parse_daily_time(f"every day at {time_ist}") or ("30 3 * * *", "9:00 AM IST")
    return cron, f"daily at {display}"


async def _handle_schedule_instruction(instruction: str, chat_id: Optional[str],
                                       history: Optional[list[dict[str, Any]]],
                                       db_path: Optional[str]) -> str:
    """Create a real scheduled job from NL (Stream C, R-05)."""
    from .monitoring_scheduler import format_ist_short
    from .scheduler import create_job

    intent = parse_schedule_intent(instruction)
    if intent is None:
        return (
            "I can schedule that, but I couldn't work out the timing. "
            "Try 'do this in 2 minutes', 'every 15 minutes', or 'daily at 8am'."
        )
    task_text = _strip_schedule_clause(instruction)
    if not task_text or task_text.lower() in ("this", "that", "it", "do it", "this task"):
        task_text = _instruction_from_history(history, instruction)
    if not task_text:
        return (
            "Got the timing, but what should I do on that schedule? "
            "Tell me the task — for example, 'summarise the news daily at 8am' — "
            "and I'll set it up."
        )
    try:
        sched_str, desc = _schedule_to_create_job_args(intent)
        job = create_job(task_text, sched_str, db_path=db_path)
    except ValueError as exc:
        return f"I couldn't create that schedule: {exc}"
    next_run = format_ist_short(job.get("next_run_at"))
    job_id = job["id"]
    return (
        f"### 📅 Scheduled job created\n\n"
        f"I'll run **{task_text[:120]}** {desc}.\n\n"
        f"• **Job ID:** `{job_id}`\n"
        f"• **Next run:** {next_run or '—'}\n"
        f"• **Status:** Active\n\n"
        f"Manage it in chat: `pause scheduled job {job_id[:8]}`, "
        f"`resume scheduled job {job_id[:8]}`, or `cancel scheduled job {job_id[:8]}`. "
        f"The job keeps running even with the frontend closed, as long as the backend is up."
    )


# --- Stream C: chat control for jobs and watches (task 6) -------------
_JOB_CONTROL_RE = re.compile(
    r"\b(pause|resume|cancel|delete|stop)\b[^.\n]{0,30}?\b(?:scheduled\s+)?jobs?\b\s*([0-9a-fA-F]{6,})?",
    re.IGNORECASE,
)
_JOB_LIST_RE = re.compile(
    r"\b(?:list|show|what\s+are)\b[^.\n]{0,30}?\b(?:my\s+)?(?:scheduled\s+)?jobs?\b",
    re.IGNORECASE,
)
_WATCH_CONTROL_RE = re.compile(
    r"\b(pause|resume|cancel|delete|stop)\b[^.\n]{0,30}?\bwatch\b\s*(?:for\s+)?(.{0,80})?",
    re.IGNORECASE,
)


def _resolve_job(job_ref: str | None, db_path: Optional[str]) -> dict | None:
    """A job by full id, id prefix (first 8 chars), or None."""
    from .scheduler import get_job, list_jobs

    if job_ref:
        job = get_job(job_ref, db_path=db_path)
        if job:
            return job
        for candidate in list_jobs(db_path=db_path):
            if candidate["id"].startswith(job_ref.lower()):
                return candidate
    return None


def _handle_job_control(instruction: str, db_path: Optional[str]) -> str | None:
    """Pause/resume/cancel/list scheduled jobs from chat. None = not a job-control ask."""
    from .monitoring_scheduler import format_ist_short
    from .scheduler import delete_job, list_jobs, set_job_status

    m = _JOB_LIST_RE.search(instruction or "")
    if m and not _JOB_CONTROL_RE.search(instruction or ""):
        jobs = list_jobs(db_path=db_path)
        if not jobs:
            return "No scheduled jobs yet. Say 'summarise the news daily at 8am' to create one."
        lines = ["### 📅 Scheduled jobs\n"]
        for job in jobs:
            outcome = job.get("last_outcome") or "—"
            lines.append(
                f"• `{job['id'][:8]}` — {str(job['instruction'])[:80]} — "
                f"{job['schedule']} — {job['status']} — next: "
                f"{format_ist_short(job.get('next_run_at')) or '—'} — last run: {outcome}"
            )
        return "\n".join(lines)
    m = _JOB_CONTROL_RE.search(instruction or "")
    if not m:
        return None
    action = m.group(1).lower()
    ref = (m.group(2) or "").strip() or None
    job = _resolve_job(ref, db_path)
    if job is None:
        if ref:
            return f"I couldn't find a scheduled job starting with `{ref}`. Say 'list scheduled jobs' to see them."
        return "Which scheduled job? Say 'list scheduled jobs' and give me its id (the first 8 characters are enough)."
    if action in ("cancel", "delete", "stop"):
        # Cancel keeps the row (status 'cancelled', visible in the
        # list with its last outcome); delete removes it entirely.
        if action == "delete":
            delete_job(job["id"], db_path=db_path)
            return f"Deleted scheduled job `{job['id'][:8]}` ({str(job['instruction'])[:60]})."
        set_job_status(job["id"], "cancelled", db_path=db_path)
        return (
            f"Cancelled scheduled job `{job['id'][:8]}` ({str(job['instruction'])[:60]}). "
            f"It will not run again. Say 'list scheduled jobs' to confirm."
        )
    status = "paused" if action == "pause" else "active"
    set_job_status(job["id"], status, db_path=db_path)
    verb = "Paused" if action == "pause" else "Resumed"
    extra = f" Next run: {format_ist_short(job.get('next_run_at')) or '—'}." if action == "resume" else ""
    return f"{verb} scheduled job `{job['id'][:8]}` ({str(job['instruction'])[:60]}).{extra}"


def _resolve_watch(ref: str | None, chat_id: Optional[str], db_path: Optional[str]):
    """A watch by id/prefix, or by words in its title. Returns (row, ambiguous)."""
    if not ref:
        return None, False
    ref = ref.strip().strip("\"'").strip()
    if not ref:
        return None, False
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at DESC"
        ).fetchall()
    hex_ref = re.sub(r"[^0-9a-fA-F]", "", ref)
    if len(hex_ref) >= 6:
        for row in rows:
            if row["id"].replace("-", "").startswith(hex_ref.lower()):
                return row, False
    wanted = _normalised_product_title(ref)
    matches = [r for r in rows if wanted and wanted in _normalised_product_title(r["title"])]
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        return None, True
    return None, False


def _ref_after_watch(text: str) -> str | None:
    """The old regex's post-"watch" capture, punctuation-stripped.

    "Pause the first watch." used to capture "." here and fail with
    "couldn't find a watch for '.'" — a trailing "." is not a
    referent, so it normalises to None now."""
    m = _WATCH_CONTROL_RE.search(text or "")
    if not m:
        return None
    ref = (m.group(2) or "").strip().strip("\"'").rstrip(".,;:!?").strip()
    return ref or None


def _apply_watch_action(action: str, row: Any, db_path: Optional[str]) -> str:
    """Pause/resume/cancel one watch row, VERIFIED by DB re-read.

    Stream 5 F1: a reply must never claim a pause/resume/cancel
    the DB disagrees with ("Watch Cancelled" while the row stayed
    active). Every branch re-reads the row after the write and
    reports honestly when the state did not change. Already-in-state
    (pause a paused watch) is reported as such, not as a fresh
    change."""
    mission_id = row["id"]
    title = row["title"]
    current = str(row["status"] or "")
    if action in ("cancel", "delete", "stop"):
        with db.get_connection(db_path) as conn:
            conn.execute("DELETE FROM monitoring_events WHERE mission_id=?", (mission_id,))
            conn.execute("DELETE FROM monitoring_missions WHERE id=?", (mission_id,))
        hub.spawn(hub.broadcast_control_state())
        with db.get_connection(db_path) as conn:
            gone = conn.execute(
                "SELECT id FROM monitoring_missions WHERE id=?", (mission_id,)
            ).fetchone() is None
        if not gone:
            return (
                f"I tried to cancel the watch **{title}**, but it is still in the store — "
                "nothing was cancelled. Say 'list watches' to see its current state."
            )
        return f"Cancelled the watch **{title}**. It will not check again."
    now = time.time()
    if action == "pause":
        if current == "paused":
            return f"The watch **{title}** is already paused."
        with db.get_connection(db_path) as conn:
            conn.execute(
                "UPDATE monitoring_missions SET status='paused', next_run_at=NULL WHERE id=?",
                (mission_id,),
            )
        hub.spawn(hub.broadcast_control_state())
        with db.get_connection(db_path) as conn:
            status = conn.execute(
                "SELECT status FROM monitoring_missions WHERE id=?", (mission_id,)
            ).fetchone()
        if not status or status["status"] != "paused":
            return (
                f"I tried to pause the watch **{title}**, but it is still active — "
                "nothing changed. Say 'list watches' to see its current state."
            )
        return f"Paused the watch **{title}**. Say 'resume watch {title}' to start it again."
    if current == "active":
        return f"The watch **{title}** is already active."
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE monitoring_missions SET status='active', next_run_at=? WHERE id=?",
            (now, mission_id),
        )
    hub.spawn(hub.broadcast_control_state())
    with db.get_connection(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    if not status or status["status"] != "active":
        return (
            f"I tried to resume the watch **{title}**, but it is still paused — "
            "nothing changed. Say 'list watches' to see its current state."
        )
    return f"Resumed the watch **{title}** — the next check runs now."


# Watch-marker history + pronoun/interval follow-up routing.
# A turn whose assistant history carries a watch marker ("cancel
# it", "change it to every 45 mins") refers to a real watch the
# handler can act on; without one it is not a monitoring turn.
_WATCH_MARKER_RE = re.compile(r"<!--\s*privagent-watch:\s*[0-9a-fA-F-]{8,}\s*-->")
_PRONOUN_CONTROL_RE = re.compile(
    r"\b(?:pause|resume|cancel|delete|stop)\b[^.\n]{0,40}?"
    r"\b(?:it|that|this|them|that\s+one|this\s+one|the\s+watch|the\s+(?:\w+\s+){0,3}?one)\b",
    re.IGNORECASE,
)
_INTERVAL_FOLLOW_UP_RE = re.compile(
    r"\bchange\b[^.\n]{0,40}?\b(?:it|that|this|them|every)\b"
    r"|\bmake\b[^.\n]{0,20}?\bthat\s+check\b",
    re.IGNORECASE,
)


def _history_has_watch_marker(history: Optional[list[dict[str, Any]]]) -> bool:
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        if "privagent-watch:" in content:
            return True
    return False


def _is_watch_follow_up(text: str, history: Optional[list[dict[str, Any]]]) -> bool:
    if not _history_has_watch_marker(history):
        return False
    if _PRONOUN_CONTROL_RE.search(text or ""):
        return True
    if _INTERVAL_FOLLOW_UP_RE.search(text or ""):
        return True
    if parse_relative_interval_factor(text or "") is not None:
        return True
    return False


def _handle_watch_control(instruction: str, chat_id: Optional[str],
                          db_path: Optional[str], history: Optional[list[dict[str, Any]]] = None) -> str | None:
    """Pause/resume/cancel watches from chat. None = not a watch-control ask.

    Stream 2 follow-up (Stream 5 F2): the old code took its referent
    ONLY from _WATCH_CONTROL_RE's post-"watch" capture, so "Pause
    the first watch" / "Cancel the Samsung watch" captured "." and
    pronoun-only turns ("cancel it") never matched the regex at all.
    The regex is now only a trigger. The REFERENT is resolved from
    the working context (working_context.resolve_watch_target):
    explicit id/prefix, ordinal by creation order, the word before
    "watch" (brand / product / "website"), any other product token,
    pronoun -> most recent watch, or the single watch in the chat.
    Anything else is a concise clarification naming the watches —
    never a guess, and never a success claim without a DB re-read
    (see _apply_watch_action).
    """
    text = instruction or ""
    verb_m = re.search(r"\b(pause|resume|cancel|delete|stop)\b", text, re.IGNORECASE)
    if not verb_m:
        return None
    action = verb_m.group(1).lower()
    # 1. Explicit id/prefix after "watch" — the old path, kept.
    after = _ref_after_watch(text)
    if after:
        row, ambiguous = _resolve_watch(after, chat_id, db_path)
        if ambiguous:
            return "That matches more than one watch — give me more of its name, or its id."
        if row is not None:
            return _apply_watch_action(action, row, db_path)
        # A post-"watch" ref that matches nothing falls through to
        # working-context resolution below (it may be an ordinal or
        # brand the id matcher cannot see) instead of failing here.
    # 2. Working-context resolution — the referent lives in the
    # conversation (ordinal / brand / pronoun), not in the regex.
    ctx = load_working_context(chat_id or "")
    result = resolve_watch_target(ctx, text)
    if result["status"] == "none":
        return (
            "You don't have any watches in this chat yet. Tell me a product "
            "to watch (for example, \"watch the Aurora X9 earbuds\") and I'll set one up."
        )
    if result["status"] == "clarification":
        return result["question"]
    watch = result["watch"]
    # Re-read the live row: the resolver works off the synced blob,
    # the action applies to the DB state.
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (watch["watch_id"],)
        ).fetchone()
    if row is None:
        return (
            f"I couldn't find the watch for **{watch.get('label') or watch.get('title')}** "
            "any more — it may have been deleted already. Say 'list watches' to see what's running."
        )
    return _apply_watch_action(action, row, db_path)


# FIX-B (B4): a stock / availability ask. "out of stock" covers
# "tell me if it goes out of stock"; "back in stock" is the same
# condition from the other side. A bare "in stock" on its own is a
# question, not a condition — it only counts with "back" or "goes".
_STOCK_ALERT_RE = re.compile(
    r"\b(?:out\s+of\s+stock|back\s+in\s+stock|goes\s+out\s+of\s+stock|"
    r"stock\s+alert|availability\s+alert|tell\s+me\s+if\s+it\s+goes\s+out\s+of\s+stock)\b",
    re.IGNORECASE,
)


def _wants_stock_alert(text: str) -> bool:
    """True when the instruction asks to be told about stock /
    availability ("tell me if it goes out of stock")."""
    return bool(_STOCK_ALERT_RE.search(text or ""))


_WATCH_LIST_RE = re.compile(
    r"\b(?:list|show|what\s+are)\b[^.\n]{0,40}?\b(?:my\s+)?(?:product\s+)?watches\b",
    re.IGNORECASE,
)


def _handle_watch_list(instruction: str, chat_id: Optional[str],
                       db_path: Optional[str], history: Optional[list[dict[str, Any]]] = None) -> str | None:
    """List the user's watches, read from the DB (Stream 5 F4, FIX-B M5).

    FIX-B (M5): the old answer read the working context, which is
    synced per-chat and active/paused-only — so "show my watches"
    omitted paused watches and every watch from other chats. "Show
    my watches" now reads ALL rows in monitoring_missions (every
    chat, every status — paused included, status shown), and says
    its scope in the reply. Guard: "show me both watches" can also
    be a wristwatch-shopping ask, so this only fires for "product
    watches" or when this chat already has a watch. None = not a
    list-watches ask."""
    if not _WATCH_LIST_RE.search(instruction or ""):
        return None
    # "show MY watches" is unambiguous (the user's own watches, not
    # wristwatch shopping) — it lists even from a fresh chat with no
    # marker. "show me both watches" stays guarded: without "my" /
    # "product" / a marker it may be shopping, so it falls through.
    if ("product" not in (instruction or "").lower()
            and "my watches" not in (instruction or "").lower()
            and not _history_has_watch_marker(history)):
        return None
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        return (
            "No watches are set up yet — nothing is being monitored right now. "
            "Tell me a product to watch and I'll set one up."
        )
    lines = ["### 👀 Your watches (all chats, including paused)\n"]
    for row in rows:
        interval = row["interval_seconds"] or 0
        if row["trigger_type"] == "cron":
            cadence = "daily"
        elif interval:
            cadence = _describe_interval(interval)
        else:
            cadence = "—"
        title = row["title"] or ""
        label = title.removeprefix("Price Watch: ").removeprefix("New listings: ")
        lines.append(
            f"• `{row['id']}` — **{label}** — {row['status']} — {cadence} — {row['target_url'] or ''}"
        )
    return "\n".join(lines)


# FIX-B (M6): "what did I ask you to watch earlier" — a summary of
# the user's own watch requests. The model layer used to answer this
# from its own guess and confidently denied a request the user HAD
# made ("You never mentioned a Kindle" — D4 of the same chat asked
# for a Kindle watch). This handler answers deterministically from
# (a) every monitoring_missions row in the DB and (b) the user's own
# earlier turns, including asks that never became a watch
# (clarification-pending / unfulfilled). It never asserts what the
# user did or did not ask beyond what those two sources show; when
# they show nothing it says its scope instead of denying the request.
_WATCH_SUMMARY_RE = re.compile(
    r"\bwhat\s+did\s+i\s+ask\s+you\s+to\s+(?:watch|monitor|track)\b"
    r"|\bwhat\s+(?:have\s+i\s+asked|did\s+i\s+ask)\b[^.\n]{0,40}?\b(?:watch|monitor|track)\b"
    r"|\bwhat\s+(?:watches|monitors)\s+did\s+i\s+ask\b"
    r"|\bwhat\s+did\s+i\s+ask\s+you\s+to\s+watch\b",
    re.IGNORECASE,
)
_WATCH_ASK_VERB_RE = re.compile(
    r"\b(?:watch|monitor|track|keep\s+an\s+eye\s+on)\b", re.IGNORECASE
)


def _display_product(phrase: str) -> str:
    """Short display name for a product phrase pulled from a compound
    ask ("new Kindle and also remind me about my 3pm meeting" ->
    "new Kindle"): cut a trailing second ask ("and also remind ...",
    "and tell me ...") and an alert tail, without touching the
    product itself."""
    text = (phrase or "").strip()
    # A sanitizer may already have eaten the "and" ("Fire-Boltt
    # Smartwatch Also") — cut a dangling "also" on its own too.
    text = re.split(r"\s+also\b", text, flags=re.IGNORECASE)[0]
    text = re.split(
        r"\s+and\s+(?:also\s+)?(?:remind|tell|notify|alert|check|watch|monitor|book|call|set|do|find|search)\b",
        text, flags=re.IGNORECASE,
    )[0]
    text = re.split(r"\s*,\s*(?:alert|notify|tell)\b", text, flags=re.IGNORECASE)[0]
    return text.strip().strip("\"'").rstrip(".,;:!?").strip()


def _watch_ask_product(content: str) -> str:
    """The product a user turn asked to watch, or "".

    A turn counts as a watch ask when it carries a monitor verb
    (or matches the monitoring directive pattern) and names a
    product. Garbage / service / retailer candidates are not
    products — they are skipped, never reported as asks."""
    text = content or ""
    if not _WATCH_ASK_VERB_RE.search(text) and not MONITORING_DIRECTIVE_PATTERN.search(text):
        return ""
    product, _url = _extract_monitored_product(text)
    if not product or _is_garbage_product_title(product):
        return ""
    if _is_service_name(product) or _is_retailer_or_place_product(product):
        return ""
    return _display_product(product)


def _handle_watch_summary(instruction: str, chat_id: Optional[str],
                          db_path: Optional[str], history: Optional[list[dict[str, Any]]] = None) -> str | None:
    """Answer "what did I ask you to watch" from the DB + history.

    Set-up watches come from monitoring_missions (all chats, all
    statuses) plus the working context's synced watches; asked-but-
    not-set-up requests come from the user's own earlier turns in
    this chat — the passed history topped up from the messages table
    (the conversational path only passes the last 6 messages, so a
    request four turns back would otherwise be invisible). None =
    not a watch-summary ask."""
    if not _WATCH_SUMMARY_RE.search(instruction or ""):
        return None
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions ORDER BY created_at DESC"
        ).fetchall()
    # Working context (this chat, synced from the same table): any
    # watch it carries that the DB read missed is still shown — the
    # two sources are merged, never played against each other.
    wc_labels: list[str] = []
    try:
        ctx = load_working_context(chat_id or "")
        for w in (ctx.active_watches or []):
            label = (w.get("label") or w.get("title") or "").strip()
            if label:
                wc_labels.append(label)
    except Exception:
        logger.warning("watch summary: working-context load failed", exc_info=True)
    # Earlier user turns: passed history first, then the messages
    # table (same chat, last 30) so older asks are not lost to the
    # 6-message conversational window.
    turns: list[str] = []
    seen: set[str] = set()
    for entry in (history or []):
        if isinstance(entry, dict) and (entry.get("role") or "").lower() == "user":
            content = str(entry.get("content") or entry.get("message") or "")
            if content and content not in seen:
                seen.add(content)
                turns.append(content)
    if chat_id:
        try:
            for m in db.get_recent_messages(chat_id, limit=30, db_path=db_path):
                if (m.get("role") or "").lower() != "user":
                    continue
                content = str(m.get("content") or "")
                if content and content not in seen:
                    seen.add(content)
                    turns.append(content)
        except Exception:
            logger.warning("watch summary: message-table top-up failed", exc_info=True)
    asked: list[str] = []
    for content in turns:
        product = _watch_ask_product(content)
        if product and product.lower() not in {a.lower() for a in asked}:
            asked.append(product)
    # Which asked-about products already have a watch? Match on the
    # normalised title or the stored target param.
    set_up: list[str] = []
    for row in rows:
        title = _normalised_product_title(row["title"] or "")
        try:
            target = _normalised_product_title(
                (json.loads(row["condition_params"] or "{}") or {}).get("target") or ""
            )
        except Exception:
            target = ""
        for product in asked:
            norm = _normalised_product_title(f"Price Watch: {product}")
            if norm and (norm == title or norm == target or _normalised_product_title(product) == target):
                if product not in set_up:
                    set_up.append(product)
    pending = [a for a in asked if a not in set_up]
    lines: list[str] = []
    if rows:
        lines.append("### 👀 Watches set up (all chats, including paused)\n")
        for row in rows:
            interval = row["interval_seconds"] or 0
            cadence = "daily" if row["trigger_type"] == "cron" else (
                _describe_interval(interval) if interval else "—")
            label = (row["title"] or "").removeprefix("Price Watch: ").removeprefix("New listings: ")
            lines.append(
                f"• `{row['id']}` — **{label}** — {row['status']} — {cadence}"
            )
    for label in wc_labels:
        if not any(label.lower() in line.lower() for line in lines):
            lines.append(f"• **{label}** — listed in this chat's working context")
    if pending:
        lines.append("\n### 📝 You also asked about — no watch set up yet\n")
        for product in pending:
            lines.append(
                f"• **{product}** — you asked me to watch it, but no watch is set up "
                "for it yet. Tell me the missing detail and I'll set it up."
            )
    if not rows and not pending:
        return (
            "I don't see any watch request in this conversation yet, and no "
            "watches are set up. Tell me a product to watch and I'll set one up."
        )
    if rows and not pending and not asked:
        lines.append(
            "\nThat's everything set up. (This summary covers all your chats; "
            "I only looked at watches and watch requests, not other tasks.)"
        )
    return "\n".join(lines)


def _find_watch_by_product(product_title: str, chat_id: Optional[str], db_path: Optional[str]):
    """Most recent watch for this product in this chat, any status —
    an active one first, else the most recent paused one. A stock
    condition can be added to a paused watch too; the follow-up
    handler uses this instead of the active-only lookup."""
    try:
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM monitoring_missions
                   WHERE conversation_id = ?
                   ORDER BY created_at DESC""",
                (chat_id or "default",),
            ).fetchall()
    except Exception:
        logger.warning("watch-by-product lookup failed for %r", product_title, exc_info=True)
        return None
    wanted = _normalised_product_title(f"Price Watch: {product_title}")
    for row in rows:
        if _normalised_product_title(row["title"]) == wanted and row["status"] == "active":
            return row
    for row in rows:
        if _normalised_product_title(row["title"]) == wanted:
            return row
    return None


def _handle_stock_follow_up(instruction: str, chat_id: Optional[str],
                            db_path: Optional[str], history: Optional[list[dict[str, Any]]] = None) -> str | None:
    """FIX-B (B4): "also tell me if it goes out of stock" — add a
    stock condition to the watch this chat is already setting up.

    Only a PURE stock follow-up reaches here (no product, threshold
    or URL named in this turn): a turn that names a product goes
    through creation, which stores the condition at creation time.
    The flag is stored on the watch row (``condition_params`` ->
    ``stock_alert: true``) and verified by DB re-read before the
    reply claims anything. With no watch to attach it to, this asks
    which product to watch — it never creates a watch on its own.
    None = not a pure stock follow-up."""
    if not _wants_stock_alert(instruction or ""):
        return None
    if _PRODUCT_RE.search(instruction or "") or _product_phrase_from_text(instruction or ""):
        return None
    if parse_below_price(instruction or "") is not None or parse_percent_drop(instruction or "") is not None:
        return None
    if re.search(r"https?://[^\s\"'<>)]+", instruction or ""):
        return None
    # Resolve the watch: the product named earlier in this chat, else
    # the single watch running here. Several watches and no referent
    # is ambiguous — ask, never guess.
    row = None
    product, _url = _extract_monitored_product("", history=history)
    if product and not _is_garbage_product_title(product):
        row = _find_watch_by_product(product, chat_id, db_path)
    if row is None:
        db.init_db(db_path)
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM monitoring_missions
                   WHERE conversation_id = ? AND status IN ('active', 'paused')
                   ORDER BY created_at DESC""",
                (chat_id or "default",),
            ).fetchall()
        if len(rows) == 1:
            row = rows[0]
            product = (row["title"] or "").removeprefix("Price Watch: ")
        elif len(rows) > 1:
            names = ", ".join(
                f"**{(r['title'] or '').removeprefix('Price Watch: ')}**" for r in rows
            )
            return (
                "You have more than one watch running here — tell me which one "
                f"should also flag out-of-stock ({names}). Nothing was changed."
            )
    if row is None:
        return (
            "I don't have a watch running to add the out-of-stock alert to yet — "
            "tell me which product to watch first and I'll set it up with the stock alert included."
        )
    title = (row["title"] or "").removeprefix("Price Watch: ")
    try:
        params = json.loads(row["condition_params"] or "{}")
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}
    if params.get("stock_alert") is True:
        return (
            f"The watch for **{title}** already flags out-of-stock — nothing changed.\n\n"
            f"<!-- privagent-watch: {row['id']} -->"
        )
    params["stock_alert"] = True
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE monitoring_missions SET condition_params=? WHERE id=?",
            (json.dumps(params), row["id"]),
        )
        conn.commit()
    hub.spawn(hub.broadcast_control_state())
    with db.get_connection(db_path) as conn:
        reread = conn.execute(
            "SELECT condition_params FROM monitoring_missions WHERE id=?", (row["id"],)
        ).fetchone()
    try:
        raw_params = reread["condition_params"] if reread is not None else None
        stored = json.loads(raw_params or "{}")
    except Exception:
        stored = {}
    if not isinstance(stored, dict) or stored.get("stock_alert") is not True:
        return (
            f"I tried to add the out-of-stock alert to the watch for **{title}**, "
            "but it did not save — nothing changed. Say 'list watches' to see its current state."
        )
    interval = row["interval_seconds"] or 300
    return (
        f"Done — the watch for **{title}** will now also alert you if it goes out of stock, "
        f"in addition to its price check {_describe_interval(interval)}.\n\n"
        f"• **Watch ID:** `{row['id']}`\n"
        f"• **Status:** {row['status']}\n\n"
        f"<!-- privagent-watch: {row['id']} -->"
    )


def _handle_relative_interval(instruction: str, chat_id: Optional[str],
                              db_path: Optional[str]) -> str | None:
    """A relative cadence change ("make that check twice as often").

    Stream 5 D10: the model used to claim "Frequency Doubled — Now
    Every 30 Minutes" while the DB row stayed at 3600 s. The factor
    (0.5 / 2.0) applies to the RESOLVED watch's stored interval, the
    row is updated, and the new value is verified by DB re-read
    before the reply claims anything. None = no relative change
    named. Absolute interval changes ("change it to every 15
    minutes") go through the normal update path, not here."""
    factor = parse_relative_interval_factor(instruction or "")
    if factor is None:
        return None
    ctx = load_working_context(chat_id or "")
    result = resolve_watch_target(ctx, instruction or "")
    if result["status"] == "none":
        return "I don't have a watch in this chat to change — tell me which product to watch first."
    if result["status"] != "resolved":
        return result["question"]
    watch = result["watch"]
    current = int(watch.get("interval_seconds") or 300)
    new_interval = max(60, min(604800, int(round(current * factor))))
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE monitoring_missions SET interval_seconds=?, next_run_at=? WHERE id=?",
            (new_interval, time.time(), watch["watch_id"]),
        )
    hub.spawn(hub.broadcast_control_state())
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT interval_seconds FROM monitoring_missions WHERE id=?",
            (watch["watch_id"],),
        ).fetchone()
    if not row or row["interval_seconds"] != new_interval:
        return (
            f"I tried to change the watch for **{watch.get('label') or watch.get('title')}**, "
            "but the interval did not update — nothing changed. "
            "Say 'list watches' to see its current state."
        )
    return (
        f"Updated the watch for **{watch.get('label') or watch.get('title')}** — "
        f"it was checking {_describe_interval(current)}, now checking {_describe_interval(new_interval)}."
    )


_BELOW_PRICE_RE = re.compile(
    r"(?:\b(?:below|under|less\s+than|at\s+most)\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
    r"|\b(?:reaches?|drops?|falls?|goes?|gets?|dips?)\s+(?:below|under|to|under)\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
    r"|<\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?))",
    re.IGNORECASE,
)


def parse_below_price(instruction: str) -> float | None:
    """Parse 'alert me if the price falls below ₹5000' into 5000.0.

    Also accepts the forms users actually type: 'reaches <5000',
    'drops to ₹5000', 'falls below 5000', '<₹5000'. Returns None
    when no below/under threshold is named. The caller (a
    monitoring directive) has already established the intent, so
    this only extracts the number."""
    m = _BELOW_PRICE_RE.search(instruction or "")
    if not m:
        return None
    raw = next((g for g in m.groups() if g is not None), None)
    if raw is None:
        return None
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


_PERCENT_WORD_NUMBERS = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))

# A %-drop needs a drop construction ADJACENT to the number — a drop
# word before it ("drops by 10%", "down 10%") or after it ("10% off",
# "10 percent discount"). A bare number + % ("100% sure", "50%
# chance") is not a threshold: it used to parse as min_percent 100
# / 50, and a 100% watch can never fire. Two patterns because the
# number is in a different group in each order.
_PERCENT_DROP_BEFORE_RE = re.compile(
    r"(?:drops?|falls?|down|discount(?:ed)?|cut)\s+(?:by\s+)?"
    rf"(\d+(?:\.\d{{1,2}})?|{_PERCENT_WORD_NUMBERS})\s*(?:%|percent|per\s*cent)",
    re.IGNORECASE,
)
_PERCENT_DROP_AFTER_RE = re.compile(
    rf"(\d+(?:\.\d{{1,2}})?|{_PERCENT_WORD_NUMBERS})\s*(?:%|percent|per\s*cent)"
    r"\s*(?:off|drops?|down|discount|less|cheaper)",
    re.IGNORECASE,
)


def parse_percent_drop(instruction: str) -> float | None:
    """Parse 'live monitor iPhone 17 when it 10% off' into 10.0.

    Accepts the forms users actually type: '10% off', 'drops by
    10%', '10 percent discount', 'ten percent off', 'a 12.5% drop'.
    Returns None when no percent is named, when the number is
    meaningless as a drop (0% or over 100%), or when the % is not
    part of a drop construction ('100% sure', '50% chance'). The
    caller (a monitoring directive) has already established the
    intent, so this only extracts the number.

    Residual limit, by design: '15% off right now' IS a drop
    construction used as a statement of the CURRENT discount, which
    no regex can distinguish from a threshold — it parses as 15.
    """
    for pattern in (_PERCENT_DROP_BEFORE_RE, _PERCENT_DROP_AFTER_RE):
        m = pattern.search(instruction or "")
        if not m:
            continue
        raw = m.group(1)
        if raw is None:
            continue
        lowered = raw.lower()
        if lowered in _WORD_NUMBERS:
            value = float(_WORD_NUMBERS[lowered])
        else:
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue
        if 0 < value <= 100:
            return value
    return None


def _percent_from_history(history: Optional[list[dict[str, Any]]]) -> float | None:
    """Most recent %-drop in the last few history turns.

    Mirrors _threshold_from_history: a follow-up that only changes
    the cadence ("check every 5 minutes") names no percent — the one
    from the ask it follows ("when it 10% off") still applies, or
    the watch would be rebuilt with min_percent 0 and the threshold
    silently wiped. Only the last few turns are scanned, so an old
    percent for a different product in the same chat cannot leak in.
    """
    for entry in reversed((history or [])[-4:]):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        value = parse_percent_drop(content)
        if value is not None:
            return value
    return None


def _normalised_product_title(title: str) -> str:
    """Product titles compare ignoring case and a leading brand:
    'Apple iPhone 17' is the same product as 'iPhone 17'. Only a
    LEADING brand is stripped, so 'Smart Band 10' and 'Smart Band'
    stay different products, and 'iPhone 17 Pro' stays different
    from 'iPhone 17'."""
    t = (title or "").strip().lower()
    t = t.removeprefix("price watch:").strip()
    m = re.match(rf"(?:{_MONITORED_BRANDS})\b\s*", t)
    if m:
        t = t[m.end():].strip()
    return re.sub(r"\s+", " ", t)


def _find_active_watch(product_title: str, chat_id: Optional[str], db_path: Optional[str]):
    """Most recent active watch for this product in this chat,
    matched on the normalised title — a re-ask that adds or drops
    the brand ('Apple iPhone 17' after 'iPhone 17') is the same
    watch, not a second one."""
    try:
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM monitoring_missions
                   WHERE conversation_id = ? AND status = 'active'
                   ORDER BY created_at DESC""",
                (chat_id or "default",),
            ).fetchall()
    except Exception:
        logger.warning("active-watch lookup failed for %r", product_title, exc_info=True)
        return None
    wanted = _normalised_product_title(f"Price Watch: {product_title}")
    for row in rows:
        if _normalised_product_title(row["title"]) == wanted:
            return row
    return None


def _stock_alert_from_existing_watch(
    product_title: str, chat_id: Optional[str], db_path: Optional[str]
) -> bool:
    """True when the running watch for this product already carries a
    stock condition — a later cadence/threshold change must not wipe
    it (upsert overwrites ``condition_params`` wholesale)."""
    row = _find_watch_by_product(product_title, chat_id, db_path)
    if row is None:
        return False
    try:
        params = json.loads(row["condition_params"] or "{}")
    except Exception:
        return False
    return isinstance(params, dict) and params.get("stock_alert") is True


def _is_pure_follow_up(instruction: str) -> bool:
    """True when the turn names no product of its own — it only
    changes the cadence, adds a stock flag, or repeats the check
    ("change it to every 45 mins", "check every 5 minutes"). Its
    product, if any, comes from history, never from this turn."""
    text = instruction or ""
    if re.search(r"https?://[^\s\"'<>)]+", text):
        return False
    if _PRODUCT_RE.search(text) or _product_phrase_from_text(text):
        return False
    return bool(
        _INTERVAL_RE.search(text)
        or parse_relative_interval_factor(text) is not None
        or _wants_stock_alert(text)
        or re.search(r"\b(?:change|update|modify)\b[^.\n]{0,40}?\b(?:it|that|this|them|every)\b", text, re.IGNORECASE)
    )


def _thresholds_from_existing_watch(
    product_title: str, chat_id: Optional[str], db_path: Optional[str]
) -> tuple[float | None, float | None]:
    """(below, percent) of the active watch for this product, if any.

    Covers the case _threshold_from_history / _percent_from_history
    cannot: a cadence follow-up that NAMES the product again ("live
    monitor Apple iPhone 17, check every 5 minutes") — the history
    guards only run when no product is named, so without this the
    watch would be rebuilt with min_percent 0, silently wiping a
    ₹ threshold (below_price) or a % threshold (price_drop). The
    lookup is keyed by the same normalised title the duplicate
    check uses, so a different product never inherits. Returns
    (None, None) when there is no active threshold watch for this
    product, or the stored value is not valid.
    """
    row = _find_active_watch(product_title, chat_id, db_path)
    if row is None:
        return None, None
    try:
        params = json.loads(row["condition_params"] or "{}")
    except Exception:
        return None, None
    if row["condition_type"] == "below_price":
        value = params.get("threshold")
        if isinstance(value, (int, float)) and value > 0:
            return float(value), None
    elif row["condition_type"] == "price_drop":
        value = params.get("min_percent")
        if isinstance(value, (int, float)) and 0 <= value <= 100:
            return None, float(value)
    return None, None


def _as_float(value: Any) -> float | None:
    """Coerce a stored threshold/percent to float, or None.

    Stream 2 (Failure E format-string fix): ``condition_params`` is
    an untyped JSON blob — the missions API stores whatever the
    caller sent, so a threshold can arrive as the STRING "5000".
    Formatting a string with ``:g`` raises ``ValueError: Unknown
    format code 'g' for object of type 'str'`` and a string never
    equals the float the handler parses, so the same-settings check
    also misfires. Every read of a stored numeric goes through here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _percent_threshold_text(percent: float | None) -> str:
    """Reply fragment for a %-drop threshold: 0 means any drop, not
    the nonsense 'drops 0% or more' a bare-drop watch used to show."""
    percent = _as_float(percent)
    if percent is None:
        return ""
    if percent == 0:
        return ", alerting on any price drop"
    return f", alerting when the price drops {percent:g}% or more"


# Brands a monitored product can belong to. The extractor matches
# "<brand> <model...>" in the instruction first, then in the recent
# history — so a fitness band, a phone or a laptop all work, and the
# product is never a hardcoded fallback model.
_MONITORED_BRANDS = (
    "samsung|amazfit|xiaomi|fitbit|garmin|apple|huawei|noise|boat|fire-boltt|"
    "realme|oneplus|oppo|vivo|google|sony|lenovo|honor|zebronics|fastrack|"
    "logitech|hp|dell|asus|acer|razer|bose|jbl|nothing|iqoo|poco|motorola"
)
# Brand + up to three model tokens ("Galaxy Fit3", "Smart Band 10",
# "Band 7"). Trailing stop-words are stripped after the match so a
# sentence like "Samsung Galaxy Fit3 is the best" yields just the model.
# A model token that is a bare number followed by "%" is a percent
# ("Apple iPhone 17 10% off"), not part of the model — the lookahead
# refuses it, or the title becomes "Apple iPhone 17 10".
_PRODUCT_RE = re.compile(
    rf"\b(?:{_MONITORED_BRANDS})((?:\s+(?![\d,]+(?:\.\d{{1,2}})?%)[A-Za-z0-9][\w\-]*){{1,3}})",
    re.IGNORECASE,
)
_PRODUCT_STOPWORDS = {
    "is", "the", "a", "an", "best", "price", "on", "at", "and", "if", "when",
    "for", "with", "under", "in", "of", "to", "from", "by", "vs", "or", "it",
    "this", "that", "please", "now", "just", "me", "my", "i", "want", "like",
    "also",
}


def _clean_product_match(text: str) -> str:
    """Trim a brand+model match to the model name (drop stop-words)."""
    tokens = [t for t in text.split() if t.lower() not in _PRODUCT_STOPWORDS]
    return _truncate_title(" ".join(tokens).strip(" -"))


# App / service names that look like brand+model to _PRODUCT_RE
# ("Google Calendar", "Google Drive") but are never a product to
# watch. The agent's own greeting lists connected apps, so the
# history scan used to pick "Google Calendar" out of it and watch a
# calendar listing for a bike stand the user actually asked about.
_SERVICE_NAMES = {
    "google calendar", "google drive", "google docs", "google sheets",
    "google slides", "google meet", "google maps", "google photos",
    "google keep", "gmail", "outlook", "microsoft teams", "slack",
    "github", "google chrome", "google pay",
}


def _is_service_name(title: str) -> bool:
    """True when a cleaned brand+model match is really an app/service.
    Exact match plus a service name followed by a generic suffix
    (\"Google Calendar app\") — the exact-only check missed those."""
    t = (title or "").strip().lower()
    candidates = {t, t[:-1]} if t.endswith("s") else {t}
    return any(
        c in _SERVICE_NAMES or any(c.startswith(name + " ") for name in _SERVICE_NAMES)
        for c in candidates
    )


# Model tokens that can never name a product (FIX-B M4): a retailer,
# a storefront, a country or a page word. The assistant's own prose
# ("the Amazon.in / Flipkart / Croma / Sony India page for the
# XM6") used to yield the brand+model match "Sony India" — "India"
# is a model token to _PRODUCT_RE — and the follow-up minted a watch
# for a retailer name. A match whose model tokens are ALL in this
# set is not a product, wherever it appears (instruction, user
# history, or assistant history).
_NON_PRODUCT_MODEL_TOKENS = {
    "india", "amazon", "flipkart", "croma", "store", "shop", "online",
    "website", "webpage", "page", "site", "app", "in",
}


def _is_retailer_or_place_product(title: str) -> bool:
    """True when a brand+model candidate names a retailer / place /
    page instead of a product ("Sony India", "Sony India Page").

    The first token is the brand; the rest are the model. Only a
    match with NO real model token is rejected, so "Sony WH-1000XM6"
    and "Apple iPhone 17" (real model tokens) pass, while a
    retailer/country/page word alone does not."""
    tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    if not tokens:
        return False
    brands = set(_MONITORED_BRANDS.split("|"))
    rest = tokens[1:] if tokens[0] in brands else tokens
    if not rest:
        return False
    return all(t in _NON_PRODUCT_MODEL_TOKENS for t in rest)


def _truncate_title(text: str, limit: int = 80) -> str:
    """Shorten a product title to at most `limit` chars without cutting
    a word in half: cut at the last space inside the limit. A title cut
    mid-word ("... Stand (Floo") reads broken in the watch card and in
    every reply that names the product, and it leaks into the search
    URL built from the title too."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        return text[:limit]
    return text[:cut]


def _product_phrase_from_text(text: str) -> str:
    """Product phrase named after a monitor/track/watch/check verb.

    "monitor this, Grandbiker Rear Wheel Paddock Stand ..." names a
    product with no brand in _MONITORED_BRANDS, so brand+model matching
    misses it entirely. Take the phrase after the verb, cut the
    threshold / cadence / alert tail off, and require at least two
    real words. A pronoun-led phrase ("check its price ...") is NOT a
    product — it resolves from history instead.
    """
    if not text:
        return ""
    m = re.search(
        # The pronoun after the verb must be a whole word (\b):
        # without it "check its price" matched "it" inside "its" and
        # the phrase became the nonsense "s price and just alert me".
        # FIX-B (M3): "keep an eye on" is a monitor verb too — a
        # non-brand product named after it ("keep an eye on the
        # Aurora X9 earbuds") must extract, not fall through.
        r"\b(?:keep\s+an\s+eye\s+on|keep\s+a?\s*watch\s+on|monitor|track|watch|check)\s+(?:(?:this|that|it|the\s+product|this\s+product)\b)?\s*,?\s*(.+)$",
        text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    phrase = m.group(1).strip()
    # A bare cadence is not a product ("check every 2 hours", "check
    # every day"): the phrase after the verb is only the interval, so
    # strip a LEADING cadence and let the rest resolve from history —
    # otherwise the follow-up becomes a phantom watch titled
    # "Every 2 Hours" / "Every Day" and the real watch never changes.
    phrase = re.sub(
        # FIX-B (M3): "evry" is the common misspelling of "every".
        r"^(?:every|evry)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b\s*,?\s*",
        "",
        phrase,
        flags=re.IGNORECASE,
    ).strip()
    phrase = re.split(
        # The alert tail may follow a comma ("..., alert me") as well
        # as "and" — without the comma split the title became
        # "Grandbiker Rear Wheel Paddock Stand, Alert Me". The number
        # after "every" may be a word ("every five minutes"). A daily
        # / morning cadence cuts the title too ("watch the stand every
        # day" must not become a product called "Stand Every Day").
        # A percent token cuts it too — no product name contains "%",
        # so "iphone 17 10% off" must stay "iphone 17", not become
        # "Iphone 17 10% Off" in the watch card and every alert.
        # No word boundary after the %: "%" is a non-word character,
        # so \b never matches after it ("10% off" would not cut).
        r",\s*(?:alert|notify|tell|ping)\b"
        r"|\s+if\s+the\s+price\b|\s+if\s+it\b|\s+when\b|\s+and\s+(?:alert|notify|tell|ping)\b"
        r"|\s+and\s+also\s+(?:alert|notify|tell|ping|remind|check|watch|monitor|book|call|set|do|find|search)\b"
        r"|\s+(?:every|evry)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b"
        r"|\s+(?:\d+(?:\.\d{1,2})?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*(?:%|percent|per\s*cent)",
        phrase,
        flags=re.IGNORECASE,
    )[0]
    phrase = phrase.strip().strip("\"'").rstrip(".,;:!?").strip()
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE).strip()
    phrase = re.sub(r"^price\s+of\s+", "", phrase, flags=re.IGNORECASE).strip()
    # "the price of the X" leaves a second "the" behind after the
    # "price of" strip — strip the article once more.
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE).strip()
    words = phrase.split()
    if len(words) < 2:
        return ""
    # A product phrase never starts with a preposition ("keep a watch
    # on product prices" is the greeting's capability list, not a
    # product — without this the greeting alone counts as a product).
    # "out" covers the phrasal verbs ("check out the new deals",
    # "watch out for ..."), which name no product.
    if words[0].lower() in {"on", "for", "over", "about", "of", "at", "to", "in", "with", "from", "by", "out"}:
        return ""
    # Possessive pronouns name someone's account, not a product
    # ("watch my email", "check your inbox" must not become watches
    # for "my email" / "your inbox").
    if words[0].lower() in {"its", "it", "this", "that", "them", "their", "they", "he", "she", "my", "your", "his", "her", "our"}:
        return ""
    if _is_service_name(phrase):
        return ""
    return _truncate_title(phrase)


_FIND_VERB_RE = re.compile(r"\b(?:find|search|look\s+for|list|show\s+me|get\s+me)\b", re.IGNORECASE)
_TRACK_VERB_RE = re.compile(r"\b(?:track|watch|monitor|alert|notify)\b", re.IGNORECASE)


def _is_compound_find_and_track(text: str) -> bool:
    """True when the instruction asks to find/search something NOW
    and also to track it — a compound ask, not a pure tracking ask.

    The find/search verb must come before the tracking verb, and must
    not sit inside a "when you find ..." / "if you find ..." clause
    (that phrasing is a future-discovery tracking ask, not a
    search-now ask). A compound ask goes to the browser task, which
    searches now and offers the watch; routing it to the handler
    would create a watch without ever showing results."""
    if not text:
        return False
    fm = _FIND_VERB_RE.search(text)
    tm = _TRACK_VERB_RE.search(text)
    if not fm or not tm:
        return False
    if fm.start() >= tm.start():
        return False
    window = text[max(0, fm.start() - 15):fm.start()].lower()
    if re.search(r"\b(?:when|if|once|whenever)\b", window):
        return False
    return True


def is_continuous_monitoring_directive(text: str, history: Optional[list[dict[str, Any]]] = None) -> bool:
    """Check if the user instruction requests continuous background monitoring."""
    if not text:
        return False
    # A compound find-now + track ask is not a pure tracking ask.
    if _is_compound_find_and_track(text):
        return False
    # If it is strictly an email scan mission, let email intelligence handle it
    if re.search(r"\b(?:e-?mails?|inbox|mailbox)\b", text, re.IGNORECASE) and not re.search(r"\b(?:price|product|store|amazon|flipkart|croma)\b", text, re.IGNORECASE):
        return False
    if MONITORING_DIRECTIVE_PATTERN.search(text):
        return True
    # Stream C: a monitor/track/watch verb with a pasted URL is a watch
    # ask even when no price/cadence word matches the patterns above —
    # the {0,60} windows in the pattern cannot span a dotted URL
    # ([^.\n] excludes '.'), so "monitor http://127.0.0.1:8765/notes
    # every 1 minute" never matched and fell through to a browser task
    # (EXTENSION_DISCONNECTED with no extension). The handler validates
    # the URL with the unified gate and asks when it cannot use it.
    if re.search(r"\b(?:monitor|track|watch)\b[^.\n]{0,40}?https?://\S+", text, re.IGNORECASE):
        return True
    # Stream C (R-05/R-06): NL scheduling + chat control for jobs and
    # watches route here too — the handler dispatches them before the
    # monitor logic. The monitor pattern above wins, so a price watch
    # never becomes a job. Job control needs the word "scheduled" or
    # an id, so a bare "cancel the job" (a browser task) does not get
    # hijacked into the scheduler.
    if is_schedule_directive(text):
        return True
    if _JOB_LIST_RE.search(text):
        return True
    if _JOB_CONTROL_RE.search(text) and ("scheduled" in text.lower() or re.search(r"\b[0-9a-fA-F]{6,}\b", text)):
        return True
    if _WATCH_CONTROL_RE.search(text):
        return True
    # Stream 2 follow-up (Stream 5 F1/F2): pronoun-only watch
    # control ("cancel it", "Resume it."), interval-change follow-ups
    # ("change it to every 45 mins") and relative changes ("make
    # that check twice as often") never match the regex, so they fell
    # through to the model layer, which fabricated the outcome in
    # prose while the DB stayed unchanged. Route them to the
    # deterministic handler — but ONLY when this chat already has a
    # watch (a watch marker in the recent history): a bare "cancel
    # it" with no watch is not a monitoring turn.
    if _is_watch_follow_up(text, history):
        return True
    # A list-watches ask is answered from the DB by the handler.
    # Guard: "show me both watches" can be wristwatch shopping, so
    # this needs the word "product" or a watch in this chat.
    if _WATCH_LIST_RE.search(text) and (
        "product" in text.lower() or "my watches" in text.lower()
        or _history_has_watch_marker(history)
    ):
        return True
    # FIX-B (M6): "what did I ask you to watch earlier" is answered
    # deterministically from the DB + history by the handler — never
    # by the model layer, which denied a request the user had made.
    if _WATCH_SUMMARY_RE.search(text):
        return True
    # FIX-B (B4): a stock follow-up ("also tell me if it goes out of
    # stock") is a watch update, not a new conversational turn — but
    # only when this chat already has something to attach it to (a
    # watch marker or a monitored product in the history). With no
    # watch context it stays conversational, so a bare stock question
    # ("is it in stock?") is not hijacked into the handler.
    if _STOCK_ALERT_RE.search(text) and (
        _history_has_watch_marker(history) or _history_has_monitored_product(history or [])
    ):
        return True
    # Fix 5: a bare "yes" answering the alert offer the agent just
    # made ("Want me to keep an eye on it?") is an acceptance, not a
    # new conversational turn — route it to the handler so a real
    # watch is created for the product in the history, instead of the
    # LLM merely promising one. Requires the offer in the last
    # assistant message and a real product (brand+model or store URL)
    # in the history; otherwise a bare "yes" stays conversational.
    if history and _is_bare_affirmative(text):
        last_assistant = next(
            (m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "assistant"),
            None,
        )
        content = str((last_assistant or {}).get("content") or (last_assistant or {}).get("message") or "")
        if ALERT_OFFER_MARKER in content and _history_has_monitored_product(history):
            return True
    return False


# A whole instruction that is only a go-ahead ("yes", "sure") — used
# for accepting the alert offer. Deliberately narrower than a general
# continuation: "retry"/"continue" mean "redo the failed task", not
# "yes, watch it".
_BARE_AFFIRMATIVES = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "please do",
    "go ahead", "do it", "yes please", "yeah please", "sure go ahead",
    "yes go ahead", "yeah go ahead",
}


def _is_bare_affirmative(text: str) -> bool:
    """True when the instruction is only an affirmative go-ahead."""
    t = (text or "").strip().lower()
    t = re.sub(r"[.!?,\s]+$", "", t).strip()
    return t in _BARE_AFFIRMATIVES


def _history_has_monitored_product(history: list[dict[str, Any]]) -> bool:
    """True when the recent history names a real product to watch."""
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        prod_m = _PRODUCT_RE.search(content)
        if prod_m and not _is_service_name(_clean_product_match(prod_m.group(0))) and not _is_retailer_or_place_product(_clean_product_match(prod_m.group(0))):
            return True
        if _product_phrase_from_text(content):
            return True
        if re.search(r"https?://[^\s\"'<>)]*(?:amazon\.|flipkart\.|croma\.)", content, re.IGNORECASE):
            return True
        # A listing/search page is watchable too: a bare "yes" to the
        # alert offer after a listing search must create a
        # new_listings watch, not stay conversational. Any pasted
        # http(s) URL counts (Stream A: no hardcoded domain list —
        # the SSRF guard at creation time decides what is watchable).
        for m in _URL_RE.finditer(content):
            if m.group(0).lower().startswith(("http://", "https://")):
                return True
    return False


# Bare pronouns that cannot title a watch ("Track them and alert me"
# -> "them"). When the extracted title is one of these, derive it
# from the watch URL instead.
_PRONOUN_TITLES = {"them", "it", "these", "those", "this", "that", "one", "ones", "me"}


def _title_from_listing_url(url: str) -> str:
    """Human title for a listing URL: the last meaningful path segment,
    de-slugified (e.g. software-development-internship -> 'software
    development internship'), else the hostname. ID-like segments
    (a Flipkart '/p/itm123' product ID) are skipped — they are not a
    title."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    segments = [s for s in (parts.path or "").split("/") if s and s not in ("internships", "jobs", "search")]
    for slug in reversed(segments):
        slug = re.sub(r"[?#].*$", "", slug)
        if re.fullmatch(r"(?:p|itm|pid|dp|product)?\d+", slug):
            continue
        if len(slug) < 3:
            continue
        title = re.sub(r"[-_]+", " ", slug).strip()
        if title:
            return title[:80]
    host = (parts.hostname or "").removeprefix("www.")
    return host[:80]


# The alert offer appended after a product suggestion / comparison
# finishes, and the plain note for a conversational suggestion whose
# prices no browsing verified. ALERT_OFFER_MARKER is the exact
# sentence the offer-acceptance detector looks for — keep in sync.
ALERT_OFFER_MARKER = "Want me to keep an eye on it?"
ALERT_OFFER_TEXT = (
    "Want me to keep an eye on it? I can alert you right here in this conversation "
    "if the price drops — just say \"alert me if it drops below ₹X\" — or send you a "
    "daily price update every morning at 9:00 AM IST (\"every day at 9am tell me the price\")."
)
UNVERIFIED_SUGGESTION_NOTE = (
    "Note: I didn't check the stores for this — these prices come from my general "
    "knowledge, not verified live listings, so treat them as a starting point, not a quote."
)

_SUGGESTION_RE = re.compile(
    r"\b(?:suggest|recommend|best|cheapest|cheaper|compare|comparison|versus|vs\.?|"
    r"price\s+of|how\s+much|budget|cheapest)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(?:₹|\$|Rs\.?\s?)\s?[\d,]+")
_STORE_LINK_RE = re.compile(
    r"https?://[^\s\"'<>)]*(?:amazon\.|flipkart\.|croma\.)", re.IGNORECASE
)


def needs_unverified_suggestion_note(instruction: str, answer: str) -> bool:
    """True when a conversational (no-browsing) answer states product
    prices the agent never verified: the user asked about a product
    (suggestion / comparison / brand+model) and the answer states a
    price or a store link. Live case: the fitness-band suggestion
    answered from LLM knowledge with ₹3,899 and a Flipkart link no
    browsing had ever checked."""
    if not instruction or not answer:
        return False
    if not (_SUGGESTION_RE.search(instruction) or _PRODUCT_RE.search(instruction)):
        return False
    if not (_PRICE_RE.search(answer) or _STORE_LINK_RE.search(answer)):
        return False
    return True


def _is_garbage_product_title(title: str) -> bool:
    """True when a candidate "product" is really the instruction's
    tail misread as a name: a pronoun-led fragment ("This For Every
    5 Mins", "That Watch") or a bare cadence ("Twice As Often",
    "Every 5 Mins"). No real product name contains those words, so
    this is a safe repair trigger — never a product filter."""
    words = set(re.findall(r"[a-z]+", (title or "").lower()))
    return bool(words & {"this", "that", "it", "every", "twice", "half"})


def _extract_monitored_product(instruction: str, history: Optional[list[dict[str, Any]]] = None,
                               objective_product: str | None = None,
                               objective_url: str | None = None) -> tuple[str, str]:
    """Extract product title and best available URL from instruction or recent history.

    Returns ("", "") when neither a product nor a store URL can be
    found anywhere — the caller must ask the user instead of
    inventing a watch. The old fallback watched the Flipkart homepage
    for "your last suggestion", and the detector reads the first ₹
    price on the page, so that watch tracked a random banner price.

    ``objective_product`` / ``objective_url`` are the structured
    Objective's (Stream 1) product / target URL when the handler has
    one: a pronoun-led or cadence-led regex candidate ("This For
    Every 5 Mins") is garbage, and the objective's product replaces
    it instead of minting a watch for the garbage name.
    """
    candidate_title = ""
    # A brand + model named in the instruction wins ("I want like the
    # Samsung Galaxy Fit3", "monitor the Xiaomi Smart Band 10").
    prod_m = _PRODUCT_RE.search(instruction or "")
    if prod_m:
        cleaned_match = _clean_product_match(prod_m.group(0))
        if not _is_service_name(cleaned_match) and not _is_retailer_or_place_product(cleaned_match):
            candidate_title = cleaned_match
    # The product named after the verb ("monitor this, Grandbiker Rear
    # Wheel Paddock Stand ...") — no brand in _MONITORED_BRANDS, so
    # brand+model matching misses it entirely.
    if not candidate_title:
        candidate_title = _product_phrase_from_text(instruction or "")
    # Weaker signal: "monitor price of X" / "track X on Amazon".
    # A pronoun-led or cadence-led capture ("this for every 5 mins",
    # "that watch", "every 5 mins") is the instruction's tail, not a
    # product — the old code accepted it and minted a garbage watch
    # titled "This For Every 5 Mins" (S1 Failure E). Reject it here
    # so the handler asks instead of inventing a watch.
    if not candidate_title:
        m = re.search(r"\b(?:for|of|on)\s+([^,.\n]+?)(?:\s+(?:on|at|and|if|when)|$)", instruction, re.IGNORECASE)
        raw = m.group(1).strip() if m else ""
        first = raw.lower().split(" ", 1)[0] if raw else ""
        if (raw.lower() not in ("this", "this product", "the product", "this price", "it", "")
                and first not in ("this", "that", "it")
                and not raw.lower().startswith(("every ", "evry "))):
            candidate_title = raw

    # Stream 2 (Failure E): the extractors above can hand back
    # instruction metadata as the "product" — the weak for/of/on
    # fallback took the interval phrase ("every 5 mins") and the
    # verb-phrase extractor took the alert tail ("alert me") or a
    # dangling preposition ("product for"), so the Flipkart search
    # URL was built from the cadence instead of the product. Strip
    # pronouns, intervals, conditions and notification phrases from
    # the candidate; when nothing product-like remains there is no
    # product in THIS instruction, so the history scan below (or the
    # handler's ask-the-user branch) decides instead of a phantom
    # watch titled "Every 5 Mins".
    # Merge note (S2+S3): sanitizing can erase the garbage signal —
    # "3 so can u watch the price of this for" sanitizes to
    # "3 so can u watch", which no longer contains a garbage word,
    # so the history scan and the objective repair below would both
    # skip it and mint a watch for the remnant. Keep the pre-sanitize
    # candidate so both checks below judge garbage-ness on it too.
    raw_candidate_title = candidate_title
    if candidate_title:
        candidate_title = sanitize_query_text(candidate_title)

    # A URL pasted in the instruction itself wins — the history
    # loop below only sees earlier messages, so a URL given with the
    # ask was ignored and the watch fell back to a Flipkart search
    # page (whose first price is a banner, not the product's).
    # Stream C: ANY http(s) URL counts, not only stores — a generic
    # page watch ("monitor http://127.0.0.1:8765/notes every 15
    # minutes") targets that page with a text_change condition. The
    # unified URL gate validates it at creation (see the handler).
    best_url = ""
    url_m = re.search(r"https?://[^\s\"'<>)]+", instruction or "")
    if url_m:
        best_url = url_m.group(0)
    # The objective's target URL is the structured decision — it wins
    # over a URL scraped from raw text when both are present.
    if objective_url:
        best_url = objective_url

    # Look back in history for product name and URL. The history scan
    # used to take the first brand+model match from ANY message —
    # including the agent's own greeting ("Connected apps — Gmail,
    # Google Calendar ..."), which produced a watch for "Google
    # Calendar" when the user asked about a bike stand. Now: user
    # messages may name the product (verb phrase or brand+model);
    # assistant messages only count when they carry a real store URL,
    # never a greeting or capability list — and never on a bare price
    # mention alone (FIX-B M4: the assistant's B3 reply mentioned
    # ₹34,990, which let "Sony India" out of its own prose become the
    # product for B5's follow-up). Service names ("Google Calendar")
    # and retailer/place names ("Sony India") are rejected everywhere.
    # Two passes, user first: the operator's own words outrank the
    # assistant's paraphrase, so an assistant-derived candidate can
    # never shadow the product the user actually named.
    if history:
        # Pass 0: a store URL from any message (a pasted URL is a real
        # signal whatever its role) — only when the instruction gave none.
        if not best_url:
            for entry in reversed(history):
                content = str(entry.get("content") or entry.get("message") or "")
                url_match = re.search(r"https?://[^\s\"'<>)]+", content)
                if url_match and ("amazon" in url_match.group(0) or "flipkart" in url_match.group(0) or "croma" in url_match.group(0)):
                    best_url = url_match.group(0)
                    break
        # A garbage candidate ("This For Every 5 Mins") must not
        # block the history scan: a real product named in an
        # earlier message replaces it below. Judge garbage-ness
        # on the pre-sanitize candidate too (see above).
        needs_product = (
            not candidate_title
            or _is_garbage_product_title(candidate_title)
            or _is_garbage_product_title(raw_candidate_title)
        )
        if needs_product:
            # Pass 1: the user's own messages, most recent first.
            for entry in reversed(history):
                if (entry.get("role") or "").lower() != "user":
                    continue
                content = str(entry.get("content") or entry.get("message") or "")
                phrase = _product_phrase_from_text(content)
                if phrase:
                    candidate_title = phrase
                    break
                prod_m = _PRODUCT_RE.search(content)
                if prod_m:
                    cleaned_match = _clean_product_match(prod_m.group(0))
                    if not _is_service_name(cleaned_match) and not _is_retailer_or_place_product(cleaned_match):
                        candidate_title = cleaned_match
                        break
        if needs_product and (not candidate_title or _is_garbage_product_title(candidate_title)):
            # Pass 2: assistant messages, ONLY when they carry a real
            # store URL (a suggestion with a link). A bare price in
            # prose ("launched around ₹34,990") is not a suggestion
            # and never yields a product.
            for entry in reversed(history):
                if (entry.get("role") or "").lower() == "user":
                    continue
                if not _STORE_LINK_RE.search(str(entry.get("content") or entry.get("message") or "")):
                    continue
                content = str(entry.get("content") or entry.get("message") or "")
                prod_m = _PRODUCT_RE.search(content)
                if prod_m:
                    cleaned_match = _clean_product_match(prod_m.group(0))
                    if not _is_service_name(cleaned_match) and not _is_retailer_or_place_product(cleaned_match):
                        candidate_title = cleaned_match
                        break

    # Stream 2 + Stream 3 (Failure E / S5 D10): a pronoun- or cadence-led
    # candidate is garbage, never a product — and when the regexes
    # find NO product at all, the structured objective's product is
    # the answer (it extracted "Galaxy S26 Ultra 5G" from the preamble
    # the regexes cannot read). Prefer the objective's product when the
    # candidate is empty or garbage; otherwise drop the garbage so the
    # caller asks instead of minting a watch for it. A clean regex
    # candidate still wins — the objective is the repair, not the
    # default. Then strip any threshold tail the history scan may have
    # kept ("kestrel 4k drone below 40000"). Judge garbage-ness on the
    # pre-sanitize candidate too (see above): a sanitized remnant like
    # "3 so can u watch" must still trigger the objective repair.
    if not candidate_title or _is_garbage_product_title(candidate_title) or _is_garbage_product_title(raw_candidate_title):
        if objective_product and not _is_service_name(objective_product) and not _is_retailer_or_place_product(objective_product):
            candidate_title = objective_product
        elif candidate_title:
            candidate_title = ""
    if candidate_title:
        candidate_title = sanitize_query_text(candidate_title)

    if not candidate_title and not best_url:
        # No product was named anywhere: say so instead of inventing
        # one. The old hardcoded phone fallback ("Samsung Galaxy A27")
        # would have watched the wrong product entirely.
        return "", ""
    if not candidate_title:
        # A URL but no name: title it from the URL. A store product
        # page gets "Product on <host>"; any other page gets its path
        # slug ("Field Notes" for /notes) or the host.
        host = (urlsplit(best_url).hostname or "store").removeprefix("www.")
        if any(d in best_url.lower() for d in ("amazon.", "flipkart.", "croma.")):
            candidate_title = f"Product on {host}"
        else:
            candidate_title = _title_from_listing_url(best_url) or f"Page on {host}"
    if not best_url:
        # A product but no store URL: watch its search page, never a
        # store homepage (whose first price is a banner, not the
        # product's).
        best_url = f"https://www.flipkart.com/search?q={urllib_quote(candidate_title)}"

    return _truncate_title(candidate_title.title()), best_url


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)


# Stream A: there is NO hardcoded domain list for new-listings
# watches any more. Any http(s) URL the user pastes (or the history
# carries) is a candidate; watchability is decided by the SSRF guard
# (change_detector.validate_monitor_url) at creation time, the same
# gate the fetch path uses — so a watch that is created can always
# be checked. The old six-domain list refused isro.gov.in (and every
# other site outside it) before any fetch.
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
_TRACK_LISTING_TITLE_RE = re.compile(
    r"\b(?:track|watch|monitor)\b\s+(?:the\s+)?(.+?)(?:\s+and\s+(?:alert|notify|tell)|"
    r"\s+when\s+a\s+new|\s+on\s+(?:internshala|linkedin|unstop|amazon|flipkart|croma)\b|\s*$)",
    re.IGNORECASE,
)
_NEW_NOUN_RE = re.compile(
    r"\bnew\s+(?:([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,3}))\s+(?:appears?|shows?\s+up|comes?\s+up|posted)",
    re.IGNORECASE,
)


def _is_track_new_ask(instruction: str) -> bool:
    """True when the instruction asks to watch for NEW items
    (listings, jobs, internships) rather than a price."""
    return bool(_TRACK_NEW_RE.search(instruction or ""))


_STORE_DOMAINS = ("amazon.", "flipkart.", "croma.")


def _is_store_url(url: str) -> bool:
    """True for the retail stores the price detector is built for."""
    return any(d in (url or "").lower() for d in _STORE_DOMAINS)


_KEYWORD_RE = re.compile(
    r"\b(?:mentions?|contains?|says?|includes?|shows?)\s+[\"']?([A-Za-z][\w\- ]{1,50}?)[\"']?(?:\s*(?:,|and|then|$))",
    re.IGNORECASE,
)


def _parse_keyword(instruction: str) -> str | None:
    """'monitor this page and tell me when it mentions X' -> 'X'.

    The keyword becomes a regex condition (escaped, so it is a plain
    substring match). Returns None when no mention/contains ask is
    present. A price ask never yields a keyword — the handler only
    consults this for generic page watches.
    """
    m = _KEYWORD_RE.search(instruction or "")
    if not m:
        return None
    word = re.sub(r"\s+", " ", m.group(1)).strip().strip("\"'")
    if len(word) < 2 or word.lower() in ("a", "an", "the", "it", "price", "change", "changes"):
        return None
    return word


def _extract_tracked_listing(instruction: str, history: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """Extract the tracked query + a listing/search URL for a
    track-new-items ask. Returns ("", "") when either is missing —
    the caller must ask the user instead of inventing a watch.
    The URL comes from the instruction, else from the most recent
    listing-site URL in history (the search page the agent already
    used); a search URL is never constructed from a site name alone,
    because each site's URL scheme differs and a guessed URL would
    watch the wrong page."""
    title = ""
    m = _TRACK_LISTING_TITLE_RE.search(instruction or "")
    if m:
        title = m.group(1).strip(" .").strip()
    if not title:
        nm = _NEW_NOUN_RE.search(instruction or "")
        if nm and nm.group(1).lower() not in ("one", "ones"):
            title = nm.group(1).strip()
    if not title and history:
        for entry in reversed(history):
            content = str(entry.get("content") or entry.get("message") or "")
            m2 = _TRACK_LISTING_TITLE_RE.search(content)
            if m2:
                title = m2.group(1).strip(" .").strip()
                break
    title = re.sub(r"\s+", " ", title)[:80]
    # A URL pasted in the instruction must not leak into the title
    # ("Track new SDE internships on https://..." would otherwise
    # title the watch with the URL itself). Strip URLs, a leading
    # "new" (the mission title already says "New listings:"), and a
    # dangling preposition left behind by the URL removal.
    title = _URL_RE.sub("", title).strip()
    title = re.sub(r"^new\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+(?:on|at|for|in|and|&)\s*$", "", title).strip()
    title = re.sub(r"\s+", " ", title)[:80]

    url = ""
    for source in (instruction or "",):
        for m3 in _URL_RE.finditer(source):
            # Stream A: any pasted http(s) URL is a candidate — the
            # SSRF guard in _create_new_listings_watch decides
            # watchability, not a hardcoded domain list.
            url = m3.group(0)
            break
        if url:
            break
    if not url and history:
        for entry in reversed(history):
            content = str(entry.get("content") or entry.get("message") or "")
            for m4 in _URL_RE.finditer(content):
                url = m4.group(0)
                break
            if url:
                break
    # A bare pronoun title ("Track them and alert me" -> "them") cannot
    # name a watch. Derive it from the URL the watch will check.
    if url and (not title or title.lower() in _PRONOUN_TITLES):
        title = _title_from_listing_url(url)
    return title, url


def _create_new_listings_watch(
    listing_title: str,
    listing_url: str,
    chat_id: Optional[str],
    db_path: Optional[str],
) -> str:
    """Insert a new_listings mission (hourly) and return its confirmation.
    Watchability is decided by the ONE monitoring URL gate
    (change_detector.validate_monitor_url — the SSRF guard the fetch
    path uses), never by a domain allowlist: a watch that is created
    can always be checked, and a URL the guard refuses is refused
    honestly here instead of being created to fail forever."""
    from .change_detector import validate_monitor_url

    ok, gate_reason = validate_monitor_url(listing_url)
    if not ok:
        return (
            f"I can't watch {listing_url} — {gate_reason} "
            f"Give me a public page URL (http or https) and I'll set up the watch."
        )
    mission_id = str(uuid.uuid4())
    now = time.time()
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                mission_id,
                chat_id or "default",
                f"New listings: {listing_title}",
                listing_url,
                "interval",
                None,
                3600,
                "new_listings",
                json.dumps({"target": listing_title}),
                now,
                now,
            ),
        )
        conn.commit()
    # Stream C (R-03): broadcast_control_state now exists on the hub;
    # the old try/except: pass swallowed the AttributeError and no
    # client was ever told a watch was created/updated.
    hub.spawn(hub.broadcast_control_state())
    from .monitoring_scheduler import format_ist_short

    return (
        f"### 👀 New-listings watch active\n\n"
        f"I'll check **{listing_title}** every hour and alert you right here in this conversation when a new one appears.\n\n"
        f"• **Status:** Active — alerts land in this conversation\n"
        f"• **Watch ID:** `{mission_id}`\n"
        f"• **Watching:** [{listing_title} ↗]({listing_url})\n"
        f"• **Check frequency:** Every hour\n"
        f"• **Next check:** {format_ist_short(now) or '—'}\n"
        f"• **Manage:** say `pause watch {mission_id[:8]}`, `resume watch {mission_id[:8]}`, "
        f"or `cancel watch {mission_id[:8]}`, or use the card below\n"
        f"• **Alert:** The moment a new listing shows up\n\n"
        f"<!-- privagent-watch: {mission_id} -->"
    )


def _threshold_from_history(history: Optional[list[dict[str, Any]]]) -> float | None:
    """Most recent below-price threshold in the last few history turns.

    A follow-up that only changes the cadence ("check its price and
    just alert me every 5 minutes") names no threshold — the one from
    the ask it follows ("if the price is below 1000") still applies.
    Only the last few turns are scanned, so an old threshold for a
    different product in the same chat cannot leak in.
    """
    for entry in reversed((history or [])[-4:]):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        value = parse_below_price(content)
        if value is not None:
            return value
    return None


def upsert_monitor_mission(*, product_title: str, watch_title: str, target_url: str,
                         trigger_type: str, schedule_cron: str | None,
                         interval_seconds: int, condition_type: str,
                         condition_params: dict, below: float | None = None,
                         percent: float | None = None,
                         stock_alert: bool | None = None,
                         chat_id: str | None = None,
                         db_path: str | None = None) -> dict[str, Any]:
    """Structured create-or-update of a ``monitoring_missions`` row.

    Stream 3: the single creation core shared by the NL handler
    (:func:`handle_continuous_monitoring_instruction`, which parses
    raw text) and the structured orchestration pipeline
    (:func:`app.monitoring_orchestration.orchestrate_monitor`, which
    is driven by Objective + WorkingContext, never by raw text).

    Returns ``{"mission_id", "outcome", "row", "previous"}`` where
    outcome is "created" | "existing" | "updated", "row" is the
    post-state row and "previous" is the pre-existing active row for
    this product (or None). Dedup semantics (unchanged from the
    handler's inline logic): a follow-up that only changes the
    cadence must not create a SECOND watch — same settings returns
    the existing mission; different settings update the existing
    mission in place; only a genuinely new product creates a row.
    The comparison uses the condition-relevant values only
    (threshold for below_price, min_percent normalised None -> 0
    for price_drop), never the display target, so a re-ask that adds
    or drops the brand still matches the same watch.
    ``stock_alert`` is tri-state (FIX-B B4): True/False sets the
    out-of-stock flag, None (the default, used by the structured
    orchestration pipeline) preserves whatever the stored row has —
    a cadence change must not silently wipe a stock condition the
    user asked for earlier.
    """
    db.init_db(db_path)
    now = time.time()
    with db.get_connection(db_path) as conn:
        existing = conn.execute(
            """SELECT id, trigger_type, condition_type, condition_params, interval_seconds, schedule_cron
               FROM monitoring_missions
               WHERE conversation_id = ? AND title = ? AND status = 'active'
               ORDER BY created_at DESC LIMIT 1""",
            (chat_id or "default", watch_title),
        ).fetchone()
    # A re-ask that adds or drops the brand ("Apple iPhone 17"
    # after "iPhone 17") is the same watch — fall back to the
    # normalised-title match, or it becomes a duplicate watch.
    if existing is None:
        existing = _find_active_watch(product_title, chat_id, db_path)
    if existing is not None:
        previous = dict(existing)
        try:
            stored_params = json.loads(previous.get("condition_params") or "{}")
        except Exception:
            stored_params = {}
        stored_threshold = stored_params.get("threshold")
        stored_percent = stored_params.get("min_percent")
        stored_stock = bool(stored_params.get("stock_alert")) if isinstance(stored_params, dict) else False
        effective_stock = stored_stock if stock_alert is None else bool(stock_alert)
        same_settings = (
            previous["trigger_type"] == trigger_type
            and previous["interval_seconds"] == interval_seconds
            and (previous["schedule_cron"] or None) == (schedule_cron or None)
            and previous["condition_type"] == condition_type
            and (stored_threshold if condition_type == "below_price" else None)
            == (below if condition_type == "below_price" else None)
            and ((stored_percent or 0) if condition_type == "price_drop" else None)
            == ((percent or 0) if condition_type == "price_drop" else None)
            and stored_stock == effective_stock
        )
        if same_settings:
            return {"mission_id": previous["id"], "outcome": "existing",
                    "row": previous, "previous": previous}
        merged_params = dict(condition_params or {})
        if effective_stock:
            merged_params["stock_alert"] = True
        else:
            merged_params.pop("stock_alert", None)
        with db.get_connection(db_path) as conn:
            conn.execute(
                """UPDATE monitoring_missions
                   SET target_url = ?, trigger_type = ?, schedule_cron = ?, interval_seconds = ?,
                       condition_type = ?, condition_params = ?, next_run_at = ?
                   WHERE id = ?""",
                (target_url, trigger_type, schedule_cron, interval_seconds,
                 condition_type, json.dumps(merged_params), now, previous["id"]),
            )
            conn.commit()
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM monitoring_missions WHERE id=?", (previous["id"],)
            ).fetchone()
        return {"mission_id": previous["id"], "outcome": "updated",
                "row": dict(row) if row else previous, "previous": previous}
    mission_id = str(uuid.uuid4())
    merged_params = dict(condition_params or {})
    if stock_alert is True:
        merged_params["stock_alert"] = True
    elif stock_alert is False:
        merged_params.pop("stock_alert", None)
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (mission_id, chat_id or "default", watch_title, target_url,
             trigger_type, schedule_cron, interval_seconds, condition_type,
             json.dumps(merged_params), now, now),
        )
        conn.commit()
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    return {"mission_id": mission_id, "outcome": "created",
            "row": dict(row) if row else {"id": mission_id}, "previous": None}


# ---------------------------------------------------------------------------
# Structured Objective consumption (Stream 1 + Stream 3, S5 D10 / S1 E)
# ---------------------------------------------------------------------------

def _objective_for(instruction: str, objective: Any | None = None) -> dict | None:
    """The structured Objective for this turn, as a plain dict.

    Prefer the objective the caller already built (agentLoop stores
    it in working memory as ``initial_memory["objective"]``); otherwise
    build it from the instruction with Stream 1's deterministic
    classifier. Returns None when no objective module exists (the base
    build has no ``objective.py``) or the build fails — the regex paths
    below are the fallback, so the handler never depends on the
    objective existing.
    """
    if objective is not None:
        if isinstance(objective, dict):
            return objective
        to_dict = getattr(objective, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except Exception:
                return None
        return None
    try:
        from .objective import build_objective
    except Exception:
        return None
    try:
        obj = build_objective(instruction or "")
    except Exception:
        return None
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            return None
    return obj if isinstance(obj, dict) else None


def _relative_interval_seconds(factor: str, current: int) -> int:
    """New interval for a relative update, 60 s floor. ``twice_as_often``
    halves the cadence; ``half_as_often`` doubles it. The factor is
    NEVER a guessed number — it comes from the objective's
    ``relative_interval`` entity; the current interval comes from the
    DB row being updated."""
    if factor == "twice_as_often":
        return max(60, int(current or 300) // 2)
    if factor == "half_as_often":
        return int(current or 300) * 2
    raise ValueError(f"unknown relative_interval {factor!r}")


def _resolve_update_target(objective: dict, chat_id: Optional[str],
                           history: Optional[list[dict[str, Any]]],
                           db_path: Optional[str]) -> tuple[dict | None, bool]:
    """The watch a pronoun-only update refers to. Returns (row,
    ambiguous). Resolution order: the objective's watch ref / product,
    the most recent product named in history, then — only when exactly
    one active watch exists — that watch. Several active watches and
    no referent is ambiguous, never a guess."""
    entities = (objective or {}).get("entities") or {}
    for ref in (entities.get("watch_id"), entities.get("watch_ref"),
                entities.get("product")):
        if ref:
            row, ambiguous = _resolve_watch(str(ref), chat_id, db_path)
            if row is not None or ambiguous:
                return (dict(row) if row is not None else None), ambiguous
    if history:
        product, _url = _extract_monitored_product("", history=history)
        if product and not _is_garbage_product_title(product):
            row, ambiguous = _resolve_watch(product, chat_id, db_path)
            if row is not None or ambiguous:
                return (dict(row) if row is not None else None), ambiguous
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        active = conn.execute(
            "SELECT * FROM monitoring_missions WHERE status='active' "
            "ORDER BY created_at DESC"
        ).fetchall()
    if len(active) == 1:
        return dict(active[0]), False
    if len(active) > 1:
        return None, True
    return None, False


def _apply_relative_interval_update(objective: dict, instruction: str,
                                    chat_id: Optional[str],
                                    history: Optional[list[dict[str, Any]]],
                                    db_path: Optional[str]) -> str | None:
    """S5 gen D10: "make that check twice as often". Halve (or double)
    the resolved watch's interval IN PLACE and return the reply.

    Returns None only when the objective carries no relative interval
    (not this turn's case). Every relative-interval turn is answered
    here — updated, or an honest "which watch?" — so it can never fall
    through to creation, where the old path extracted "Twice As Often"
    as a product name and minted a second 300 s watch while the
    original stayed at 3600 s.
    """
    entities = (objective or {}).get("entities") or {}
    factor = entities.get("relative_interval")
    if factor not in ("twice_as_often", "half_as_often"):
        return None
    row, ambiguous = _resolve_update_target(objective, chat_id, history, db_path)
    if ambiguous:
        return ("That matches more than one watch — tell me which one to check "
                f"{'twice' if factor == 'twice_as_often' else 'half'} as often, "
                "by name or id (from the watch card). No watch was changed.")
    if row is None:
        return ("I don't have a watch to change yet — no watch is running in "
                "this conversation. Tell me which product to watch first, and "
                "I'll set it up.")
    mission_id = row["id"]
    title = row["title"] or mission_id
    old_interval = row.get("interval_seconds") or 300
    old_cadence = ("daily" if row.get("trigger_type") == "cron"
                   else _describe_interval(old_interval))
    try:
        new_interval = _relative_interval_seconds(factor, old_interval)
    except ValueError:
        return (f"I can't apply that relative change ({factor!r}) — say the new "
                "interval directly, e.g. 'check every 30 minutes'. No watch was changed.")
    now = time.time()
    with db.get_connection(db_path) as conn:
        # A daily cron watch has no interval to halve: the relative
        # change converts it to an interval watch at the halved (or
        # doubled) cadence, and the reply says so plainly.
        conn.execute(
            """UPDATE monitoring_missions
               SET trigger_type='interval', schedule_cron=NULL,
                   interval_seconds=?, next_run_at=?
               WHERE id=?""",
            (new_interval, now, mission_id),
        )
        conn.commit()
    with db.get_connection(db_path) as conn:
        reread = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    if reread is None or reread["interval_seconds"] != new_interval:
        return ("The watch change did not persist — the stored interval is "
                f"unchanged. No watch was changed; try again or check the dashboard.")
    hub.spawn(hub.broadcast_control_state())
    from .monitoring_scheduler import format_ist_short

    new_cadence = _describe_interval(new_interval)
    return (
        f"Updated the watch for **{title}** — it was checking {old_cadence}, "
        f"now checking {new_cadence}.\n\n"
        f"• **Watch ID:** `{mission_id}`\n"
        f"• **Next check:** {format_ist_short(now) or '—'}\n"
        f"• **Manage:** say `pause watch {mission_id[:8]}`, `resume watch {mission_id[:8]}`, "
        f"or `cancel watch {mission_id[:8]}`, or use the card below."
    ) + f"\n\n<!-- privagent-watch: {mission_id} -->"


async def handle_continuous_monitoring_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[str] = None,
    objective: Any | None = None,
) -> str:
    """Register a persistent continuous monitoring mission and return natural departure handoff."""
    db.init_db(db_path)
    # Stream 3 (S5 D10 / S1 E): consume the structured Objective
    # (Stream 1) when one is available — built here from the
    # instruction when the caller did not pass one (agentLoop stores
    # it in working memory as initial_memory["objective"]). A relative
    # interval ("twice as often") is handled before anything else: it
    # updates the resolved watch in place and never reaches creation.
    obj = _objective_for(instruction, objective=objective)
    if obj and (obj.get("entities") or {}).get("relative_interval"):
        relative_answer = _apply_relative_interval_update(
            obj, instruction, chat_id, history, db_path)
        if relative_answer is not None:
            return relative_answer
    # Stream C: chat control + NL scheduling dispatch first — but a
    # genuine monitor ask always wins over a control phrase that
    # happens to share a word ("show me the jobs" inside a track-new
    # ask is still a listings watch, not the job list).
    if not MONITORING_DIRECTIVE_PATTERN.search(instruction or "") and not _is_track_new_ask(instruction or ""):
        job_answer = _handle_job_control(instruction, db_path)
        if job_answer is not None:
            return job_answer
        # Stream 2 follow-up (Stream 5 F2/F4/D10): watch list,
        # watch control and relative-cadence follow-ups are answered
        # by the deterministic handler (DB-verified), never by the
        # model layer — the model used to claim the change in prose
        # while the DB stayed unchanged.
        list_answer = _handle_watch_list(instruction, chat_id, db_path, history)
        if list_answer is not None:
            return list_answer
        summary_answer = _handle_watch_summary(instruction, chat_id, db_path, history)
        if summary_answer is not None:
            return summary_answer
        watch_answer = _handle_watch_control(instruction, chat_id, db_path, history)
        if watch_answer is not None:
            return watch_answer
        stock_answer = _handle_stock_follow_up(instruction, chat_id, db_path, history)
        if stock_answer is not None:
            return stock_answer
        relative_answer = _handle_relative_interval(instruction, chat_id, db_path)
        if relative_answer is not None:
            return relative_answer
        if is_schedule_directive(instruction):
            return await _handle_schedule_instruction(instruction, chat_id, history, db_path)
    # Price signals are read first: a price ask must never become a
    # listing watch. Dispatch order is daily -> below -> percent ->
    # new_listings -> price_drop, so the handler body matches the
    # documented order.
    daily = parse_daily_time(instruction)
    below = parse_below_price(instruction)
    percent = parse_percent_drop(instruction)
    # Whether THIS instruction names a percent. Inheritance (history
    # / existing watch) may fill percent in below, but only a percent
    # named here counts for the percent+below note — an inherited
    # one was not mentioned by the user in this ask.
    percent_named = percent is not None
    # A follow-up that only changes the cadence ("check its price and
    # just alert me every 5 minutes") names no threshold — the one from
    # the ask it follows ("if the price is below 1000") still applies.
    # Only when the instruction names NO product of its own (a pronoun
    # follow-up): an explicit new product must never inherit an old
    # threshold for a different product.
    if (
        below is None
        and not _PRODUCT_RE.search(instruction or "")
        and not _product_phrase_from_text(instruction or "")
    ):
        below = _threshold_from_history(history)
    # Same inheritance for a %-drop: a bare cadence follow-up names
    # no percent — the one from the ask it follows ("when it 10%
    # off") still applies, or the watch would be rebuilt with
    # min_percent 0 and the threshold silently wiped.
    if (
        percent is None
        and not _PRODUCT_RE.search(instruction or "")
        and not _product_phrase_from_text(instruction or "")
    ):
        percent = _percent_from_history(history)
    # Track-new-items ("track SDE internships and alert me when a
    # new one appears"): a new_listings mission, not a price watch.
    # Hourly interval — listing pages change slowly; a 5-minute tick
    # would burn the scheduler and the sites for no gain.
    if _is_track_new_ask(instruction) and daily is None and below is None:
        listing_title, listing_url = _extract_tracked_listing(instruction, history=history)
        if not listing_title or not listing_url:
            return (
                "I can watch for new listings, but I need the search page to watch. "
                "Paste the search results URL (for example, a job board's search page), "
                "or ask me to search for the listings first and I'll offer the watch after."
            )
        return _create_new_listings_watch(listing_title, listing_url, chat_id, db_path)
    # The objective's product / target URL (Stream 1) is the
    # structured decision: it repairs a pronoun-led regex candidate
    # ("This For Every 5 Mins") inside _extract_monitored_product
    # instead of minting a watch for the garbage name (S1 Failure E).
    obj_product = obj_target_url = None
    if obj and obj.get("primary_intent") in (
            "monitor_product", "monitor_website", "monitor_new_listings"):
        obj_product = ((obj.get("entities") or {}).get("product") or "").strip() or None
        obj_target_url = (obj.get("target_url") or "").strip() or None
    product_title, target_url = _extract_monitored_product(
        instruction, history=history,
        objective_product=obj_product, objective_url=obj_target_url)
    if not product_title:
        # Bare "yes" answering the alert offer after a listing search
        # (e.g. internships): the history carries the search page, so
        # the watch is a new_listings one, not a price watch. This
        # branch runs only when there is NO product in the history —
        # a retail suggestion (Fit3 on Flipkart) has a brand+model, so
        # it takes the price-watch path below. The detector only routes
        # here when the offer marker is in the last assistant message
        # and the history has a tracked listing.
        if _is_bare_affirmative(instruction) and daily is None and below is None:
            listing_title, listing_url = _extract_tracked_listing(instruction, history=history)
            if listing_title and listing_url:
                return _create_new_listings_watch(listing_title, listing_url, chat_id, db_path)
        # No product and no store URL anywhere: do NOT create a watch
        # for "your last suggestion" on a store homepage. Ask which
        # product to watch instead.
        return (
            "I don't have a product to watch yet — no product name or store link "
            "in this conversation. Tell me which product to watch (for example, "
            "\"watch the Samsung Galaxy Fit3\") or paste the store link, and I'll "
            "set up the watch."
        )

    # FIX-B (M4): a pure follow-up ("change it to every 45 mins")
    # names no product of its own — its product comes from history.
    # When no watch exists for that product, the follow-up cannot be
    # an update of anything: creating one now would mint a watch
    # from a paraphrase (in the live run, from the assistant's own
    # "Sony India" text). Ask instead — never create a watch from a
    # follow-up with no resolvable existing watch.
    if _is_pure_follow_up(instruction) and _find_watch_by_product(product_title, chat_id, db_path) is None:
        return (
            f"I don't have a watch running for **{product_title}** yet, so there's "
            "nothing to change — the earlier ask never became a watch. Tell me "
            "which product to watch (with its price condition and how often to "
            "check) and I'll set it up."
        )

    # Stream C (RC-LIVE-04): creation uses the SAME URL gate as the
    # fetch — a watch the checker would refuse (private host, bad
    # scheme) is refused here, honestly, instead of being created to
    # fail forever. The 127.0.0.1 fixture host passes this gate. A
    # DNS "does not resolve" failure is TRANSIENT (the fetch path
    # treats it the same way): the watch is created with a note, and
    # the scheduler keeps retrying, instead of refusing a page that
    # may resolve a moment later.
    from .change_detector import validate_monitor_url

    ok, gate_reason = validate_monitor_url(target_url)
    dns_transient = (not ok) and gate_reason and "does not resolve" in gate_reason
    if not ok and not dns_transient:
        return (
            f"I can't watch {target_url} — {gate_reason} "
            f"Give me a public page URL (http or https) and I'll set up the watch."
        )

    # A product-named cadence follow-up ("live monitor Apple iPhone
    # 17, check every 5 minutes") names no threshold — the threshold
    # from the watch already running for this product still applies,
    # or the watch would be rebuilt with min_percent 0 and a ₹ or %
    # threshold silently wiped. The history inheritance above only
    # covers pronoun follow-ups (no product named), so this covers
    # the same-product case; a different product never inherits.
    if percent is None and below is None:
        below, percent = _thresholds_from_existing_watch(product_title, chat_id, db_path)

    # Stream 3: the mission id is minted inside upsert_monitor_mission
    # (shared creation core) — no id is allocated before the dedup
    # check, or a re-ask would burn an id for a watch that already exists.
    now = time.time()
    # Stream C: a generic (non-store) page watch is a text_change /
    # keyword watch, NOT a price watch. The old code built a
    # price_drop mission for every URL, and price_drop with no
    # extractable price can never fire — a "monitor this page" watch
    # sat active forever and silently never alerted. A store URL (or
    # any price signal) keeps the price branches below.
    keyword = _parse_keyword(instruction)
    is_generic = not _is_store_url(target_url)
    watch_title = product_title if is_generic else f"Price Watch: {product_title}"
    reply_header = "### 🎯 Continuous Price Monitoring Activated\n\n"
    dns_note = ""
    if dns_transient:
        dns_note = (
            f"• **Note:** {target_url} didn't resolve just now — I'll keep retrying on schedule "
            f"instead of refusing the watch.\n"
        )
    if is_generic and daily is None and below is None and percent is None:
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        if keyword:
            condition_type, condition_params = "regex", {"target": product_title, "pattern": re.escape(keyword)}
            alert_line = f"• **Alert Condition:** Alert the moment the page mentions '{keyword}'\n"
        else:
            condition_type, condition_params = "text_change", {"target": product_title}
            alert_line = "• **Alert Condition:** Alert the moment the page content changes\n"
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        reply_header = "### 👀 Watch active\n\n"
    # A daily cadence and a threshold COMPOSE: "below ₹5000, notify
    # me every day" is a below_price mission on a daily cron — the
    # check runs once a morning and alerts only when the price is
    # under the threshold. Before this, daily swallowed the threshold
    # (a daily_price report with no alert) and a threshold alone fell
    # back to a 5-minute interval the user never asked for. The
    # scheduler's claim_due handles a cron trigger for any condition,
    # so the composed mission is checked the same way.
    elif daily and below is not None:
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "below_price", {"target": product_title, "threshold": below}
        cadence_line = f"• **Check Schedule:** Every day at **{display}**\n"
        alert_line = (
            f"• **Alert Condition:** Alert when the price falls below ₹{below:g} "
            f"(if it is already below, I'll alert you on the first check)\n"
        )
    elif daily and percent is not None:
        # A %-drop and a daily cadence COMPOSE, like daily+below: a
        # price_drop mission on a daily cron — the check runs once a
        # morning and alerts only when the price has dropped the
        # asked percent since the previous check.
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "price_drop", {"target": product_title, "min_percent": percent}
        cadence_line = f"• **Check Schedule:** Every day at **{display}**\n"
        alert_line = (
            f"• **Alert Condition:** Alert when the price drops {percent:g}% or more "
            f"from the price on the previous check (the first check records today's price)\n"
        )
    elif is_generic and daily:
        # Stream C: a generic page on a daily cadence is a daily
        # text_change check, NOT a daily price report — there is no
        # price to report on a notices page.
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        if keyword:
            condition_type, condition_params = "regex", {"target": product_title, "pattern": re.escape(keyword)}
            alert_line = f"• **Alert Condition:** Alert the moment the page mentions '{keyword}'\n"
        else:
            condition_type, condition_params = "text_change", {"target": product_title}
            alert_line = "• **Alert Condition:** Alert when the page content changes\n"
        cadence_line = f"• **Check Schedule:** Every day at **{display}**\n"
        reply_header = "### 👀 Watch active\n\n"
    elif daily:
        # A daily time ("every day at 9am", "every morning at 9")
        # becomes a cron mission that REPORTS the price each morning,
        # instead of a 5-minute interval mission that only fires on a
        # drop. The cron is UTC (the scheduler stores five UTC
        # fields); parse_daily_time converts the user's IST morning
        # into it. (daily / below were already read at the top of
        # this handler for dispatch order.)
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "daily_price", {"target": product_title}
        cadence_line = f"• **Check Schedule:** Every day at **{display}** — I'll send you the current price each morning\n"
        alert_line = "• **Alert Condition:** A price report every morning at the scheduled time, even when the price has not changed\n"
    elif below is not None:
        # Absolute threshold ("alert me when the price falls below
        # ₹5000"): a below_price mission, not a %-drop one. The
        # detector fires on the first run too when the price is
        # already under the threshold. The cadence is the one the
        # user asked for ("every 2 hours" is 2 hours, not 5
        # minutes); no cadence in the instruction -> 300s default.
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        condition_type, condition_params = "below_price", {"target": product_title, "threshold": below}
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        alert_line = (
            f"• **Alert Condition:** Alert the moment the price falls below ₹{below:g} "
            f"(if it is already below, I'll alert you on the first check)\n"
        )
    elif percent is not None:
        # Percent drop ("live monitor iPhone 17 when it 10% off"):
        # a price_drop mission carrying the asked percent — before
        # this the percent was never parsed, so every %-drop ask
        # became a min_percent 0 watch that fired on ANY drop. The
        # cadence is the one the user asked for ("every 10 minutes"
        # is 10 minutes, not 5); no cadence -> 300s default. The
        # first check records today's price, so the alert can fire
        # from the second check on.
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        condition_type, condition_params = "price_drop", {"target": product_title, "min_percent": percent}
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        alert_line = (
            f"• **Alert Condition:** Alert the moment the price drops {percent:g}% or more "
            f"from the price on the previous check — the first check records today's price, "
            f"so the alert can fire from the second check on\n"
        )
    else:
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        condition_type, condition_params = "price_drop", {"target": product_title, "min_percent": 0}
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        alert_line = "• **Alert Condition:** Instant notification whenever the price drops or special discount offers are detected\n"

    # Percent + below together: a watch stores ONE condition, and
    # below wins — so say so, or the user believes both are watched.
    # Only when THIS instruction named the percent; an inherited one
    # was not mentioned in this ask.
    if below is not None and percent_named:
        alert_line += (
            f"• **Note:** You also mentioned a {percent:g}% drop — this watch tracks only "
            f"the below-₹{below:g} condition, not the percent drop.\n"
        )

    # FIX-B (B4): a stock condition is stored on the watch, never
    # dropped. It is requested either in this turn ("watch X, tell me
    # if it goes out of stock") or inherited from the running watch
    # (a later cadence change must not wipe it — upsert overwrites
    # condition_params wholesale). A stock-only change — every other
    # setting identical to the stored row — is applied directly
    # below, so the reply says what actually changed instead of the
    # upsert's "Updated ... now checking <same cadence>".
    stock_alert = _wants_stock_alert(instruction) or _stock_alert_from_existing_watch(
        product_title, chat_id, db_path
    )
    if stock_alert:
        condition_params = {**condition_params, "stock_alert": True}
        alert_line += "• **Stock alert:** I'll also alert you if it goes out of stock\n"
    if stock_alert and _wants_stock_alert(instruction):
        _existing_row = _find_watch_by_product(product_title, chat_id, db_path)
        if _existing_row is not None:
            try:
                _stored = json.loads(_existing_row["condition_params"] or "{}")
            except Exception:
                _stored = {}
            if not isinstance(_stored, dict):
                _stored = {}
            _same_except_stock = (
                _existing_row["trigger_type"] == trigger_type
                and (_existing_row["interval_seconds"] or 300) == interval_sec
                and (_existing_row["schedule_cron"] or None) == (schedule_cron or None)
                and _existing_row["condition_type"] == condition_type
                and (_as_float(_stored.get("threshold")) if condition_type == "below_price" else None)
                == (below if condition_type == "below_price" else None)
                and ((_as_float(_stored.get("min_percent")) or 0) if condition_type == "price_drop" else None)
                == ((percent or 0) if condition_type == "price_drop" else None)
            )
            if _same_except_stock and _stored.get("stock_alert") is not True:
                _stored["stock_alert"] = True
                with db.get_connection(db_path) as conn:
                    conn.execute(
                        "UPDATE monitoring_missions SET condition_params=? WHERE id=?",
                        (json.dumps(_stored), _existing_row["id"]),
                    )
                    conn.commit()
                hub.spawn(hub.broadcast_control_state())
                return (
                    f"Done — the watch for **{product_title}** will now also alert you if it goes "
                    f"out of stock, in addition to its price check "
                    f"{_describe_interval(interval_sec) if trigger_type == 'interval' else 'daily'}.\n\n"
                    f"• **Watch ID:** `{_existing_row['id']}`\n"
                    f"• **Status:** {_existing_row['status']}\n\n"
                    f"<!-- privagent-watch: {_existing_row['id']} -->"
                )

    # A follow-up that only changes the cadence ("check its price and
    # just alert me every 5 minutes") must not create a SECOND watch
    # for the same product — the first watch already checks every 5
    # minutes, so a duplicate would double-alert on the same drop.
    # But the guard must compare the REQUESTED settings with the
    # STORED row: a daily cron watch exists and the user asks for
    # every 5 minutes is a settings CHANGE, not a duplicate — the old
    # code answered "is being checked every 5 minutes" and created
    # nothing, so the reply was false and the request was silently
    # dropped. Same settings -> say so; different settings -> update
    # the existing mission in place and say what changed.
    # Stream 3: the dedup/update/insert itself lives in
    # upsert_monitor_mission (shared with the structured
    # orchestration pipeline); this handler only builds the replies.
    upsert = upsert_monitor_mission(
        product_title=product_title, watch_title=watch_title, target_url=target_url,
        trigger_type=trigger_type, schedule_cron=schedule_cron,
        interval_seconds=interval_sec, condition_type=condition_type,
        condition_params=condition_params, below=below, percent=percent,
        stock_alert=True if stock_alert else None,
        chat_id=chat_id, db_path=db_path,
    )
    mission_id = upsert["mission_id"]
    existing = upsert["previous"]
    if upsert["outcome"] == "existing" and existing is not None:
        try:
            stored_params = json.loads(existing.get("condition_params") or "{}")
        except Exception:
            stored_params = {}
        stored_threshold = stored_params.get("threshold")
        stored_percent = stored_params.get("min_percent")
        cadence = _describe_interval(interval_sec) if trigger_type == "interval" else "daily"
        if below is not None:
            threshold_line = f", alerting when the price falls below \u20b9{below:g}"
        elif condition_type == "price_drop":
            threshold_line = _percent_threshold_text(
                percent if percent is not None else stored_percent
            )
        else:
            threshold_line = ""
        return (
            f"That watch is already running — **{product_title}** is being checked "
            f"{cadence}{threshold_line}."
        ) + f"\n\n<!-- privagent-watch: {existing['id']} -->"
    if upsert["outcome"] == "updated" and existing is not None:
        try:
            stored_params = json.loads(existing.get("condition_params") or "{}")
        except Exception:
            stored_params = {}
        stored_threshold = _as_float(stored_params.get("threshold"))
        stored_percent = _as_float(stored_params.get("min_percent"))
        # Stream 2 (Failure E format-string fix): stored_params is
        # untyped JSON — a threshold stored as the string "5000"
        # used to crash the reply below at f"{stored_threshold:g}"
        # (ValueError: Unknown format code 'g' for object of type
        # 'str') and to fail the same-settings comparison against
        # the float the handler parses. Coerced here, both the
        # comparison and the :g formatting below see floats.
        # A bare price_drop watch stores min_percent 0 while a bare
        # re-ask parses percent None — compare them normalized (None
        # -> 0) or the re-ask never matches and the user gets a false
        # "Updated" instead of "already running".
        same_settings = (
            existing["trigger_type"] == trigger_type
            and existing["interval_seconds"] == interval_sec
            and (existing["schedule_cron"] or None) == (schedule_cron or None)
            and existing["condition_type"] == condition_type
            and (stored_threshold if condition_type == "below_price" else None)
            == (below if condition_type == "below_price" else None)
            and ((stored_percent or 0) if condition_type == "price_drop" else None)
            == ((percent or 0) if condition_type == "price_drop" else None)
        )
        if same_settings:
            cadence = _describe_interval(interval_sec) if trigger_type == "interval" else "daily"
            if below is not None:
                threshold_line = f", alerting when the price falls below ₹{below:g}"
            elif condition_type == "price_drop":
                threshold_line = _percent_threshold_text(
                    percent if percent is not None else stored_percent
                )
            else:
                threshold_line = ""
            return (
                f"That watch is already running — **{product_title}** is being checked "
                f"{cadence}{threshold_line}."
            ) + f"\n\n<!-- privagent-watch: {existing['id']} -->"
        old_cadence = (
            "daily"
            if existing["trigger_type"] == "cron"
            else _describe_interval(existing["interval_seconds"] or 300)
        )
        new_cadence = _describe_interval(interval_sec) if trigger_type == "interval" else "daily"
        old_threshold = (
            f", alerting below \u20b9{stored_threshold:g}"
            if stored_threshold is not None
            else (
                _percent_threshold_text(stored_percent)
                if existing["condition_type"] == "price_drop"
                else ""
            )
        )
        new_threshold = (
            f", alerting when the price falls below \u20b9{below:g}"
            if below is not None
            else (
                _percent_threshold_text(percent)
                if condition_type == "price_drop"
                else ""
            )
        )
        # Stream C (R-03): broadcast_control_state now exists on the
        # hub; the old try/except: pass hid the missing method.
        hub.spawn(hub.broadcast_control_state())
        from .monitoring_scheduler import format_ist_short

        return (
            f"Updated the watch for **{product_title}** — it was checking {old_cadence}{old_threshold}, "
            f"now checking {new_cadence}{new_threshold}.\n\n"
            f"• **Watch ID:** `{existing['id']}`\n"
            f"• **Next check:** {format_ist_short(now) or '—'}\n"
            f"• **Manage:** say `pause watch {existing['id'][:8]}`, `resume watch {existing['id'][:8]}`, "
            f"or `cancel watch {existing['id'][:8]}`, or use the card below."
        ) + f"\n\n<!-- privagent-watch: {existing['id']} -->"

    # Inform connected workspace clients (alerts land in the
    # conversation the watch was created in).
    # Stream C (R-03): broadcast_control_state now exists on the hub;
    # the old try/except: pass swallowed the AttributeError and no
    # client was ever told a watch was created/updated.
    hub.spawn(hub.broadcast_control_state())

    # The watch marker is machine-readable: the workspace strips it
    # before rendering and turns it into an interactive watch card
    # (Pause / Resume / Cancel / View changes) in the conversation.
    # Monitoring is chat-only since 2026-11-18 — there is no monitor
    # form or scheduled-missions page anymore, so the confirmation
    # must not point at the Control Center dashboard.
    # Stream C (task 3): the reply must carry the watch id, the next
    # check in IST, and a pause/resume/cancel affordance — the old
    # reply had none of the three (only the hidden card marker).
    from .monitoring_scheduler import format_ist_short

    intro = (
        f"I've configured continuous background price tracking for **{product_title}**.\n\n"
        if not is_generic
        else f"I'll watch **{product_title}** and alert you right here in this conversation when it changes.\n\n"
    )
    return (
        f"{reply_header}"
        f"{intro}"
        f"• **Status:** Active — I'll post an alert right here in this conversation the moment anything changes\n"
        f"• **Watch ID:** `{mission_id}`\n"
        f"• **Monitored Target:** [{product_title} ↗]({target_url})\n"
        f"{cadence_line}"
        f"{alert_line}"
        f"{dns_note}"
        f"• **Next check:** {format_ist_short(now) or '—'}\n"
        f"• **Manage:** say `pause watch {mission_id[:8]}`, `resume watch {mission_id[:8]}`, "
        f"or `cancel watch {mission_id[:8]}`, or use the card below\n"
        f"• **Persistence:** Stored in SQLite — checks continue automatically even if you navigate away\n\n"
        f"---\n\n"
        f"*I've got the watch running in the background — I'll post an alert right here in this conversation the moment anything changes. Catch you later, buddy!* 👋\n"
        f"<!-- privagent-watch: {mission_id} -->"
    )

