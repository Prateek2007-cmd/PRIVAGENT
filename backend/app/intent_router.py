"""Task intent routing module.

Classifies task instructions before any browser-related operation occurs.
Non-browser tasks (conversations, coding, calculations, explanations) route directly
to the conversational model path without triggering browser tab discovery,
tab creation, extension action dispatch, or browser observation.

Autonomous research and multi-format artifact generation requests (DOCX, PDF, PPTX,
XLSX, CSV, Markdown, HTML) route to isolated browser research sessions and
preserve deliverable format metadata.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("app.intent_router")

# Same visibility fix as app.objective / app.semantic_classifier:
# uvicorn leaves the root logger at WARNING, so without this the
# DISPATCH_DECISION / SEMANTIC_OVERRIDE INFO lines would never reach
# the backend log.
if not logger.handlers:
    _route_handler = logging.StreamHandler()
    _route_handler.setLevel(logging.INFO)
    logger.addHandler(_route_handler)
logger.setLevel(logging.INFO)


class TaskIntent(str, Enum):
    NON_BROWSER = "NON_BROWSER"
    NEW_BROWSER_TASK = "NEW_BROWSER_TASK"
    CONTINUE_BROWSER_TASK = "CONTINUE_BROWSER_TASK"
    USE_EXISTING_USER_TAB = "USE_EXISTING_USER_TAB"
    RESEARCH_TASK = "RESEARCH_TASK"


class ResearchOperationType(str, Enum):
    NEW_RESEARCH = "NEW_RESEARCH"
    EXPORT_EXISTING_PACKAGE = "EXPORT_EXISTING_PACKAGE"
    REFINE_EXISTING_RESEARCH = "REFINE_EXISTING_RESEARCH"
    RESUME_EXISTING_MISSION = "RESUME_EXISTING_MISSION"
    NORMAL_CONVERSATION = "NORMAL_CONVERSATION"


@dataclass
class IntentClassification:
    intent: TaskIntent
    confidence: float
    reason: str
    target_tab_hint: Optional[str] = None
    is_ambiguous: bool = False
    requested_format: Optional[str] = None
    is_research: bool = False
    research_topic: Optional[str] = None
    deliverable_type: Optional[str] = None
    operation_type: Optional[ResearchOperationType] = None
    # Stream 1 (CONTRACTS4 §1): the structured objective for this
    # instruction (None when the objective builder failed). Downstream
    # code routes on objective.primary_intent, never by re-matching
    # keywords in the raw text.
    objective: Optional[Any] = None

    @property
    def is_browser(self) -> bool:
        """True if the classified intent requires a browser session."""
        return self.intent in (
            TaskIntent.NEW_BROWSER_TASK,
            TaskIntent.CONTINUE_BROWSER_TASK,
            TaskIntent.USE_EXISTING_USER_TAB,
            TaskIntent.RESEARCH_TASK,
        )

    @property
    def is_conversational(self) -> bool:
        """True if the classified intent is conversational (non-browser)."""
        return self.intent == TaskIntent.NON_BROWSER

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "targetTabHint": self.target_tab_hint,
            "isAmbiguous": self.is_ambiguous,
            "isBrowser": self.is_browser,
            "requestedFormat": self.requested_format,
            "isResearch": self.is_research,
            "researchTopic": self.research_topic,
            "deliverableType": self.deliverable_type,
            "operationType": self.operation_type.value if self.operation_type else None,
            "objective": self.objective.to_dict() if self.objective is not None else None,
        }


# ---------------------------------------------------------------------------
# Stream B (CONTRACTS6): semantic objective controls dispatch.
#
# Before this, agentLoop dispatched on ``classification.intent`` /
# ``classification.is_browser`` — the legacy keyword result — while
# the structured objective (semantic or deterministic) was attached
# to the classification and consulted in only two places
# (should_route_email_interval, the watch-control branch). A
# semantic ``browser_task`` for "what is latest from this" therefore
# never browsed, and a legacy RESEARCH_TASK for a watch ask never
# created a watch. :func:`resolve_dispatch` is the single place
# where the legacy result and the objective are reconciled, and the
# DISPATCH_DECISION line it feeds is the audit record of the choice
# actually taken at the dispatch site.
#
# Precedence rule (semantic wins on ambiguity/conflict):
#   1. Semantic failed (a REAL classifier consulted, transient
#      failure) + a legacy browser intent -> ASK, don't guess: the
#      turn ends ``awaiting_input`` with a clarification question
#      instead of running a browser/research workflow on a guess.
#      Structural unavailability (mock provider — no real model
#      exists) is NOT a failure and never asks: the system runs on
#      legacy routing, as designed. The ask is also suppressed for
#      compound sub-tasks (``allow_ask=False``): a part runs
#      autonomously inside its parent mission and nobody is there
#      to answer it — it falls through to legacy dispatch, which
#      fails honestly downstream instead of blocking the parent
#      for 300 s.
#   2. Semantic objective whose task type differs from the legacy
#      intent -> the semantic type wins (either direction:
#      NON_BROWSER -> browser, or browser -> NON_BROWSER).
#      Logged as SEMANTIC_OVERRIDE with the legacy keyword rule.
#   3. Otherwise the legacy intent stands. In particular a
#      DETERMINISTIC objective never overrules a legacy browser
#      intent: the legacy router owns two guards the single-family
#      cascade does not — the compound find+track policy (a "find X
#      and watch it" ask searches now and offers the watch; the
#      handler would create a watch without ever showing results)
#      and the how-to guard ("how do I check my email?" is advice,
#      not a search, even though the cascade says simple_search).
#      Verified live 2026-11-27: "check every 10 minutes for a
#      price drop on the Sony headphones" is a watch by the legacy
#      monitoring directive and a one-off simple_search by the
#      cascade — the legacy answer is the right one there.
#   4. CONTINUE_BROWSER_TASK / USE_EXISTING_USER_TAB are never
#      overridden: the semantic classifier has no notion of the
#      active session or the user's tab, and the browser gate
#      already asks when the tab request itself is ambiguous.
# ---------------------------------------------------------------------------

#: Objective intents whose execution needs a live browser session.
#: Every other canonical intent runs in the NON_BROWSER chain
#: (monitoring / schedule / memory / connector handlers, or the
#: conversational sink).
OBJECTIVE_BROWSER_INTENTS = frozenset({"simple_search", "research", "browser_task"})


def _effective_intent(objective_type: str, obj_is_browser: bool) -> TaskIntent:
    """The legacy TaskIntent equivalent of a semantic objective."""
    if not obj_is_browser:
        return TaskIntent.NON_BROWSER
    if objective_type == "research":
        return TaskIntent.RESEARCH_TASK
    return TaskIntent.NEW_BROWSER_TASK


@dataclass
class DispatchDecision:
    """The reconciled dispatch choice for one turn."""

    is_browser: bool
    source: str  # "semantic" | "deterministic" | "fallback" | "legacy"
    objective_type: Optional[str]
    legacy_intent: str
    overridden: bool
    effective_intent: TaskIntent
    await_input: bool
    keyword_rule: Optional[str] = None


def resolve_dispatch(
    classification: "IntentClassification",
    task_id: Optional[str] = None,
    allow_ask: bool = True,
) -> DispatchDecision:
    """Reconcile the legacy keyword intent with the structured objective.

    Pure (no I/O): safe to unit-test without a provider. The
    SEMANTIC_OVERRIDE log line is emitted here when the semantic
    objective overrules a disagreeing legacy intent; the
    DISPATCH_DECISION line itself is logged by the caller at the
    dispatch site via :func:`log_dispatch_decision`. ``allow_ask``
    is False for compound sub-tasks (nobody is there to answer);
    the caller (agentLoop) passes it from ``record.compound_part``.
    """
    legacy = classification.intent
    legacy_intent = legacy.value
    objective = getattr(classification, "objective", None)
    if objective is None:
        return DispatchDecision(
            is_browser=classification.is_browser,
            source="legacy",
            objective_type=None,
            legacy_intent=legacy_intent,
            overridden=False,
            effective_intent=legacy,
            await_input=False,
        )
    primary = getattr(objective, "primary_intent", None) or "conversational"
    source = getattr(objective, "source", "deterministic") or "deterministic"
    obj_is_browser = primary in OBJECTIVE_BROWSER_INTENTS
    # Session/tab intents are never overridden (rule 4 above).
    if legacy in (TaskIntent.CONTINUE_BROWSER_TASK, TaskIntent.USE_EXISTING_USER_TAB):
        return DispatchDecision(
            is_browser=classification.is_browser,
            source=source,
            objective_type=primary,
            legacy_intent=legacy_intent,
            overridden=False,
            effective_intent=legacy,
            await_input=False,
        )
    # Rule 1: semantic failed + a browser workflow would have run on
    # the legacy guess -> ask, don't guess. Only when a real provider
    # was consulted and failed transiently (semantic_failed) — never
    # for structural unavailability (semantic_unavailable: mock
    # provider, no real model) — and never for compound sub-tasks
    # (allow_ask=False: nobody is there to answer).
    if (
        getattr(objective, "semantic_failed", False)
        and not getattr(objective, "semantic_unavailable", False)
        and allow_ask
        and classification.is_browser
    ):
        return DispatchDecision(
            is_browser=False,
            source="fallback",
            objective_type=primary,
            legacy_intent=legacy_intent,
            overridden=True,
            effective_intent=TaskIntent.NON_BROWSER,
            await_input=True,
            keyword_rule=classification.reason,
        )
    # Rule 2: semantic wins when its task type differs from legacy.
    if source == "semantic":
        effective = _effective_intent(primary, obj_is_browser)
        if effective != legacy:
            # A compound find+track ask ("find the cheapest X and
            # watch its price") is deliberately a browser task: it
            # searches now and offers the watch. The semantic
            # objective says monitor_product, but routing it to the
            # watch handler would create a watch without ever
            # showing results — so the compound policy stands and
            # the semantic result does not override here.
            try:
                from .continuous_monitoring_companion import _is_compound_find_and_track

                _is_compound = _is_compound_find_and_track(
                    getattr(objective, "raw", "") or ""
                )
            except Exception:
                _is_compound = False
            if _is_compound and effective == TaskIntent.NON_BROWSER:
                return DispatchDecision(
                    is_browser=classification.is_browser,
                    source=source,
                    objective_type=primary,
                    legacy_intent=legacy_intent,
                    overridden=False,
                    effective_intent=legacy,
                    await_input=False,
                )
            logger.info(
                "SEMANTIC_OVERRIDE task_id=%s keyword_rule=%s semantic_result=%s reason=legacy_keyword_intent",
                task_id or "unknown",
                classification.reason,
                primary,
            )
            return DispatchDecision(
                is_browser=obj_is_browser,
                source="semantic",
                objective_type=primary,
                legacy_intent=legacy_intent,
                overridden=True,
                effective_intent=effective,
                await_input=False,
                keyword_rule=classification.reason,
            )
    # Rule 3: legacy stands.
    return DispatchDecision(
        is_browser=classification.is_browser,
        source=source,
        objective_type=primary,
        legacy_intent=legacy_intent,
        overridden=False,
        effective_intent=legacy,
        await_input=False,
    )


def fallback_second_failure(decision: DispatchDecision) -> DispatchDecision:
    """Second semantic failure, after the user already answered the ask
    gate once: do not ask again. The clarified text goes to the
    conversational path (NON_BROWSER) instead of running a workflow
    guessed from keywords.

    ``overridden`` stays True on purpose: the fallback overrules the
    legacy browser guess, so the dispatch site applies
    ``effective_intent`` (``if decision.overridden: classification.intent
    = decision.effective_intent``). A regression (2026-11-27, found
    live in the fallback run) set ``overridden=False`` here, which
    silently skipped that application — the task then dispatched on
    the legacy NEW_BROWSER_TASK while the DISPATCH_DECISION line
    recorded non-browser. The log and the dispatch must agree.
    """
    return replace(
        decision,
        await_input=False,
        overridden=True,
        is_browser=False,
        effective_intent=TaskIntent.NON_BROWSER,
    )


def log_dispatch_decision(task_id: Optional[str], decision: DispatchDecision) -> None:
    """The DISPATCH_DECISION audit line (CONTRACTS6 vocabulary).

    Logged at the dispatch site for every turn, overridden or not:
    ``source`` is the provenance of the objective that controlled
    the choice (semantic | deterministic | fallback | legacy when
    no objective exists), ``objective_type`` is its primary intent,
    ``legacy_intent`` is the keyword result it was reconciled with.
    """
    logger.info(
        "DISPATCH_DECISION task_id=%s source=%s objective_type=%s legacy_intent=%s",
        task_id or "unknown",
        decision.source,
        decision.objective_type or "none",
        decision.legacy_intent,
    )


# ---------------------------------------------------------------------------
# Format and Deliverable Detection Patterns (Step 4: Research & Artifacts)
# ---------------------------------------------------------------------------

FORMAT_PDF_RE = re.compile(r"(?:\bpdf\b|\.pdf\b)", re.IGNORECASE)
FORMAT_DOCX_RE = re.compile(r"(?:\bdocx\b|\bword(?:\s+doc(?:ument)?)?\b|\.docx\b)", re.IGNORECASE)
# Bare "ppt" is how users actually say it ("ppt report on topic X",
# "make a ppt", "a nice looking ppt") — it must read as pptx. The
# omission (live 2026-11-19) meant detect_requested_format returned
# None for the PPT instruction, so the request never reached the
# research pipeline and the agent hallucinated a `createpresentation`
# tool call instead. Compare FORMAT_XLSX_RE's "xcel" alias precedent.
FORMAT_PPTX_RE = re.compile(r"(?:\bpptx\b|\bppt\b|\bpowerpoint\b|\bpresentation\b|\bslide\s*deck\b|\bslides?\b|\.pptx\b|\.ppt\b)", re.IGNORECASE)
FORMAT_XLSX_RE = re.compile(r"(?:\bxlsx\b|\bexcel\b|\bxcel\b|\bspreadsheet\b|\bworkbook\b|\.xlsx\b)", re.IGNORECASE)
FORMAT_CSV_RE = re.compile(r"(?:\bcsv\b|\.csv\b)", re.IGNORECASE)
FORMAT_MD_RE = re.compile(r"(?:\bmarkdown\b|\.md\b)", re.IGNORECASE)
FORMAT_HTML_RE = re.compile(r"(?:\bhtml\b|\bwebpage\b|\.html?\b)", re.IGNORECASE)

# A bare leading deliverable preamble — a deliverable noun (or two)
# with a topic introducer and NO leading verb: "ppt report on topic X",
# "presentation about X", "report on X", "ppt topic X". The introducer
# (on/about/of/topic) is required, so a topic that legitimately starts
# with a deliverable noun ("report writing techniques") is untouched.
BARE_DELIVERABLE_PREAMBLE_RE = re.compile(
    r"^(?:(?:please|hey|hi|hello|ok|okay)[\s,]+)?"
    r"(?:(?:ppt|pptx|powerpoint|presentation|slides?|slide\s*deck|report|document|whitepaper|dossier|pdf|docx|xlsx|spreadsheet|workbook)\s+){1,2}"
    r"(?:(?:on|about|of)\s+(?:the\s+)?(?:topic\s*:?\s*)?|topic\s*:?\s+)",
    re.IGNORECASE,
)

# Words that can never be a messaging channel (review #5, M6/M7):
# month and weekday names, time words, and email words. "send the
# invoice to Priya in June" is timing, not a channel — the browser loop
# would otherwise try to send it immediately. "send the report to
# Prateek on email" belongs to the email-dispatch handler, not the
# browser. The negative lookahead is word-boundary-terminated, so a
# channel that merely STARTS with a stoplist word ("mayfair") still
# matches. No channel names are hardcoded here, so test_generalisation
# stays green.
_CHANNEL_STOPLIST = (
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
    r"|today|tomorrow|morning|afternoon|evening|tonight|week|month|year|the"
    r"|email|mail|gmail"
)


# A delivery / messaging / shopping clause inside a compound
# instruction: everything from the first such clause onward is NOT
# part of the research topic ("research about black holes and send it
# to prateek in a chat app" -> research half is "black holes"). The cut
# is used by extract_research_topic and decompose_search_queries; the
# remainder is kept by the compound splitter, never silently dropped.
# "send me the ppt about X" does NOT cut: the send has no destination
# clause (to/on/via/...) and no link noun, so it is a deliverable
# request about X, not a delivery of something else.
DELIVERY_CLAUSE_CUT_RE = re.compile(
    r"\s*(?:,\s*)?(?:and\s+)?(?:then\s+|also\s+)?"
    r"(?:send\s+(?:it|this|that|them|him|her|the\s+(?:summary|report|research|link|url|ppt|document|file|findings|results?|deck|presentation))\s+(?:to|on|via|through|in|over|by|for)\b"
    r"|send\s+(?:\S+\s+){0,3}?(?:link|url)\b"
    r"|ask\s+(?:a\s+)?(?:random\s+)?question\b"
    r"|ask\s+(?:him|her|them)\b"
    r"|share\s+(?:it|this|that|the|this\s+link|the\s+link)\b"
    r"|message\s+(?:it|this|that|him|her|them)\b)",
    re.IGNORECASE,
)


def cut_delivery_clauses(text: str) -> str:
    """Return the text before the first delivery/messaging/shopping clause.

    Used to keep the research topic clean in compound instructions.
    Returns the input unchanged when no delivery clause is present.
    """
    if not text:
        return text
    m = DELIVERY_CLAUSE_CUT_RE.search(text)
    if not m:
        return text
    return text[: m.start()].strip()


# Verbs that start a NEW sub-task inside a compound instruction.
# Deliberately narrow: mutation verbs owned by the mission-control
# dispatcher and the email+calendar compound handler (delete, add,
# create, update, remove, cancel, pause, resume, monitor) are NOT
# split verbs, so "check my email and delete promo emails" stays one
# mission and keeps its existing handling. A split needs a real clause
# boundary (comma / "and" / "then" / "also") AND one of these verbs,
# so "research Tom and Jerry" never splits on the "and".
_COMPOUND_SPLIT_VERBS = (
    r"send|ask|message|share|email|mail|open|research|search|"
    r"explain|describe|summarise|summarize"
)
_COMPOUND_SPLIT_RE = re.compile(
    r"(?:,\s*(?:and\s+)?(?:then\s+|also\s+)?|\s+and\s+(?:then\s+|also\s+)?)"
    r"(?=(?:" + _COMPOUND_SPLIT_VERBS + r")\b)",
    re.IGNORECASE,
)
_BARE_OPEN_SITE_RE = re.compile(
    # A bare "open <one word>" clause ("open a search engine", "open a store") is a
    # navigation preamble, not a task of its own: it folds into the
    # following part. Deliberately generic — no site names (see
    # test_generalisation: routing code must not hardcode sites; the
    # allow-list is data, not code).
    r"^\s*open\s+\w+\s*,?\s*$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+")
_RECIPIENT_RE = re.compile(
    # "send it to <name> in/on <any channel word>": the channel is any
    # single word — only the recipient (group 1) is used. No hardcoded
    # channel names (see test_generalisation).
    r"\bto\s+([A-Za-z][A-Za-z\-']{1,30})\s+(?:in|on|via|through|over)\s+\w+\b",
    re.IGNORECASE,
)
_RECIPIENT_FALLBACK_RE = re.compile(r"\bto\s+([A-Z][a-z]{1,30})\b")


# An "open" clause that refers back to the previous part's findings
# ("open the cheapest one", "open the first result", "open it") is not
# a new sub-task: it has no site or object of its own, and the runner
# threads only research summaries, recipients and links — not general
# findings — so the part would run blind (review #5, S8). A
# self-contained "open" clause ("open a store", "open the docs") still
# splits. The guard runs at split time (not as a merge afterwards) so
# splitting stays idempotent.
_ANAPHORIC_OPEN_RE = re.compile(
    r"^\s*open\s+(?:(?:the|that|this|those)\s+"
    r"(?:cheapest|first|best|top|newest|one|link|result|page|option|item)\b"
    r"|it\b|them\b)",
    re.IGNORECASE,
)


def split_compound_instruction(instruction: str) -> list[str]:
    """Split a compound instruction into sequential sub-tasks.

    "open a search engine, research about black holes and send it to prateek
    in a chat app, also ask a random question to him, and then open
    a store and send a phone link to prateek" becomes four parts:
    the research, the chat send, the question, and the store
    link send. A bare "open <site>" clause folds into the following
    part (it is that part's navigation preamble). Single instructions
    return unchanged, and splitting is idempotent: splitting any part
    returns just that part.
    """
    if not instruction:
        return [instruction]
    # Boundaries are found with the split regex, but a boundary whose
    # following clause is an anaphoric "open" (S8) is skipped — the
    # clause stays attached to the part whose findings it refers to.
    parts: list[str] = []
    _start = 0
    for _m in _COMPOUND_SPLIT_RE.finditer(instruction):
        if _ANAPHORIC_OPEN_RE.match(instruction[_m.end():]):
            continue
        parts.append(instruction[_start:_m.start()].strip())
        _start = _m.end()
    parts.append(instruction[_start:].strip())
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [instruction.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and _BARE_OPEN_SITE_RE.match(merged[-1]):
            merged[-1] = f"{merged[-1]}, {part}"
        else:
            merged.append(part)
    # A trailing bare "open <site>" with nothing after it is a real
    # task on its own; only fold when a following part exists.
    if len(merged) < 2:
        return [instruction.strip()]
    return merged


# The channel word of a messaging part ("in/on/via <word>") — generic,
# no hardcoded channel names. Only read from messaging-verb parts, so
# "go to paris in june" never sets a channel (review #4, N1). The
# channel slot excludes the stoplist (review #5, M6/M7): month/time
# words ("in June", "in the morning") and email words ("on email")
# are never channels.
_CHANNEL_RE = re.compile(
    r"\b(?:in|on|via|through|over)\s+(?!" + _CHANNEL_STOPLIST + r"\b)(\w+)\b",
    re.IGNORECASE,
)
_MESSAGING_PART_RE = re.compile(
    r"^\s*(?:(?:please|hey|hi|hello|can\s+you)\s*,?\s*)?(?:send|message|share|ask)\b",
    re.IGNORECASE,
)
# A messaging clause anywhere in a part ("open a store, send ... to
# prateek") — the part need not START with the verb, since a navigation
# preamble can precede it. The recipient is any name/pronoun except
# articles and group words ("the", "a", "everyone"), so "to the store"
# never counts as a recipient.
_MESSAGING_CLAUSE_RE = re.compile(
    r"\b(?:send|message|share|ask)\b.*?\b(?:to|with)\s+"
    r"(?!(?:the|a|an|everyone|everybody|all)\b)"
    r"(?:[A-Za-z][A-Za-z\-']{1,30}|him|her|them|me)\b",
    re.IGNORECASE,
)
_HAS_RECIPIENT_RE = re.compile(
    r"\b(?:to|with)\s+"
    r"(?!(?:the|a|an|everyone|everybody|all)\b)"
    r"(?:[A-Za-z][A-Za-z\-']{1,30}|him|her|them|me)\b",
    re.IGNORECASE,
)


def thread_compound_context(part: str, context: dict) -> str:
    """Resolve pronouns in a later part against earlier parts' results.

    "it" -> the summary (appended as a block), "him/her/them" -> the
    last named recipient, "the/that link" -> the first URL an earlier
    part produced, and a messaging part with a recipient but no channel
    inherits the last channel ("ask ... to Prateek" -> "... on <channel>").
    The wording is trigger-free by design: "the summary below" and the
    "Summary:" header contain no "research", so the threaded part can
    never be misread as a research directive (review #4, M1). All
    replacements run on the part text BEFORE the summary block is
    appended, so the summary itself is never rewritten (review #4, M2).
    Unresolvable references are left as-is; the runner skips a part
    whose dependency failed instead.
    """
    if not context or not part:
        return part
    out = part
    recipient = context.get("last_recipient")
    if recipient:
        out = re.sub(r"\b(him|her|them)\b", recipient, out, count=1, flags=re.IGNORECASE)
    links = context.get("found_links") or []
    if links and re.search(r"\b(?:the|that|this)\s+link\b", out, re.IGNORECASE):
        out = re.sub(r"\b(?:the|that|this)\s+link\b", links[0], out, count=1, flags=re.IGNORECASE)
    channel = context.get("last_channel")
    if (
        channel
        and _MESSAGING_CLAUSE_RE.search(out)
        and _HAS_RECIPIENT_RE.search(out)
        and not _CHANNEL_RE.search(out)
    ):
        out = f"{out} on {channel}"
    summary = context.get("research_summary")
    if summary and re.search(r"\bit\b", out, re.IGNORECASE):
        out = re.sub(r"\bit\b", "the summary below", out, count=1, flags=re.IGNORECASE)
        out = f"{out}\n\nSummary:\n{summary}"
    return out


def has_unresolved_reference(part: str, context: dict) -> bool:
    """True when a part refers to an earlier result that does not exist.

    "it" needs a research summary, "the/that link" needs a URL an
    earlier part produced. A recipient pronoun is never unresolved:
    the recipient comes from the part text itself, not an outcome.
    """
    if not context:
        context = {}
    if re.search(r"\bit\b", part, re.IGNORECASE) and not context.get("research_summary"):
        return True
    if re.search(r"\b(?:the|that|this)\s+link\b", part, re.IGNORECASE) and not context.get("found_links"):
        return True
    return False


def update_compound_context(context: dict, part: str, outcome: tuple) -> dict:
    """Fold one part's outcome into the threading context.

    outcome is (intent, state, message). The research summary is kept
    only from a completed research part; the recipient is read from
    the part text (it is known even when the part failed); URLs are
    collected from any completed part's message.
    """
    ctx = dict(context or {})
    intent, state, message = outcome
    if state == "completed" and intent == "RESEARCH_TASK" and message:
        ctx["research_summary"] = message[:3000]
    m = _RECIPIENT_RE.search(part) or _RECIPIENT_FALLBACK_RE.search(part)
    if m:
        ctx["last_recipient"] = m.group(1).strip().title()
    if _MESSAGING_PART_RE.match(part):
        ch = _CHANNEL_RE.search(part)
        if ch:
            ctx["last_channel"] = ch.group(1).strip().lower()
    if state == "completed" and message:
        found = [u.rstrip(".,)") for u in _URL_RE.findall(message)]
        if found:
            seen = set(ctx.get("found_links") or [])
            ctx["found_links"] = (ctx.get("found_links") or []) + [u for u in found if u not in seen][:5]
    return ctx




def detect_requested_format(text: str) -> Optional[str]:
    """Detect explicitly or strongly requested output file format."""
    if not text:
        return None
    if FORMAT_PDF_RE.search(text):
        return "pdf"
    if FORMAT_DOCX_RE.search(text):
        return "docx"
    if FORMAT_PPTX_RE.search(text):
        return "pptx"
    if FORMAT_XLSX_RE.search(text):
        return "xlsx"
    if FORMAT_CSV_RE.search(text):
        return "csv"
    if FORMAT_MD_RE.search(text):
        return "markdown"
    if FORMAT_HTML_RE.search(text):
        return "html"
    return None


DELIVERABLE_PRESENTATION_RE = re.compile(
    r"\b(?:presentation|slide\s*deck|slides?|powerpoint|pptx|ppt)\b",
    re.IGNORECASE,
)
DELIVERABLE_SPREADSHEET_RE = re.compile(
    r"\b(?:excel(?:\s+comparison)?|spreadsheet|workbook|xlsx|csv|matrix|data\s+table)\b",
    re.IGNORECASE,
)
DELIVERABLE_REPORT_RE = re.compile(
    r"\b(?:report|whitepaper|dossier|research\s+paper|assignment|document|doc|pdf|docx|word)\b",
    re.IGNORECASE,
)


def detect_deliverable_type(text: str) -> Optional[str]:
    """Detect high-level deliverable type (presentation, dataset, report)."""
    if not text:
        return None
    if DELIVERABLE_PRESENTATION_RE.search(text):
        return "presentation"
    if DELIVERABLE_SPREADSHEET_RE.search(text):
        return "dataset"
    if DELIVERABLE_REPORT_RE.search(text):
        return "report"
    return None


RESEARCH_DIRECTIVE_RE = re.compile(
    r"\b(?:research(?:ing|es)?|conduct\s+research|investigate|investigation|deep\s+dive|survey|comprehensive\s+analysis)\b",
    re.IGNORECASE,
)
PREPARE_ARTIFACT_RE = re.compile(
    r"\b(?:prepare|create|generate|write|make|build|produce|export|need|want|give\s+me|can\s+(?:i|we)\s+get)\s+(?:a|an)\s+(?:(?:new|detailed|comprehensive|fresh|research)\s+)*(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|ppt|powerpoint|xlsx|csv|html|markdown|comparison)\b",
    re.IGNORECASE,
)
EXPLANATION_WITH_DELIVERABLE_RE = re.compile(
    r"\b(?:explain|describe|analyze|summarize)\b.+?\b(?:in\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)|as\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)|with\s+diagrams\s+in\s+pdf|in\s+an?\s+excel\s+(?:comparison|sheet|workbook))\b",
    re.IGNORECASE,
)
COMPARISON_WITH_DELIVERABLE_RE = re.compile(
    r"\b(?:compare|comparison)\b.+?\b(?:in\s+(?:an?\s+)?(?:excel|spreadsheet|workbook|report|pdf|xlsx|csv)|generate\s+(?:an?\s+)?(?:excel|spreadsheet|workbook|report|pdf|xlsx|csv)\s+comparison|matrix|table)\b",
    re.IGNORECASE,
)

FOLLOW_UP_TRIGGERS_RE = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:could|can|would)\s+(?:you|u)\b"
    r"|(?:now|then|next|also|and\s+also)\s+(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|research|search|compare)\b"
    r"|(?:add|include|expand\s+(?:on\s+)?|elaborate\s+(?:on\s+)?|give|show|provide)\s+(?:more|further|additional|better|technical)\b"
    r"|(?:make\s+it|turn\s+it\s+into|convert\s+it\s+to|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\b"
    r"|(?:make|turn|convert|change|format|prepare)\s+(?:the\s+)?(?:previous\s+|prior\s+)?(?:report|analysis|document|research|deliverable|it)\b"
    r"|(?:i|we)\s+(?:need|want|would\s+like)\s+(?:a|an)?\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)?\s*[- ]?(?:page|slide)s?\b"
    r"|\b(?:more\s+(?:[a-zA-Z]+\s+)?details?|detailed\s+version|in\s+depth|expand\s+on|make\s+it\s+better|elaborate|supporting\s+images|with\s+diagrams|with\s+formulas)\b)",
    re.IGNORECASE,
)

# A reference to the research / work done so far — "based on till now",
# "based on research till now", "based on the work so far", "till now",
# "so far", "up to now". These are anaphoric: they name NO new subject
# and must resolve to the prior research topic from context, never be
# treated as a literal topic or search query. The noun after "based on"
# is limited to research-ish words, so an explicit new subject
# ("based on quantum mechanics") does NOT match — it stays a new
# research task.
PRIOR_RESEARCH_REF_RE = re.compile(
    r"^(?:based\s+on\s+)?"
    r"(?:(?:the\s+)?(?:research|work|findings|results|analysis|stuff|material|content|what\s+we\s+(?:have|found|did|covered|discussed|got|learned))\s+)?"
    r"(?:the\s+)?"
    r"(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date)"
    r"[!., \s]*$",
    re.IGNORECASE,
)


def is_prior_research_reference(text: str) -> bool:
    """True when the text is ONLY a reference to prior research (no new subject)."""
    if not text:
        return False
    return bool(PRIOR_RESEARCH_REF_RE.match(text.strip()))


EXPORT_DIRECTIVE_PATTERN = re.compile(
    # Leading conversational opener ("or atleast give a pdf
    # report..." — "or" / "at least" are not greetings, so the old
    # pattern anchored at ^ failed on the first word).
    r"^(?:(?:or|so|and|but)\s+)?(?:at\s+least\s+|atleast\s+)?"
    r"(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can\'t|cant|can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|just)\s+)?"
    r"(?:(?:can|could|may)\s+(?:i|we)\s+(?:get|have|receive)|(?:i|we)\s+(?:want|need|would\s+like)|generate|create|export|save|compile|download|produce|make|render|output|convert|turn|give|format|put|send|prepare|provide)\s+"
    r"(?:(?:me\s+|us\s+)?(?:the\s+|this\s+|that\s+|my\s+|it\s+)?)?"
    r"(?:previous\s+|prior\s+|current\s+|last\s+|collected\s+|above\s+)?(?:research\s+|analysis\s+|report\s+|findings\s+|results\s+|data\s+|it\s+|page\s+|content\s+|summary\s+)?\s*"
    r"(?:(?:in|into|as|to|of)\s+)?"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?"
    r"(pdf|docx|word(?:\s+doc(?:ument)?)?|pptx|powerpoint|presentation|slides?|slide\s*deck|xlsx|excel(?:\s+sheet)?|spreadsheet|workbook|csv|markdown|html|report|document)\b"
    # The format's own noun ("pdf report", "word document", "excel
    # sheet"): "report" was missing, so "give a pdf report" failed
    # even though "pdf" itself matched.
    r"(?:\s+(?:file|document|deck|sheet|format|report))?"
    # A pronoun topic reference ("about this", "about this topic",
    # "of the previous report"): only pronouns (optionally followed by
    # a deliverable noun) / the-above-style references, never a
    # free-form subject. An explicit new subject ("about Quantum
    # Mechanics") must NOT match here — it falls through to new
    # research + deliverable instead of being treated as an export
    # of existing research. A bare pronoun without its noun left the
    # noun behind ("give a pdf report about this research" decomposed
    # to the garbage query "research"), so the noun is part of the
    # clause.
    r"(?:\s+(?:about|of|on|for)\s+(?:(?:this|that|it)(?:\s+(?:topic|research|report|analysis|findings|results|document|content|package))?|the\s+(?:above|previous|prior|same)(?:\s+(?:topic|research|report|analysis|findings|results|document|content|package))?))?"
    # A "based on ... till now / so far" reference to the research done
    # so far ("generate pdf based on research till now", "based on till
    # now"): a pure reference to prior research, never a new subject.
    # The noun after "based on" is limited to research-ish words, so an
    # explicit new subject ("based on quantum mechanics") does NOT
    # match — it stays a new research task.
    r"(?:\s+based\s+on\s+(?:(?:the\s+)?(?:research|work|findings|results|analysis|stuff|material|content|what\s+we\s+(?:have|found|did|covered|discussed|got|learned))\s+)?(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date))?"
    # A trailing requirement clause (", that should include images
    # related to it too", " with images"): the old tail only allowed
    # an (add|include|with) clause and a few closers, so anything
    # after the format killed the match.
    r"(?:\s*,?\s*(?:that\s+)?(?:should\s+)?(?:include|with|having|add|containing)\s+[^\n]+)?"
    r"(?:\s+(?:(?:and|but)\s+)?(?:also\s+)?(?:add|include|with)\s+[^\n]+)?"
    r"(?:\s+(?:bro|man|dude|pls|please|for\s+me|thanks|thank\s+you|of\s+this|from\s+this|instead|of\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s+slides?))?[!.\s]*$",
    re.IGNORECASE,
)

REFORMAT_PAGES_IMAGES_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can\'t|cant|can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|just)\s+)?"
    r"(?:make|turn|convert|change|format|prepare|render|generate|create|produce|put|give)\s+"
    r"(?:(?:me\s+|us\s+)?(?:the\s+|this\s+|that\s+|my\s+|our\s+)?(?:previous\s+|prior\s+|current\s+|last\s+|above\s+)?(?:report|analysis|document|research|findings|results|deliverable|it)\s+)"
    r"(?:into\s+|as\s+|to\s+|in\s+)?"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|pages|slide|slides)?\b)?"
    r".*$",
    re.IGNORECASE,
)

CONTINUATION_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:continue|resume|proceed|keep\s+going|pick\s+up(?:\s+where\s+(?:you|we)\s+left\s+off)?|finish\s+(?:the\s+)?(?:mission|task|research))"
    r"(?:\s+(?:bro|man|dude|pls|please))?[!.\s]*$",
    re.IGNORECASE,
)

REFINEMENT_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|and\s+also)\s+)?"
    r"(?:(?:make|turn|convert|change|format|prepare)\s+(?:the\s+)?(?:previous\s+|prior\s+)?(?:report|analysis|document|research|deliverable|it)\b|add|include|compare|incorporate|expand(?:\s+on)?|elaborate(?:\s+on)?|give\s+more|provide\s+more|i\s+(?:need|want))\s+"
    r".+",
    re.IGNORECASE,
)


def is_export_deliverable_directive(
    text: str,
    context: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Detect whether user prompt is a pure export or deliverable compilation directive."""
    if not text:
        return False, None, None
    clean = text.strip()
    # Stream 1 (S5 gen B5): a watch/schedule interval change is never a
    # deliverable export. "change it to every 45 mins" names no format
    # and no deliverable noun, but the reformat pattern below (whose
    # tail is `.*$`) reads "change it to <anything>" as "reformat the
    # deliverable" and defaults the format to pdf — so an interval
    # update became an empty Technical_Analysis.pdf. A genuine export
    # always names its format or its deliverable noun.
    if (
        re.search(
            r"\bevery\s+(?:\d+\s*)?(?:minutes?|mins?|hours?|hrs?|days?)\b",
            clean,
            re.IGNORECASE,
        )
        and not re.search(
            r"\b(?:pdf|docx|word|pptx|powerpoint|presentation|slides?|slide\s*deck|"
            r"xlsx|excel|spreadsheet|workbook|csv|markdown|html|report|document|"
            r"whitepaper|dossier|analysis|findings|results)\b",
            clean,
            re.IGNORECASE,
        )
    ):
        return False, None, None
    m = EXPORT_DIRECTIVE_PATTERN.match(clean)
    if m:
        raw_fmt = m.group(1).lower()
        fmt = detect_requested_format(raw_fmt) or detect_requested_format(clean)
        deliv = detect_deliverable_type(clean) or ("presentation" if fmt == "pptx" else "report")
        if not fmt and deliv == "presentation":
            fmt = "pptx"
        elif not fmt and context:
            fmt = context.get("requested_format")
        if not fmt:
            fmt = "pdf"
        return True, fmt, deliv

    # "based on the research till now, generate a pdf" — the reference
    # comes first and the deliverable request second. The main pattern
    # anchors on the verb, so this order needs its own check. The tail
    # mirrors the main pattern's tail (pronoun topic clause /
    # requirement clause / closers, then $): without it the check
    # matched a prefix, so "generate a pdf about quantum mechanics"
    # was silently treated as a pure export of prior research and the
    # explicit new subject was dropped.
    m_ref_first = re.match(
        r"^(?:based\s+on\s+)?"
        r"(?:(?:the\s+)?(?:research|work|findings|results|analysis|stuff|material|content|what\s+we\s+(?:have|found|did|covered|discussed|got|learned))\s+)?"
        r"(?:the\s+)?"
        r"(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date)"
        r"\s*,?\s*"
        r"(?:generate|create|export|save|compile|download|produce|make|render|output|convert|turn|give|format|put|send|prepare|provide)\s+"
        r"(?:(?:a|an|the)\s+)?"
        r"(pdf|docx|word(?:\s+doc(?:ument)?)?|pptx|powerpoint|presentation|slides?|slide\s*deck|xlsx|excel(?:\s+sheet)?|spreadsheet|workbook|csv|markdown|html|report|document)\b"
        r"(?:\s+(?:file|document|deck|sheet|format|report))?"
        r"(?:\s+(?:about|of|on|for)\s+(?:(?:this|that|it)(?:\s+(?:topic|research|report|analysis|findings|results|document|content|package))?|the\s+(?:above|previous|prior|same)(?:\s+(?:topic|research|report|analysis|findings|results|document|content|package))?))?"
        r"(?:\s*,?\s*(?:that\s+)?(?:should\s+)?(?:include|with|having|add|containing)\s+[^\n]+)?"
        r"(?:\s+(?:(?:and|but)\s+)?(?:also\s+)?(?:add|include|with)\s+[^\n]+)?"
        r"(?:\s+(?:bro|man|dude|pls|please|for\s+me|thanks|thank\s+you|of\s+this|from\s+this|instead))?[!.\s]*$",
        clean,
        re.IGNORECASE,
    )
    if m_ref_first:
        raw_fmt = m_ref_first.group(1).lower()
        fmt = detect_requested_format(raw_fmt) or detect_requested_format(clean)
        deliv = detect_deliverable_type(clean) or ("presentation" if fmt == "pptx" else "report")
        if not fmt and deliv == "presentation":
            fmt = "pptx"
        elif not fmt and context:
            fmt = context.get("requested_format")
        if not fmt:
            fmt = "pdf"
        return True, fmt, deliv

    # Check reformat / page-adjustment / image-adding export directives on existing deliverables
    m_reformat = REFORMAT_PAGES_IMAGES_PATTERN.match(clean)
    if m_reformat:
        fmt = detect_requested_format(clean)
        deliv = detect_deliverable_type(clean) or ("presentation" if fmt == "pptx" else "report")
        if not fmt and deliv == "presentation":
            fmt = "pptx"
        elif not fmt and context:
            fmt = context.get("requested_format")
        if not fmt:
            fmt = "pdf"
        return True, fmt, deliv

    return False, None, None


def resolve_topic_from_context(context: Optional[dict[str, Any]]) -> Optional[str]:
    """Resolve active research subject from working memory, checkpoint, or prior messages."""
    if not context or not isinstance(context, dict):
        return None
    # 1. Direct explicit context keys
    for k in ("research_topic", "target_item"):
        val = context.get(k)
        if val and isinstance(val, str) and val.strip():
            clean = val.strip()
            if len(clean) > 2 and clean.lower() not in {"it", "this", "that"}:
                return clean

    # 2. Session / chat memory keys
    sess_mem = context.get("session_memory") or context.get("chat_memory") or {}
    if isinstance(sess_mem, dict):
        for k in ("research_topic", "target_item"):
            val = sess_mem.get(k)
            if val and isinstance(val, str) and val.strip():
                clean = val.strip()
                if len(clean) > 2 and clean.lower() not in {"it", "this", "that"}:
                    return clean

    # 3. Knowledge package topic if present
    pkg_data = context.get("research_knowledge_package")
    if pkg_data:
        topic_obj = pkg_data.get("topic") if isinstance(pkg_data, dict) else getattr(pkg_data, "topic", None)
        if topic_obj:
            t_title = topic_obj.get("title") if isinstance(topic_obj, dict) else getattr(topic_obj, "title", None)
            if t_title and isinstance(t_title, str) and t_title.strip():
                return t_title.strip()

    # 4. Scan conversation history backwards for original inquiry topic
    conv_hist = context.get("conversation_history") or []
    if isinstance(conv_hist, list):
        for msg in reversed(conv_hist):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = str(msg.get("content") or "").strip()
                if content:
                    cand = extract_research_topic(content, context=None)
                    if cand and len(cand.split()) >= 2 and cand.lower() not in {"it", "this", "that", "them"}:
                        return cand
    return None


def extract_research_topic(text: str, context: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Extract core topic string from research or deliverable prompt.

    Handles conversational follow-ups and inherits/augments the active topic
    from prior context when anaphora or partial modifiers are supplied.
    """
    if not text:
        return None
    cleaned = text.strip()

    # If pure export/deliverable directive, return prior topic directly
    is_exp, _, _ = is_export_deliverable_directive(cleaned, context=context)
    if is_exp:
        return resolve_topic_from_context(context)

    # A bare prior-research reference ("so far", "till now") must be
    # checked on the RAW text: step 1 below strips leading "so" as a
    # conversational opener, so "so far" would become "far" before the
    # reference check ever ran.
    if is_prior_research_reference(cleaned):
        return resolve_topic_from_context(context)

    # An opener in front of a bare reference ("yeah, so far", "ok, so
    # far") is still just the reference — but step 1 strips the opener
    # AND "so" (also an opener), leaving the nonsense topic "far".
    # Check the reference on opener-stripped text too, before any
    # stripping runs. The prestrip deliberately EXCLUDES "so" from its
    # opener list: including it would strip "so" out of "so far" and
    # leave "far" again. The guard skips the re-check when nothing
    # was stripped (the raw check above already covered that case).
    _prestripped = re.sub(
        r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|well|yeah|yep|yes)[\s,]+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if _prestripped != cleaned and is_prior_research_reference(_prestripped):
        return resolve_topic_from_context(context)

    # A reference followed by an EXPLICIT new subject ("based on the
    # research till now, generate a pdf about quantum mechanics") is
    # new research on that subject — the subject after "about/on" is
    # the topic, not the prior one. Without this, step 4 below
    # (trailing deliverable clause, whose `.*$` eats everything after
    # the format noun) strips "generate a pdf about quantum
    # mechanics" entirely, leaving the bare reference, which the
    # check above then resolves to the prior topic — the explicit new
    # subject silently disappears. The verb list mirrors the export
    # pattern's verbs ("give a pdf about ..." was missed at first:
    # "give" is a deliverable verb too).
    m_new_subject = re.search(
        r"(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date)\b"
        r"[^.\n]*?\b(?:generate|create|make|prepare|produce|write|build|export|give|save|compile|download|render|output|convert|turn|format|put|send|provide)\b"
        r"[^.\n]*?\b(?:pdf|docx|word|pptx|presentation|slides?|slide\s*deck|xlsx|excel|spreadsheet|report|document)\b"
        r"\s+(?:about|on|of)\s+(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_new_subject:
        subject = re.sub(r"[.!?,]+$", "", m_new_subject.group(1).strip()).strip()
        # Strip a requirement tail off the subject ("quantum mechanics
        # with images" -> "quantum mechanics"): only the known
        # requirement nouns, so a subject that legitimately contains
        # "with" ("living with diabetes") is left alone.
        subject = re.sub(
            r"\s+with\s+(?:relevant\s+)?(?:images|diagrams|figures|charts|tables|references|citations|examples|equations|formulas)\b.*$",
            "",
            subject,
            flags=re.IGNORECASE,
        ).strip()
        subject = re.sub(
            r"\s+(?:that|which)\s+(?:should\s+)?(?:include|contain|cover)\b.*$",
            "",
            subject,
            flags=re.IGNORECASE,
        ).strip()
        subject = re.sub(r"\s+including\b.*$", "", subject, flags=re.IGNORECASE).strip()
        subject = re.sub(r"[.!?,]+$", "", subject).strip()
        # A pronoun subject ("about this research") is not a new
        # subject at all — the export check above already returned
        # for those; anything reaching here with only a pronoun has
        # no usable subject, so fall through to the normal path.
        if subject and not re.fullmatch(r"(?:this|that|it)(?:\s+(?:topic|research|report|analysis|findings|results|document|content|package))?", subject, re.IGNORECASE):
            return subject

    # 1. Strip conversational slang openers (including bare
    # affirmations like "yeah" / "yep" — "yeah research about the
    # topic first" is a follow-up on the prior topic, not a topic
    # containing the word "yeah").
    cleaned = re.sub(
        r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|so|well|yeah|yep|yes)[\s,]+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 1b. Strip a leading delivery-request prefix ("send me the ppt
    # about X", "give me the ppt for review"): the send verb and the
    # pronoun/article are not the topic. Only when a deliverable noun
    # follows — "send it to prateek in a chat app" has none, so a
    # messaging instruction is untouched (review #5, S7).
    if re.search(
        r"\b(?:ppt|pptx|powerpoint|presentation|slides?|slide\s*deck|report|document|whitepaper|dossier|pdf|docx|xlsx|spreadsheet|workbook)\b",
        cleaned,
        re.IGNORECASE,
    ):
        cleaned = re.sub(
            r"^(?:send|give|share|show|forward)\s+(?:me\s+|us\s+)?(?:the\s+|this\s+|that\s+|my\s+|it\s+)?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    # 2. Strip conversational command openers and directives
    cleaned = re.sub(
        r"^(?:could|can|would)\s+(?:you|u)\s+(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do|research|investigate)?\s*(?:me\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do)\s+(?:me\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:make\s+it|turn\s+it\s+into|convert\s+it\s+to|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:add|include|expand\s+(?:on\s+)?|elaborate\s+(?:on\s+)?|give|show|provide)\s+(?:more|further|additional|better|technical)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^(?:now|then|next|also)\s+", "", cleaned, flags=re.IGNORECASE).strip()

    # 2b. Strip a bare leading deliverable preamble ("ppt report on topic
    # X", "presentation about X", "report on X"). Without this the
    # preamble becomes the topic (live 2026-11-19: "ppt on topic
    # university should teach..." survived as the topic).
    cleaned = BARE_DELIVERABLE_PREAMBLE_RE.sub("", cleaned, count=1).strip()

    # 2c. Cut delivery / messaging / shopping clauses out of a compound
    # instruction ("research about black holes and send it to prateek
    # in a chat app" -> research half only). The remainder is kept by the
    # compound splitter, never silently dropped — this only keeps the
    # RESEARCH topic clean.
    cleaned = cut_delivery_clauses(cleaned)
    cleaned = re.sub(
        r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|so|well|yeah|yep|yes)[\s,]+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 3. Clean leading command verbs
    # A navigation preamble ("open google, research about X") names the
    # search engine, not the topic. Only a named engine counts — "open
    # source licensing" must keep "open". Runs before the verb strip so
    # the "research" after it is stripped by the verb pattern below.
    cleaned = re.sub(
        r"^open\s+(?:google|bing|duckduckgo|yahoo)\s*,?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|deep\s+dive\s+into|survey|explain|describe|analyze|summarize|compare(?:\s+the)?|prepare|create|generate|write|make|build)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 4. Clean trailing deliverable / format / illustration clauses BEFORE stripping
    #    the deliverable nouns they contain.
    # A trailing purpose clause ("... for review", "... for checking")
    # is why the deliverable is wanted, not the topic: "send me the
    # ppt for review" must not become a research mission on "review"
    # (review #5, S7).
    cleaned = re.sub(
        r"\s+for\s+(?:review|checking)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s+(?:and\s+)?(?:bro\s+|please\s+)?(?:prepare|create|generate|write|make|build|export|output)\s+(?:a|an)?\s*(?:more\s+)?(?:detailed\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|xlsx|csv|html)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s+(?:with\s+(?:relevant\s+)?(?:diagrams|references|equations|formulas|examples|figures|citations|charts|tables).*)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s+(?:with\s+diagrams\s+)?(?:in|as)\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 5. Strip deliverable artifacts & slide/page counts (numbers or words)
    cleaned = re.sub(
        r"\b(?:a\s+|an\s+)?(?:more\s+)?(?:detailed\s+)?(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|ppt|powerpoint|xlsx|csv|html|comparison)(?:\s+(?:of|with)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s+(?:slides?|pages?))?\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\b(?:of\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s*[- ]?(?:slides?|pages?)\b", "", cleaned, flags=re.IGNORECASE).strip()

    # 6. Strip inclusion clauses for visuals / formulas
    cleaned = re.sub(
        r"\b(?:it\s+should\s+include|including|with)\b(?:(?!\b(?:and\s+also|and\s+research|also\s+research|research)\b).)*\b(?:formulas|equations|diagrams|figures|charts|tables|citations|references)(?:\s+(?:too|also|as\s+well))?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    # Trailing style clauses ("... use images and make it nice looking
    # ppt") are presentation wishes, not topic. The (.+?) guard keeps a
    # topic that legitimately STARTS with "use images" intact.
    cleaned = re.sub(
        r"^(.+?)\s+use\s+(?:relevant\s+)?(?:images?|diagrams?|figures?|charts?)\b.*$",
        r"\1",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(.+?)\s+make\s+it\s+(?:nice(?:[-\s]looking)?|better|good|great|beautiful|presentable)\b.*$",
        r"\1",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 7. Strip connectors / research commands
    cleaned = re.sub(
        r"\b(?:and\s+)?(?:also\s+)?(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|look\s+into)\s+(?:different\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 8. Clean punctuation, trailing slang, leftover connectors, dangling prepositions and verbs
    cleaned = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:and|also|too|with|in|for|about|on)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(?:too|also|as\s+well|for\s+it|about\s+it|instead)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[.!?,]+$", "", cleaned).strip()
    cleaned = re.sub(
        r"\s+(?:and\s+)?(?:prepare|create|generate|write|make|build|export|output)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+(?:in|on|at|for|with|about|from|of)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", cleaned, flags=re.IGNORECASE).strip()

    # Noun-stripping above can leave double spaces ("send me the  for
    # review"); collapse them so the topic is well-formed.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    prior_topic = resolve_topic_from_context(context)

    # A bare "based on till now / so far" names no new subject — it is
    # a reference to the research done so far. Resolve to the prior
    # topic; with no prior topic there is nothing to research, so
    # return None instead of letting the literal phrase become a
    # garbage topic ("based on till now") and a garbage search query.
    if is_prior_research_reference(cleaned):
        return prior_topic or None

    # An anaphoric "the topic" (optionally with a sequencing word —
    # "research about the topic first", "yeah research about the topic
    # first") names no new subject either: it is the prior topic.
    # Without this, "the topic first" became the literal topic and
    # decomposed into garbage queries ("yeah first overview technical
    # principles").
    if prior_topic and re.fullmatch(
        r"(?:(?:about|on|of)\s+)?(?:the|this|that)\s+topic(?:\s+first)?",
        cleaned,
        re.IGNORECASE,
    ):
        return prior_topic

    generic_words = {
        "platforms", "different platforms", "frameworks", "implementations",
        "tools", "architectures", "details", "more details", "comparison", "overview",
        "diagrams", "formulas", "equations", "technical details", "more technical details",
        "better", "it better", "elaborate", "can you elaborate", "clarify",
        "bro", "man", "dude", "generate", "create", "export", "save", "download",
    }
    is_explicit_research_cmd = bool(
        re.match(
            r"^(?:(?:hey|hi|yo|ok|okay|bro|dude|man|please|pls)\s+)*(?:(?:could|can|would)\s+(?:you|u)\s+)?(?:please\s+)?(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|deep\s+dive\s+into|survey|create\s+(?:a\s+)?new\s+report\s+on)\b",
            text.strip(),
            re.IGNORECASE,
        )
    )
    if not cleaned or cleaned.lower() in {
        "it", "this", "that", "them", "instead", "report", "slides", "presentation",
        "details", "technical details", "more details", "more technical details", "better",
        "it better", "elaborate", "can you elaborate", "clarify",
        "bro", "man", "dude", "generate", "create", "export", "save", "download",
    }:
        return prior_topic or None
    elif not is_explicit_research_cmd and prior_topic and cleaned.lower() in generic_words:
        return f"{prior_topic} {cleaned}"
    elif (
        not is_explicit_research_cmd
        and prior_topic
        and prior_topic.lower() not in cleaned.lower()
        and len(cleaned.split()) <= 10
        and any(w in cleaned.lower() for w in ("platform", "framework", "tool", "code", "implementation", "detail", "equation", "diagram", "comparison", "compare", "jax", "pytorch", "latest", "development", "2024", "2025", "2026", "versus", "vs"))
    ):
        # Strip a leading prior-reference phrase before appending the
        # modifier: "based on till now, and also compare with biology"
        # must become "<prior> - compare with biology", not "<prior> -
        # based on till now, and also compare with biology".
        modifier = re.sub(
            r"^(?:based\s+on\s+(?:(?:the\s+)?(?:research|work|findings|results|analysis|stuff|material|content|what\s+we\s+(?:have|found|did|covered|discussed|got|learned))\s+)?(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date)"
            r"|(?:till\s+now|so\s*,?\s*far|up\s+to\s+now|till\s+date|up\s+to\s+date))\s*,?\s*(?:and\s+also\s+|and\s+)?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        return f"{prior_topic} - {modifier or cleaned}"

    return cleaned or prior_topic or None


def is_research_or_deliverable_request(
    text: str,
    context: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Detect whether user prompt represents an autonomous research or deliverable request.

    Returns
    -------
    (is_research, deliverable_type, requested_format, topic_hint)
    """
    if not text:
        return False, None, None, None

    has_research = bool(RESEARCH_DIRECTIVE_RE.search(text))
    has_artifact = bool(PREPARE_ARTIFACT_RE.search(text))
    has_expl_deliverable = bool(EXPLANATION_WITH_DELIVERABLE_RE.search(text))
    has_comp_deliverable = bool(COMPARISON_WITH_DELIVERABLE_RE.search(text))
    req_format = detect_requested_format(text)
    # A bare deliverable noun phrase with a topic ("ppt report on topic
    # X", "presentation about X", "report on X") is a research +
    # deliverable request even though it has no leading verb —
    # PREPARE_ARTIFACT_RE requires one, so without this the instruction
    # falls through to NON_BROWSER and the LLM answers directly (live
    # 2026-11-19: hallucinated `createpresentation` tool call, no file).
    # The preamble regex requires a topic introducer, so "report writing
    # techniques" does not count. A detected format or deliverable type
    # must be present, so a bare "report on the meeting" still counts
    # (report -> docx default) but plain conversation does not.
    has_bare_deliverable = bool(
        BARE_DELIVERABLE_PREAMBLE_RE.search(text)
        and (req_format or detect_deliverable_type(text))
    )
    has_format_directive = bool(
        req_format
        and re.search(
            # The verb list mirrors the export pattern's verbs: "give
            # a pdf about <new subject>" routes to research, but
            # "give" (like send/provide/turn/render/compile/download/
            # format/put/save/write) was missing here, so the give
            # variant classified NON_BROWSER while the generate variant
            # correctly yielded RESEARCH_TASK / NEW_RESEARCH.
            r"\b(?:generate|create|prepare|make|build|produce|export|output|give|send|provide|turn|render|compile|download|format|put|save|write|in|as|into)\b",
            text,
            re.IGNORECASE,
        )
    )

    prior_is_research = bool((context or {}).get("is_research"))
    prior_topic = resolve_topic_from_context(context)
    is_follow_up = bool(
        FOLLOW_UP_TRIGGERS_RE.search(text)
        or (prior_is_research and REFINEMENT_DIRECTIVE_PATTERN.match(text))
        or (prior_is_research and req_format)
        or (prior_is_research and any(k in text.lower() for k in ["page", "pages", "slide", "slides", "report", "image", "images", "diagram", "diagrams", "table", "tables", "formula", "formulas", "compare", "comparison"]))
    )

    if not (has_research or has_artifact or has_expl_deliverable or has_comp_deliverable or has_format_directive or has_bare_deliverable or (prior_is_research and is_follow_up)):
        return False, None, None, None

    deliv_type = detect_deliverable_type(text)
    if not deliv_type and is_follow_up and context:
        deliv_type = context.get("deliverable_type")

    # Standard default format based on deliverable type when unspecified
    if not req_format:
        prior_deliv = context.get("deliverable_type") if context else None
        if is_follow_up and context and context.get("requested_format") and not (deliv_type and deliv_type != prior_deliv):
            req_format = context["requested_format"]
        elif deliv_type == "presentation":
            req_format = "pptx"
        elif deliv_type == "dataset":
            req_format = "xlsx"
        elif deliv_type == "report":
            req_format = "docx"

    topic_hint = extract_research_topic(text, context=context) or prior_topic
    # A bare format directive with no subject and no prior topic is not
    # research: "send me the ppt for review" / "create a ppt" name no
    # topic, so there is nothing to research — routing them as
    # RESEARCH_TASK with topic None runs a garbage mission (review #5,
    # S7). With a real subject, another trigger, or prior context the
    # route stands.
    if (
        not topic_hint
        and not prior_topic
        and has_format_directive
        and not (has_research or has_artifact or has_expl_deliverable or has_comp_deliverable or has_bare_deliverable or (prior_is_research and is_follow_up))
    ):
        return False, None, None, None
    return True, deliv_type, req_format, topic_hint


# ---------------------------------------------------------------------------
# 1. Non-browser patterns
# ---------------------------------------------------------------------------

GREETING_PATTERN = re.compile(
    r"^(?:hi|hello|hey|good\s+morning|good\s+afternoon|good\s+evening|greetings|howdy|sup|yo)(?:\s+(?:there|man|agent|privagent|buddy|friend|assistant|team|all|everyone))?[!.\s]*$",
    re.IGNORECASE,
)

CONVERSATIONAL_PATTERN = re.compile(
    r"^(?:how\s+are\s+you|who\s+are\s+you|what\s+can\s+you\s+do|thank\s+you|thanks|bye|goodbye|tell\s+me\s+a\s+joke|help|what\s+is\s+your\s+name)[!.\s\?]*$",
    re.IGNORECASE,
)

CODE_PATTERN = re.compile(
    r"\b(?:write|code|implement|generate|create)\s+(?:a|an)\s+(?:c\+\+|python|java|javascript|typescript|c#|rust|go|sql|html|css|algo|algorithm|function|script|program|class|bfs|dfs|quicksort|mergesort)\b"
    r"|\b(?:write|code|implement)\s+(?:bfs|dfs|binary\s+search|linked\s+list|fibonacci|factorial)\b"
    r"|\b(?:how\s+to\s+write|help\s+me\s+debug|debug\s+this|explain\s+this\s+code)\b",
    re.IGNORECASE,
)

MATH_PATTERN = re.compile(
    r"^(?:calculate|compute|solve|what\s+is)\s+[\d\s\+\-\*\/\%\^\.\(\)]+(?:\?|$)"
    r"|\b(?:calculate|compute|what\s+is)\s+\d+(?:\.\d+)?\s*%\s+of\s+\d+(?:\.\d+)?\b"
    r"|^\s*\d+[\d\s\+\-\*\/\%\^\.\(\)]+\s*$",
    re.IGNORECASE,
)

EXPLANATION_PATTERN = re.compile(
    r"^(?:what\s+is|what\s+are|define|explain)\s+"
    r"(?:browser\s+automation|automation|web\s+scraping|ai|artificial\s+intelligence|llm|llms|transformers|attention\s+mechanism|neural\s+networks|recursion|polymorphism|gravity|relativity|photosynthesis|entropy|dna|rna|quantum\s+computing|machine\s+learning|deep\s+learning|quicksort|binary\s+search|backpropagation|google\s+search)\b"
    r"|^(?:how\s+does|how\s+do)\s+.*?\s+(?:work|function|operate)\??$"
    r"|^(?:explain|define|describe|what\s+is|what\s+are|how\s+does|why\s+is)\s+"
    r"(?:transformers|attention\s+mechanism|neural\s+networks|recursion|polymorphism|gravity|relativity|photosynthesis|entropy|dna|rna|quantum\s+computing|machine\s+learning|deep\s+learning|quicksort|binary\s+search|backpropagation)\b"
    r"|^(?:explain|describe)\s+how\s+.*\s+works\??$",
    re.IGNORECASE,
)

# 2. Explicit User Tab request patterns
# Task 8: a "site token" is a registry name (DATA from known_domains.json,
# never a hard-coded list in a regex), a domain-like token
# (``example.com``), or a generic service word (mail, inbox, docs...).
# Intent routing must never enumerate sites: any site the user names is a
# site, whether or not this codebase has heard of it. Longest names first
# so a two-word name wins over a one-word prefix in alternation.
def _site_token_pattern() -> str:
    try:
        from .website_resolver import KNOWN_DOMAINS_REGISTRY

        names = sorted(
            {entry["name"].lower() for entry in KNOWN_DOMAINS_REGISTRY.values()}
            | {key.replace("_", " ") for key in KNOWN_DOMAINS_REGISTRY},
            key=len,
            reverse=True,
        )
    except Exception:
        names = []
    service_words = {"mail", "gmail", "email", "inbox", "webmail", "docs", "drive", "calendar"}
    parts = [re.escape(n) for n in names if n]
    parts.extend(sorted(service_words))
    parts.append(r"[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+")
    return "(?:" + "|".join(parts) + ")"


_SITE_TOKEN = _site_token_pattern()

USE_TAB_PATTERNS = [
    re.compile(r"\b(?:i\s+)?(?:have\s+)?opened\s+(?:a|the)\s+(?:new\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\buse\s+(?:the|my)\s+(?:new\s+tab|tab\s+i\s+(?:just\s+)?opened)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+in\s+that\s+tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on|switch\s+to)\s+the\s+tab\s+i\s+(?:just\s+)?opened\b", re.IGNORECASE),
    re.compile(r"\buse\s+my\s+active\s+tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on|use|attach\s+to|from)\s+(?:my\s+|the\s+)?(?:currently\s+open|currently\s+active|existing|active|current)\s+(?:browser\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\buse\s+(?:the\s+)?(?:existing|current|active)\s+(?:user\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on)\s+the\s+(?:open|existing)\s+tab\s+(?:for|with|named|titled|at)\s+([^\.,;]+)", re.IGNORECASE),
    re.compile(r"\bon\s+the\s+open\s+([a-z][a-z0-9\-]*(?:\.[a-z]{2,})?)\s+tab\b", re.IGNORECASE),
    # Current-tab page inspection patterns (e.g. "Summarize this webpage", "Read this page")
    re.compile(r"\b(?:summarize|read|explain|inspect|analyze|extract(?:\s+from)?|what\s+is\s+on)\s+(?:this|the\s+current)\s+(?:webpage|page|website|article|tab|screen)\b", re.IGNORECASE),
    re.compile(r"^(?:summarize|explain|inspect|read)\s+this\s+page[!.\s]*$", re.IGNORECASE),
]

# 3. Continuation markers (only valid when an active browser session exists)
CONTINUATION_PATTERNS = [
    re.compile(r"^(?:now\s+|then\s+)?(?:search\s+for|search\s+in|look\s+for)\s+.+", re.IGNORECASE),
    re.compile(r"^(?:now\s+|then\s+)?(?:click|open|select)\s+(?:on\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|\d+(?:st|nd|rd|th)?)\s+(?:result|product|link|item|one)\b", re.IGNORECASE),
    re.compile(r"^(?:go\s+back|previous\s+page|back\s+button)(?:\s+(?:to\s+the\s+previous\s+page|and\s+.*))?", re.IGNORECASE),
    re.compile(r"\bgo\s+back\s+and\s+(?:open|click)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+where\s+you\s+left\s+off\b", re.IGNORECASE),
    re.compile(r"^(?:now\s+|then\s+)?(?:continue|scroll(?:\s+down|\s+up)?|click(?:\s+on)?|next\s+page|go\s+back|add\s+to\s+cart|buy\s+now|type\s+in|select|read\s+more|open\s+the\s+(?:first|second|third|fourth|next)\s+one)\b", re.IGNORECASE),
    re.compile(r"\b(?:what\s+is\s+its\s+price|how\s+much\s+is\s+it|buy\s+it|summarize\s+that(?:\s+page)?|what\s+else\s+does\s+it\s+say|from\s+that\s+page)\b", re.IGNORECASE),
    re.compile(r"^(?:continue|proceed|next\s+step|scroll(?:\s+down|\s+up)?)[!.\s]*$", re.IGNORECASE),
    re.compile(r"\b(?:is\s+there\s+any\s+other|any\s+other|what\s+about\s+(?:the\s+)?other|check\s+other)\s+(?:mail|email|message|unread|product|item|flight|result)s?\b", re.IGNORECASE),
    re.compile(r"\b(?:more\s+important|more\s+urgent|cheaper|better|higher|lower)\s+than\s+(?:this|that|the\s+first\s+one)\b", re.IGNORECASE),
    re.compile(r"\b(?:is\s+there\s+any\s+other\s+mail|which\s+mail\s+is\s+more|check\s+my\s+other\s+mail)\b", re.IGNORECASE),
    re.compile(r"\b(?:in\s+my\s+(?:inbox|mailbox|email)|from\s+my\s+(?:inbox|mailbox|email))\b", re.IGNORECASE),
    # Explicit request to finish or resume the task left mid-flight (checkpoint resumption)
    re.compile(r"\b(?:complete|finish|resume|pick\s+up)\s+(?:the\s+)?(?:unfinished|incomplete|previous|prior|last|pending|halted|paused)\s+(?:task|mission|job|work|run)\b", re.IGNORECASE),
    re.compile(r"\b(?:complete|finish|resume)\s+the\s+unfinished\b", re.IGNORECASE),
    re.compile(r"\b(?:where\s+(?:were|did)\s+(?:you|we)\s+(?:leave|stop)|from\s+where\s+(?:you|we)\s+(?:left|stopped))\b", re.IGNORECASE),
    # Conversational follow-ups and refinements
    FOLLOW_UP_TRIGGERS_RE,
]

# 4. Explicit browser task markers
# Task 8: direct-site trigger is generic — any registry name, service
# word, or domain-like token after open/visit/go to. (Email and calendar
# words route to connectors; see Task 14.)
DIRECT_SITE_TRIGGER = re.compile(
    r"\b(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+"
    r"(?:the\s+)?(?:official\s+)?(?:website\s+|webpage\s+|site\s+|page\s+)?"
    + _SITE_TOKEN +
    r"(?:\s+(?:docs|documentation|homepage|website|search|site|store|app|portal|page|inbox))?\b",
    re.IGNORECASE,
)

EMAIL_TRIGGER = re.compile(
    r"\b(?:open|check|go\s+to|access|view|read)\s+(?:my\s+)?(?:e-?mail|mail|inbox|gmail|outlook|webmail)\b"
    r"|^(?:open\s+mail|check\s+mail|open\s+gmail|check\s+email|go\s+to\s+inbox|open\s+inbox)[!.\s]*$",
    re.IGNORECASE,
)

EMAIL_SCAN_MISSION_PATTERN = re.compile(
    # "check/scan/monitor my gmail/email/inbox every N minutes (or every
    # hour/day)": a monitoring setup, not a one-off read. Must sit before
    # the EMAIL_TRIGGER step, which would otherwise send any "check my
    # gmail" to the browser. Cadence may be numbered or bare ("every
    # hour"); "second" is excluded from the bare form. The mailbox may
    # carry ".com" ("outlook.com"/"gmail.com").
    r"\b(?:check|scan|monitor)\s+(?:my\s+)?(?:e-?mails?|mail|inbox|gmail(?:\.com)?|outlook(?:\.com)?)\b[^.]{0,60}?\bevery\s+(?:\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)|(?:hours?|hrs?|minutes?|mins?|days?))\b|"
    r"\b(?:continuously\s+scan|monitor|moinitor|scan\s+and\s+clean|clean\s+up\s+my|cleanup\s+my|scan\s+my)\s+(?:my\s+)?(?:e-?mails?|mailbox|inbox)\b|"
    r"\b(?:identify\s+unnecessary\s+emails?|remove\s+unnecessary|detect\s+important\s+emails?|email\s+monitoring)\b|"
    r"\b(?:what\s+did\s+you\s+delete|why\s+did\s+you\s+delete|show\s+me\s+important\s+emails?|emails?\s+i\s+haven'?t\s+replied|what\s+deadlines|today'?s?\s+email\s+summary|pause\s+email|resume\s+email)\b|"
    r"\b(?:what\s+is\s+my\s+(?:e-?mail|mail|account)|which\s+account\s+are\s+you\s+monitoring|are\s+you\s+(?:still\s+)?scanning|is\s+monitoring\s+active|are\s+(?:you|u)\s+deleting)\b|"
    r"\b(?:check\s+now|i\s+(?:have\s+)?connected(?:\s+ev[e]?rything)?|connected\s+ev[e]?rything|is\s+my\s+(?:e-?mail|mail)\s+connected|verify\s+(?:connection|e-?mail|mail))\b|"
    r"\b(?:can\s+(?:you|u)|do\s+(?:you|u)|are\s+(?:you|u)\s+able\s+to|have\s+(?:you|u))\s+(?:access|see|read|check)\s+(?:my\s+)?(?:e-?mails?|mails?|inbox|gmail|outlook|account)\b|"
    r"\b(?:do\s+(?:you|u)\s+have\s+access\s+to\s+(?:my\s+)?(?:e-?mails?|mails?|inbox|gmail|outlook|account))\b|"
    r"\b(?:can\s+(?:you|u)|are\s+(?:you|u)\s+capable\s+of|capable\s+of)\s+(?:restoring|restore|untrashing|recover|bringing\s+back)\b.*?\b(?:e-?mails?|mails?|inbox)\b|"
    r"\b(?:then\s+)?restore\s+(?:the\s+)?(?:last|lastly|most\s+recent|latest)?\s*(?:deleted|trashed)?\s*(?:e-?mail|mail)\b|"
    r"\b(?:untrash|bring\s+back|recover)\s+(?:the\s+)?(?:last|lastly|most\s+recent|latest)?\s*(?:deleted|trashed)?\s*(?:e-?mail|mail)\b|"
    r"\brestore\s+(?:lastly|last)\s+deleted\b|"
    r"\b(?:can\s+(?:you|u)\s+)?(?:automatically\s+)?(?:delete|remove|clean\s*up|clear|trash)\s+(?:promotions?|promotional|unnecessary|marketing|spam|junk)\s*(?:e-?mails?|mails?)?\b|"
    r"\b(?:auto[- ]?delete|automatic\s+deletion|delete\s+promotions?|clean\s+promotions?)\b|"
    r"\b(?:ensure|make\s+sure|keep)\s+(?:they\s+are\s+not|them|emails\s+are\s+not)?\s*important\b|"
    r"\b(?:protect|save|preserve|whitelist|lock)\s+important\s+(?:e-?mails?|mails?)\b|"
    r"\b(?:it\s+is\s+connected|connected)[,\s]+(?:let'?s?\s+start|start|begin|go)\b|"
    r"\b(?:let'?s?\s+start|start)\s+(?:cleaning|scanning|monitoring|cleanup|deleting)\b|"
    r"\b(?:remind|notify|alert)\s+(?:me\s+)?(?:if|when|about)\s+.*?\b(?:emails?|mails?|inbox)\b|"
    r"\b(?:emails?|mails?|inbox)\b.*?\b(?:notify|alert|remind)\s+me\b",
    re.IGNORECASE,
)

CALENDAR_DIRECTIVE_PATTERN = re.compile(
    r"\b(?:check|show|view|list|get|what'?s?\s+on)\s+(?:my\s+)?(?:calendar|schedule|agenda|meetings?|events?)\b|"
    r"\b(?:do\s+i\s+have|are\s+there)\s+any\s+(?:meetings?|events?|appointments?|calls?)\b|"
    r"\bcalendar\s+(?:status|events?|schedule)\b|"
    r"\b(?:upcoming|next)\s+(?:meetings?|events?|appointments?|calls?)\b|"
    r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:event|meeting|invite|appointment|call)\b|"
    # "add it / the Microsoft interview to my calendar".
    r"\b(?:add|put)\s+\S+[^.]{0,50}?\b(?:to|on|onto)\s+(?:my\s+)?calendar\b|"
    r"\bgoogle\s+calendar\b",
    re.IGNORECASE,
)

# Explicit UI navigation — the operator asked to SEE the site, so the
# browser owns it even when a connector could answer the same question.
# Mirrors connector_router._EXPLICIT_NAVIGATION_RE. Without this,
# "Open Google Calendar" is swallowed by CALENDAR_DIRECTIVE_PATTERN
# (it contains "google calendar") and never reaches the browser,
# while "Open Gmail" and "Open Google Drive" correctly route there.
EXPLICIT_NAVIGATION_PATTERN = re.compile(
    r"\b(?:open|go\s+to|navigate\s+to|visit|browse\s+to|show\s+me)\s+"
    r"(?:the\s+)?(?:gmail(?:\s+app|\s+website|\s+inbox)?|google\s+(?:calendar|drive)|"
    r"my\s+inbox|my\s+google\s+drive|drive\s+app)\b"
    r"|\bopen\s+(?:my\s+)?inbox\b",
    re.IGNORECASE,
)

OFFICIAL_DOCS_TRIGGER = re.compile(
    r"\b(?:open|navigate\s+to|go\s+to|browse\s+to)\s+(?:the\s+)?official\s+.+\s+(?:docs|documentation|website|page|site)\b",
    re.IGNORECASE,
)

SEARCH_WEB_TRIGGER = re.compile(
    r"\b(?:search\s+" + _SITE_TOKEN + r"\s+for\s+.+)\b"
    r"|\b(?:search\s+for\s+.+\s+on\s+(?:" + _SITE_TOKEN + r"|the\s+web))\b"
    r"|\b(?:search|look\s+up|find)\s+.+\s+(?:online|on\s+the\s+web|on\s+the\s+internet)\b",
    re.IGNORECASE,
)

PRODUCT_SEARCH_TRIGGER = re.compile(
    r"\b(?:find|search|check|look\s+up|buy|order|price\s+of)\s+.+\s+(?:on\s+|from\s+)" + _SITE_TOKEN + r"\b"
    r"|\b(?:find|check\s+price\s+of)\s+[a-zA-Z0-9\s-]+\s+(?:on\s+)" + _SITE_TOKEN + r"\b",
    re.IGNORECASE,
)

COMPARISON_BROWSER_TRIGGER = re.compile(
    r"\b(?:compare|comparison\s+of)\s+.*?\s+(?:prices?|costs?|rates?|deals?|specs?|features?|models?|options?|between|across|on\b|at\b|vs\.?|versus)\b"
    r"|\b(?:compare|comparison)\s+(?:on\s+|across\s+|between\s+)?" + _SITE_TOKEN + r"\b"
    r"|\b(?:compare|comparison)\s+.*?\s+(?:and|vs\.?|versus)\s+.*?\s+(?:prices?|costs?|on\b|at\b)",
    re.IGNORECASE,
)

SHOPPING_DISCOVERY_TRIGGER = re.compile(
    r"\b(?:find|search(?:\s+for)?|look\s+for|shop(?:\s+for)?|buy|order|purchase)\s+(?:the\s+)?(?:cheapest|best|top|lowest\s+price|budget|discounted)?\s+.*?\b(?:laptop|phone|iphone|mobile|gpu|rtx|shoes|watch|camera|tv|headphones|earbuds|tablet|monitor|pc|deal|deals)\b"
    r"|\b(?:find|get|show)\s+(?:three|four|five|\d+)?\s*(?:good|best|cheapest|top)\s+.*?\b(?:under|below|for)\s+(?:₹|\$|rs\.?|inr|usd)?\s*\d+",
    re.IGNORECASE,
)

TRAVEL_SEARCH_TRIGGER = re.compile(
    r"\b(?:find|search(?:\s+for)?|check|book|look\s+up)\s+(?:flights?|hotels?|trains?|cabs?|trips?|tickets?)\s+(?:from|to|in|between|for)\b"
    r"|\b(?:flight|hotel|train)\s+(?:prices?|fares?|rates?|booking)\b"
    # Trip planning: "Plan Hyderabad to Delhi next month: cheapest
    # flights, hotels under 3000, and give me the itinerary doc."
    # The verb is "plan" and the travel nouns come later in the
    # instruction (after the route), so neither of the two branches
    # above matches — the instruction fell through to NON_BROWSER and
    # the conversational fast-path answered with clarifying questions
    # instead of searching. A "plan" followed by a travel noun in the
    # same instruction is a browser task.
    r"|\bplan\b[^.]{0,160}?\b(?:trips?|itinerary|flights?|hotels?|vacation|holiday)\b"
    # "cheapest flights" / "hotels under 3000" imply a live search —
    # the cheapest price cannot be known without one.
    r"|\b(?:cheapest|lowest|best)\s+(?:flights?|hotels?|trains?|tickets?|fares?)\b"
    r"|\b(?:flights?|hotels?|trains?)\s+(?:under|below|within)\s+(?:rs\.?|₹|\$|inr)?\s*\d",
    re.IGNORECASE,
)

GENERIC_SITE_OPEN_TRIGGER = re.compile(
    r"^(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+([a-zA-Z0-9_\-\.\:\/]+(?:\s+[a-zA-Z0-9_\-\.]+)?)(?:\s+(?:and|,)\s+.*)?$",
    re.IGNORECASE,
)

# Messaging send: "send/message/share/ask <thing> to|with <name> in|on|via
# <channel>" is a browser task on the messaging app — never a chat answer.
# Without this, "send it to prateek in a chat app" falls to the NON_BROWSER
# fast-path and "completes" by chatting, sending nothing (live 2026-11-19,
# review #4 M1). The channel is any single word (no hardcoded names)
# EXCEPT the stoplist (review #5, M6/M7): month/time words ("in June",
# "in the morning") and email words ("on email", "via email") are not
# channels, so those instructions stay out of the browser loop. The
# prepositions exclude "by"/"for" so "send it to me by email" stays
# with the email-dispatch handler (step 4c3, which runs first). A direct
# "message <Name> on <channel>" is covered by the second arm.
MESSAGING_SEND_RE = re.compile(
    r"^\s*(?:(?:please|hey|hi|hello|can\s+you)\s*,?\s*)?"
    r"(?:send|message|share|ask)\s+"
    r"(?:\S+\s+){0,4}?"
    r"(?:to|with)\s+[A-Za-z][A-Za-z\-']{1,30}\s+"
    r"(?:in|on|via|through|over)\s+(?!" + _CHANNEL_STOPLIST + r"\b)\w+\b",
    re.IGNORECASE,
)
MESSAGING_DIRECT_RE = re.compile(
    r"^\s*(?:(?:please|hey|hi|hello|can\s+you)\s*,?\s*)?"
    r"message\s+[A-Z][A-Za-z\-']{1,30}\s+"
    r"(?:in|on|via|through|over)\s+(?!" + _CHANNEL_STOPLIST + r"\b)\w+\b",
    re.IGNORECASE,
)

BROWSER_TRIGGERS = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    EMAIL_TRIGGER,
    DIRECT_SITE_TRIGGER,
    OFFICIAL_DOCS_TRIGGER,
    SEARCH_WEB_TRIGGER,
    PRODUCT_SEARCH_TRIGGER,
    COMPARISON_BROWSER_TRIGGER,
    SHOPPING_DISCOVERY_TRIGGER,
    TRAVEL_SEARCH_TRIGGER,
    re.compile(r"\b(?:navigate\s+to|go\s+to|open|browse\s+to|visit)\s+(?:the\s+)?(?:website\s+|site\s+)?(?:https?://|[a-zA-Z0-9-]+\.(?:com|org|in|net|edu|gov|io|co|ai|dev|app|info))\b", re.IGNORECASE),
    re.compile(r"\b(?:search\s+(?:on\s+)?" + _SITE_TOKEN + r"|search\s+for\s+.*\s+on\s+[a-zA-Z0-9-]+)\b", re.IGNORECASE),
    re.compile(r"\b(?:look\s+up|find\s+products?|find\s+prices?|check\s+weather|check\s+flights?)\s+on\b", re.IGNORECASE),
    re.compile(r"\b(?:buy|order|purchase)\s+.*?\s+(?:from|on)\s+" + _SITE_TOKEN + r"\b", re.IGNORECASE),
]

# Latest-release check (step 9b, Failure B): "check the latest
# <brand> <product> thats released". A recency word (latest/newest)
# plus a release word (released/launched/announced) after "check"
# means a live lookup. Deliberately narrow: "check the latest email"
# has no release word and never matches; "check the latest news"
# has none either.
_CHECK_LATEST_RE = re.compile(
    r"\bcheck\b[^.\n]{0,90}?\b(?:latest|newest|most\s+recent)\b[^.\n]{0,60}?\b(?:released|launched|announced|out|new)\b"
    r"|\bcheck\b[^.\n]{0,60}?\b(?:phone|mobile|laptop|tablet|watch|headphones|earbuds|camera|tv|gpu|car|bike|shoes|device|model)\b[^.\n]{0,40}?\b(?:released|launched|announced)\b",
    re.IGNORECASE,
)

# Question-form latest-release check (fixC M8): "What is the latest
# <brand> <product> released?" / "What's the newest Pixel phone
# out?" — the same live question as step 9b, asked as a question
# instead of a "check …" command. A recency word plus a release word
# or a product noun. Deliberately narrow: "what is the latest news"
# has neither and never matches, and "which is the latest from these
# results?" has no release word or product noun either — both stay
# conversational. The dispatch layer only applies this when the
# extension is connected (context flag); without a live browser the
# turn stays conversational and answers honestly from memory.
_LATEST_RELEASE_QUESTION_RE = re.compile(
    r"\b(?:what(?:'s|s| is)|which)\b[^.\n]{0,60}?"
    r"\b(?:latest|newest|most\s+recent)\b[^.\n]{0,60}?"
    r"\b(?:released|launched|announced|out|new|phone|mobile|laptop|tablet|watch|headphones|earbuds|camera|tv|gpu|car|bike|device|model)\b",
    re.IGNORECASE,
)


def should_route_email_interval(
    objective: Any,
    instruction: str,
    has_active_email_mission: bool,
) -> bool:
    """CONTRACTS4 §1 precedence gate for the email-interval branch.

    True only when the structured objective says this instruction IS
    an email-polling change — i.e. its object is email/inbox polling
    (email noun present, no product/website/price noun). A monitor
    intent ("live monitor iPhone 17 ... check every 10 minutes") is
    never email polling, however strong its interval token is: the
    legacy ``is_email_interval_change_instruction`` predicate matches
    any "check every N minutes" (its non-email-object guard misses
    product brand/model nouns and percent conditions), so it must
    never be the sole decider. When no objective could be built, the
    legacy predicate is the fallback (previous behaviour preserved).
    """
    if not has_active_email_mission:
        return False
    if objective is not None:
        return getattr(objective, "primary_intent", None) == "configure_email_polling"
    try:
        from .email_intelligence.mission_setup import is_email_interval_change_instruction

        return bool(is_email_interval_change_instruction(instruction))
    except Exception:
        return False


def classify_intent(
    instruction: str,
    context: Optional[dict[str, Any]] = None,
    semantic_classifier: Optional[Callable[[str], str]] = None,
) -> IntentClassification:
    """Classify user task intent before any browser operation.

    Thin wrapper over :func:`_classify_intent_impl`: the legacy
    regex classification runs unchanged, and the structured
    :class:`app.objective.Objective` for the instruction is attached
    to the result (``classification.objective``). Downstream code
    routes on ``objective.primary_intent``; the objective is built
    deterministically here (no model call — a bare interval stays
    ambiguous at this point because the active-email-mission flag
    lives in the dispatch layer, which re-resolves it).

    ``semantic_classifier`` is the provider-backed callable from
    :func:`app.semantic_classifier.make_semantic_classifier`
    (production path: agentLoop builds it per task and passes it in).
    It is consulted ONLY for genuinely ambiguous or compound
    instructions — exactly one deterministic rule firing never calls
    it — and an absent/invalid semantic result falls back honestly
    instead of guessing. Callers that pass None keep the old
    behaviour: ambiguous stays ``conversational`` with
    missing=["intent_ambiguous"].
    """
    result = _classify_intent_impl(instruction, context=context)
    try:
        from .objective import build_objective

        result.objective = build_objective(
            instruction, context=context, semantic_classifier=semantic_classifier
        )
    except Exception:
        result.objective = None
    return result


def _classify_intent_impl(
    instruction: str,
    context: Optional[dict[str, Any]] = None,
) -> IntentClassification:
    """Classify user task intent before any browser operation.

    Parameters
    ----------
    instruction : str
        The raw task instruction from the user.
    context : Optional[dict[str, Any]]
        Execution context, including active browser session info:
        - ``active_session``: Optional active BrowserSession
        - ``conversation_history``: list of previous turn messages

    Returns
    -------
    IntentClassification
        The classified intent and supporting metadata.
    """
    text = (instruction or "").strip()
    ctx = context or {}
    has_active_browser_session = bool(ctx.get("active_session"))

    if not text:
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason="empty instruction defaults to non-browser",
        )

    # 1. Check for conflicting or ambiguous tab instructions
    ambiguous_tab_pattern = re.compile(
        r"\b(?:use\s+a\s+tab|pick\s+any\s+tab|some\s+tab|random\s+tab)\b",
        re.IGNORECASE,
    )
    if ambiguous_tab_pattern.search(text):
        return IntentClassification(
            intent=TaskIntent.USE_EXISTING_USER_TAB,
            confidence=0.5,
            reason="ambiguous tab request requires resolution or failure",
            is_ambiguous=True,
        )

    # 2. Check for explicit existing user tab request
    for pat in USE_TAB_PATTERNS:
        m = pat.search(text)
        if m:
            hint = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else None
            return IntentClassification(
                intent=TaskIntent.USE_EXISTING_USER_TAB,
                confidence=0.95,
                reason="explicit user request to attach to existing tab",
                target_tab_hint=hint,
            )

    # 3. Check for continuation of an active browser session or mission
    if CONTINUATION_DIRECTIVE_PATTERN.match(text):
        prior_is_res = bool((context or {}).get("is_research"))
        prior_fmt = (context or {}).get("requested_format")
        prior_deliv = (context or {}).get("deliverable_type")
        prior_topic = resolve_topic_from_context(context)
        return IntentClassification(
            intent=TaskIntent.CONTINUE_BROWSER_TASK if has_active_browser_session else TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason="instruction requests continuing or resuming the active mission or research task",
            operation_type=ResearchOperationType.RESUME_EXISTING_MISSION,
            requested_format=prior_fmt,
            is_research=prior_is_res,
            research_topic=prior_topic,
            deliverable_type=prior_deliv,
        )

    if has_active_browser_session:
        for pat in CONTINUATION_PATTERNS:
            if pat.search(text):
                is_res_cont, deliv_cont, fmt_cont, topic_cont = is_research_or_deliverable_request(text, context=ctx)
                prior_is_res = bool((context or {}).get("is_research"))
                prior_fmt = (context or {}).get("requested_format")
                prior_deliv = (context or {}).get("deliverable_type")
                prior_topic = resolve_topic_from_context(context)
                return IntentClassification(
                    intent=TaskIntent.CONTINUE_BROWSER_TASK,
                    confidence=0.9,
                    reason="instruction continues existing active browser session",
                    operation_type=ResearchOperationType.RESUME_EXISTING_MISSION if prior_is_res else None,
                    requested_format=fmt_cont or prior_fmt,
                    is_research=is_res_cont or prior_is_res,
                    research_topic=topic_cont or prior_topic,
                    deliverable_type=deliv_cont or prior_deliv,
                )

    # 4. Check for unsupported desktop OS operations (P5.2 integrity)
    from .capabilities import check_supported_capability
    is_supp, supp_reason = check_supported_capability(text)
    if not is_supp:
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason=f"unsupported capability: {supp_reason}",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 4c. Check for autonomous email monitoring mission or companion query
    if EMAIL_SCAN_MISSION_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.99,
            reason="Autonomous Email Intelligence directive",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 4c2. Check for continuous background monitoring directive (price watch, page monitoring)
    try:
        from .continuous_monitoring_companion import is_continuous_monitoring_directive
        if is_continuous_monitoring_directive(
            text, history=(context or {}).get("conversation_history") or None
        ):
            return IntentClassification(
                intent=TaskIntent.NON_BROWSER,
                confidence=0.99,
                reason="Continuous background monitoring directive",
                operation_type=ResearchOperationType.NORMAL_CONVERSATION,
            )
    except Exception:
        pass

    # 4c3. Check for direct email dispatch directive
    try:
        from .email_dispatch import is_email_dispatch_instruction
        is_dispatch, _ = is_email_dispatch_instruction(text)
        if is_dispatch:
            return IntentClassification(
                intent=TaskIntent.NON_BROWSER,
                confidence=0.99,
                reason="Direct email dispatch directive",
                operation_type=ResearchOperationType.NORMAL_CONVERSATION,
            )
    except Exception:
        pass

    # 4d. Explicit navigation wins over the calendar directive: "Open
    # Google Calendar" means show the site, not query the API.
    if EXPLICIT_NAVIGATION_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.99,
            reason="explicit navigation to a Google app — operator asked to see the site",
        )

    # 4e. Check for Google Calendar directive
    if CALENDAR_DIRECTIVE_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.99,
            reason="Google Calendar directive",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 4f. "How do I / how to" questions are advice requests, not tasks:
    # "How do I plan a trip to Delhi?" asks for the method, it does not
    # ask the agent to plan the trip. This sits before the email /
    # webmail trigger (step 5) and the browser triggers (step 9):
    # without it, "How do I check my email?" opens Gmail and "How do
    # I plan a trip?" starts browsing, instead of answering.
    if re.match(
        r"^\s*how\s+(?:do\s+i|can\s+i|to|does\s+one|should\s+i|do\s+you)\b",
        text,
        re.IGNORECASE,
    ):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.9,
            reason="how-to question asks for advice, not a browser task",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 5. Check for explicit email / webmail directive (must route to browser, never refusal)
    if EMAIL_TRIGGER.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.99,
            reason="instruction requests email / webmail navigation",
            target_tab_hint="mail.google.com",
            operation_type=None,
        )

    # 5a. Messaging send (review #4, M1): "send/message/share/ask <thing>
    # to|with <name> in|on <channel>" is a browser task on the messaging
    # app. The NON_BROWSER fast-path has no messaging handler, so without
    # this rule a send "completes" by chatting and nothing is ever sent.
    if MESSAGING_SEND_RE.search(text) or MESSAGING_DIRECT_RE.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.97,
            reason="messaging send to a named recipient on a named channel",
        )

    # 5b. Check for pure deliverable export / compilation directive (e.g. "bro generate a pdf man", "export the previous analysis as a pdf")
    is_exp, exp_fmt, exp_deliv = is_export_deliverable_directive(text, context=ctx)
    if is_exp:
        prior_top = resolve_topic_from_context(ctx)
        has_prior_context = bool(ctx.get("is_research") or prior_top or ctx.get("conversation_history"))
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason=f"instruction requests deliverable compilation ({exp_fmt or 'document'}) from existing research findings",
            operation_type=ResearchOperationType.EXPORT_EXISTING_PACKAGE,
            requested_format=exp_fmt,
            is_research=has_prior_context,
            research_topic=prior_top,
            deliverable_type=exp_deliv,
        )



    # 6. Check for autonomous research & multi-format deliverable generation (Step 4)
    #    Evaluated BEFORE simple conceptual/explanation patterns to prevent requests
    #    with explicit deliverable artifacts (e.g. "Explain transformers in a PDF")
    #    from being trapped as pure text chat.
    is_res, deliv_type, req_fmt, topic_hint = is_research_or_deliverable_request(text, context=ctx)
    if is_res:
        prior_top = resolve_topic_from_context(ctx)
        prior_is_res = bool(ctx.get("is_research") or prior_top)
        is_explicit_new = bool(re.search(r"\b(?:new\s+(?:research|report|topic|paper)|start\s+a\s+new|instead\s+research)\b", text, re.IGNORECASE))
        is_refinement = False
        if prior_is_res and not is_explicit_new:
            if REFINEMENT_DIRECTIVE_PATTERN.match(text) or FOLLOW_UP_TRIGGERS_RE.search(text):
                is_refinement = True
            elif topic_hint and prior_top and (prior_top.lower() in topic_hint.lower()):
                is_refinement = True

        op_type = ResearchOperationType.REFINE_EXISTING_RESEARCH if is_refinement else ResearchOperationType.NEW_RESEARCH
        return IntentClassification(
            intent=TaskIntent.RESEARCH_TASK,
            confidence=0.96,
            reason=f"instruction specifies {'refinement of existing research' if is_refinement else 'autonomous new research'} / {deliv_type or 'artifact'} deliverable generation",
            operation_type=op_type,
            requested_format=req_fmt,
            is_research=True,
            research_topic=topic_hint or resolve_topic_from_context(context),
            deliverable_type=deliv_type,
        )

    # 7. Check for explicit non-browser categories
    # 7a. Greetings
    if GREETING_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason="greeting matches non-browser conversational intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7b. Conversational / Meta
    if CONVERSATIONAL_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason="conversational meta request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7c. Code writing / programming questions
    if CODE_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="programming and coding request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7d. Calculations and arithmetic
    if MATH_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="calculation request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7e. Explanations and conceptual questions (without web browse commands or artifact deliverables)
    if EXPLANATION_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.92,
            reason="conceptual explanation matches non-browser conversational intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 8. Check for explicit direct site navigation (with ambiguity verification)
    gen_site_m = GENERIC_SITE_OPEN_TRIGGER.match(text)
    if gen_site_m:
        cand_entity = gen_site_m.group(1).strip().rstrip(".?!")
        from .website_resolver import is_ambiguous_entity
        is_ambig, ambig_term, ambig_candidates = is_ambiguous_entity(cand_entity)
        if is_ambig:
            return IntentClassification(
                intent=TaskIntent.NEW_BROWSER_TASK,
                confidence=0.7,
                reason=f"destination '{cand_entity}' is ambiguous and could refer to multiple distinct entities",
                is_ambiguous=True,
                target_tab_hint=cand_entity,
            )
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.98,
            reason=f"instruction specifies direct navigation to website '{cand_entity}'",
            target_tab_hint=cand_entity,
        )

    # 9. Check for explicit browser triggers
    for pat in BROWSER_TRIGGERS:
        if pat.search(text):
            return IntentClassification(
                intent=TaskIntent.NEW_BROWSER_TASK,
                confidence=0.98,
                reason="instruction contains explicit URL or web browsing target",
            )

    # 9b. Latest-release check (Failure B): "check the latest <brand>
    # <product> thats released" is a live lookup, not a knowledge
    # question. "check" alone is not a web keyword (step 10), so
    # without this the conversational fast-path answers from memory
    # and the task "completes" with no search. Email checks are
    # already claimed by step 5 and monitoring cadences by step 4c2,
    # so this only fires on a recency + release phrasing. The
    # objective attached to the result is simple_search, so the
    # verifier must not apply the research bar to it (CONTRACTS4 §3).
    if _CHECK_LATEST_RE.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.9,
            reason="latest-release check needs a live search",
        )

    # 9c. Question-form latest-release check (fixC M8): "What is the
    # latest <brand> <product> released?" asks the same live question
    # as step 9b but without the word "check", so it fell through to
    # NON_BROWSER and the chat-only path answered from memory while
    # claiming it could not confirm whether the extension was
    # connected — even with the extension connected. Route it to the
    # browser ONLY when the extension is actually connected (the
    # dispatch layer passes extension_connected in context); with no
    # extension the turn stays conversational, as before.
    if ctx.get("extension_connected") and _LATEST_RELEASE_QUESTION_RE.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.9,
            reason="latest-release question needs a live search and the extension is connected",
        )

    # 10. Web research keywords (search, find, look up, buy on the web)
    web_keywords = re.compile(
        r"\b(?:search|browse|look\s+up|find|research|visit|open\s+site|webpage|website|url)\b",
        re.IGNORECASE,
    )
    if web_keywords.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.85,
            reason="instruction specifies web research or website interaction",
        )

    # 10. Default to NON_BROWSER for general questions without browser indicators
    # e.g. "What is the capital of France?", "Help me understand photosynthesis", "Give me a recipe"
    return IntentClassification(
        intent=TaskIntent.NON_BROWSER,
        confidence=0.8,
        reason="general knowledge/conversational request without browsing directives",
    )

# ---------------------------------------------------------------------------
# Monitoring / scheduling NL intents (Stream D routes, Stream C creates)
# CONTRACTS §4. Pure parsers — no side effects, no job/watch creation
# here. The conversational path in agentLoop.py calls these and hands
# the shape to the existing backend functions (scheduler.create_job /
# the monitoring companion); a shape this module cannot map to a real
# backend call returns None instead of a half-built intent.
# ---------------------------------------------------------------------------

_MONITOR_WORDS_RE = re.compile(
    r"\b(watch|monitor|alert|track|notify|price|stock|availability)\b",
    re.IGNORECASE,
)

_SCHEDULE_EVERY_RE = re.compile(
    r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE
)
_SCHEDULE_DAILY_RE = re.compile(
    r"\bdaily\b.{0,20}\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
_SCHEDULE_ONCE_RE = re.compile(
    r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE
)
# A schedule phrase only counts when the instruction is a *task* to
# repeat, not a watch: page/price watches belong to the monitoring
# directive, and "check every 5 minutes" with no task is a watch.
_SCHEDULE_TASK_VERB_RE = re.compile(
    r"\b(summari[sz]e|research|send|email|message|remind|check|do|run|"
    r"give|tell|fetch|get|prepare|create|generate)\b",
    re.IGNORECASE,
)


def _to_ist_hhmm(hour: int, minute: int, ampm: Optional[str]) -> tuple[int, int]:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return hour % 24, minute % 60


def _ist_to_utc_hhmm(hour: int, minute: int) -> tuple[int, int]:
    """IST (UTC+5:30) wall time → UTC wall time for scheduler.create_job,
    which stores daily times as UTC (see R-11). The reply states both."""
    total = (hour * 60 + minute) - 330
    total %= 24 * 60
    return total // 60, total % 60


def detect_schedule_intent(text: str) -> Optional[dict[str, Any]]:
    """Parse a scheduling instruction into the CONTRACTS §4 shape.

    Returns ``{"intent": "create_schedule", "instruction": ...,
    "schedule": {"kind": "every"|"daily"|"once", ...}}`` or None.
    "once" (``in N minutes``) is a real one-off job: ``create_job``
    stores it as schedule_kind=once and it runs exactly once.
    """
    raw = (text or "").strip()
    if not raw or _MONITOR_WORDS_RE.search(raw):
        return None
    if not _SCHEDULE_TASK_VERB_RE.search(raw):
        return None

    m = _SCHEDULE_EVERY_RE.search(raw)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        minutes = amount * {"minute": 1, "minutes": 1, "min": 1, "mins": 1,
                            "hour": 60, "hours": 60, "hr": 60, "hrs": 60,
                            "day": 1440, "days": 1440}[unit]
        if minutes < 1:
            return None
        instruction = _SCHEDULE_EVERY_RE.sub("", raw).strip(" ,.")
        if len(instruction.split()) < 2:
            # A bare "check every 5 minutes" is a watch, not a job —
            # it must fall through to the monitoring directive.
            return None
        return {
            "intent": "create_schedule",
            "instruction": instruction or raw,
            "schedule": {"kind": "every", "interval_minutes": minutes,
                         "in_minutes": None, "time_ist": None},
        }
    m = _SCHEDULE_DAILY_RE.search(raw)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        hour, minute = _to_ist_hhmm(hour, minute, m.group(3))
        uh, um = _ist_to_utc_hhmm(hour, minute)
        instruction = _SCHEDULE_DAILY_RE.sub("", raw).strip(" ,.")
        if len(instruction.split()) < 2:
            return None
        return {
            "intent": "create_schedule",
            "instruction": instruction or raw,
            "schedule": {"kind": "daily", "time_ist": f"{hour:02d}:{minute:02d}",
                         "time_utc": f"{uh:02d}:{um:02d}",
                         "in_minutes": None, "interval_minutes": None},
        }
    m = _SCHEDULE_ONCE_RE.search(raw)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        minutes = amount * (60 if unit.startswith(("hour", "hr")) else 1)
        instruction = _SCHEDULE_ONCE_RE.sub("", raw).strip(" ,.")
        if len(instruction.split()) < 2:
            return None
        return {
            "intent": "create_schedule",
            "instruction": instruction or raw,
            "schedule": {"kind": "once", "in_minutes": minutes,
                         "interval_minutes": None, "time_ist": None},
        }
    return None


_PRICE_RE = re.compile(
    r"(?:price|₹|rs\.?|\$).{0,25}?\b(below|under|less than|above|over|more than|drops?|falls?|rises?)\b.{0,12}?(?:₹|rs\.?|\$)?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_MONITOR_TARGET_RE = re.compile(
    r"\b(?:watch|monitor|track|alert\s+me(?:\s+when|if)?|notify\s+me(?:\s+when|if)?)\b.{0,80}?(https?://\S+|\S+\.(?:com|in|org|net|co\.uk|io)\S*)?",
    re.IGNORECASE,
)


def detect_monitor_intent(text: str) -> Optional[dict[str, Any]]:
    """Parse a monitoring instruction into the CONTRACTS §4 shape.

    Returns ``{"intent": "create_monitor", "url": ..., "condition":
    {...}, "interval_minutes": int, "label": ...}`` or None when the
    text is not a watch request or has no usable target. URL
    validation itself is NOT done here — the creation path validates
    with the same gate the fetch path uses (CONTRACTS §4 / RC-LIVE-04).
    """
    raw = (text or "").strip()
    if not raw or not _MONITOR_WORDS_RE.search(raw):
        return None
    if not _MONITOR_TARGET_RE.search(raw):
        return None
    # A URL is required for a real watch: prefer an explicit URL,
    # else a bare domain. (The target regex above only decides that
    # this IS a watch request; the URL itself is extracted here so a
    # non-greedy optional group cannot silently match empty.)
    url = ""
    um = re.search(r"https?://\S+", raw)
    if um:
        url = um.group(0).rstrip(".,;!?)")
    else:
        dm = re.search(r"\b(?:[a-z0-9-]+\.)+(?:com|in|org|net|co\.uk|io)\S*", raw, re.IGNORECASE)
        if dm:
            url = "https://" + dm.group(0).rstrip(".,;!?)")
    interval = 60
    im = re.search(r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", raw, re.IGNORECASE)
    if im:
        amount, unit = int(im.group(1)), im.group(2).lower()
        interval = amount * (60 if unit.startswith(("hour", "hr")) else 1)
    condition: dict[str, Any] = {"kind": "any_change", "value": None}
    pm = _PRICE_RE.search(raw)
    if pm:
        try:
            value = float(pm.group(2).replace(",", ""))
        except ValueError:
            value = None
        if value is not None:
            word = (pm.group(1) or "").lower()
            kind = "price_below" if word in ("below", "under", "less than", "drops", "drop", "falls", "fall") else "price_above"
            condition = {"kind": kind, "value": value}
    if not url:
        # No usable target: not a creatable watch (the creation path
        # would reject it — say so here instead of emitting a shape
        # with an empty URL).
        return None
    label = raw[:80]
    return {
        "intent": "create_monitor",
        "url": url or "",
        "condition": condition,
        "interval_minutes": interval,
        "label": label,
    }
