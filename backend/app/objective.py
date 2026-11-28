"""Structured objective understanding (Stream 1, CONTRACTS4 §1).

Every user instruction is first understood as a structured
:class:`Objective` — WHAT the user wants, about WHICH object, with
WHICH parameters — before any router, dispatcher or handler acts on
the raw text. Downstream code routes on the objective's
``primary_intent``, never by re-matching keywords in the raw text.

Design
------
* Deterministic fast paths first. The regex rules below cover the
  genuinely unambiguous shapes (a monitor verb + a product, an
  explicit "search for X", an email noun + an interval change).
* Contextual semantic classification (a model producing structured
  JSON validated against the schema in :func:`classify_semantic`)
  runs ONLY when no deterministic rule fires — i.e. the instruction
  is genuinely ambiguous. It is never on the hot path: the caller
  passes a ``semantic_classifier`` callable, and without one the
  instruction falls back to ``conversational`` with
  ``missing=["intent_ambiguous"]`` instead of guessing.
* Precedence rule (CONTRACTS4 §1, the Failure A fix): an instruction
  is classified by its OBJECT, never by a bare interval token.
  ``configure_email_polling`` matches ONLY when the object is
  email/inbox polling — an email noun is present AND no
  product/website/price noun is present. A monitoring interval
  ("every 10 minutes") is a PARAMETER of a monitor intent, never
  evidence of an email intent. A bare interval ("check every 5
  minutes") with no object at all is ambiguous unless the caller
  supplies ``context["has_active_email_mission"]``.
* Query hygiene (the Failure E fix): pronouns (``this/that/it``),
  interval phrases (``every 5 mins``), condition phrases (``when it
  is 10% off``) and notification phrases (``then remind me``) are
  parameters/references — they NEVER become part of a product name
  or a search query. :func:`_extract_product` returns ``""`` for a
  pronoun-only object and records the pronoun in ``references``.

No user example prompt or expected answer is hardcoded here. All
rules are generic patterns over verbs, nouns and parameters.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("app.objective")

# CONTRACTS5 requires the DETERMINISTIC_ROUTE lines in backend logs
# for verification, but uvicorn only configures its own loggers at
# INFO (the root logger stays at WARNING), so INFO records from this
# module would be swallowed — verified live 2026-11-27: zero
# DETERMINISTIC_ROUTE lines in the backend log while WARNINGs did
# appear. Give this logger its own INFO handler; records still
# propagate, so test caplog keeps working, and the handler is added
# once so the lines appear exactly once.
if not logger.handlers:
    _route_handler = logging.StreamHandler()
    _route_handler.setLevel(logging.INFO)
    logger.addHandler(_route_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Canonical intent vocabulary (CONTRACTS4 §1). Every router/dispatcher
# maps into these strings and never invents a new one.
# ---------------------------------------------------------------------------

INTENTS = frozenset({
    "greet",
    "capability_query",
    "simple_search",
    "research",
    "monitor_product",
    "monitor_website",
    "monitor_new_listings",
    "monitor_inbox",
    "update_monitor",
    "cancel_monitor",
    "pause_monitor",
    "resume_monitor",
    "list_monitors",
    "schedule_task",
    "update_schedule",
    "cancel_schedule",
    "configure_email_polling",
    "browser_task",
    "ask_user_answer",
    "memory_store",
    "memory_recall",
    "mcp_task",
    "connector_task",
    "artifact_request",
    "conversational",
})

_MONITOR_INTENTS = frozenset({
    "monitor_product",
    "monitor_website",
    "monitor_new_listings",
    "monitor_inbox",
    "update_monitor",
    "cancel_monitor",
    "pause_monitor",
    "resume_monitor",
    "list_monitors",
})

# Completion criteria per primary_intent (CONTRACTS4 §3). A task that
# ends without meeting these is failed/blocked, never completed.
COMPLETION_CRITERIA: dict[str, list[str]] = {
    "simple_search": [
        ">=1 real search result with title+url, returned to the user",
    ],
    "research": [
        ">=3 accepted sources (HTTP 200, not 404/403/short)",
        "evidence table present",
        "artifact file exists on disk and read-back verified (when an artifact was requested)",
    ],
    "monitor_product": [
        "persisted watch row with correct product, interval_seconds and condition",
        "baseline observation row, or an honest baseline_pending state",
    ],
    "monitor_website": [
        "persisted watch row with correct url, interval_seconds and condition",
        "baseline observation row, or an honest baseline_pending state",
    ],
    "monitor_new_listings": [
        "persisted watch row with correct listing url and interval_seconds",
        "baseline observation row, or an honest baseline_pending state",
    ],
    "monitor_inbox": [
        "persisted email mission row with correct interval_seconds and condition",
        "baseline scan recorded, or an honest baseline_pending state",
    ],
    "update_monitor": ["the named watch row actually updated in the DB, verified by re-read"],
    "pause_monitor": ["the named watch row actually paused in the DB, verified by re-read"],
    "resume_monitor": ["the named watch row actually resumed in the DB, verified by re-read"],
    "cancel_monitor": ["the named watch row actually cancelled in the DB, verified by re-read"],
    "list_monitors": ["the current watch list returned to the user"],
    "schedule_task": ["persisted job row with next_run_at and a receipt"],
    "update_schedule": ["the named job row actually updated in the DB, verified by re-read"],
    "cancel_schedule": ["the named job row actually cancelled in the DB, verified by re-read"],
    "configure_email_polling": [
        "the email polling setting row actually changed, verified by re-read",
        "must NOT fire for any monitor intent",
    ],
    "browser_task": [
        ">=1 verified browser observation (real extension observation)",
        "the requested outcome achieved; a promise sentence is never completion",
    ],
    "artifact_request": [
        "the file exists, opens, and contains the requested content",
    ],
    "connector_task": ["execution receipt from the connector (id, status)"],
    "mcp_task": ["execution receipt from the MCP tool (id, status)"],
    "memory_store": ["store: row exists on re-read"],
    "memory_recall": ["recall: fact returned from the store"],
    "greet": ["greeting answered"],
    "capability_query": ["capability question answered"],
    "ask_user_answer": ["the pending question answered and the task resumed"],
    "conversational": ["a substantive answer returned to the user"],
}

# Registry tool names required per intent (see app/tool_registry.py).
_TOOLS: dict[str, list[str]] = {
    "simple_search": ["browser.navigate", "browser.observe"],
    "research": ["research.start_package", "browser.navigate", "browser.observe"],
    "monitor_product": ["monitoring.create_watch"],
    "monitor_website": ["monitoring.create_watch"],
    "monitor_new_listings": ["monitoring.create_watch"],
    "monitor_inbox": ["monitoring.create_watch"],
    "update_monitor": ["monitoring.create_watch"],
    "pause_monitor": ["monitoring.create_watch"],
    "resume_monitor": ["monitoring.create_watch"],
    "cancel_monitor": ["monitoring.create_watch"],
    "list_monitors": ["monitoring.create_watch"],
    "schedule_task": ["scheduling.create_job"],
    "update_schedule": ["scheduling.create_job"],
    "cancel_schedule": ["scheduling.create_job"],
    "browser_task": ["browser.navigate", "browser.observe"],
    "artifact_request": ["artifact.render_document"],
    "connector_task": [],
    "mcp_task": ["mcp_tool"],
    "memory_store": [],
    "memory_recall": ["memory.recall"],
    "ask_user_answer": ["interaction.ask_user"],
}

# Permission labels required per intent.
_PERMISSIONS: dict[str, list[str]] = {
    "simple_search": ["browser.navigate"],
    "research": ["browser.navigate"],
    "monitor_product": ["monitoring.create"],
    "monitor_website": ["monitoring.create"],
    "monitor_new_listings": ["monitoring.create"],
    "monitor_inbox": ["monitoring.create"],
    "update_monitor": ["monitoring.manage"],
    "pause_monitor": ["monitoring.manage"],
    "resume_monitor": ["monitoring.manage"],
    "cancel_monitor": ["monitoring.manage"],
    "list_monitors": ["monitoring.read"],
    "schedule_task": ["scheduling.create"],
    "update_schedule": ["scheduling.manage"],
    "cancel_schedule": ["scheduling.manage"],
    "configure_email_polling": ["email.polling.configure"],
    "browser_task": ["browser.navigate"],
    "artifact_request": ["artifact.create"],
    "connector_task": ["connector.execute"],
    "mcp_task": ["mcp.execute"],
    "memory_store": ["memory.store"],
    "memory_recall": ["memory.recall"],
    "ask_user_answer": [],
    "greet": [],
    "capability_query": [],
    "conversational": [],
}


@dataclass
class Objective:
    """The structured understanding of one user instruction."""

    raw: str
    primary_intent: str
    secondary_intents: list[str] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    interval_seconds: Optional[int] = None
    condition: Optional[dict[str, Any]] = None
    target_url: Optional[str] = None
    missing: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    # Stream B (CONTRACTS6): provenance of the routing decision —
    # "deterministic" (exactly one rule family fired), "semantic"
    # (the model classified an ambiguous/compound instruction), or
    # "fallback" (no usable classification exists). Dispatch reads
    # this; it never re-infers provenance from the text.
    source: str = "deterministic"
    # True when a semantic classifier was consulted and produced
    # nothing usable (raised / empty / invalid / non-canonical).
    # Dispatch treats that as "ask, don't guess" (see
    # intent_router.resolve_dispatch).
    semantic_failed: bool = False
    # True when semantic classification was structurally unavailable:
    # the configured provider is the mock provider (or no provider
    # could be built), so no real model exists to consult. This is
    # NOT a failure — the system runs on legacy routing, as designed
    # — and dispatch must NOT ask the user about it (see
    # build_objective). Distinct from semantic_failed, which is a
    # transient failure of a real provider.
    semantic_unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        # Stream B (CONTRACTS6): ``source`` / ``semantic_failed`` are
        # dispatch-local provenance carried on the live object, NOT
        # part of the serialized objective schema — the schema key set
        # is pinned by test_objective_routing.py (CONTRACTS4 §1) and
        # the verifier / companion read the dict for routing fields
        # only. ``from_dict`` still restores them when present.
        data = asdict(self)
        data.pop("source", None)
        data.pop("semantic_failed", None)
        data.pop("semantic_unavailable", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Objective":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _defaults_for_intent(primary_intent: str) -> dict[str, Any]:
    """Deliverables / completion criteria / tools / permissions for an intent."""
    deliverables: dict[str, list[str]] = {
        "simple_search": ["search_results"],
        "research": ["research_package"],
        "monitor_product": ["persisted_watch", "baseline"],
        "monitor_website": ["persisted_watch", "baseline"],
        "monitor_new_listings": ["persisted_watch", "baseline"],
        "monitor_inbox": ["persisted_watch", "baseline"],
        "update_monitor": ["updated_watch"],
        "pause_monitor": ["updated_watch"],
        "resume_monitor": ["updated_watch"],
        "cancel_monitor": ["updated_watch"],
        "list_monitors": ["watch_list"],
        "schedule_task": ["scheduled_job", "receipt"],
        "update_schedule": ["updated_job"],
        "cancel_schedule": ["updated_job"],
        "configure_email_polling": ["polling_setting"],
        "browser_task": ["browser_outcome"],
        "artifact_request": ["artifact_file"],
        "connector_task": ["connector_receipt"],
        "mcp_task": ["mcp_receipt"],
        "memory_store": ["stored_fact"],
        "memory_recall": ["recalled_fact"],
    }
    return {
        "deliverables": list(deliverables.get(primary_intent, [])),
        "completion_criteria": list(COMPLETION_CRITERIA.get(primary_intent, [])),
        "tools": list(_TOOLS.get(primary_intent, [])),
        "permissions": list(_PERMISSIONS.get(primary_intent, [])),
    }


def _make_objective(
    raw: str,
    primary_intent: str,
    *,
    secondary_intents: Optional[list[str]] = None,
    entities: Optional[dict[str, Any]] = None,
    references: Optional[list[str]] = None,
    interval_seconds: Optional[int] = None,
    condition: Optional[dict[str, Any]] = None,
    target_url: Optional[str] = None,
    missing: Optional[list[str]] = None,
    source: str = "deterministic",
    semantic_failed: bool = False,
    semantic_unavailable: bool = False,
) -> Objective:
    defaults = _defaults_for_intent(primary_intent)
    return Objective(
        raw=raw,
        primary_intent=primary_intent,
        secondary_intents=secondary_intents or [],
        entities=entities or {},
        references=references or [],
        interval_seconds=interval_seconds,
        condition=condition,
        target_url=target_url,
        missing=missing or [],
        deliverables=defaults["deliverables"],
        completion_criteria=defaults["completion_criteria"],
        tools=defaults["tools"],
        permissions=defaults["permissions"],
        source=source,
        semantic_failed=semantic_failed,
        semantic_unavailable=semantic_unavailable,
    )


# ---------------------------------------------------------------------------
# Noun sets for the CONTRACTS4 §1 precedence rule.
# ---------------------------------------------------------------------------

_EMAIL_NOUN_RE = re.compile(
    r"\b(?:e-?mails?|mail|inbox|mailbox|gmail|outlook|webmail)\b", re.IGNORECASE
)
# A product / website / price noun. When one of these is present, an
# interval token can never imply email polling.
_PRODUCT_NOUN_RE = re.compile(
    r"\b(?:product|price|prices|pricing|stock|stocks?|crypto|flight|flights?|"
    r"train|bus|hotel|order|package|delivery|news|ticket|weather|page|"
    r"website|webpage|site|url|link|phone|iphone|mobile|laptop|tablet|"
    r"watch|headphones|earbuds|camera|tv|gpu|shoes|deal|deals|item|listing|"
    r"share|shares?|bitcoin|ethereum)\b",
    re.IGNORECASE,
)
_WEBSITE_NOUN_RE = re.compile(
    r"\b(?:website|webpage|site|page|url|link)\b", re.IGNORECASE
)
_PRICE_NOUN_RE = re.compile(r"\b(?:price|prices|pricing|cost|₹|\$)\b", re.IGNORECASE)

_MONITOR_VERB_RE = re.compile(
    r"\b(?:keep\s+an\s+eye\s+on|keep\s+a?\s*watch\s+on|monitor|track|watch|alert|notify)\b",
    re.IGNORECASE,
)
_SEARCH_VERB_RE = re.compile(
    r"\b(?:search|find|look\s+up|look\s+for|check)\b", re.IGNORECASE
)
_RESEARCH_WORD_RE = re.compile(
    r"\b(?:research(?:ing|es)?|investigate|deep\s+dive|comprehensive|"
    r"multiple\s+(?:credible\s+)?sources?|independent\s+sources?|"
    r"cross-?reference|in\s+depth|survey)\b",
    re.IGNORECASE,
)
_DELIVERABLE_WORD_RE = re.compile(
    r"\b(?:report|presentation|slides?|slide\s*deck|spreadsheet|workbook|"
    r"document|whitepaper|dossier|pdf|docx|pptx|ppt|powerpoint|xlsx|csv|"
    r"markdown|html|comparison|excel)\b",
    re.IGNORECASE,
)
_NOTIFICATION_RE = re.compile(
    r"\b(?:remind|notify|alert|ping|tell\s+me|let\s+me\s+know)\b", re.IGNORECASE
)
_PRONOUN_RE = re.compile(r"\b(?:this|that|it|them|these|those)\b", re.IGNORECASE)

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}
_NUM_WORD_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))

_INTERVAL_RE = re.compile(
    # FIX-B (M3): "evry" is the common misspelling of "every" —
    # "check evry 20 mins" is a 1200 s interval, not a missing one.
    r"\b(?:every|evry)\s+(?:(\d+|" + _NUM_WORD_ALT + r")\s*)?"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
# A bare daily cadence ("every day", "daily") is a 24h interval.
_DAILY_RE = re.compile(r"\b(?:every\s+day|everyday|daily)\b", re.IGNORECASE)

_PERCENT_BEFORE_RE = re.compile(
    r"(?:drops?|falls?|down|discount(?:ed)?|cut)\s+(?:by\s+)?"
    r"(\d+(?:\.\d{1,2})?|" + _NUM_WORD_ALT + r")\s*(?:%|percent|per\s*cent)",
    re.IGNORECASE,
)
_PERCENT_AFTER_RE = re.compile(
    r"(\d+(?:\.\d{1,2})?|" + _NUM_WORD_ALT + r")\s*(?:%|percent|per\s*cent)"
    r"\s*(?:off|drops?|down|discount|less|cheaper)",
    re.IGNORECASE,
)
_BELOW_PRICE_RE = re.compile(
    r"(?:below|under|less\s+than|drops?\s+(?:to|below)|falls?\s+(?:to|below)|"
    r"reaches?)\s*(?:₹|rs\.?|\$)?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ABOVE_PRICE_RE = re.compile(
    r"(?:above|over|more\s+than|rises?\s+(?:to|above)|goes?\s+above)\s*"
    r"(?:₹|rs\.?|\$)?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    r"\b(?:back\s+in\s+stock|in\s+stock|available|availability)\b", re.IGNORECASE
)
_NEW_LISTING_RE = re.compile(
    r"\bnew\s+(?:[A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,3})\s+"
    r"(?:appears?|shows?\s+up|comes?\s+up|posted|listed)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)

_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|good\s+morning|good\s+afternoon|good\s+evening|"
    r"greetings|howdy|sup|yo)(?:\s+(?:there|man|buddy|friend|assistant|team|all|everyone))?[!.\s]*$",
    re.IGNORECASE,
)
_CAPABILITY_RE = re.compile(
    r"^(?:what\s+can\s+you\s+do|what\s+are\s+your\s+capabilities|"
    r"what\s+can\s+u\s+do|who\s+are\s+you|what\s+is\s+your\s+name|help)\b",
    re.IGNORECASE,
)
_REMEMBER_RE = re.compile(r"^\s*(?:please\s+)?remember\b", re.IGNORECASE)
_RECALL_RE = re.compile(
    r"\b(?:do\s+you\s+remember|what\s+did\s+i\s+(?:tell|say|ask)|"
    r"recall|what\s+do\s+you\s+know\s+about)\b",
    re.IGNORECASE,
)

# Clauses that are parameters, never part of a product name or query.
_TAIL_CUT_RES = [
    # interval clause
    re.compile(
        r"\s+every\s+(?:(?:\d+|" + _NUM_WORD_ALT + r")\s*)?"
        r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|mornings?|"
        r"evenings?|nights?)\b.*$",
        re.IGNORECASE,
    ),
    # condition clauses
    re.compile(r"\s+when\b.*$", re.IGNORECASE),
    re.compile(r"\s+if\b.*$", re.IGNORECASE),
    re.compile(
        r"\s+(?:\d+(?:\.\d{1,2})?|" + _NUM_WORD_ALT + r")\s*"
        r"(?:%|percent|per\s*cent)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"\s+(?:below|under|above|over)\s+(?:₹|rs\.?|\$)?\s*[\d,]+.*$", re.IGNORECASE),
    # notification clauses
    re.compile(r"\s*,?\s*(?:and\s+)?(?:then\s+)?(?:alert|notify|remind|tell|ping)\b.*$", re.IGNORECASE),
    re.compile(r"\s+and\s+(?:alert|notify|remind|tell|ping)\b.*$", re.IGNORECASE),
    # daily / clock-time clauses
    re.compile(r"\s+(?:daily|every\s+day|everyday)\b.*$", re.IGNORECASE),
    re.compile(r"\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b.*$", re.IGNORECASE),
    # trailing site / web markers (not the product)
    re.compile(r"\s+(?:on|from|at)\s+(?:the\s+)?(?:web|internet|google|amazon|flipkart)\b.*$", re.IGNORECASE),
    re.compile(r"\s+(?:online|on\s+the\s+web)\b.*$", re.IGNORECASE),
]

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "please", "pls",
    "bro", "man", "dude", "buddy", "pal", "friend", "me", "my", "i", "you",
    "u", "can", "could", "would", "should", "so", "just", "now", "then",
    "also", "and", "or", "for", "of", "on", "at", "to", "in", "with",
    "from", "by", "about", "as", "it", "this", "that", "them", "they",
    "he", "she", "his", "her", "their", "our", "your", "its", "one",
    "ones", "price", "check", "search", "find", "watch", "monitor",
    "track", "latest", "newest", "new", "released", "launched", "live",
}
_LEADING_FILLER_RE = re.compile(
    r"^(?:(?:please|hey|hi|hello|ok|okay|so|well|yeah|yep|yes|bro|dude|man|"
    r"can\s+you|can\s+u|cn\s+u|could\s+you|would\s+you)(?:[\s,]+|$))+",
    re.IGNORECASE,
)
_TRAILING_FILLER_RE = re.compile(
    r"(?:[\s,]+(?:so\s+)?(?:can|could|would)\s+(?:you|u)\b.*$"
    r"|[\s,]+(?:please|pls|bro|man|dude|buddy|pal|friend|for\s+me|thanks|thank\s+you)\s*$)",
    re.IGNORECASE,
)


def _parse_interval_seconds(text: str) -> Optional[int]:
    """Normalised interval in seconds, or None when no cadence is named.

    Accepts digit and word numbers, seconds/minutes/hours/days, and a
    bare daily cadence (24h). Clamped to a 60-second floor: a
    sub-minute tick would burn the scheduler.
    """
    if not text:
        return None
    m = _INTERVAL_RE.search(text)
    if m:
        num_s, unit = m.group(1), m.group(2).lower()
        if num_s is None:
            num = 1
        elif num_s.isdigit():
            num = int(num_s)
        else:
            num = _WORD_NUMBERS.get(num_s.lower(), 1)
        if unit.startswith("sec"):
            seconds = num
        elif unit.startswith("hour") or unit.startswith("hr"):
            seconds = num * 3600
        elif unit.startswith("day"):
            seconds = num * 86400
        else:
            seconds = num * 60
        return max(60, seconds)
    if _DAILY_RE.search(text):
        return 86400
    return None


def _parse_condition(text: str) -> Optional[dict[str, Any]]:
    """The alert condition named in the instruction, or None.

    Kinds: ``percent_discount`` ("10% off", "drops by 10%"),
    ``absolute_price`` ("below ₹5000", "above $100"),
    ``availability`` ("back in stock"), ``new_listing`` ("a new
    listing appears"). A bare price watch with no threshold is NOT a
    condition — the caller records ``price_change`` for it.
    """
    if not text:
        return None
    for pattern in (_PERCENT_BEFORE_RE, _PERCENT_AFTER_RE):
        m = pattern.search(text)
        if m:
            raw = m.group(1)
            value = float(_WORD_NUMBERS[raw.lower()]) if raw.lower() in _WORD_NUMBERS else float(raw.replace(",", ""))
            if 0 < value <= 100:
                return {"kind": "percent_discount", "value": value, "operator": ">="}
    m = _BELOW_PRICE_RE.search(text)
    if m:
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            value = None
        if value is not None and value > 0:
            return {"kind": "absolute_price", "value": value, "operator": "<"}
    m = _ABOVE_PRICE_RE.search(text)
    if m:
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            value = None
        if value is not None and value > 0:
            return {"kind": "absolute_price", "value": value, "operator": ">"}
    if _AVAILABILITY_RE.search(text):
        return {"kind": "availability", "value": True, "operator": "=="}
    if _NEW_LISTING_RE.search(text):
        return {"kind": "new_listing", "value": True, "operator": "=="}
    return None


def _find_references(text: str) -> list[str]:
    """Unresolved deictic tokens in the instruction, in order."""
    seen: list[str] = []
    for m in _PRONOUN_RE.finditer(text or ""):
        tok = m.group(0).lower()
        if tok not in seen:
            seen.append(tok)
    return seen


def _cut_tails(phrase: str) -> str:
    """Strip interval / condition / notification / site tails off a phrase."""
    out = (phrase or "").strip()
    for rx in _TAIL_CUT_RES:
        out = rx.sub("", out).strip()
    return out


def _clean_phrase(phrase: str) -> str:
    """Normalise a candidate product/query phrase; "" when unusable."""
    out = (phrase or "").strip()
    # A bare cadence is not a product ("check every 2 hours": the
    # phrase after the verb is only the interval).
    out = re.sub(
        r"^every\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b\s*,?\s*",
        "",
        out,
        flags=re.IGNORECASE,
    ).strip()
    out = _cut_tails(out)
    out = _LEADING_FILLER_RE.sub("", out).strip()
    out = _TRAILING_FILLER_RE.sub("", out).strip()
    out = re.sub(r"^(?:the|a|an)\s+", "", out, flags=re.IGNORECASE).strip()
    out = re.sub(r"^(?:price\s+of\s+|the\s+price\s+of\s+)", "", out, flags=re.IGNORECASE).strip()
    out = re.sub(r"^(?:the|a|an)\s+", "", out, flags=re.IGNORECASE).strip()
    out = out.strip("\"'").rstrip(".,;:!?").strip()
    out = re.sub(r"\s+", " ", out).strip()
    # A URL is a target, never a product name.
    if re.search(r"https?://", out):
        return ""
    # Drop trailing dangling stopwords left behind by a tail cut
    # ("Sony headphones for" -> "Sony headphones", "OnePlus 13
    # price" -> "OnePlus 13" — "the X price" is the price OF X).
    _words = out.split()
    while _words and _words[-1].lower() in {
        "for", "of", "on", "at", "to", "in", "with", "and", "the", "a", "an",
        "price", "prices", "cost",
    }:
        _words.pop()
    out = " ".join(_words)
    if not out:
        return ""
    words = out.split()
    # A product/query never starts with a preposition or a verb.
    if words[0].lower() in {
        "on", "for", "over", "about", "of", "at", "to", "in", "with",
        "from", "by", "out", "and", "then", "so", "if", "when",
        "instead", "rather",
        "change", "update", "set", "modify", "start", "stop", "pause",
        "resume", "cancel", "delete", "remove", "create", "make", "give",
        "send", "tell", "remind", "schedule", "do", "run",
    }:
        return ""
    # Possessive pronouns name someone's account, not a product
    # ("my email", "your inbox" must never become a watch title or
    # a product entity — they are the email object). First-person
    # pronouns are the same shape ("I made" is a clause about the
    # user, never a product — it used to title a bogus watch).
    if words[0].lower() in {"my", "your", "his", "her", "our", "their", "its", "i", "we", "you"}:
        return ""
    # An email noun on its own is the mailbox, never a product.
    if all(w.lower() in {"email", "emails", "mail", "mails", "inbox", "mailbox", "gmail", "outlook"} for w in words):
        return ""
    # Pronoun-only is a reference, not a product (Stream 2 resolves
    # it from working context; never let it title a watch or a query).
    if all(w.lower() in {"this", "that", "it", "them", "these", "those", "one", "ones"} for w in words):
        return ""
    if all(w.lower() in _STOPWORDS for w in words):
        return ""
    if re.search(r"\d+\s*(?:%|percent)", out, re.IGNORECASE):
        return ""
    if re.search(r"\bevery\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b", out, re.IGNORECASE):
        return ""
    return out


def _extract_product(text: str) -> str:
    """The product named in the instruction, or "".

    Two shapes, both tail-cleaned so interval/condition/notification
    words can never leak in (Failure E):

    * the product comes BEFORE the monitor/search verb — "Galaxy S26
      Ultra 5G so can u watch the price of this ..." names the
      product up front, and the clause after the verb is only
      "the price of this";
    * the product comes AFTER the verb — "watch the price of the
      Sony headphones ..." — where "the price of" and any pronoun
      are stripped first.

    A pronoun-only object ("watch the price of this") returns ""
    and the pronoun is recorded in ``references`` instead.
    """
    if not text:
        return ""
    clean = (text or "").strip()
    # Shape 1: product before the first monitor/search verb.
    verb_m = re.search(
        r"\b(?:keep\s+an\s+eye\s+on|keep\s+a?\s*watch\s+on|monitor|track|watch|alert|notify|search|find|look\s+up|look\s+for|check)\b",
        clean,
        re.IGNORECASE,
    )
    if verb_m and verb_m.start() > 0:
        head = _clean_phrase(clean[: verb_m.start()])
        if head:
            return head
    # Shape 2: product after the verb.
    if verb_m:
        tail = clean[verb_m.end():]
        tail = re.sub(
            r"^\s+(?:the\s+)?(?:price\s+of\s+|cost\s+of\s+)?",
            "",
            tail,
            flags=re.IGNORECASE,
        ).strip()
        phrase = _clean_phrase(tail)
        if phrase:
            return phrase
    # Shape 3: no verb at all — the whole instruction minus openers
    # and tails, but ONLY when a condition is named ("iPhone 17 10%
    # off" as a statement). An interval alone ("iPhone 17 every 10
    # minutes") names no verb and no condition: without context it is
    # genuinely ambiguous, and Stream 2 resolves it from working
    # context — never guess a product from the bare cadence.
    if _parse_condition(clean) is not None:
        phrase = _clean_phrase(clean)
        if phrase:
            return phrase
    return ""


def _extract_search_query(text: str) -> str:
    """The query for a simple search: verbs/openers stripped, tails cut."""
    if not text:
        return ""
    clean = (text or "").strip()
    clean = re.sub(
        r"^(?:(?:please|hey|hi|hello|ok|okay|so|well|yeah|bro|dude|man|can\s+you|can\s+u|could\s+you|would\s+you)[\s,]+)+",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(
        r"^(?:search\s+for|search|find|look\s+up|look\s+for|check|"
        r"research(?:ing)?|investigate|study)\s+(?:about\s+|on\s+)?",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(
        r"^(?:the\s+)?price\s+of\s+",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r"^(?:the|a|an)\s+", "", clean, flags=re.IGNORECASE).strip()
    # A leading cadence is not a query ("check every 5 minutes").
    clean = re.sub(
        r"^every\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b\s*,?\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = _cut_tails(clean)
    # A trailing deliverable clause is not the query ("research X and
    # give me a pdf report" -> "X").
    clean = re.sub(
        r"\s+(?:and\s+)?(?:give|generate|create|prepare|make|produce|export|write|build)\s+"
        r"(?:me\s+)?(?:a\s+|an\s+|the\s+)?(?:\w+\s+)?"
        r"(?:pdf|docx|pptx|xlsx|csv|report|presentation|slides?|spreadsheet|document)\b.*$",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r"\s+(?:on\s+the\s+web|online|on\s+google)\b.*$", "", clean, flags=re.IGNORECASE).strip()
    clean = clean.strip("\"'").rstrip(".,;:!?").strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _strip_schedule_clause(text: str) -> str:
    """The scheduled task itself, with the cadence clause removed."""
    out = (text or "").strip()
    for rx in (
        re.compile(r"\bin\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE),
        re.compile(
            r"\bevery\s+(?:\d+\s*)?(?:minutes?|mins?|hours?|hrs?|days?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:daily|every\s+day|everyday|every\s+morning|each\s+morning)\b"
            r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:every\s+day|everyday|daily)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\btomorrow\b(?:\s+(?:morning|afternoon|evening))?"
            r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?",
            re.IGNORECASE,
        ),
    ):
        out = rx.sub("", out).strip(" ,.")
    return re.sub(r"\s+", " ", out).strip(" ,.")


def _detect_brand(product: str) -> Optional[str]:
    """Brand token of a product phrase, from the companion's brand data.

    Lazy import: the companion owns the brand list; objective.py must
    stay importable without it (tests import this module standalone).
    """
    if not product:
        return None
    try:
        from .continuous_monitoring_companion import _MONITORED_BRANDS

        brands = _MONITORED_BRANDS.split("|")
    except Exception:
        return None
    first = product.split()[0].lower() if product.split() else ""
    for brand in brands:
        if first == brand.lower():
            return brand
    m = re.search(r"\b(" + "|".join(re.escape(b) for b in brands) + r")\b", product, re.IGNORECASE)
    return m.group(1) if m else None


def _has_email_noun(text: str) -> bool:
    return bool(_EMAIL_NOUN_RE.search(text or ""))


def _has_product_noun(text: str) -> bool:
    return bool(_PRODUCT_NOUN_RE.search(text or "")) or bool(_extract_product(text or ""))


def _has_website_noun(text: str) -> bool:
    return bool(_WEBSITE_NOUN_RE.search(text or "")) or bool(_URL_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Semantic classification (model fallback, ambiguous instructions only)
# ---------------------------------------------------------------------------

SEMANTIC_PROMPT = (
    "You classify a user's instruction for a browser agent. Reply with JSON ONLY, no other text.\n"
    "Schema: {\"primary_intent\": \"<one of: "
    + ", ".join(sorted(INTENTS))
    + ">\", \"secondary_intents\": [\"...\"], \"entities\": {\"product\": \"...\", \"brand\": \"...\", \"query\": \"...\"}, "
    "\"references\": [\"this\"], \"interval_seconds\": <int or null>, "
    "\"condition\": {\"kind\": \"percent_discount|absolute_price|price_change|availability|new_listing|deadline\", \"value\": ..., \"operator\": ...} or null, "
    "\"target_url\": \"<url or null>\", \"missing\": [\"...\"]}\n"
    "Rules: an interval (\"every 10 minutes\") is a parameter, never evidence of an email intent; "
    "an email intent needs an email/inbox noun; pronouns, intervals, conditions and notification phrases "
    "are never part of a product name or search query.\n"
    "Instruction: "
)


def classify_semantic(
    text: str,
    call_model: Callable[[str], str],
) -> Optional[Objective]:
    """Classify an ambiguous instruction with a model, schema-validated.

    ``call_model`` takes the prompt and returns the model's raw reply
    (expected to be JSON). Returns None when the model output is
    missing, malformed, or names an intent outside the canonical
    vocabulary — the caller must then fall back to the deterministic
    result instead of trusting it. One exception propagates instead:
    :class:`app.semantic_classifier.MockProviderRefused` (the
    configured provider is the mock provider, so no real model
    exists). The caller (:func:`build_objective`) catches it to tell
    structural unavailability apart from a transient failure — a
    mock refusal must never be treated as "the model failed, ask
    the user", or every mock-provider turn would block on a
    question nobody can answer (Stream B regression, found live in
    the suite: compound sub-tasks hung 300 s in the ask gate).
    """
    if not text or not callable(call_model):
        return None
    try:
        raw_reply = call_model(SEMANTIC_PROMPT + text.strip())
    except Exception as exc:
        # Stream B: mock refusal is structural, not transient — let
        # it propagate (see docstring). Lazy import: this module
        # must not import semantic_classifier at top level.
        try:
            from .semantic_classifier import MockProviderRefused
        except Exception:
            MockProviderRefused = None  # type: ignore[assignment]
        if MockProviderRefused is not None and isinstance(exc, MockProviderRefused):
            raise
        return None
    if not raw_reply:
        return None
    try:
        data = json.loads(raw_reply)
    except (ValueError, TypeError):
        # Tolerate a fenced code block around the JSON.
        m = re.search(r"\{.*\}", raw_reply, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    primary = data.get("primary_intent")
    if not isinstance(primary, str) or primary not in INTENTS:
        return None
    secondary = data.get("secondary_intents") or []
    if not isinstance(secondary, list) or any(
        not isinstance(s, str) or s not in INTENTS for s in secondary
    ):
        return None
    entities = data.get("entities") or {}
    if not isinstance(entities, dict):
        return None
    references = data.get("references") or []
    if not isinstance(references, list) or any(not isinstance(r, str) for r in references):
        return None
    interval = data.get("interval_seconds")
    if interval is not None and (not isinstance(interval, int) or isinstance(interval, bool) or interval < 0):
        return None
    condition = data.get("condition")
    if condition is not None and not isinstance(condition, dict):
        return None
    target_url = data.get("target_url")
    if target_url is not None and not isinstance(target_url, str):
        return None
    missing = data.get("missing") or []
    if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
        return None
    return _make_objective(
        text,
        primary,
        secondary_intents=[s for s in secondary if s != primary],
        entities={str(k): v for k, v in entities.items()},
        references=references,
        interval_seconds=interval,
        condition=condition,
        target_url=target_url or None,
        missing=missing,
        # Stream B (CONTRACTS6): a model-built objective carries its
        # provenance so dispatch can tell it apart from a keyword
        # fast-path result without re-matching the text.
        source="semantic",
    )


# ---------------------------------------------------------------------------
# Deterministic objective construction
# ---------------------------------------------------------------------------

_MONITOR_CONTROL_RE = re.compile(
    r"\b(?:pause|resume|cancel|stop|delete|remove)\b[^.\n]{0,40}?"
    r"\b(?:watch|monitor|alert|mission)\b|"
    r"\b(?:pause|resume|cancel|stop)\s+(?:the\s+)?(?:email|mail)\s+"
    r"(?:monitoring|monitor|watch)\b",
    re.IGNORECASE,
)
_UPDATE_MONITOR_RE = re.compile(
    r"\b(?:change|update|modify)\b[^.\n]{0,40}?"
    r"\b(?:watch|monitor|alert|mission)\b|"
    r"\b(?:watch|monitor)\b[^.\n]{0,40}?\b(?:every|interval)\b.*\b(?:change|update)\b",
    re.IGNORECASE,
)
# Pronoun/deictic interval update (S5 gen B5): "change it to every 45
# mins", "update it to check every 45 minutes" — a change verb + a
# pronoun referent + an interval, with no watch noun and no product.
# The referent lives in working context (Stream 2); the handler
# updates the existing watch in place when reached with history.
_PRONOUN_INTERVAL_UPDATE_RE = re.compile(
    r"\b(?:change|update|modify|switch|make)\b[^.\n]{0,40}?"
    r"\b(?:it|that|this|them)\b[^.\n]{0,40}?"
    r"\bevery\s+(?:\d+\s*)?(?:minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
# Relative interval update (S5 gen D10): "make that check twice as
# often". The new interval needs the watch's current interval, read
# from the DB by Stream 2/3 — the objective carries the factor, never
# a guessed number.
_RELATIVE_INTERVAL_RE = re.compile(
    r"\b(?:twice|half)\s+as\s+often\b",
    re.IGNORECASE,
)
_LIST_MONITORS_RE = re.compile(
    r"\b(?:list|show|what)\b[^.\n]{0,40}?\b(?:watches|monitors|missions|alerts)\b"
    r"(?:\s+(?:are|do|have|am)\b[^.\n]*)?$"
    # FIX-B (M6): "what did I ask you to watch earlier" is a
    # watch-summary ask, not a memory question — it must route to
    # list_monitors (answered deterministically from the DB + history)
    # instead of memory_recall, where the model denied the request.
    r"|\bwhat\s+did\s+i\s+ask\s+you\s+to\s+(?:watch|monitor|track)\b"
    r"|\bwhat\s+(?:have\s+i\s+asked|did\s+i\s+ask)\b[^.\n]{0,40}?\b(?:watch|monitor|track)\b",
    re.IGNORECASE,
)
_SCHEDULE_VERB_RE = re.compile(
    r"\b(?:schedule|remind|do|run|repeat|perform|summari[sz]e|give|tell|"
    r"send|fetch|search|generate|create|write)\b",
    re.IGNORECASE,
)
_SCHEDULE_CADENCE_RE = re.compile(
    r"\b(?:every\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?"
    r"(?:minutes?|mins?|hours?|hrs?|days?)|daily|every\s+day|everyday|"
    r"tomorrow|in\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_SCHEDULE_CONTROL_RE = re.compile(
    r"\b(?:cancel|delete|remove|update|change)\b[^.\n]{0,40}?"
    r"\b(?:scheduled|schedule|job)\b",
    re.IGNORECASE,
)
_INTERVAL_CHANGE_VERB_RE = re.compile(
    r"\b(?:check|scan|monitor|sync|refresh|change|update|set)\b[^.\n]{0,40}?"
    r"\bevery\s+(?:\d+\s*)?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b|"
    r"^\s*every\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
_NAVIGATE_RE = re.compile(
    r"\b(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+"
    r"(?:the\s+)?(?:https?://|[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+|"
    r"[a-z][a-z0-9\-]*\s+(?:website|webpage|site|page|docs|store|app|portal))",
    re.IGNORECASE,
)
_LATEST_RELEASE_RE = re.compile(
    r"\b(?:latest|newest|most\s+recent)\b[^.\n]{0,80}?"
    r"\b(?:released|launched|announced|out)\b|"
    r"\b(?:released|launched|announced)\b",
    re.IGNORECASE,
)
_EXPORT_RE = re.compile(
    r"\b(?:generate|create|export|save|compile|download|produce|make|give)\b"
    r"[^.\n]{0,60}?\b(?:pdf|docx|word|pptx|presentation|slides?|xlsx|excel|"
    r"spreadsheet|csv|markdown|html|report|document)\b"
    r"[^.\n]{0,40}?\b(?:previous|prior|last|above|this|that)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Precedence gate (Stream 1, CONTRACTS5): the deterministic fast path
# runs ONLY when the instruction is genuinely unambiguous.
#
# THE RULE (one sentence): exactly one deterministic rule may fire.
# Count the distinct primary intents the deterministic rules imply
# (``_deterministic_families``); 1 -> run the deterministic cascade
# below and log DETERMINISTIC_ROUTE; 0 (vague / pronoun-only, e.g.
# "hmm, what about that thing") or >= 2 (a compound, e.g. a monitor
# plus a schedule) -> semantic classification first. When semantic
# returns None / invalid / unavailable, the answer is honest
# (``conversational`` with missing=["intent_ambiguous"]) — never a
# guess, never a silent fall-through to an unrelated branch
# (especially never configure_email_polling: an interval token is a
# parameter, never evidence of an email intent).
#
# The family detectors below mirror the cascade sections' conditions,
# with the cascade's own priority order encoded as guards (a rule
# lower in the cascade cannot also claim an instruction a higher
# rule already owns — e.g. "check my email every 15 minutes" is
# email polling, not a search for "my email"; a URL inside a monitor
# instruction is the monitor's object, not navigation). ONE deliberate
# deviation: the cascade's schedule rule excludes monitor verbs, so a
# monitor+schedule compound would silently drop the schedule half;
# the gate counts it as TWO families so it reaches semantic instead.
# A bare remind/notify on a monitor ("then remind, check every 10
# minutes") is NOT a second intent — it is the monitor's own
# notification secondary — so only a non-notification schedule verb
# (schedule/do/run/...) counts as a second family next to a monitor.
# ---------------------------------------------------------------------------

# Schedule verbs for the gate. Deliberately narrower than
# _SCHEDULE_VERB_RE: "search"/"fetch" are retrieval verbs owned by
# the search family ("search for X every day" is a recurring lookup,
# not a scheduled job).
_SCHEDULE_GATE_VERB_RE = re.compile(
    r"\b(?:schedule|remind|do|run|repeat|perform|summari[sz]e|give|tell|"
    r"send|generate|create|write)\b",
    re.IGNORECASE,
)
# A schedule verb that is NOT a notification: next to a monitor verb
# only these count as a second intent. "tell" counts only when it is
# not "tell me" (a notification phrase).
_SCHEDULE_NON_NOTIFICATION_VERB_RE = re.compile(
    r"\b(?:schedule|do|run|repeat|perform|summari[sz]e|give|send|"
    r"generate|create|write)\b|\btell\b(?!\s+me\b)",
    re.IGNORECASE,
)
_STRONG_SEARCH_VERB_RE = re.compile(
    r"\b(?:search|find|look\s+up|look\s+for)\b", re.IGNORECASE
)
_EMAIL_ONLY_QUERIES = frozenset({
    "my email", "email", "mail", "my mail", "gmail", "inbox", "my inbox",
})


def _listing_noun_for(t: str, product: str, has_price: bool) -> Optional[str]:
    """Module-level form of the listings-noun rule (shared by the gate
    and the deterministic cascade so the two cannot drift apart)."""
    if has_price:
        return None
    if not re.search(
        r"\b(?:appears?|shows?\s+up|comes?\s+up|posted|listed)\b", t
    ):
        return None
    if product and product.lower().startswith("new "):
        noun = product[4:].strip()
        if noun and noun.lower() not in {"one", "ones", "it", "this", "that"}:
            return noun
    m = _NEW_LISTING_RE.search(t)
    if m:
        noun = m.group(1).strip()
        if noun and noun.lower() not in {"one", "ones", "it", "this", "that"}:
            return noun
    return None


def _monitor_control_intent(
    t: str, *, product: str, interval: Optional[int], references: list[str]
) -> Optional[str]:
    """The monitor-control intent (cascade §4) if one fires, else None.
    The watch-summary ask (list_monitors) is NOT included here: the
    cascade answers it in §3 (memory), so it belongs to that family.
    """
    if _MONITOR_CONTROL_RE.search(t):
        if re.search(r"\bpause\b", t, re.IGNORECASE):
            return "pause_monitor"
        if re.search(r"\bresume\b", t, re.IGNORECASE):
            return "resume_monitor"
        return "cancel_monitor"
    if _UPDATE_MONITOR_RE.search(t) and _MONITOR_VERB_RE.search(t):
        return "update_monitor"
    if interval is not None and _PRONOUN_INTERVAL_UPDATE_RE.search(t):
        return "update_monitor"
    _rel = _RELATIVE_INTERVAL_RE.search(t)
    if _rel and (
        references
        or _MONITOR_VERB_RE.search(t)
        or re.search(r"\bcheck\b", t, re.IGNORECASE)
    ):
        return "update_monitor"
    return None


def _monitor_intent(
    t: str,
    *,
    product: str,
    target_url: Optional[str],
    has_email: bool,
    has_product: bool,
    has_website: bool,
    has_price: bool,
    condition: Optional[dict[str, Any]],
    notify: bool,
) -> Optional[str]:
    """The monitor intent (cascade §6) if one fires, else None."""
    # A control turn (§4) or a watch-summary ask (§3) owns the
    # instruction first — a "watch" noun inside them is not a new
    # monitor (e.g. "what did I ask you to watch earlier" is a
    # list_monitors ask, not a monitor_product watch; "cancel the
    # watch" is a cancel, not a monitor).
    if _LIST_MONITORS_RE.search(t):
        return None
    if _MONITOR_CONTROL_RE.search(t):
        return None
    if _UPDATE_MONITOR_RE.search(t) and _MONITOR_VERB_RE.search(t):
        return None
    if _MONITOR_VERB_RE.search(t) and (
        product
        or target_url
        or has_website
        or has_email
        or _listing_noun_for(t, product, has_price) is not None
    ):
        if has_email and not has_product and not has_website and not has_price:
            return "monitor_inbox"
        if _listing_noun_for(t, product, has_price) is not None:
            return "monitor_new_listings"
        if (target_url or has_website) and not has_product and not has_price:
            return "monitor_website"
        if product or has_price:
            return "monitor_product"
        return "monitor_website"
    if (
        product
        and condition is not None
        and condition.get("kind") == "percent_discount"
        and notify
    ):
        return "monitor_product"
    return None


def _schedule_fires(t: str, *, monitor_fires: bool) -> bool:
    """The schedule_task rule (cascade §8) if it fires, else False."""
    if not (
        _SCHEDULE_GATE_VERB_RE.search(t) and _SCHEDULE_CADENCE_RE.search(t)
    ):
        return False
    if not monitor_fires:
        return True
    # A monitor verb is present: a bare remind/notify is the monitor's
    # own notification secondary, not a second job — only a
    # non-notification schedule verb is a second intent.
    return bool(_SCHEDULE_NON_NOTIFICATION_VERB_RE.search(t))


def _search_fires(
    t: str,
    *,
    query: str,
    monitor_fires: bool,
    control_fires: bool,
    email_polling_fires: bool,
) -> bool:
    """The simple_search rule (cascade §11) if it fires, else False."""
    if not _SEARCH_VERB_RE.search(t) or not query:
        return False
    if monitor_fires:
        # A monitor verb + object owns the instruction ("watch the
        # price of X" is a monitor, not a search); the search verb
        # inside it ("check every 20 minutes") is not a second intent.
        return False
    if control_fires:
        # A monitor-control turn owns the instruction too ("update it
        # to check every 45 minutes", "make that check twice as
        # often"): the "check" inside it is the control's verb, not
        # a search (cascade §4 precedes §11).
        return False
    if email_polling_fires:
        # "check my email every 15 minutes" is polling, not a search
        # for "my email" — unless a STRONG search verb names a real
        # query too ("search for Fairphone and check my email ..."),
        # which is a compound and both families must count.
        if not _STRONG_SEARCH_VERB_RE.search(t):
            return False
        if query.strip().lower() in _EMAIL_ONLY_QUERIES:
            return False
    return True


def _deterministic_families(
    t: str,
    ctx: dict[str, Any],
    *,
    product: str,
    target_url: Optional[str],
    has_email: bool,
    has_product: bool,
    has_website: bool,
    has_price: bool,
    interval: Optional[int],
    condition: Optional[dict[str, Any]],
    notify: bool,
    references: list[str],
) -> dict[str, str]:
    """Distinct primary intents the deterministic rules imply.

    Returns ``{family_label: primary_intent}`` — one entry per rule
    family that fires. Exactly one entry means genuinely unambiguous.
    """
    families: dict[str, str] = {}

    # §1. Greeting / capability question (whole-instruction matches).
    if _GREETING_RE.match(t):
        families["greet"] = "greet"
    if _CAPABILITY_RE.match(t):
        families["capability_query"] = "capability_query"

    # §2. Connector route supplied by the caller.
    if ctx.get("connector_route") is not None:
        families["connector_task"] = "connector_task"

    # §3. Memory (list first: a watch-summary ask beats recall).
    if _LIST_MONITORS_RE.search(t):
        families["memory"] = "list_monitors"
    elif _REMEMBER_RE.search(t):
        families["memory"] = "memory_store"
    elif _RECALL_RE.search(t):
        families["memory"] = "memory_recall"

    # §4. Monitor control.
    control = _monitor_control_intent(
        t, product=product, interval=interval, references=references
    )
    if control is not None:
        families["monitor_control"] = control

    # §5. Schedule control.
    if _SCHEDULE_CONTROL_RE.search(t):
        families["schedule_control"] = (
            "cancel_schedule"
            if re.search(r"\b(?:cancel|delete|remove)\b", t, re.IGNORECASE)
            else "update_schedule"
        )

    # §6. Monitor intents, classified by OBJECT.
    monitor = _monitor_intent(
        t,
        product=product,
        target_url=target_url,
        has_email=has_email,
        has_product=has_product,
        has_website=has_website,
        has_price=has_price,
        condition=condition,
        notify=notify,
    )
    if monitor is not None:
        families["monitor"] = monitor

    # §7. Email polling — ONLY when the object is email/inbox polling.
    email_polling = False
    if has_email and not has_product and not has_website and not has_price:
        if _INTERVAL_CHANGE_VERB_RE.search(t) or interval is not None:
            families["email_polling"] = "configure_email_polling"
            email_polling = True
    if (
        interval is not None
        and not has_product
        and not has_website
        and not has_price
        and not has_email
        and ctx.get("has_active_email_mission") is True
    ):
        families["email_polling"] = "configure_email_polling"
        email_polling = True

    # §8. Schedule a task (see _schedule_fires for the monitor rule).
    if _schedule_fires(t, monitor_fires=monitor is not None):
        families["schedule"] = "schedule_task"

    # §9. Research / deliverable.
    if _RESEARCH_WORD_RE.search(t) or (
        _DELIVERABLE_WORD_RE.search(t)
        and re.search(
            r"\b(?:prepare|create|generate|write|make|build|produce|export|need|want|give\s+me|research|compare)\b",
            t,
            re.IGNORECASE,
        )
    ):
        families["research"] = "research"

    # §10. Export of existing work (no new topic).
    if _EXPORT_RE.search(t) and not _RESEARCH_WORD_RE.search(t):
        families["export"] = "artifact_request"

    # §11. Simple search (guarded: see _search_fires).
    if _search_fires(
        t,
        query=_extract_search_query(t),
        monitor_fires=monitor is not None,
        control_fires=control is not None,
        email_polling_fires=email_polling,
    ):
        families["simple_search"] = "simple_search"

    # §12. Explicit navigation; a bare URL counts only when no other
    # family claimed the instruction (a URL inside a monitor/search
    # instruction is that instruction's object, not navigation).
    if _NAVIGATE_RE.search(t):
        families["navigate"] = "browser_task"
    elif target_url and not families:
        families["navigate"] = "browser_task"

    return families


def build_objective(
    text: str,
    context: Optional[dict[str, Any]] = None,
    semantic_classifier: Optional[Callable[[str], str]] = None,
) -> Objective:
    """Build the structured Objective for one instruction.

    Precedence rule (CONTRACTS5, Stream 1): the deterministic fast
    path runs ONLY when exactly one deterministic rule fires (see
    :func:`_deterministic_families`). Ambiguous (zero rules) or
    compound (>= 2 rules) instructions go to the semantic classifier
    first; when it is absent or returns None/invalid, the result is
    honestly ``conversational`` with missing=["intent_ambiguous"] —
    never a guess, never a fall-through to an unrelated branch.
    Stream B (CONTRACTS6): when >= 2 rules fired and the semantic
    result differs from what the deterministic cascade would have
    picked, a ``SEMANTIC_OVERRIDE`` line records the override
    (keyword_rule = the cascade's answer). The returned objective
    carries ``source`` ("deterministic" | "semantic" | "fallback")
    and ``semantic_failed`` so dispatch never re-infers provenance.
    ``context`` may carry ``has_active_email_mission`` (bool|None —
    a bare interval is ambiguous without it), ``conversation_history``,
    ``connector_route`` and ``task_id`` (used in the route log lines).
    """
    raw = text or ""
    t = raw.strip()
    ctx = context or {}
    references = _find_references(t)
    interval = _parse_interval_seconds(t)
    condition = _parse_condition(t)
    url_m = _URL_RE.search(t)
    target_url = url_m.group(0).rstrip(".,;!?)") if url_m else None
    product = _extract_product(t)
    has_email = _has_email_noun(t)
    has_product = bool(product) or _PRODUCT_NOUN_RE.search(t) is not None
    has_website = _has_website_noun(t)
    has_price = _PRICE_NOUN_RE.search(t) is not None
    notify = bool(_NOTIFICATION_RE.search(t))
    secondary = ["notify"] if notify else []

    if not t:
        return _make_objective(raw, "conversational", missing=["instruction"])

    # Precedence gate: exactly one deterministic rule -> fast path;
    # zero or >= 2 -> semantic classification first.
    families = _deterministic_families(
        t,
        ctx,
        product=product,
        target_url=target_url,
        has_email=has_email,
        has_product=has_product,
        has_website=has_website,
        has_price=has_price,
        interval=interval,
        condition=condition,
        notify=notify,
        references=references,
    )
    task_id = ctx.get("task_id") or "unknown"

    def _det() -> Objective:
        # The deterministic cascade (same arguments in both call
        # sites, so the gate and the conflict check cannot drift).
        return _build_deterministic(
            raw,
            t,
            ctx,
            references=references,
            interval=interval,
            condition=condition,
            target_url=target_url,
            product=product,
            has_email=has_email,
            has_product=has_product,
            has_website=has_website,
            has_price=has_price,
            notify=notify,
            secondary=secondary,
        )

    if len(families) == 1:
        obj = _det()
        logger.info(
            "DETERMINISTIC_ROUTE task_id=%s rule=%s", task_id, obj.primary_intent
        )
        expected = next(iter(families.values()))
        if obj.primary_intent != expected:
            # Tripwire: the gate and the cascade disagree. The
            # cascade's answer stands (behaviour unchanged), but the
            # mismatch is a gate bug to fix, not something to hide.
            logger.warning(
                "DETERMINISTIC_MISMATCH task_id=%s family=%s expected=%s got=%s text=%r",
                task_id,
                next(iter(families)),
                expected,
                obj.primary_intent,
                t[:120],
            )
        return obj
    _semantic_unavailable = False
    if semantic_classifier is not None:
        try:
            semantic = classify_semantic(t, semantic_classifier)
        except Exception as exc:
            # Stream B: a mock-provider refusal is structural
            # unavailability (no real model exists), not a failure —
            # see classify_semantic. Anything else is a transient
            # failure of a real provider.
            try:
                from .semantic_classifier import MockProviderRefused
            except Exception:
                MockProviderRefused = None  # type: ignore[assignment]
            if MockProviderRefused is not None and isinstance(exc, MockProviderRefused):
                semantic = None
                _semantic_unavailable = True
            else:
                raise
        if semantic is not None:
            if len(families) >= 2:
                # Stream B (CONTRACTS6): deterministic-vs-semantic
                # conflict. Two or more keyword families fired, so
                # the deterministic cascade WOULD have picked one —
                # if the model picked a different one, it overrode a
                # real keyword match, and that override is logged
                # (keyword_rule = the cascade's answer). Zero
                # families means no deterministic match existed, so
                # there is nothing to override and nothing is logged.
                try:
                    would_be = _det()
                except Exception:
                    would_be = None
                if (
                    would_be is not None
                    and would_be.primary_intent != "conversational"
                    and would_be.primary_intent != semantic.primary_intent
                ):
                    logger.info(
                        "SEMANTIC_OVERRIDE task_id=%s keyword_rule=%s semantic_result=%s reason=deterministic_cascade",
                        task_id,
                        would_be.primary_intent,
                        semantic.primary_intent,
                    )
            return semantic
    return _make_objective(
        raw,
        "conversational",
        references=references,
        missing=["intent_ambiguous"],
        # Stream B (CONTRACTS6): no usable classification exists.
        # semantic_failed is True only when a REAL classifier was
        # consulted and failed transiently (raised / empty /
        # invalid); semantic_unavailable is True when no real model
        # exists at all (mock provider) — the system then runs on
        # legacy routing, as designed, and dispatch must NOT ask the
        # user about it. With no classifier at all this is the old
        # honest fallback (both False).
        source="fallback",
        semantic_failed=semantic_classifier is not None
        and not _semantic_unavailable,
        semantic_unavailable=_semantic_unavailable,
    )


def _build_deterministic(
    raw: str,
    t: str,
    ctx: dict[str, Any],
    *,
    references: list[str],
    interval: Optional[int],
    condition: Optional[dict[str, Any]],
    target_url: Optional[str],
    product: str,
    has_email: bool,
    has_product: bool,
    has_website: bool,
    has_price: bool,
    notify: bool,
    secondary: list[str],
) -> Objective:
    """The deterministic cascade (the old build_objective body).

    Called ONLY when the precedence gate counted exactly one
    deterministic rule family. Every ``return`` below is a
    deterministic routing decision; the semantic fallback lives in
    :func:`build_objective`, not here.
    """

    def _monitor_objective(primary: str, entities_override: Optional[dict[str, Any]] = None) -> Objective:
        cond = condition
        missing: list[str] = []
        if cond is None:
            # A bare price watch with no threshold still watches:
            # any price change is the condition.
            cond = {"kind": "price_change", "value": None, "operator": None}
            missing.append("notification_condition")
        if interval is None:
            missing.append("check_interval")
        entities: dict[str, Any] = dict(entities_override) if entities_override else {}
        if product and "product" not in entities and primary != "monitor_new_listings":
            entities["product"] = product
            brand = _detect_brand(product)
            if brand:
                entities["brand"] = brand
        if target_url:
            entities["url"] = target_url
        return _make_objective(
            raw,
            primary,
            secondary_intents=secondary,
            entities=entities,
            references=references,
            interval_seconds=interval,
            condition=cond,
            target_url=target_url,
            missing=missing,
        )

    def _listing_noun() -> Optional[str]:
        """The thing a new-listings watch is for, or None.

        Delegates to the module-level rule shared with the precedence
        gate so the two cannot drift apart.
        """
        return _listing_noun_for(t, product, has_price)

    # 1. Greeting / capability question.
    if _GREETING_RE.match(t):
        return _make_objective(raw, "greet")
    if _CAPABILITY_RE.match(t):
        return _make_objective(raw, "capability_query")

    # 2. Connector route supplied by the caller (agentLoop ran the
    # connector router first): the connector owns it.
    if ctx.get("connector_route") is not None:
        route = ctx["connector_route"]
        connector = route.get("connector") if isinstance(route, dict) else getattr(route, "connector", None)
        return _make_objective(
            raw,
            "connector_task",
            entities={"connector": connector} if connector else {},
            references=references,
        )

    # 3. Memory.
    # FIX-B (M6): a watch-summary ask ("what did I ask you to watch
    # earlier") matches _RECALL_RE ("what did I ask") and would
    # otherwise classify as memory_recall, where the model denied the
    # request. The list check wins here so it routes to list_monitors
    # (deterministic DB + history answer); a plain recall question
    # ("what did I tell you about my laptop?") has no watch noun and
    # still reaches memory_recall below.
    if _LIST_MONITORS_RE.search(t):
        return _make_objective(raw, "list_monitors", references=references)
    if _REMEMBER_RE.search(t):
        return _make_objective(raw, "memory_store", references=references)
    if _RECALL_RE.search(t):
        return _make_objective(raw, "memory_recall", references=references)

    # 4. Monitor control (pause / resume / cancel / update / list).
    if _MONITOR_CONTROL_RE.search(t):
        verb = "pause_monitor" if re.search(r"\bpause\b", t, re.IGNORECASE) else (
            "resume_monitor" if re.search(r"\bresume\b", t, re.IGNORECASE) else "cancel_monitor"
        )
        entities: dict[str, Any] = {}
        if product:
            # A bare id after the verb ("pause watch abc12345") is a
            # watch reference, not a product name.
            if re.fullmatch(r"[0-9a-fA-F\-]{6,}", product):
                entities["watch_ref"] = product
            else:
                entities["product"] = product
        return _make_objective(
            raw, verb,
            entities=entities,
            references=references,
        )
    if _UPDATE_MONITOR_RE.search(t) and _MONITOR_VERB_RE.search(t):
        return _make_objective(
            raw, "update_monitor",
            entities={"product": product} if product else {},
            references=references,
            interval_seconds=interval,
            condition=condition,
            # "change that watch to every 15 minutes" names no product:
            # the referent is the watch in working context.
            missing=["watch_ref"] if not product and references else [],
        )
    # Pronoun-only interval update (B5): no watch noun, no product —
    # the interval + pronoun + change verb IS the intent. Without
    # this rule the turn fell to conversational (and, in the legacy
    # router, to the export/research sink).
    if interval is not None and _PRONOUN_INTERVAL_UPDATE_RE.search(t):
        return _make_objective(
            raw, "update_monitor",
            references=references,
            interval_seconds=interval,
            missing=["watch_ref"],
        )
    # Relative interval update (D10): "make that check twice as often".
    _rel = _RELATIVE_INTERVAL_RE.search(t)
    if _rel and (
        references
        or _MONITOR_VERB_RE.search(t)
        or re.search(r"\bcheck\b", t, re.IGNORECASE)
    ):
        factor = (
            "twice_as_often"
            if "twice" in _rel.group(0).lower()
            else "half_as_often"
        )
        return _make_objective(
            raw, "update_monitor",
            entities={"relative_interval": factor},
            references=references,
            missing=["watch_ref"],
        )
    if _LIST_MONITORS_RE.search(t):
        return _make_objective(raw, "list_monitors", references=references)

    # 5. Schedule control.
    if _SCHEDULE_CONTROL_RE.search(t):
        verb = "cancel_schedule" if re.search(r"\b(?:cancel|delete|remove)\b", t, re.IGNORECASE) else "update_schedule"
        return _make_objective(raw, verb, references=references)

    # 6. Monitor intents — classified by OBJECT (CONTRACTS4 §1).
    # An email noun with no product/website/price noun is an inbox
    # watch; a new-listing noun is a listings watch; a URL or website
    # noun is a website watch; anything else with a product is a
    # product watch. The interval is a parameter, never the object.
    if _MONITOR_VERB_RE.search(t) and (product or target_url or has_website or has_email or _listing_noun() is not None):
        if has_email and not has_product and not has_website and not has_price:
            return _monitor_objective("monitor_inbox")
        listing = _listing_noun()
        if listing is not None:
            return _monitor_objective(
                "monitor_new_listings", entities_override={"listing": listing}
            )
        if (target_url or has_website) and not has_product and not has_price:
            return _monitor_objective("monitor_website")
        if product or has_price:
            return _monitor_objective("monitor_product")
        return _monitor_objective("monitor_website")
    # A product + a drop condition + a notification verb is a product
    # watch even without an explicit monitor verb ("iPhone 17 10% off,
    # tell me when it drops").
    if product and condition is not None and condition.get("kind") == "percent_discount" and notify:
        return _monitor_objective("monitor_product")

    # 7. Email polling — ONLY when the object is email/inbox polling
    # (email noun present, no product/website/price noun). A bare
    # interval with no object is ambiguous unless the caller says an
    # email mission is active in this chat.
    if has_email and not has_product and not has_website and not has_price:
        if _INTERVAL_CHANGE_VERB_RE.search(t) or interval is not None:
            return _make_objective(
                raw,
                "configure_email_polling",
                interval_seconds=interval,
                references=references,
            )
    if interval is not None and not has_product and not has_website and not has_price and not has_email:
        if ctx.get("has_active_email_mission") is True:
            return _make_objective(
                raw,
                "configure_email_polling",
                interval_seconds=interval,
                references=references,
            )

    # 8. Schedule a task (a repeating/one-off job, not a watch).
    if (
        _SCHEDULE_VERB_RE.search(t)
        and _SCHEDULE_CADENCE_RE.search(t)
        and not _MONITOR_VERB_RE.search(t)
        and not has_product
        and not has_price
    ):
        task_desc = _strip_schedule_clause(t)
        return _make_objective(
            raw,
            "schedule_task",
            secondary_intents=secondary,
            entities={"task": task_desc} if task_desc else {},
            references=references,
            interval_seconds=interval,
        )

    # 9. Research / deliverable (explicit research words, a deliverable
    # artifact, or a multi-source comparison ask).
    if _RESEARCH_WORD_RE.search(t) or (
        _DELIVERABLE_WORD_RE.search(t)
        and re.search(r"\b(?:prepare|create|generate|write|make|build|produce|export|need|want|give\s+me|research|compare)\b", t, re.IGNORECASE)
    ):
        query = _extract_search_query(t)
        entities: dict[str, Any] = {}
        if product:
            entities["product"] = product
        if query:
            entities["query"] = query
        fmt_m = re.search(r"\b(pdf|docx|pptx|xlsx|csv|markdown|html)\b", t, re.IGNORECASE)
        deliverables_extra = [f"artifact:{fmt_m.group(1).lower()}"] if fmt_m else []
        obj = _make_objective(
            raw, "research",
            entities=entities,
            references=references,
            target_url=target_url,
        )
        obj.deliverables = obj.deliverables + deliverables_extra
        return obj

    # 10. Export of existing work (no new topic): an artifact request.
    if _EXPORT_RE.search(t) and not _RESEARCH_WORD_RE.search(t):
        return _make_objective(raw, "artifact_request", references=references, target_url=target_url)

    # 11. Simple search: a search verb + a query, with no research /
    # deliverable / compare / monitor signals. This is the Failure B
    # and C fix — "check the latest ... released" and "search for X"
    # are live lookups, not knowledge questions and not research.
    if _SEARCH_VERB_RE.search(t):
        query = _extract_search_query(t)
        if query:
            entities = {"query": query}
            brand = _detect_brand(query)
            if brand:
                entities["brand"] = brand
            return _make_objective(
                raw, "simple_search",
                entities=entities,
                references=references,
                target_url=target_url,
            )

    # 12. Explicit navigation to a site/URL.
    if _NAVIGATE_RE.search(t) or target_url:
        return _make_objective(
            raw, "browser_task",
            entities={"url": target_url} if target_url else {},
            references=references,
            target_url=target_url,
        )

    # 13. No deterministic rule fired. The gate only calls this
    # function when exactly one family fired, so reaching here means
    # the gate and the cascade disagree — the tripwire warning in
    # build_objective records it. Say so honestly instead of guessing.
    return _make_objective(raw, "conversational", references=references, missing=["intent_ambiguous"])
