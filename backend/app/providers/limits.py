"""Rate-limit hint parsing shared by the planner providers."""
from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"(?:(\d+)h)?(?:(\d+)m)?([\d\.]+)(m?s)", re.IGNORECASE
)


def parse_retry_seconds(text: str) -> float:
    """Seconds from hints like 'in 11h15m48.8s', 'in 899ms', 'in 23m13.2s'."""
    if not text:
        return 0.0
    match = re.search(
        r"(?:retry|try\s+again)\s+in\s+([0-9hms\.]+)", text, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r"(?:retryDelay'?\s*[:=]\s*'?)([0-9hms\.]+)", text, re.IGNORECASE
        )
    if not match:
        return 0.0
    raw = match.group(1).lower()
    milli = re.fullmatch(r"([\d\.]+)ms", raw)
    if milli:
        try:
            return float(milli.group(1)) / 1000.0
        except ValueError:
            return 0.0
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", raw):
        matched = True
        amount = float(value)
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        else:
            total += amount
    if matched:
        return total
    try:
        return float(raw)
    except ValueError:
        return 0.0


def quota_scope(text: str) -> str:
    """'day' for a daily cap, 'minute' for a short window, '' if unknown."""
    low = (text or "").lower()
    if any(k in low for k in ("tokens per day", "tpd", "requests per day", "rpd", "per day limit")):
        return "day"
    if any(k in low for k in ("tokens per minute", "tpm", "requests per minute", "rpm", "per minute limit")):
        return "minute"
    return ""
