"""Conversational working context (Stream 2, CONTRACTS4 §2).

A *working* context is the short-lived, per-chat state of the current
conversation: the entities discussed in the last few turns, the watches
currently active in this chat, and the results just returned. It is
deliberately NOT persistent personal memory:

* ``user_semantic_facts`` / ``long_memory_facts`` are the operator's
  durable, operator-stated facts (long-term memory). Working context
  never reads them, never writes them, and is never merged into them.
* Working context lives in its own table, ``working_context`` — one
  JSON blob per ``chat_id`` — and only ever feeds the *current* chat.

Deictic resolution order (CONTRACTS4 §2):

1. most recent matching entity in working context,
2. active watch with a matching label,
3. a concise clarification — ONLY when no candidate scores above
   threshold (0.5).

Only the resolved objective + at most the last 2 entity labels ever
go to a remote model (see :func:`WorkingContext.model_view`). Full
history and sensitive fields are never sent.

This module is stdlib-only at import time (``db`` is imported lazily
inside functions) so the query builder and the agent loop can import
it without import cycles.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# A candidate below this score is not good enough to act on: the
# caller must ask a concise clarification instead of guessing.
RESOLUTION_THRESHOLD = 0.5

# How many entities / results / watches the blob keeps. Working
# context is a short window, not an archive.
_MAX_ENTITIES = 20
_MAX_RESULTS = 10
_MAX_WATCHES = 20

# Reference tokens that name "the thing we were just talking about".
_ORDINAL_WORDS = {
    "first": 0,
    "second": 1,
    "third": 2,
    "fourth": 3,
    "fifth": 4,
    "latest": 0,
    "most recent": 0,
    "previous": 1,
}

_WATCH_MARKER_RE = re.compile(
    r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{8,})\s*-->"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class WorkingContext:
    """Structured conversational working state for one chat.

    ``entities`` is ordered most-recent-first; each entry is
    ``{"kind": "product"|"website"|"watch"|"source"|"result",
    "label": str, "detail": dict, "turn": int}``.
    ``active_watches`` entries are ``{"watch_id", "label", "title",
    "product", "interval_seconds", "condition",
    "state": "active"|"paused"}``.
    ``last_results`` entries are ``{"title", "url", "date", "source"}``.
    """

    chat_id: str
    entities: list[dict] = field(default_factory=list)
    active_watches: list[dict] = field(default_factory=list)
    last_results: list[dict] = field(default_factory=list)
    preferences_used: list[str] = field(default_factory=list)
    turn_counter: int = 0

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "entities": self.entities,
            "active_watches": self.active_watches,
            "last_results": self.last_results,
            "preferences_used": self.preferences_used,
            "turn_counter": self.turn_counter,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkingContext":
        return cls(
            chat_id=str(data.get("chat_id") or ""),
            entities=list(data.get("entities") or []),
            active_watches=list(data.get("active_watches") or []),
            last_results=list(data.get("last_results") or []),
            preferences_used=list(data.get("preferences_used") or []),
            turn_counter=int(data.get("turn_counter") or 0),
        )

    # -- model-safe view ------------------------------------------------
    def model_view(self, objective: Optional[str] = None) -> dict[str, Any]:
        """The ONLY working-context shape a remote model may see.

        At most the resolved objective + the last 2 entity labels
        (most recent first) + active watch labels. No history, no
        details, no prices, no URLs, no sensitive fields.
        """
        view: dict[str, Any] = {
            "entity_labels": [e.get("label", "") for e in self.entities[:2]
                              if e.get("label")],
            "active_watch_labels": [w.get("label", "") for w in self.active_watches
                                    if w.get("state") == "active" and w.get("label")],
        }
        if objective:
            view["objective"] = objective
        return view


# ---------------------------------------------------------------------------
# Persistence (table ``working_context`` — one JSON blob per chat_id).
# Separate from user_semantic_facts / long_memory_facts by design.
# ---------------------------------------------------------------------------

def load_working_context(chat_id: str, db_path: Optional[str] = None) -> WorkingContext:
    """Load the working context for a chat; empty when none exists
    (or the stored blob is corrupt — a bad blob must never break a
    turn). Active watches are re-synced from the missions table so a
    watch paused/cancelled through another path is reflected here."""
    ctx = WorkingContext(chat_id=chat_id or "")
    if not chat_id:
        return ctx
    try:
        from . import db

        db.init_db(db_path)
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT data FROM working_context WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row and row["data"]:
            try:
                ctx = WorkingContext.from_dict(json.loads(row["data"]))
                ctx.chat_id = chat_id
            except (ValueError, TypeError, AttributeError):
                ctx = WorkingContext(chat_id=chat_id)
    except Exception:
        return WorkingContext(chat_id=chat_id)
    try:
        sync_watches_from_db(ctx, db_path)
    except Exception:
        pass
    return ctx


def save_working_context(ctx: WorkingContext, db_path: Optional[str] = None) -> None:
    """Persist the working context blob for its chat."""
    if not ctx.chat_id:
        return
    from . import db

    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO working_context (chat_id, data, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data,"
            " updated_at=excluded.updated_at",
            (ctx.chat_id, json.dumps(ctx.to_dict()), time.time()),
        )
        conn.commit()


def _mission_row_to_watch(row: Any) -> dict[str, Any]:
    """A monitoring_missions row -> a working-context watch entry."""
    try:
        params = json.loads(row["condition_params"] or "{}")
    except (ValueError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    product = str(params.get("target") or "")
    title = str(row["title"] or "")
    return {
        "watch_id": str(row["id"]),
        "label": product or title,
        "title": title,
        "product": product,
        "target_url": str(row["target_url"] or ""),
        "created_at": row["created_at"],
        "interval_seconds": row["interval_seconds"],
        "condition": {
            "type": row["condition_type"],
            "params": params,
        },
        "state": str(row["status"] or "active"),
    }


def sync_watches_from_db(ctx: WorkingContext, db_path: Optional[str] = None) -> None:
    """Rebuild ``active_watches`` from the missions table for this
    chat (most recent first). Watches the operator paused, resumed
    or cancelled through any other path show up correctly; the blob
    is only a cache, the missions table is the source of truth."""
    if not ctx.chat_id:
        return
    from . import db

    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_missions WHERE conversation_id = ?"
            " AND status IN ('active', 'paused') ORDER BY created_at DESC",
            (ctx.chat_id,),
        ).fetchall()
    ctx.active_watches = [_mission_row_to_watch(r) for r in rows[:_MAX_WATCHES]]


def find_mission_row(mission_id: str, db_path: Optional[str] = None) -> Any | None:
    """One monitoring_missions row by id, or None."""
    from . import db

    with db.get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM monitoring_missions WHERE id = ?", (mission_id,)
        ).fetchone()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_entity(ctx: WorkingContext, kind: str, label: str,
                  detail: Optional[dict] = None) -> None:
    """Prepend an entity (most-recent-first). A re-mention of the
    same label moves it to the front instead of duplicating it."""
    label = (label or "").strip()
    if not label:
        return
    ctx.turn_counter += 1
    ctx.entities = [e for e in ctx.entities
                    if str(e.get("label", "")).casefold() != label.casefold()]
    ctx.entities.insert(0, {
        "kind": kind,
        "label": label,
        "detail": detail or {},
        "turn": ctx.turn_counter,
    })
    del ctx.entities[_MAX_ENTITIES:]


def record_result(ctx: WorkingContext, title: str, url: str,
                  date: Optional[str] = None, source: Optional[str] = None) -> None:
    """Prepend a search/research result (most-recent-first)."""
    url = (url or "").strip()
    if not url:
        return
    ctx.last_results = [r for r in ctx.last_results if r.get("url") != url]
    ctx.last_results.insert(0, {
        "title": (title or "").strip()[:200],
        "url": url,
        "date": date,
        "source": source,
    })
    del ctx.last_results[_MAX_RESULTS:]


def record_watch(ctx: WorkingContext, watch: dict) -> None:
    """Prepend/replace a watch entry by watch_id (most-recent-first)."""
    watch_id = str(watch.get("watch_id") or "")
    if not watch_id:
        return
    ctx.active_watches = [w for w in ctx.active_watches
                          if str(w.get("watch_id")) != watch_id]
    ctx.active_watches.insert(0, dict(watch))
    del ctx.active_watches[_MAX_WATCHES:]


def remove_watch(ctx: WorkingContext, watch_id: str) -> None:
    """Drop a watch (cancelled) from the working context."""
    ctx.active_watches = [w for w in ctx.active_watches
                          if str(w.get("watch_id")) != str(watch_id)]


def note_completed_turn(ctx: WorkingContext, *, instruction: str,
                        message: str, state: str,
                        db_path: Optional[str] = None) -> None:
    """Record one finished turn into the working context.

    This is deliberately conservative — it only records what the
    turn itself proves, never a guess:

    * a ``<!-- privagent-watch: <id> -->`` marker in the reply proves
      a watch was created/updated for this chat: the mission row is
      read back and recorded as both the watch and its product entity;
    * otherwise, when the task completed, up to 3 URLs in the reply
      become ``last_results`` (title = the line carrying the URL);
    * otherwise, when the task completed and the instruction
      sanitises to a clean multi-word noun phrase, that phrase is
      recorded as a ``result`` entity — the sanitiser strips
      pronouns, intervals, conditions and notification phrases, so a
      stored label can never be "this for every 5 mins".
    """
    from .query_builder import sanitize_query_text

    ctx.turn_counter += 1
    watch_marker = _WATCH_MARKER_RE.search(message or "")
    if watch_marker:
        try:
            row = find_mission_row(watch_marker.group(1), db_path)
        except Exception:
            row = None
        if row is not None:
            watch = _mission_row_to_watch(row)
            record_watch(ctx, watch)
            if watch.get("product"):
                record_entity(ctx, "product", watch["product"],
                              {"watch_id": watch["watch_id"],
                               "url": row["target_url"]})
            return
    if (state or "").lower() != "completed":
        return
    for url in _URL_RE.findall(message or "")[:3]:
        line = ""
        for raw_line in (message or "").splitlines():
            if url in raw_line:
                line = re.sub(r"https?://\S+", "", raw_line).strip(" *#-")
                break
        record_result(ctx, line[:120] or url, url)
    clean = sanitize_query_text(instruction or "")
    if clean and len(clean.split()) >= 2:
        record_entity(ctx, "result", clean)


# ---------------------------------------------------------------------------
# Deictic resolution
# ---------------------------------------------------------------------------

_REFERENCE_RES = [
    # Most specific first: ordinals and comparatives, then the
    # "do the same" repeat (it must win over the bare deictic inside
    # it — "do the same for this" is a repeat, not a plain "this").
    (re.compile(r"\b(?:the\s+)?(first|second|third|fourth|fifth|latest|most\s+recent|previous|cheapest)\b"
                r"(?:\s+(?:one|result|product|item|listing|watch))?", re.IGNORECASE), "ordinal"),
    (re.compile(r"\bdo\s+the\s+same\s+for\s+(?:this|that|it)\b", re.IGNORECASE), "repeat"),
    (re.compile(r"\b(?:that|the|this)\s+watch\b", re.IGNORECASE), "watch"),
    (re.compile(r"\b(?:this|that)\s+(?:one|product|item|result|listing)\b", re.IGNORECASE), "deictic"),
    (re.compile(r"\b(?:this|that|it)\b", re.IGNORECASE), "deictic"),
]

_CONTROL_VERBS = ("change", "cancel", "pause", "resume", "stop", "delete", "update")


def find_reference_token(text: str) -> tuple[str, str] | None:
    """The deictic reference inside ``text`` as (token, kind), or
    None when the text names no reference. ``kind`` is one of
    "ordinal" / "watch" / "deictic" / "repeat"."""
    if not text:
        return None
    lowered = text.lower()
    for pattern, kind in _REFERENCE_RES:
        m = pattern.search(text)
        if m:
            return m.group(0), kind
    return None


def _score_entity(entity: dict, *, kind: Optional[str], recency: float) -> float:
    """Recency is the base score (most recent = 1.0, decaying);
    a kind match adds a bonus. No label-token guessing: a bare
    "this" must resolve to the most recent thing, not to a
    coincidental word match."""
    score = recency
    if kind and entity.get("kind") == kind:
        score += 0.15
    return min(1.0, score)


def resolve_reference(ctx: WorkingContext, text: str, *,
                      kind: Optional[str] = None,
                      prefer: Optional[str] = None) -> dict[str, Any]:
    """Resolve a deictic reference in ``text`` against the working
    context.

    Resolution order: most recent matching entity -> active watch
    with a matching label -> concise clarification ONLY when no
    candidate scores above :data:`RESOLUTION_THRESHOLD`.

    ``prefer="watch"`` is for control follow-ups ("change it...",
    "cancel that watch"): a watch is the expected target, so a watch
    outranks an entity. Returns::

        {"status": "resolved", "entity": {...}|None, "watch": {...}|None,
         "score": float, "repeat": bool, "question": None}
        {"status": "clarification", "entity": None, "watch": None,
         "score": float, "repeat": False,
         "question": "Which one do you mean — <label A> or <label B>?"}
    """
    found = find_reference_token(text or "")
    if found is None:
        return {"status": "clarification", "entity": None, "watch": None,
                "score": 0.0, "repeat": False,
                "question": "Which one do you mean?"}
    token, ref_kind = found
    token_l = token.lower()
    repeat = ref_kind == "repeat"

    # A control verb ("change it...", "cancel that watch") targets a
    # watch first; a plain reference targets the most recent entity.
    wants_watch = prefer == "watch" or ref_kind == "watch" or any(
        re.search(rf"\b{v}\b", (text or "").lower()) for v in _CONTROL_VERBS
    )

    # --- ordinals / comparatives over entities (most-recent-first) ---
    ordinal_word = None
    for word in ("first", "second", "third", "fourth", "fifth",
                 "latest", "most recent", "previous", "cheapest"):
        if re.search(rf"\b{word}\b", token_l):
            ordinal_word = word
            break
    if ordinal_word == "cheapest":
        priced = [e for e in ctx.entities
                  if isinstance((e.get("detail") or {}).get("price"), (int, float))]
        if priced:
            cheapest = min(priced, key=lambda e: e["detail"]["price"])
            return {"status": "resolved", "entity": cheapest, "watch": None,
                    "score": 0.9, "repeat": repeat, "question": None}
        return {"status": "clarification", "entity": None, "watch": None,
                "score": 0.4, "repeat": repeat,
                "question": "I don't have prices for the items here — which one is the cheapest?"}
    if ordinal_word in _ORDINAL_WORDS:
        idx = _ORDINAL_WORDS[ordinal_word]
        pool = ctx.entities
        if ordinal_word == "previous":
            pool = [e for e in ctx.entities if e.get("kind") == "product"]
        if idx < len(pool):
            return {"status": "resolved", "entity": pool[idx], "watch": None,
                    "score": 0.95, "repeat": repeat, "question": None}
        return {"status": "clarification", "entity": None, "watch": None,
                "score": 0.3, "repeat": repeat,
                "question": "I only have %d item(s) here — which one do you mean?" % len(pool)}

    # --- watches first when the ask is a control follow-up ---
    if wants_watch and ctx.active_watches:
        watch = ctx.active_watches[0]
        return {"status": "resolved", "entity": None, "watch": watch,
                "score": 0.95, "repeat": repeat, "question": None}

    # --- most recent matching entity ---
    if ctx.entities:
        best: dict[str, Any] | None = None
        best_score = 0.0
        for i, entity in enumerate(ctx.entities):
            recency = 1.0 - (i * 0.1)
            score = _score_entity(entity, kind=kind, recency=max(0.1, recency))
            if score > best_score:
                best, best_score = entity, score
        if best is not None and best_score >= RESOLUTION_THRESHOLD:
            return {"status": "resolved", "entity": best, "watch": None,
                    "score": best_score, "repeat": repeat, "question": None}

    # --- any active watch as a fallback target ---
    if ctx.active_watches:
        watch = ctx.active_watches[0]
        return {"status": "resolved", "entity": None, "watch": watch,
                "score": 0.6, "repeat": repeat, "question": None}

    # --- nothing scores above threshold: ask, don't guess ---
    labels = [e.get("label", "") for e in ctx.entities[:2] if e.get("label")]
    if labels:
        question = "Which one do you mean — %s?" % (" or ".join(labels))
    else:
        question = "Which one do you mean? I don't have an item from this chat to point at yet."
    return {"status": "clarification", "entity": None, "watch": None,
            "score": 0.0, "repeat": repeat, "question": question}


def resolve_label(ctx: WorkingContext, text: str, *,
                  kind: Optional[str] = None,
                  prefer: Optional[str] = None) -> str | None:
    """The resolved label for a reference in ``text``, or None when
    the reference needs clarification."""
    result = resolve_reference(ctx, text, kind=kind, prefer=prefer)
    if result["status"] != "resolved":
        return None
    if result.get("entity"):
        return str(result["entity"].get("label") or "")
    if result.get("watch"):
        return str(result["watch"].get("label") or result["watch"].get("title") or "")
    return None


# ---------------------------------------------------------------------------
# Watch-target resolution (Stream 2 follow-up: Stream 5 F2)
# ---------------------------------------------------------------------------

# Maker -> signature product lines. A user refers to a watch by its
# maker ("Cancel the Samsung watch") while the watch title carries
# the line ("Galaxy S26 Ultra 5G"), never the maker. This small
# static map is the only maker knowledge the resolver uses; a token
# that appears in the title/label itself always wins over the map.
_BRAND_FAMILIES: dict[str, tuple[str, ...]] = {
    "samsung": ("galaxy",),
    "apple": ("iphone", "ipad", "mac", "airpods"),
    "sony": ("wh-1000xm", "playstation", "xperia", "bravia"),
    "boat": ("airdopes", "bassheads", "rockerz"),
    "xiaomi": ("redmi", "poco"),
    "google": ("pixel",),
    "oneplus": ("oneplus", "nord"),
    "noise": ("noise",),
    "jbl": ("jbl",),
    "bose": ("bose", "quietcomfort"),
}

_GENERIC_PAGE_TOKENS = {"website", "webpage", "page", "site"}
_STORE_HOSTS = ("amazon.", "flipkart.", "croma.")

_ORDINAL_WATCH_RES: list[tuple[Any, int]] = [
    (re.compile(r"\b(first|1st)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(second|2nd)\b", re.IGNORECASE), 1),
    (re.compile(r"\b(third|3rd)\b", re.IGNORECASE), 2),
    (re.compile(r"\b(fourth|4th)\b", re.IGNORECASE), 3),
    (re.compile(r"\b(fifth|5th)\b", re.IGNORECASE), 4),
    (re.compile(r"\b(last|latest|most\s+recent)\b", re.IGNORECASE), -1),
]

_WATCH_CONTROL_VERBS = ("pause", "resume", "cancel", "delete", "stop")
_WATCH_REF_STOPWORDS = {
    "the", "a", "an", "watch", "watches", "please", "pls", "my",
    "pause", "resume", "cancel", "delete", "stop",
    "first", "second", "third", "fourth", "fifth", "last", "latest",
    "1st", "2nd", "3rd", "4th", "5th",
    # Generic verbs/fillers that never name a watch ("make that check
    # twice as often" must resolve by pronoun, not stall on "make").
    "make", "check", "change", "update", "often", "twice", "half",
    "frequently", "frequency", "time", "times", "do", "does", "did",
    "go", "get", "set", "run", "keep", "let", "put", "turn", "switch",
    "every", "minute", "minutes", "hour", "hours", "day", "days",
    "more", "less", "as", "just", "now", "then", "than", "much",
    "many", "also", "still", "again", "back", "on", "off", "up",
    "down", "out", "over", "under", "with", "without", "for", "from",
    "into", "about", "that", "this", "it", "its", "they", "them",
    "their", "he", "she", "we", "you", "your", "our", "his", "her",
    "me", "us", "him", "one", "ones",
    # Watch-state words ("resume the paused one") describe a state,
    # never a product — they must reach the status-weighted pronoun
    # step instead of stalling step 4 as an unmatched token.
    "paused", "active",
}

# The word right before "watch" ("Samsung" in "Cancel the Samsung
# watch", "website" in "cancel that website watch"). A named slot
# that matches no watch is a failed reference, not a licence to
# fall back to the only watch in the chat.
_BEFORE_WATCH_RE = re.compile(
    r"\b(?:the\s+|that\s+|this\s+|a\s+)?([A-Za-z][\w\-]{2,})\s+watch\b",
    re.IGNORECASE,
)
_ORDINAL_WORDS_SET = {
    "first", "second", "third", "fourth", "fifth", "last", "latest",
    "1st", "2nd", "3rd", "4th", "5th",
}


def _watches_by_creation(watches: list[dict]) -> list[dict]:
    """Oldest first (creation order — what "first watch" means)."""
    return sorted(watches, key=lambda w: (w.get("created_at") or 0))


def _watch_is_generic_page(watch: dict) -> bool:
    """A page watch, not a product watch. The condition type decides
    first (page_change / new_listings vs below_price / price_drop);
    only when it is unknown does the target URL's host decide (a
    store host = product watch). Condition may be the dict shape
    from _mission_row_to_watch or a plain string (as tests build it)."""
    condition = watch.get("condition")
    if isinstance(condition, dict):
        ctype = str(condition.get("type") or "").lower()
    elif isinstance(condition, str):
        ctype = condition.lower()
    else:
        ctype = ""
    if ctype in ("page_change", "new_listings", "content_change"):
        return True
    if ctype in ("below_price", "price_drop", "percent_discount"):
        return False
    url = str(watch.get("target_url") or "").lower()
    if not url:
        return False
    return not any(host in url for host in _STORE_HOSTS)


def _watch_matches_token(watch: dict, token: str) -> bool:
    """True when ``token`` names this watch: as a whole word in its
    label / title, via the maker->line map, or as a generic-page
    word for a page watch."""
    token = (token or "").lower().strip()
    if not token:
        return False
    hay = f"{watch.get('label') or ''} {watch.get('title') or ''} {watch.get('product') or ''}".lower()
    if re.search(rf"\b{re.escape(token)}\b", hay):
        return True
    for line in _BRAND_FAMILIES.get(token, ()):  # maker -> its lines
        if line in hay:
            return True
    if token in _GENERIC_PAGE_TOKENS and _watch_is_generic_page(watch):
        return True
    return False


def _list_hint(watches: list[dict]) -> str:
    """One-line inventory for a clarification question."""
    if not watches:
        return "You don't have any watches in this chat yet."
    shown = ", ".join(
        f"{w.get('label') or w.get('title') or 'watch'}"
        f" ({str(w.get('watch_id') or '')[:8]})"
        for w in watches[:3]
    )
    return f"Your watches here: {shown}."


def resolve_watch_target(ctx: WorkingContext, text: str) -> dict[str, Any]:
    """Resolve which watch a control turn targets, from the working
    context (active watches synced from the missions table).

    Resolution order: explicit id/prefix -> ordinal by creation
    order ("first watch" = oldest) -> brand/product token in the
    text ("Samsung" -> a Galaxy watch, "headphones" -> the
    headphones watch) -> pronoun / bare "the watch" -> status-
    weighted (a resume takes the most recently created paused
    watch, a pause takes the most recently created active watch,
    any other control takes the most recent watch) -> single watch
    in the chat. Anything else is a concise clarification naming
    the candidates — never a guess.

    Returns ``{"status": "resolved", "watch": {...}, "score": float,
    "question": None, "candidates": [...]}``,
    ``{"status": "clarification", ...}`` or ``{"status": "none",
    ...}`` when the chat has no watches at all.
    """
    watches = list(ctx.active_watches or [])
    if not watches:
        return {"status": "none", "watch": None, "score": 0.0,
                "question": "You don't have any watches in this chat yet.",
                "candidates": []}
    lowered = (text or "").lower()

    def _clarify(cands: list[dict]) -> dict[str, Any]:
        shown = ", ".join(
            f"{w.get('label') or w.get('title') or 'watch'}"
            f" ({str(w.get('watch_id') or '')[:8]}, {w.get('state') or 'active'})"
            for w in cands[:3]
        )
        return {"status": "clarification", "watch": None, "score": 0.0,
                "question": f"Which watch do you mean — {shown}? "
                            "Give me more of its name, or its id.",
                "candidates": cands}

    # 1. Explicit id / id prefix.
    hex_tokens = re.findall(r"\b[0-9a-fA-F]{6,}\b", text or "")
    for token in hex_tokens:
        for watch in watches:
            if str(watch.get("watch_id") or "").replace("-", "").startswith(token.lower()):
                return {"status": "resolved", "watch": watch, "score": 1.0,
                        "question": None, "candidates": [watch]}

    # 2. Ordinal by creation order ("first watch" = oldest).
    creation_order = _watches_by_creation(watches)
    for pattern, idx in _ORDINAL_WATCH_RES:
        if pattern.search(text or ""):
            if idx == -1:
                target = creation_order[-1]
            elif 0 <= idx < len(creation_order):
                target = creation_order[idx]
            else:
                # "third watch" with only two watches: ask, never
                # index out of range (was an IndexError) or guess.
                return _clarify(creation_order)
            return {"status": "resolved", "watch": target, "score": 0.95,
                    "question": None, "candidates": [target]}

    # 3. The word right before "watch" ("Samsung", "website").
    # A named slot that matches no watch is a FAILED reference —
    # never fall back to the only watch in the chat for it. A
    # state word ("paused" in "resume the paused watch") is not a
    # name at all — skip the slot step so the status-weighted
    # pronoun step below picks the paused watch.
    slot_m = _BEFORE_WATCH_RE.search(text or "")
    slot = slot_m.group(1) if slot_m else None
    if slot and slot.lower() not in _ORDINAL_WORDS_SET and slot.lower() not in (
            "the", "that", "this", "a", "an", "paused", "active"):
        slot_matches = [w for w in watches if _watch_matches_token(w, slot)]
        if len(slot_matches) == 1:
            return {"status": "resolved", "watch": slot_matches[0], "score": 0.9,
                    "question": None, "candidates": slot_matches}
        if len(slot_matches) > 1:
            return _clarify(slot_matches)
        # LIVE-REPLAY (Stream 4): a slot that matches nothing is NOT
        # yet a failed reference — fall through to step 4, where any
        # other word in the text can still resolve it ("Cancel the
        # Sony headphones watch": slot "headphones" matches no title,
        # but "sony" matches exactly one watch). Step 4 still
        # clarifies when no word matches at all ("pause the zebra
        # watch"), so the failed-reference behaviour is preserved.

    # 4. Any other distinctive word in the text ("headphones" in
    # "pause the headphones one"). A distinctive word that matches
    # NO watch is a failed reference ("pause the zebra one") — it
    # must clarify, never fall through to the single-watch fallback.
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", lowered)
             if w not in _WATCH_REF_STOPWORDS and len(w) >= 3]
    token_matches: list[dict] = []
    for watch in watches:
        if any(_watch_matches_token(watch, w) for w in words):
            token_matches.append(watch)
    if len(token_matches) == 1:
        return {"status": "resolved", "watch": token_matches[0], "score": 0.9,
                "question": None, "candidates": token_matches}
    if len(token_matches) > 1:
        return _clarify(token_matches)
    if words:
        # LIVE-REPLAY (Stream 4): quote the named slot with its
        # original casing when there is one ("Zebra", not "zebra") —
        # the step-3 clarification used to carry the case.
        _missing = slot if slot else words[0]
        return {"status": "clarification", "watch": None, "score": 0.0,
                "question": f"I couldn't find a watch for '{_missing}'. "
                            f"{_list_hint(watches)} Which one did you mean?",
                "candidates": watches}

    # 5. Pronoun / bare "the watch" -> status-weighted target.
    # A bare pronoun names the watch the previous turn acted on,
    # and the watch states are the record of that: a resume follows
    # a pause, so it takes the most recently created PAUSED watch —
    # not the most recent watch, which is usually still active (the
    # L11 failure: "Resume it" answered about the active Galaxy
    # while the paused iPhone stayed paused). A pause takes the most
    # recently created ACTIVE watch — pausing an already-paused
    # watch is a no-op. Any other control keeps the most recent
    # watch. When no watch carries the wanted state, fall back to
    # the most recent watch rather than clarifying.
    most_recent = watches[0]
    has_pronoun = bool(re.search(r"\b(?:it|that|this|them|that\s+one|this\s+one)\b", lowered))
    has_bare_watch = bool(re.search(r"\bthe\s+watch\b", lowered))
    # A control verb + "watch" with nothing distinctive between
    # them ("resume the paused watch", "pause the active watch")
    # is a bare control too: ordinals, brand slots and product
    # tokens above already had their chance, so what is left is
    # the state word — resolve it by status, never clarify on it.
    has_control_watch = bool(re.search(
        r"\b(?:pause|resume|cancel|delete|stop)\b[^.\n]{0,30}?\bwatch\b", lowered))
    if has_pronoun or has_bare_watch or has_control_watch:
        verb = next((v for v in _WATCH_CONTROL_VERBS
                     if re.search(rf"\b{v}\b", lowered)), None)
        target = most_recent
        if verb == "resume":
            paused = [w for w in watches if (w.get("state") or "") == "paused"]
            if paused:
                target = paused[0]
        elif verb == "pause":
            active = [w for w in watches if (w.get("state") or "") == "active"]
            if active:
                target = active[0]
        return {"status": "resolved", "watch": target, "score": 0.8,
                "question": None, "candidates": [target]}

    # 6. No referent at all: exactly one watch -> it; several -> ask.
    if len(watches) == 1:
        return {"status": "resolved", "watch": watches[0], "score": 0.9,
                "question": None, "candidates": [watches[0]]}
    return _clarify(watches)
