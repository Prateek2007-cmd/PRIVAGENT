"""Search-query construction from RESOLVED entities only (Stream 2,
CONTRACTS4 §2).

Failure E: a follow-up such as "watch this for every 5 mins" fell
through to the monitor's product extractor, whose weak ``for/of/on``
fallback took the interval phrase as the product name — the Flipkart
search query became the interval ("every 5 mins") instead of the
product the user meant. This module is the single place a search
query is built from a user instruction:

* deictic references (this / that one / the second one / the
  cheapest / the latest one) are resolved against the working
  context FIRST, and the resolved label is substituted in;
* pronouns, interval phrases, condition phrases and notification
  phrases are then stripped and can NEVER leak into the query;
* when nothing product-like remains, the builder returns "" — the
  caller must ask the user, never search garbage.

URL building uses ``urllib.parse.quote_plus`` + plain concatenation:
no ``str.format`` on any template, so a brace or ``%`` in user text
can never raise a format-string exception here.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional

from .working_context import WorkingContext, find_reference_token, resolve_label

_WORD_NUMBERS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|sixty"
)
_NUMBER = rf"(?:\d+(?:\.\d+)?|{_WORD_NUMBERS})"

# --- clause patterns: anything matching these is instruction metadata,
# --- never part of a search query.
_INTERVAL_RE = re.compile(
    rf"\bevery\s+(?:{_NUMBER}\s*)?(?:minutes?|mins?|hours?|hrs?|days?|weeks?|"
    r"mornings?|evenings?|nights?|seconds?)\b",
    re.IGNORECASE,
)
_INTERVAL_BARE_RE = re.compile(
    r"\b(?:daily|hourly|every\s+day|every\s+morning|every\s+evening|every\s+night)\b",
    re.IGNORECASE,
)
_CONDITION_RES = [
    # "when it is 10% off" / "when the price drops" / "when a new one appears"
    re.compile(r"\bwhen\s+[^,.;]+", re.IGNORECASE),
    # "if the price falls below ..." / "if it drops ..."
    re.compile(r"\bif\s+(?:the\s+)?(?:price|it|they)\b[^,.;]*", re.IGNORECASE),
    # "below ₹5000" / "under 5000" / "less than ₹5000"
    re.compile(rf"\b(?:below|under|less\s+than|at\s+most|up\s+to)\s*₹?\s*{_NUMBER}\b",
               re.IGNORECASE),
    # "10% off" / "drops by 10%" / "a 12.5% drop"
    re.compile(rf"\b(?:drops?|falls?|down|discount(?:ed)?|cut)\s+(?:by\s+)?{_NUMBER}\s*(?:%|percent|per\s*cent)\b",
               re.IGNORECASE),
    re.compile(rf"\b{_NUMBER}\s*(?:%|percent|per\s*cent)\s*(?:off|discount|drop|less|cheaper)\b",
               re.IGNORECASE),
    # "price drops / falls / goes below ..."
    re.compile(r"\bprice\s+(?:drops?|falls?|goes?|reaches?)\b[^,.;]*", re.IGNORECASE),
]
_NOTIFICATION_RES = [
    re.compile(r"\b(?:alert|notify|tell)\s+me\b[^,.;]*", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+know\b[^,.;]*", re.IGNORECASE),
    re.compile(r"\bsend\s+me\b[^,.;]*", re.IGNORECASE),
    re.compile(r"\bping\s+me\b[^,.;]*", re.IGNORECASE),
]
_MONITOR_VERB_RE = re.compile(
    r"^(?:please\s+|pls\s+)?(?:watch|monitor|track|check|follow|find|search|"
    r"look\s+for|show\s+me|get\s+me|list|keep\s+(?:an\s+)?eye\s+on)\s+",
    re.IGNORECASE,
)
_PRICE_OF_RE = re.compile(r"\b(?:the\s+)?price\s+of\b", re.IGNORECASE)
_PRONOUN_PHRASE_RE = re.compile(
    r"\b(?:this|that|these|those)\s+(?:product|item|one|result|listing|price|thing)\b",
    re.IGNORECASE,
)
_PRONOUN_RE = re.compile(r"\b(?:this|that|it|these|those|them|they|he|she)\b", re.IGNORECASE)
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_TRAILING_PREP_RE = re.compile(
    r"\s+(?:for|of|on|at|to|in|with|from|by|and|or|when|if|below|under|above|over)$",
    re.IGNORECASE,
)
_LEADING_PREP_RE = re.compile(
    r"^(?:for|of|on|at|to|in|with|from|by)\s+", re.IGNORECASE
)
_NOTIFY_LED_RE = re.compile(
    r"^(?:alert|notify|tell)\s+(?:me\s+)?", re.IGNORECASE
)

# A lone word from this set names no product — it is what's left
# after the real content was stripped ("product for" -> "product",
# "watch this for every 5 mins" -> "for"). Prepositions/articles
# are in the set because a lone one is a dangling fragment, never a
# product.
_LONE_GENERIC = {
    "product", "item", "price", "watch", "thing", "one", "result",
    "listing", "deal", "offer", "page", "site", "website", "it",
    "for", "of", "on", "at", "to", "in", "with", "from", "by",
    "a", "an", "the",
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "twenty": 20, "thirty": 30, "sixty": 60,
}

_INTERVAL_UNIT_RE = re.compile(
    rf"\bevery\s+({_NUMBER})\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|seconds?)\b",
    re.IGNORECASE,
)

_CONDITION_BELOW_RE = re.compile(
    rf"\b(?:below|under|less\s+than)\s*₹?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE
)
_CONDITION_PERCENT_RE = re.compile(
    rf"\b(\d+(?:\.\d+)?|{_WORD_NUMBERS})\s*(?:%|percent|per\s*cent)\s*(?:off|drop|discount|less|cheaper)\b",
    re.IGNORECASE,
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" ,.;:!?\"'").strip()


def sanitize_query_text(text: str) -> str:
    """Strip everything that is not the product/topic from a phrase.

    Removes, in order: notification phrases, condition phrases,
    interval phrases, monitor verbs, "price of", pronoun phrases and
    bare pronouns, leading/trailing prepositions and articles. Returns
    "" when nothing product-like remains — a lone generic noun
    ("product", "price") does not count.
    """
    if not text:
        return ""
    out = str(text)
    for pattern in _NOTIFICATION_RES:
        out = pattern.sub(" ", out)
    for pattern in _CONDITION_RES:
        out = pattern.sub(" ", out)
    out = _INTERVAL_RE.sub(" ", out)
    out = _INTERVAL_BARE_RE.sub(" ", out)
    out = _MONITOR_VERB_RE.sub(" ", out)
    out = _PRICE_OF_RE.sub(" ", out)
    out = _PRONOUN_PHRASE_RE.sub(" ", out)
    out = _PRONOUN_RE.sub(" ", out)
    out = _NOTIFY_LED_RE.sub(" ", out)
    out = _collapse(out)
    # Repeat the edge trims: stripping a middle clause can expose a
    # new dangling preposition ("noise cancelling headphones for" ->
    # "noise cancelling headphones").
    for _ in range(3):
        before = out
        out = _LEADING_ARTICLE_RE.sub("", out)
        out = _LEADING_PREP_RE.sub("", out)
        out = _TRAILING_PREP_RE.sub("", out)
        out = _collapse(out)
        if out == before:
            break
    if not out:
        return ""
    words = out.split()
    if len(words) == 1 and words[0].lower() in _LONE_GENERIC:
        return ""
    return out


def build_search_query(instruction: str,
                       ctx: Optional[WorkingContext] = None,
                       resolved_label: Optional[str] = None) -> str:
    """Build a search query from RESOLVED entities only.

    1. A deictic reference in the instruction is resolved against
       ``ctx`` (or ``resolved_label`` is used when the caller already
       resolved it) and the label is substituted in.
    2. The result is sanitised: pronouns, intervals, conditions and
       notification phrases can never leak into the query.
    3. "" means "no searchable entity" — the caller must ask the
       user instead of searching.
    """
    text = instruction or ""
    if resolved_label is None and ctx is not None:
        found = find_reference_token(text)
        if found is not None:
            prefer = "watch" if re.search(
                r"\b(?:change|cancel|pause|resume|stop|delete|update)\b",
                text, re.IGNORECASE) else None
            resolved_label = resolve_label(ctx, text, prefer=prefer)
    if resolved_label:
        found = find_reference_token(text)
        if found is not None:
            token = found[0]
            text = re.sub(re.escape(token), resolved_label, text,
                          count=1, flags=re.IGNORECASE)
        else:
            text = f"{resolved_label} {text}"
    clean = sanitize_query_text(text)
    if not clean:
        return ""
    # Search queries are case-insensitive: normalise so a resolved
    # label ("Vertex Pro laptop") and a typed phrase ("vertex pro
    # laptop") produce the same query.
    clean = clean.lower()
    # A query must name something: one surviving word is only kept
    # when it is a real noun the user typed (a brand/model fragment),
    # not a leftover verb or filler.
    words = clean.split()
    if len(words) == 1 and words[0].lower() in _LONE_GENERIC:
        return ""
    return clean


def build_store_search_url(label: str, store: str = "flipkart") -> str:
    """A retailer search URL for a RESOLVED label.

    The label must already be sanitised (see :func:`build_search_query`):
    this function refuses a blank label instead of building a search
    for garbage. Plain concatenation + ``quote_plus`` — no
    ``str.format`` on any template, so braces / ``%`` in the label
    cannot raise.
    """
    clean = sanitize_query_text(label or "")
    if not clean:
        return ""
    query = urllib.parse.quote_plus(clean)
    if store == "flipkart":
        return "https://www.flipkart.com/search?q=" + query
    if store == "amazon":
        return "https://www.amazon.in/s?k=" + query
    if store == "google":
        return "https://www.google.com/search?q=" + query
    return "https://www.google.com/search?q=" + query


def parse_interval_seconds(text: str) -> Optional[int]:
    """"every twelve minutes" / "every 2 hours" -> seconds.

    Word numbers and digits, minutes/hours/days/weeks/seconds.
    None when no interval phrase is present. Pure function — no
    context needed, so follow-ups can parse the cadence without
    touching the product resolution."""
    if not text:
        return None
    m = _INTERVAL_UNIT_RE.search(text)
    if not m:
        if re.search(r"\b(?:daily|everyday|every\s+day)\b", text, re.IGNORECASE):
            return 24 * 3600
        return None
    num_s, unit = m.group(1), m.group(2).lower()
    num = int(num_s) if num_s.isdigit() else _NUMBER_WORDS.get(num_s.lower(), 0)
    if num <= 0:
        return None
    if unit.startswith("hour") or unit.startswith("hr"):
        seconds = num * 3600
    elif unit.startswith("day"):
        seconds = num * 86400
    elif unit.startswith("week"):
        seconds = num * 604800
    elif unit.startswith("second"):
        seconds = num
    else:
        seconds = num * 60
    return max(60, seconds)


def parse_relative_interval_factor(text: str) -> float | None:
    """"twice as often" / "double the frequency" -> 0.5 (check twice
    as often = half the interval); "half as often" -> 2.0. None when
    the text names no relative change. Stream 5 D10: the model used
    to claim "Frequency Doubled — Now Every 30 Minutes" while the DB
    row stayed at 3600 s — a relative change must resolve to a real
    factor against the watch's stored interval and be written +
    verified, never claimed in prose."""
    if not text:
        return None
    lowered = text.lower()
    if re.search(r"\b(?:twice|double|2x|two\s+times)\b[^.\n]{0,30}?\b(?:often|frequent(?:ly)?|as\s+often)\b",
                 lowered):
        return 0.5
    if re.search(r"\bhalf\b[^.\n]{0,30}?\b(?:often|frequent(?:ly)?|as\s+often)\b",
                 lowered):
        return 2.0
    return None


def parse_condition(text: str) -> Optional[dict[str, Any]]:
    """The condition named in a follow-up ("add this condition too").

    Returns ``{"kind": "below_price"|"percent_discount", "value": float,
    "operator": "below"|"drop_by"}`` or None when the text names no
    condition. Only the condition is returned — the product it
    applies to comes from working-context resolution, never from
    re-reading the raw phrase."""
    if not text:
        return None
    m = _CONDITION_BELOW_RE.search(text)
    if m:
        try:
            value = float(m.group(1))
        except ValueError:
            value = None
        if value and value > 0:
            return {"kind": "below_price", "value": value, "operator": "below"}
    m = _CONDITION_PERCENT_RE.search(text)
    if m:
        raw = m.group(1).lower()
        try:
            value = float(raw) if raw not in _NUMBER_WORDS else float(_NUMBER_WORDS[raw])
        except ValueError:
            value = None
        if value and 0 < value <= 100:
            return {"kind": "percent_discount", "value": value, "operator": "drop_by"}
    return None
