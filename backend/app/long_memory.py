"""Long-term operator memory (Task 9): durable facts, skills, files.

Three tiers, all explicit and operator-owned:

* **Facts** — user-stated durable facts ("my home airport is DEL",
  "I prefer window seats"). Stored with a source and a confidence.
* **Skills** — reusable learned procedures ("how I book a flight on
  site X"), stored as prose steps, versioned, with a success count.
* **Files** — user-attached reference files. The planner never sees a
  whole file: it gets a short summary and may request one excerpt.

Provenance rule (the whole point of this module): memory is NEVER
created from page content. A fact or skill may only come from the
operator directly (``source="user"``) or from a task's confirmed
outcomes that the operator explicitly confirmed
(``source="task:<id>:confirmed"``). Anything else is rejected in code.
Page text is untrusted data (Task 3 checker, Task 6 ASR 0.50) — letting
it write memory would turn every injection into a persistent one.

Storage reuses the Task 2 pattern: SQLite (``app.db``) with Fernet
encryption at the accessor layer for values, files under
``backend/data/files/`` (dir 0700, files 0600). No embeddings, no LLM:
recall is deterministic keyword scoring.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db
from . import secrets as secrets_store
from .privacy import Sensitivity, classify_sensitivity, scrub_persistent

logger = logging.getLogger(__name__)

FACT_CATEGORIES = frozenset({"general", "personal", "retail", "coding", "travel"})
MAX_FACT_CHARS = 1000
MAX_SKILL_NAME_CHARS = 120
MAX_SKILL_STEPS_CHARS = 4000
MAX_FILE_SUMMARY_CHARS = 500
MAX_FILE_BYTES = 5 * 1024 * 1024

_FILES_DIR = Path(__file__).resolve().parents[1] / "data" / "files"

_SOURCE_RE = re.compile(r"^(?:user|task:[A-Za-z0-9_-]+:confirmed)$")

# Credential guard: the shared classifier (privacy.classify_sensitivity)
# only flags structured PII (email / card / phone). A bare password or
# API key ("password hunter2", "api key is abc123") scores as ordinary
# text there, so long-term memory gets its own explicit check.
#
# REVIEW (Task 9 audit): the first version of this guard was bypassable
# in at least eight ways, all verified by running the guard against a
# battery of inputs before this fix:
#   "password hunter" / "api key abcdef" (bare value, no digit),
#   "hunter2 is my password" (value BEFORE the keyword),
#   "pin 1234" (short numeric PIN, the old >= 6-char rule),
#   "my token is abc123" / "my secret is abc123" / "my bearer token is
#   xyz789" / "private key is abc123" / "seed phrase is ..." (keywords
#   missing from the list), "the password for my bank is 123456"
#   (keyword, filler words, then "is <value>"),
#   "password correcthorsebatterystaple" (long passphrase, no digit).
# The guard below closes those holes. Two genuinely ambiguous cases
# stay unflagged on purpose: "password hunter" (reads as a noun phrase,
# not a value) and "api key abcdef" (a 6-letter bare token with no digit
# is indistinguishable from an ordinary word). A value that looks like
# a secret is: >= 4 chars with a digit, >= 12 chars, mixed case >= 8,
# or carrying punctuation.
_CRED_RE = re.compile(
    r"\b(?:(?P<strong>password|passwd|pwd|api[-_ ]?key|client[-_ ]?secret"
    r"|access[-_ ]?token|auth[-_ ]?token|bearer(?:[-_ ]?token)?"
    r"|private[-_ ]?key|seed(?:[-_ ]?phrase)?|otp|pin)"
    r"|(?P<weak>token|secret))\b",
    re.IGNORECASE,
)
# A keyword followed by one of these nouns is a compound concept, not a
# value ("my password manager is 1Password", "api key rotation policy",
# "password reset link") — skip that occurrence, don't flag the sentence.
_CRED_NOUN_AFTER = frozenset({
    "manager", "reset", "rotation", "policy", "recovery", "change",
    "requirements", "requirement", "strength", "field", "prompt",
    "screen", "page", "login", "signin", "sign-in", "authentication",
    "verification", "hint", "question", "manager's",
})
_ASSIGNED_VALUE_RE = re.compile(r"^\s*[:=]", re.IGNORECASE)
_IS_VALUE_RE = re.compile(r"^\s+(?:is|are)\b", re.IGNORECASE)
_VALUE_TOKEN_RE = re.compile(r"^\s*(\S+)")
# "<value> is my/the <keyword>" — the value comes first.
_VALUE_BEFORE_RE = re.compile(
    r"\b([A-Za-z0-9._\-!@#$%^&*+]{4,})\s+(?:is|was)\s+(?:my\s+)?(?:the\s+)?"
    r"(?:password|passwd|pwd|api[-_ ]?key|client[-_ ]?secret|access[-_ ]?token"
    r"|auth[-_ ]?token|bearer(?:[-_ ]?token)?|private[-_ ]?key"
    r"|seed(?:[-_ ]?phrase)?|otp|pin|token|secret)\b(?:[.,;!?]|$)",
    re.IGNORECASE,
)


def _is_secret_like_token(token: str) -> bool:
    token = (token or "").strip(".,;:!?\"'()[]{}")
    if len(token) < 4:
        return False
    if any(c.isdigit() for c in token):
        return True
    if len(token) >= 12:
        return True
    if len(token) >= 8 and any(c.islower() for c in token) and any(c.isupper() for c in token):
        return True
    if len(token) >= 6 and any(not c.isalnum() and c not in "-_." for c in token):
        return True
    return False


def _looks_like_credential(text: str) -> bool:
    for match in _CRED_RE.finditer(text or ""):
        strong = match.group("strong") is not None
        rest = text[match.end():]
        nxt = re.match(r"\s*([A-Za-z]+)", rest)
        if nxt and nxt.group(1).lower() in _CRED_NOUN_AFTER:
            continue
        assigned = re.match(r"\s*[:=]\s*(\S+)", rest)
        if assigned:
            if strong:
                return True
            if _is_secret_like_token(assigned.group(1)):
                return True
            continue
        is_match = _IS_VALUE_RE.match(rest)
        if is_match:
            if strong:
                return True
            value = _VALUE_TOKEN_RE.match(rest[is_match.end():])
            if value and _is_secret_like_token(value.group(1)):
                return True
            continue
        if strong:
            # "the password for my bank is 123456": the keyword, a few
            # filler words, then "is <secret-like value>".
            far = re.match(r"(?:\s+[A-Za-z]{1,20}){1,5}?\s+(?:is|are)\s+(\S+)", rest)
            if far and _is_secret_like_token(far.group(1)):
                return True
        token = _VALUE_TOKEN_RE.match(rest)
        if token and _is_secret_like_token(token.group(1)):
            return True
    for match in _VALUE_BEFORE_RE.finditer(text or ""):
        if _is_secret_like_token(match.group(1)):
            return True
    return False


def _redact_credential_lines(text: str) -> str:
    """Redact the value out of credential-looking lines, keeping the
    label. Used for file excerpts: a file may legitimately contain a
    password line, and the excerpt path previously returned it verbatim
    (``scrub_persistent`` only redacts emails/phones/URLs)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        before = _VALUE_BEFORE_RE.search(line)
        if before and _is_secret_like_token(before.group(1)):
            out.append(line[:before.start(1)] + "[REDACTED]" + line[before.end(1):])
            continue
        if _looks_like_credential(line):
            redacted = re.sub(
                r"((?:[:=]\s*)|(?:\b(?:is|are)\b\s+))\S+",
                r"\1[REDACTED]",
                line, count=1, flags=re.IGNORECASE,
            )
            out.append(redacted if redacted != line else "[credential redacted]")
        else:
            out.append(line)
    return "\n".join(out)


def _reject_credential_text(text: str, what: str) -> None:
    if _looks_like_credential(text):
        raise ValueError(
            f"{what} looks like a credential with a value — memory never "
            "stores secrets; rephrase without the value"
        )

_STOPWORDS = frozenset(
    "a an the and or of to in on for with me my i is are was were be been "
    "please do does did can could will would should it its this that these "
    "those from at by as not no yes if then than so such very just".split()
)


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _score(query_tokens: set[str], text_tokens: set[str]) -> float:
    """Deterministic relevance: shared-token count, ties broken by
    recency at the call site (rows are ordered newest-first)."""
    if not query_tokens:
        return 0.0
    return float(len(query_tokens & text_tokens))


def _validate_source(source: str) -> str:
    """The provenance gate. Only the operator, or a confirmed task
    outcome the operator explicitly approved, may create memory.
    ``"page"``, ``"observation"``, ``"planner"`` and anything else is
    rejected — page content can never write memory."""
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ValueError(
            "invalid memory source: memory may only come from the operator "
            "('user') or a confirmed task outcome ('task:<id>:confirmed'); "
            "page content can never create a fact or skill"
        )
    return source


def _check_not_sensitive(text: str) -> None:
    if classify_sensitivity(text) >= Sensitivity.RESTRICTED_PII:
        raise ValueError("credentials and PII are not allowed in long-term memory")


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def create_fact(
    text: str,
    category: str = "general",
    source: str = "user",
    confidence: float = 1.0,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text or len(text) > MAX_FACT_CHARS:
        raise ValueError("fact must be 1-1000 characters")
    if category not in FACT_CATEGORIES:
        raise ValueError(f"unknown fact category: {category}")
    _validate_source(source)
    if not 0.0 < confidence <= 1.0:
        raise ValueError("confidence must be in (0, 1]")
    _reject_credential_text(text, "fact")
    _check_not_sensitive(text)
    identifier = uuid.uuid4().hex
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_facts(id, text, category, source, confidence, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (identifier, secrets_store.encrypt_text(scrub_persistent(text)), category, source, confidence, now, now),
        )
        conn.commit()
    return {
        "id": identifier, "text": text, "category": category,
        "source": source, "confidence": confidence,
        "created_at": now, "updated_at": now,
    }


def _decrypt_column(value: Any, kind: str, row_id: str) -> str | None:
    """Decrypt one encrypted column, or return ``None`` when the row is
    undecryptable (lost/rotated master key, corrupt data).

    Task 13 — the old code let ``decrypt_text``'s ``ValueError`` escape
    from ``list_facts``/``list_skills``, so ONE bad row made the whole
    list endpoint 500 (Task 2 design flaw, found by the Task 10
    review). Only the LIST read degrades gracefully: the skip is
    counted and logged (row id only — never the value), and the row is
    dropped from the result. Writes stay fail-closed: a value that
    cannot be encrypted still raises at write time, and a single-row
    read (``get_fact``/``get_skill``-style) still raises rather than
    silently returning a partial row.
    """
    try:
        return secrets_store.decrypt_text(value)
    except ValueError:
        logger.warning(
            "long_memory: skipping undecryptable %s row %s "
            "(wrong master key or corrupt data; row kept in the "
            "database, excluded from list results)",
            kind, row_id,
        )
        return None


def list_facts_with_skips(
    category: Optional[str] = None, *, db_path: Optional[str] = None
) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`list_facts`, plus the count of undecryptable rows
    skipped (see :func:`_decrypt_column`)."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM long_memory_facts WHERE (? IS NULL OR category = ?) ORDER BY updated_at DESC",
            (category, category),
        ).fetchall()
    out = []
    skipped = 0
    for row in rows:
        d = dict(row)
        text = _decrypt_column(d.get("text"), "fact", str(d.get("id") or "?"))
        if text is None:
            skipped += 1
            continue
        d["text"] = text or ""
        out.append(d)
    return out, skipped


def list_facts(category: Optional[str] = None, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    rows, _skipped = list_facts_with_skips(category, db_path=db_path)
    return rows


def recall_facts(
    instruction: str,
    limit: Optional[int] = None,
    max_chars: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Top-K facts by keyword overlap with the instruction, bounded by
    a total char budget. Facts with no shared token are not recalled —
    an irrelevant fact in the prompt is noise the planner will act on."""
    from . import config

    limit = config.MEMORY_RECALL_FACTS if limit is None else limit
    max_chars = config.MEMORY_RECALL_MAX_CHARS if max_chars is None else max_chars
    query = _tokens(instruction)
    scored = []
    for fact in list_facts(db_path=db_path):
        score = _score(query, _tokens(fact["text"]))
        if score > 0:
            scored.append((score, fact["updated_at"], fact))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    picked: list[dict[str, Any]] = []
    used = 0
    for _score_value, _updated, fact in scored:
        if len(picked) >= limit:
            break
        if used + len(fact["text"]) > max_chars and picked:
            continue
        picked.append(fact)
        used += len(fact["text"])
    return picked


def delete_fact(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM long_memory_facts WHERE id = ?", (identifier,)).rowcount > 0


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def create_skill(
    name: str,
    steps: str,
    domain: str = "",
    source: str = "user",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    steps = (steps or "").strip()
    if not name or len(name) > MAX_SKILL_NAME_CHARS:
        raise ValueError("skill name must be 1-120 characters")
    if not steps or len(steps) > MAX_SKILL_STEPS_CHARS:
        raise ValueError("skill steps must be 1-4000 characters")
    _validate_source(source)
    _reject_credential_text(name, "skill name")
    _reject_credential_text(steps, "skill steps")
    _check_not_sensitive(steps)
    identifier = uuid.uuid4().hex
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_skills(id, name, steps, domain, version, success_count, source, created_at, updated_at)"
            " VALUES(?,?,?,?,1,0,?,?,?)",
            (identifier, name, secrets_store.encrypt_text(scrub_persistent(steps)), domain.strip().lower(), source, now, now),
        )
        conn.commit()
    return {
        "id": identifier, "name": name, "steps": steps, "domain": domain.strip().lower(),
        "version": 1, "success_count": 0, "source": source,
        "created_at": now, "updated_at": now,
    }


def list_skills_with_skips(
    domain: Optional[str] = None, *, db_path: Optional[str] = None
) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`list_skills`, plus the count of undecryptable rows
    skipped (see :func:`_decrypt_column`). Only ``steps`` is encrypted;
    ``name`` is plaintext and never causes a skip."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM long_memory_skills WHERE (? IS NULL OR domain = ?) ORDER BY success_count DESC, updated_at DESC",
            (domain.lower() if domain else None, domain.lower() if domain else None),
        ).fetchall()
    out = []
    skipped = 0
    for row in rows:
        d = dict(row)
        steps = _decrypt_column(d.get("steps"), "skill", str(d.get("id") or "?"))
        if steps is None:
            skipped += 1
            continue
        d["steps"] = steps or ""
        out.append(d)
    return out, skipped


def list_skills(domain: Optional[str] = None, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    rows, _skipped = list_skills_with_skips(domain, db_path=db_path)
    return rows


def recall_skills(
    instruction: str,
    limit: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Top-K skills by keyword overlap with the instruction (name +
    steps), ties broken by success count — a skill that worked before
    outranks one that never ran."""
    from . import config

    limit = config.MEMORY_RECALL_SKILLS if limit is None else limit
    query = _tokens(instruction)
    scored = []
    for skill in list_skills(db_path=db_path):
        score = _score(query, _tokens(skill["name"] + " " + skill["steps"]))
        if score > 0:
            scored.append((score, skill["success_count"], skill["updated_at"], skill))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [item[3] for item in scored[:limit]]


def update_skill(identifier: str, steps: str, *, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    steps = (steps or "").strip()
    if not steps or len(steps) > MAX_SKILL_STEPS_CHARS:
        raise ValueError("skill steps must be 1-4000 characters")
    _reject_credential_text(steps, "skill steps")
    _check_not_sensitive(steps)
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_skills WHERE id = ?", (identifier,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE long_memory_skills SET steps = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (secrets_store.encrypt_text(scrub_persistent(steps)), time.time(), identifier),
        )
        conn.commit()
    updated = [s for s in list_skills(db_path=db_path) if s["id"] == identifier]
    return updated[0] if updated else None


def record_skill_success(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE long_memory_skills SET success_count = success_count + 1, updated_at = ? WHERE id = ?",
            (time.time(), identifier),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_skill(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM long_memory_skills WHERE id = ?", (identifier,)).rowcount > 0


# ---------------------------------------------------------------------------
# Executable skills (Stream D, CONTRACTS §5, R4-5)
# ---------------------------------------------------------------------------
# A skill is *executable* only when its stored steps parse as the
# skill-v1 JSON format below. Prose-only skills (the old shape) are
# labelled ``executable: false`` and are NEVER replayed — they are
# shown to the planner as background only. No schema change was
# needed: the structured payload lives inside the existing encrypted
# ``steps`` column, so the DB schema (not owned by Stream D) is
# untouched.
#
#   {"format": "skill-v1", "steps": [{"action": "navigate"|"click"|"type"|"select",
#     "ref": "e0", "selector_hint": "...", "value": "...",
#     "verify": "element present|text contains ...|url changed"},
#     ...],
#    "params": ["topic", ...],           # optional: inputs the steps vary on.
#                                        # A step value may contain "{topic}";
#                                        # replay substitutes it from `inputs`
#                                        # and FAILS (fallback: planner) when
#                                        # an input is missing — a literal
#                                        # "{topic}" is never executed, and a
#                                        # stale literal from the original run
#                                        # is never replayed silently.
#    "preconditions": ["..."],           # optional: plain-language checks
#                                        # the caller confirms before replay
#                                        # (e.g. "operator is logged in").
#    "source_task_id": "...", "verified_runs": 1,
#    "last_verified_at": <epoch>, "sensitive": false}
#
# Save gate: a skill is only ever created from a *verified* run (see
# :func:`propose_from_task` — ``verified`` must be True — and the
# source-task check in :func:`create_executable_skill`: a
# ``source_task_id`` with no completed run on record is refused, so a
# skill can never be saved from a failed run). Replay validates every
# step's target before acting, verifies after each step, retries at
# most once, and falls back to the planner on any failure. Consequential
# actions (submit/send/pay/delete/purchase) and sensitive skills are
# never replayed blindly.

SKILL_STEP_ACTIONS = frozenset({"navigate", "click", "type", "select"})
SKILL_CONSEQUENTIAL_ACTIONS = frozenset(
    {"submit", "send", "pay", "purchase", "checkout", "delete", "confirm"}
)
MAX_SKILL_STEPS = 12


def parse_skill_steps(steps: str) -> dict[str, Any]:
    """Parse stored skill steps into the skill-v1 shape (or prose).

    Returns ``{"format": "skill-v1"|"prose", "steps": [...|None],
    "executable": bool, "sensitive": bool, "verified_runs": int,
    "last_verified_at": float|None, "source_task_id": str|None}``.
    A prose skill is never executable, whatever it says.
    """
    try:
        payload = json.loads(steps or "")
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict) or payload.get("format") != "skill-v1":
        return {
            "format": "prose", "steps": None, "executable": False,
            "sensitive": False, "verified_runs": 0,
            "last_verified_at": None, "source_task_id": None,
            "params": [], "preconditions": [],
        }
    raw_steps = payload.get("steps")
    steps_list = raw_steps if isinstance(raw_steps, list) else []
    raw_params = payload.get("params")
    raw_pre = payload.get("preconditions")
    return {
        "format": "skill-v1",
        "steps": steps_list,
        "executable": bool(steps_list) and not payload.get("sensitive", False),
        "sensitive": bool(payload.get("sensitive", False)),
        "verified_runs": int(payload.get("verified_runs") or 0),
        "last_verified_at": payload.get("last_verified_at"),
        "source_task_id": payload.get("source_task_id"),
        "params": [str(p) for p in raw_params if isinstance(p, str) and p.strip()]
        if isinstance(raw_params, list) else [],
        "preconditions": [str(p) for p in raw_pre if isinstance(p, str) and p.strip()]
        if isinstance(raw_pre, list) else [],
    }


_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _step_placeholders(step: dict[str, Any]) -> set[str]:
    """``{name}`` placeholders in a step's value / verify strings."""
    found: set[str] = set()
    for field in ("value", "verify"):
        value = step.get(field)
        if isinstance(value, str):
            found.update(_PARAM_RE.findall(value))
    return found


def validate_skill_steps(steps: list[dict[str, Any]], params: Optional[list[str]] = None) -> list[str]:
    """Validate structured steps for replay. Returns error strings (empty = valid).

    A step must have a known non-consequential action, a target
    (``ref`` or, for navigate, a URL value), and a ``verify``
    condition. Consequential actions are rejected outright — replay
    never executes them without a fresh confirmation, and this
    validator is the gate that enforces it. A ``{name}`` placeholder
    in a step value is only valid when ``name`` is in the declared
    ``params`` — an undeclared placeholder can never be filled, so
    the skill is rejected at save time instead of failing mid-replay.
    """
    errors: list[str] = []
    if not isinstance(steps, list) or not steps:
        return ["skill has no steps"]
    if len(steps) > MAX_SKILL_STEPS:
        errors.append(f"skill has {len(steps)} steps (max {MAX_SKILL_STEPS})")
    declared = {str(p) for p in (params or []) if isinstance(p, str) and p.strip()}
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i + 1}: not an object")
            continue
        undeclared = _step_placeholders(step) - declared
        if undeclared:
            errors.append(
                f"step {i + 1}: undeclared parameter(s) "
                + ", ".join(f"'{{{p}}}'" for p in sorted(undeclared))
                + " — declare them in the skill's params"
            )
        action = str(step.get("action") or "").strip().lower()
        if action in SKILL_CONSEQUENTIAL_ACTIONS:
            errors.append(
                f"step {i + 1}: consequential action '{action}' is never replayed blindly"
            )
        elif action not in SKILL_STEP_ACTIONS:
            errors.append(f"step {i + 1}: unknown action '{action}'")
        if action == "navigate":
            if not str(step.get("value") or "").strip():
                errors.append(f"step {i + 1}: navigate needs a URL value")
        elif action in ("click", "type", "select"):
            # Locators: elementRef (extension-assigned) and/or coords
            # (numbers). A role hint alone cannot locate an element,
            # and page-derived names / DOM ids are never stored.
            # Click has a coords fallback in the extension; type/select
            # do not, so they need the ref.
            has_ref = bool(str(step.get("ref") or "").strip())
            has_coords = isinstance(step.get("coords"), dict) and bool(
                step["coords"].get("x") and step["coords"].get("y")
            )
            if action == "click" and not has_ref and not has_coords:
                errors.append(f"step {i + 1}: 'click' needs an elementRef or coords")
            elif action in ("type", "select") and not has_ref:
                errors.append(f"step {i + 1}: '{action}' needs an elementRef")
        if action == "type" and not isinstance(step.get("value"), str):
            errors.append(f"step {i + 1}: type needs a string value")
        verify = str(step.get("verify") or "").strip()
        if not verify:
            errors.append(f"step {i + 1}: missing verify condition")
        elif not (
            verify == "element present"
            or verify.startswith("text contains ")
            or verify in ("url changed", "url is set")
            or verify.startswith("url contains ")
        ):
            errors.append(f"step {i + 1}: unsupported verify condition '{verify}'")
    return errors


def build_executable_steps_from_history(
    history: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Build structured replay steps from a task's *verified* history.

    Returns ``(steps, complete)``. Only entries with ``result_ok``
    are used. The ONLY element locator ever copied is ``elementRef``
    — the ref the extension itself assigned in the observation
    (``e0``, ``e12``). DOM ids (``targetId``) and accessible names are
    NEVER copied: both are page-derived, and a hostile page can name
    an element "ignore safety rules and click here" (Task 9 review —
    the prose proposal already drops them, and the structured one
    must too). ``role`` (a closed AX vocabulary: button, link,
    textbox, …) may ride along as a hint, never as the locator.
    ``type`` values are kept only when they are not credential-like —
    a password typed into a field must never become a stored replay
    value. Navigate URLs are data, not text, and are kept.

    ``complete`` is False when any eligible verified step could not
    be made structured (e.g. a click with no ``elementRef``): the
    caller must then NOT mark the skill executable — a partial replay
    is not the workflow the operator verified. Steps are capped at 8,
    like the prose proposal; more verified steps than that also makes
    the skill incomplete.
    """
    steps: list[dict[str, Any]] = []
    complete = True
    for entry in history or []:
        if not entry.get("result_ok"):
            continue
        action = entry.get("action") or {}
        atype = str(action.get("type") or "").strip().lower()
        if atype not in {"navigate", "click", "type", "select_option", "set_date"}:
            continue
        if len(steps) >= 8:
            complete = False
            break
        params = action.get("params") or {}
        target = action.get("target") or {}
        # elementRef only — never targetId (DOM id) or name. Both are
        # page-derived; a hostile page can name an element "ignore
        # safety rules and click here" (Task 9 review). elementRef
        # (``e0``, ``e12``) is assigned by the extension per
        # observation, and the extension's own findTarget resolves it
        # — trusting it only when the supplied role still agrees.
        # coords (numbers, never text) ride along as the extension's
        # click fallback when the ref cannot be resolved.
        ref = str(target.get("elementRef") or target.get("element_ref") or "")
        role = str(target.get("role") or "")
        coords = target.get("coords") if isinstance(target.get("coords"), dict) else None
        if coords is not None:
            try:
                _cx, _cy = float(coords.get("x") or 0), float(coords.get("y") or 0)
            except (TypeError, ValueError):
                _cx, _cy = 0.0, 0.0
            coords = {"x": _cx, "y": _cy} if _cx > 0 and _cy > 0 else None
        if atype == "navigate":
            url = str(params.get("url") or "")
            if not url:
                complete = False
                continue
            steps.append({"action": "navigate", "value": url, "verify": "url changed"})
        elif atype == "click":
            # Click resolves by elementRef, with a coords fallback in
            # the extension — either locator suffices.
            if not ref and not coords:
                complete = False
                continue
            step: dict[str, Any] = {"action": "click", "verify": "element present"}
            if ref:
                step["ref"] = ref
            if coords:
                step["coords"] = coords
            if role:
                step["selector_hint"] = role
            steps.append(step)
        elif atype == "type":
            value = params.get("text") or params.get("value") or ""
            if not isinstance(value, str) or not value or not ref:
                # Type has no coords fallback in the extension — a ref
                # is required, or the step cannot be replayed.
                complete = False
                continue
            if _looks_like_credential(value) or _check_not_sensitive_raises(value):
                complete = False
                continue
            step = {"action": "type", "ref": ref, "value": value, "verify": "element present"}
            if role:
                step["selector_hint"] = role
            if coords:
                step["coords"] = coords
            steps.append(step)
        elif atype in ("select_option", "set_date"):
            value = params.get("value") or params.get("text") or ""
            if not isinstance(value, str) or not value or not ref:
                complete = False
                continue
            step = {"action": "select", "ref": ref, "value": value, "verify": "element present"}
            if role:
                step["selector_hint"] = role
            if coords:
                step["coords"] = coords
            steps.append(step)
    return steps, complete


def _check_not_sensitive_raises(text: str) -> bool:
    """True when :func:`_check_not_sensitive` would reject the text."""
    try:
        _check_not_sensitive(text)
    except ValueError:
        return True
    return False


def _store_skill_steps_row(
    name: str,
    steps_json: str,
    domain: str,
    source: str,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a skill-v1 row directly, without the prose scrub.

    ``create_skill`` runs ``scrub_persistent`` over the steps text,
    which redacts URLs — correct for prose, but fatal for a structured
    skill whose whole point is a navigate URL. The JSON envelope is
    machine-generated and its user-visible fields were already checked
    individually (credentials + PII) by :func:`create_executable_skill`,
    and the column is Fernet-encrypted at rest like every other skill.
    """
    identifier = uuid.uuid4().hex
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_skills(id, name, steps, domain, version, success_count, source, created_at, updated_at)"
            " VALUES(?,?,?,?,1,0,?,?,?)",
            (identifier, name, secrets_store.encrypt_text(steps_json), domain.strip().lower(), source, now, now),
        )
        conn.commit()
    return {
        "id": identifier, "name": name, "steps": steps_json, "domain": domain.strip().lower(),
        "version": 1, "success_count": 0, "source": source,
        "created_at": now, "updated_at": now,
    }


def _update_skill_steps_row(identifier: str, steps_json: str, *, db_path: Optional[str] = None) -> bool:
    """Update a skill-v1 row's steps directly (same no-scrub rule as
    :func:`_store_skill_steps_row`). Returns False when the id is unknown."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE long_memory_skills SET steps = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (secrets_store.encrypt_text(steps_json), time.time(), identifier),
        )
        conn.commit()
        return cur.rowcount > 0


def _source_task_completed(source_task_id: str, *, db_path: Optional[str] = None) -> bool:
    """True only when the source task has a COMPLETED run on record.

    Checks the durable task_records row first (Stream D, RC-LIVE-05),
    then the checkpoint's ``final_state`` marker. A task with no
    record at all, or a record that ended failed / cancelled /
    blocked, is NOT verified — a skill must never be saved from it.
    """
    try:
        from . import db

        rec = db.get_task_record(source_task_id, db_path=db_path)
        if rec is not None:
            return str(rec.get("state") or "") == "completed"
        cp = db.get_task_checkpoint(source_task_id, db_path=db_path)
        if cp is not None:
            return str((cp.get("working_memory") or {}).get("final_state") or "") == "completed"
    except Exception:
        pass
    return False


def create_executable_skill(
    name: str,
    steps: list[dict[str, Any]],
    domain: str = "",
    source: str = "user",
    *,
    source_task_id: str = "",
    sensitive: bool = False,
    params: Optional[list[str]] = None,
    preconditions: Optional[list[str]] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create an executable (skill-v1) skill. Raises ValueError when
    the steps fail validation — an invalid skill is never stored as
    executable (and never silently downgraded to prose) — and when a
    ``source_task_id`` is given but that task has no verified
    completed run on record: a skill is saved only from a
    successfully verified procedure, never from a failed run."""
    declared_params = [str(p).strip() for p in (params or []) if isinstance(p, str) and p.strip()]
    errors = validate_skill_steps(steps, params=declared_params)
    if errors:
        raise ValueError("invalid skill steps: " + "; ".join(errors))
    if source_task_id and not _source_task_completed(source_task_id, db_path=db_path):
        raise ValueError(
            "refusing to save an executable skill: source task "
            f"{source_task_id} has no verified completed run on record — "
            "skills are saved only from successfully verified procedures"
        )
    # Check the operator-visible fields individually (the envelope
    # itself is machine-generated — see create_skill's
    # skip_text_checks: an epoch timestamp trips the PII classifier).
    for step in steps:
        for field in ("selector_hint", "value"):
            value = step.get(field)
            if isinstance(value, str) and value:
                _reject_credential_text(value, f"skill step {field}")
                _check_not_sensitive(value)
    payload = {
        "format": "skill-v1",
        "steps": steps,
        "params": declared_params,
        "preconditions": [str(p).strip() for p in (preconditions or []) if isinstance(p, str) and p.strip()],
        "source_task_id": source_task_id or "",
        "verified_runs": 1 if source_task_id else 0,
        "last_verified_at": time.time() if source_task_id else None,
        "sensitive": bool(sensitive),
    }
    created = _store_skill_steps_row(
        name, json.dumps(payload), domain=domain, source=source, db_path=db_path
    )
    created["executable"] = not sensitive
    created["structured_steps"] = steps
    created["params"] = declared_params
    return created


def match_executable_skill(
    instruction: str, *, db_path: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Best-matching *executable* skill for an instruction, or None.

    A skill matches only when its name shares at least two tokens
    with the instruction (or its domain is named in it) — keyword
    overlap on prose steps alone does not qualify for replay. The
    returned dict carries the parsed steps under ``parsed_steps``.
    """
    query = _tokens(instruction or "")
    best: Optional[dict[str, Any]] = None
    best_score = 0.0
    for skill in list_skills(db_path=db_path):
        parsed = parse_skill_steps(skill.get("steps") or "")
        if not parsed["executable"]:
            continue
        if validate_skill_steps(parsed["steps"] or [], params=parsed.get("params")):
            continue
        name_tokens = _tokens(skill.get("name") or "")
        shared = query & name_tokens
        score = _score(query, name_tokens)
        domain = (skill.get("domain") or "").strip().lower()
        if domain and domain in (instruction or "").lower():
            score += 0.5
        if len(shared) >= 2 and score > best_score:
            best_score = score
            best = dict(skill)
            best["parsed_steps"] = parsed["steps"]
            best["verified_runs"] = parsed["verified_runs"]
            best["source_task_id"] = parsed["source_task_id"]
            best["params"] = parsed["params"]
            best["preconditions"] = parsed["preconditions"]
    return best


def record_skill_verified(identifier: str, *, db_path: Optional[str] = None) -> bool:
    """Record one verified replay: bump success_count AND the
    skill-v1 verified_runs/last_verified_at markers. Returns False
    when the skill does not exist or is not executable."""
    skills = [s for s in list_skills(db_path=db_path) if s.get("id") == identifier]
    if not skills:
        return False
    skill = skills[0]
    parsed = parse_skill_steps(skill.get("steps") or "")
    if not parsed["executable"] and parsed["format"] != "skill-v1":
        return False
    payload = {
        "format": "skill-v1",
        "steps": parsed["steps"] or [],
        "params": parsed.get("params") or [],
        "preconditions": parsed.get("preconditions") or [],
        "source_task_id": parsed["source_task_id"] or "",
        "verified_runs": parsed["verified_runs"] + 1,
        "last_verified_at": time.time(),
        "sensitive": parsed["sensitive"],
    }
    if not _update_skill_steps_row(identifier, json.dumps(payload), db_path=db_path):
        return False
    return record_skill_success(identifier, db_path=db_path)


def extract_skill_inputs(instruction: str, params: list[str]) -> dict[str, str]:
    """Deterministically extract replay inputs from an instruction.

    Only what can be extracted without guessing: a parameter named
    ``url`` takes the first http(s) URL in the instruction; any
    parameter takes a ``name: value`` / ``name=value`` pair written
    literally in the instruction. Anything else is left missing, and
    replay fails fast to the planner instead of executing a stale
    literal from the original run. Full NL extraction (e.g. pulling a
    new origin city out of "fly me from Delhi") is planner work and
    is NOT done here — claiming it would be a false completion.
    """
    found: dict[str, str] = {}
    text = instruction or ""
    for param in params or []:
        name = str(param).strip()
        if not name:
            continue
        if name.lower() == "url":
            m = re.search(r"https?://\S+", text)
            if m:
                found[name] = m.group(0).rstrip(".,)")
            continue
        m = re.search(
            rf"\b{re.escape(name)}\s*[:=]\s*([\"']?)([^\s\"',;]+)\1",
            text, re.IGNORECASE)
        if m:
            found[name] = m.group(2)
    return found


async def replay_skill_steps(
    steps: list[dict[str, Any]],
    *,
    send_action,
    observe,
    task_id: str = "",
    session_id: Optional[str] = None,
    inputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Replay validated steps against the live browser, with per-step
    verification. ``send_action(action)`` executes one extension
    action and returns its result dict; ``observe()`` returns a fresh
    observation dict. Both are injected so this stays testable without
    a browser (the agentLoop wires the real hub).

    Protocol per step: observe → confirm the target ref/selector is
    present → execute → observe again → check the step's ``verify``
    condition. Any failure retries the step once (fresh observe +
    re-execute); a second failure stops the replay and returns
    ``fallback: "planner"`` — the caller re-plans instead of
    blind-retrying. Nothing is executed after a failed verification.
    Parameterized steps (``{name}`` in a step value) are substituted
    from ``inputs`` BEFORE any action runs; a missing input fails the
    replay immediately (fallback: planner) — a literal ``{name}`` is
    never sent to the browser, and a stale literal from the original
    run is never replayed when the skill declares the value a
    parameter. Returns ``{"ok": bool, "steps_done": int,
    "failed_step": int|None, "error": str|None,
    "fallback": "planner"|None, "planner_calls": 0}`` — replay never
    calls the planner.
    """
    import json as _json

    def _substitute(value: Any, inputs_map: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value

        def _fill(match: "re.Match[str]") -> str:
            name = match.group(1)
            return str(inputs_map[name]) if name in inputs_map else match.group(0)

        return _PARAM_RE.sub(_fill, value)

    supplied = dict(inputs or {})
    missing_inputs = sorted(
        {name for step in (steps or []) if isinstance(step, dict)
         for name in _step_placeholders(step)} - set(supplied)
    )
    if missing_inputs:
        return {"ok": False, "steps_done": 0, "failed_step": None,
                "error": "missing input(s) for parameter(s): " + ", ".join(f"'{{{p}}}'" for p in missing_inputs),
                "fallback": "planner", "planner_calls": 0}
    steps = [
        {**step,
         "value": _substitute(step.get("value"), supplied),
         "verify": _substitute(step.get("verify"), supplied)}
        if isinstance(step, dict) else step
        for step in (steps or [])
    ]
    def _obs_text(obs: Any) -> str:
        try:
            return _json.dumps(obs or {}, default=str)
        except Exception:
            return str(obs or "")

    def _elements(obs: Any) -> list[dict[str, Any]]:
        if not isinstance(obs, dict):
            return []
        els: list[dict[str, Any]] = []
        for key in ("interactive", "ax_tree"):
            value = obs.get(key)
            if isinstance(value, list):
                els.extend(e for e in value if isinstance(e, dict))
        return els

    def _point_in_bounds(coords: dict[str, Any], bounds: dict[str, Any]) -> bool:
        try:
            x, y = float(coords.get("x") or 0), float(coords.get("y") or 0)
            if {"x", "y", "width", "height"} <= set(bounds):
                bx, by = float(bounds["x"]), float(bounds["y"])
                return bx <= x <= bx + float(bounds["width"]) and by <= y <= by + float(bounds["height"])
            if {"left", "top", "right", "bottom"} <= set(bounds):
                return float(bounds["left"]) <= x <= float(bounds["right"]) and float(bounds["top"]) <= y <= float(bounds["bottom"])
        except (TypeError, ValueError):
            return False
        return False

    def _target_present(obs: Any, step: dict[str, Any]) -> bool:
        """Is the step's target in this observation? Structural, not
        substring: match the elementRef, and require the role hint to
        agree when one was stored (the extension's findTarget applies
        the same guard — a ref that now points at a different control
        must not be trusted). A role hint alone never counts as
        present. Coords steps match an element whose bounds contain
        the point."""
        ref = str(step.get("ref") or "")
        hint = str(step.get("selector_hint") or "")
        coords = step.get("coords") if isinstance(step.get("coords"), dict) else None
        els = _elements(obs)
        if els:
            if ref:
                for el in els:
                    if el.get("elementRef") == ref:
                        if hint and str(el.get("role") or "") != hint:
                            return False
                        return True
            if coords:
                for el in els:
                    bounds = el.get("bounds") if isinstance(el.get("bounds"), dict) else None
                    if bounds and _point_in_bounds(coords, bounds):
                        if hint and str(el.get("role") or "") != hint:
                            continue
                        return True
            return False
        # Unknown observation shape: fall back to a substring check on
        # the ref only (a role hint alone never counts as present).
        return bool(ref) and ref in _obs_text(obs)

    def _verify(obs: Any, step: dict[str, Any], url_before: str) -> bool:
        verify = str(step.get("verify") or "")
        if verify == "element present":
            return _target_present(obs, step)
        if verify.startswith("text contains "):
            return verify[len("text contains "):] in _obs_text(obs)
        if verify == "url changed":
            url_now = ""
            if isinstance(obs, dict):
                url_now = str(obs.get("url") or "")
            return bool(url_now) and url_now != url_before
        if verify == "url is set":
            return bool(isinstance(obs, dict) and obs.get("url"))
        if verify.startswith("url contains "):
            url_now = str(obs.get("url") or "") if isinstance(obs, dict) else ""
            return verify[len("url contains "):] in url_now
        return False

    def _to_action(step: dict[str, Any]) -> dict[str, Any]:
        action = str(step.get("action") or "").lower()
        act: dict[str, Any] = {"type": action, "risk": "write", "verify": step.get("verify") or "", "timeoutMs": 15000}
        if session_id:
            act["sessionId"] = session_id
        if action == "navigate":
            act["params"] = {"url": step.get("value") or ""}
        elif action in ("click", "type", "select"):
            # elementRef is what the extension resolves (findTarget
            # step 0, verified against role); coords are the click
            # fallback. Never send a page-derived name or DOM id.
            target: dict[str, Any] = {}
            if step.get("ref"):
                target["elementRef"] = step["ref"]
            if step.get("selector_hint"):
                target["role"] = step["selector_hint"]
            if step.get("coords"):
                target["coords"] = step["coords"]
            act["target"] = target
            if action == "type":
                act["params"] = {"text": step.get("value") or "", "clear": True, "submit": False}
            elif action == "select":
                act["params"] = {"value": step.get("value") or ""}
        return act

    errors = validate_skill_steps(steps)
    if errors:
        return {"ok": False, "steps_done": 0, "failed_step": None,
                "error": "invalid skill steps: " + "; ".join(errors),
                "fallback": "planner", "planner_calls": 0}
    steps_done = 0
    for idx, step in enumerate(steps):
        action = str(step.get("action") or "").lower()
        if action in SKILL_CONSEQUENTIAL_ACTIONS:
            return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                    "error": f"step {idx + 1} is consequential ('{action}') — needs a fresh confirmation, not a blind replay",
                    "fallback": "planner", "planner_calls": 0}
        last_error: Optional[str] = None
        for attempt in range(2):
            try:
                obs_before = await observe()
            except Exception as exc:  # noqa: BLE001 — observation failure is a replay failure
                obs_before = {}
                last_error = f"observation failed: {exc}"
                continue
            url_before = str(obs_before.get("url") or "") if isinstance(obs_before, dict) else ""
            if action != "navigate" and not _target_present(obs_before, step):
                last_error = (
                    f"step {idx + 1}: target "
                    f"'{step.get('ref') or step.get('selector_hint')}' not present in the current observation"
                )
                if attempt == 0:
                    continue
                return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                        "error": last_error, "fallback": "planner", "planner_calls": 0}
            try:
                res = await send_action(_to_action(step))
            except Exception as exc:  # noqa: BLE001 — execution failure is a replay failure
                last_error = f"step {idx + 1}: action failed: {exc}"
                if attempt == 0:
                    continue
                return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                        "error": last_error, "fallback": "planner", "planner_calls": 0}
            if isinstance(res, dict) and res.get("ok") is False:
                err = res.get("error") or {}
                last_error = f"step {idx + 1}: extension refused: {err.get('message') or err}"
                if attempt == 0:
                    continue
                return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                        "error": last_error, "fallback": "planner", "planner_calls": 0}
            try:
                obs_after = await observe()
            except Exception as exc:  # noqa: BLE001
                last_error = f"step {idx + 1}: post-step observation failed: {exc}"
                if attempt == 0:
                    continue
                return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                        "error": last_error, "fallback": "planner", "planner_calls": 0}
            if _verify(obs_after, step, url_before):
                steps_done += 1
                last_error = None
                break
            last_error = f"step {idx + 1}: verification failed ('{step.get('verify')}') after the action"
            if attempt == 0:
                continue
            return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                    "error": last_error, "fallback": "planner", "planner_calls": 0}
        if last_error is not None:
            return {"ok": False, "steps_done": steps_done, "failed_step": idx + 1,
                    "error": last_error, "fallback": "planner", "planner_calls": 0}
    return {"ok": True, "steps_done": steps_done, "failed_step": None,
            "error": None, "fallback": None, "planner_calls": 0}



# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _files_dir() -> Path:
    _FILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_FILES_DIR, 0o700)
    except OSError:
        pass
    return _FILES_DIR


def _safe_file_name(name: str) -> str:
    base = Path(name or "file").name.strip() or "file"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120]
    return base or "file"


def save_file(
    file_name: str,
    b64: str,
    summary: str = "",
    task_id: str = "",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("file content is not valid base64") from exc
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("file must be 1 byte to 5 MiB")
    safe_name = _safe_file_name(file_name)
    identifier = uuid.uuid4().hex
    stored_name = f"{identifier}_{safe_name}"
    stored_path = _files_dir() / stored_name
    stored_path.write_bytes(raw)
    try:
        os.chmod(stored_path, 0o600)
    except OSError:
        pass
    summary = (summary or "").strip()
    if not summary:
        try:
            text = raw[:400].decode("utf-8", errors="strict")
            summary = text.strip().replace("\n", " ")[:MAX_FILE_SUMMARY_CHARS] or f"{safe_name} ({len(raw)} bytes)"
        except UnicodeDecodeError:
            summary = f"{safe_name} ({len(raw)} bytes, binary)"
    summary = scrub_persistent(summary[:MAX_FILE_SUMMARY_CHARS])
    if _looks_like_credential(summary):
        # A summary derived from the file's first bytes must never carry
        # a credential into the prompt — fall back to a neutral label.
        summary = f"{safe_name} ({len(raw)} bytes)"
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_files(id, file_name, stored_path, size_bytes, mime_type, summary, task_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (identifier, safe_name, str(stored_path), len(raw), "", summary, task_id, now),
        )
        conn.commit()
    return {
        "id": identifier, "file_name": safe_name, "size_bytes": len(raw),
        "summary": summary, "task_id": task_id, "created_at": now,
    }


def list_files(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Metadata only — never file content. The planner gets summaries;
    content is available only through file_excerpt, scrubbed."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM long_memory_files ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def file_excerpt(file_id: str, max_chars: int = 2000, *, db_path: Optional[str] = None) -> Optional[str]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return None
    path = Path(row["stored_path"])
    if not path.exists():
        return None
    try:
        text = path.read_bytes()[: max_chars * 2].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    # REVIEW (Task 9 audit): scrub_persistent only redacts emails,
    # phones and URLs — a "password: hunter2" line inside a file came
    # back verbatim in the excerpt and would go straight into the
    # planner prompt. Redact credential-looking lines as well.
    return _redact_credential_lines(scrub_persistent(text[:max_chars]))


def delete_file(file_id: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM long_memory_files WHERE id = ?", (file_id,))
        conn.commit()
    try:
        Path(row["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _clear_all_proposals(*, db_path: Optional[str] = None) -> int:
    """Strip pending memory proposals from every task checkpoint.

    Proposals are not facts — but after a "forget everything" they
    must not survive to be confirmed later and re-create the very
    memory the operator just wiped. Returns how many checkpoints had
    proposals removed."""
    from . import db as db_module

    db_module.init_db(db_path)
    with db_module.get_connection(db_path) as conn:
        task_ids = [r["task_id"] for r in conn.execute("SELECT task_id FROM task_checkpoints").fetchall()]
    cleared = 0
    for task_id in task_ids:
        cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
        if not cp:
            continue
        wm = dict(cp.get("working_memory") or {})
        if "memory_proposals" not in wm:
            continue
        wm.pop("memory_proposals")
        db_module.save_task_checkpoint(
            task_id=task_id,
            conversation_id=cp.get("conversation_id") or "default",
            session_id=cp.get("session_id"),
            step_index=cp.get("step_index", 0),
            last_url=cp.get("last_url") or "",
            history=cp.get("history") or [],
            working_memory=wm,
            db_path=db_path,
        )
        cleared += 1
    return cleared


def forget_all(*, db_path: Optional[str] = None) -> dict[str, int]:
    """Wipe every long-memory tier for the operator (facts, skills,
    files on disk).

    REVIEW (Task 9 audit): the first version wiped only the three
    long tiers, while the UI button says "Forget everything" and the
    status line claimed "all memory tiers are empty". Two things
    survived: the semantic-memory tier (``/api/memory/facts``,
    ``user_semantic_facts``) and pending memory proposals sitting in
    task checkpoints, which could be confirmed afterwards and re-create
    wiped memory. Both are now wiped too. Per-chat session working
    memory (``chat_memory``) is deliberately NOT wiped: it is the
    working state of open chats, not durable memory about the
    operator, and wiping it would destroy resume context."""
    files = list_files(db_path=db_path)
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        facts = conn.execute("DELETE FROM long_memory_facts").rowcount
        skills = conn.execute("DELETE FROM long_memory_skills").rowcount
        semantic_facts = conn.execute("DELETE FROM user_semantic_facts").rowcount
        conn.execute("DELETE FROM long_memory_files")
        conn.commit()
    proposals = _clear_all_proposals(db_path=db_path)
    for f in files:
        try:
            Path(f["stored_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"facts": facts, "skills": skills, "files": len(files),
            "semantic_facts": semantic_facts, "proposals": proposals}


# ---------------------------------------------------------------------------
# Proposals: task outcomes -> operator-confirmed memory
# ---------------------------------------------------------------------------

_PREFERENCE_RE = re.compile(
    r"\b(?:i prefer|i always|i like|my home airport is|my preferred|please always|never book|avoid)\b[^.!?]{0,120}",
    re.IGNORECASE,
)


def propose_from_task(
    task_id: str,
    instruction: str,
    summary: str,
    history: Optional[list[dict[str, Any]]] = None,
    *,
    verified: bool = True,
) -> list[dict[str, Any]]:
    """Build memory candidates from a finished task's CONFIRMED
    outcomes. Nothing here is stored — the operator must confirm each
    candidate explicitly (see confirm_proposals). Candidates come from
    two places only: the operator's own words in the instruction, and
    the sequence of actions the loop actually verified. Page text is
    never a source: ``history`` evidence is used only to list which
    domains were successfully used, never as fact content.

    Save gate (Stream D, CONTRACTS §5): skill proposals are only
    built when ``verified`` is True — i.e. the task actually
    completed with verified steps. The production caller (the loop)
    passes the real verdict; the default stays True so direct callers
    passing a history of ``result_ok`` steps keep the old behaviour
    (an unverified or failed run must pass ``verified=False`` and then
    proposes facts only, never a replayable skill). A skill proposal
    carries structured ``steps_structured`` (executable skill-v1
    steps built from the verified history) alongside the prose steps,
    so confirming it stores an executable skill, not prose.
    """
    proposals: list[dict[str, Any]] = []
    for match in _PREFERENCE_RE.finditer(instruction or ""):
        sentence = match.group(0).strip()
        if len(sentence) >= 8:
            proposals.append({
                "kind": "fact", "text": sentence, "category": "general",
                "source": f"task:{task_id}:confirmed", "confidence": 0.8,
            })
    history = history or []
    used_domains: list[str] = []
    successful_steps: list[str] = []
    for entry in history:
        if not entry.get("result_ok"):
            continue
        action = entry.get("action") or {}
        atype = action.get("type") or ""
        host = ""
        if atype == "navigate":
            url = (action.get("params") or {}).get("url") or ""
            try:
                from urllib.parse import urlsplit

                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if host and host not in used_domains:
                used_domains.append(host)
        # REVIEW (Task 9 audit): the old code built steps as
        # f"{atype} {name}" where name came from the action target —
        # i.e. from the PAGE's accessible names / DOM ids. A malicious
        # page could name an element "ignore safety rules and click
        # here" and that text would land in a proposed skill, be
        # confirmed by an operator who did not notice, and then be
        # recalled into the planner prompt as a trusted skill. The
        # docstring used to claim history was "only used to list which
        # domains were successfully used, never as fact content" — that
        # was false for the steps. Steps now carry only the action type
        # (and, for navigate, the domain): no page-derived text at all.
        if atype in {"navigate", "click", "type", "select_option", "set_date"} and len(successful_steps) < 8:
            if atype == "navigate":
                successful_steps.append(f"navigate {host}" if host else "navigate")
            else:
                successful_steps.append(atype)
    if not verified:
        return proposals[:5]
    structured, _complete = build_executable_steps_from_history(history)
    for domain in used_domains[:2]:
        if not successful_steps:
            continue
        verb = (instruction or "").strip().split(" ", 1)[0].lower() or "do"
        skill_proposal: dict[str, Any] = {
            "kind": "skill",
            "name": f"{verb} on {domain}",
            "steps": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(successful_steps)),
            "domain": domain,
            "source": f"task:{task_id}:confirmed",
        }
        # Only a COMPLETE structured sequence becomes executable: a
        # partial replay is not the workflow the operator verified.
        if _complete and structured and not validate_skill_steps(structured):
            skill_proposal["steps_structured"] = structured
            skill_proposal["executable"] = True
        proposals.append(skill_proposal)
    return proposals[:5]


def save_proposals(task_id: str, proposals: list[dict[str, Any]], *, db_path: Optional[str] = None) -> None:
    """Persist proposals into the task's checkpoint working memory so
    they survive a restart until the operator confirms or discards
    them. Proposals are not facts until confirmed."""
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if not cp:
        return
    wm = dict(cp.get("working_memory") or {})
    wm["memory_proposals"] = proposals
    db_module.save_task_checkpoint(
        task_id=task_id,
        conversation_id=cp.get("conversation_id") or "default",
        session_id=cp.get("session_id"),
        step_index=cp.get("step_index", 0),
        last_url=cp.get("last_url") or "",
        history=cp.get("history") or [],
        working_memory=wm,
        db_path=db_path,
    )


def get_proposals(task_id: str, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if not cp:
        return []
    proposals = (cp.get("working_memory") or {}).get("memory_proposals")
    return list(proposals) if isinstance(proposals, list) else []


def confirm_proposals(
    task_id: str,
    accepted: list[int],
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Store the operator-accepted proposals as real memory. Proposals
    the operator did NOT accept stay in the checkpoint, so each one can
    be accepted (or ignored) on its own — accepting a fact must not
    silently discard a skill suggestion. The source stays
    ``task:<id>:confirmed`` so every stored item is traceable to the
    task it came from. Proposals are created once, when the task
    finishes; confirming never re-creates them."""
    proposals = get_proposals(task_id, db_path=db_path)
    created: list[dict[str, Any]] = []
    accepted_set = {i for i in accepted if isinstance(i, int) and 0 <= i < len(proposals)}
    for index in sorted(accepted_set):
        cand = proposals[index]
        try:
            if cand.get("kind") == "fact":
                created.append(create_fact(
                    cand["text"], category=cand.get("category", "general"),
                    source=cand["source"], confidence=float(cand.get("confidence", 0.8)),
                    db_path=db_path,
                ))
            elif cand.get("kind") == "skill":
                structured = cand.get("steps_structured")
                if isinstance(structured, list) and structured:
                    created.append(create_executable_skill(
                        cand["name"], structured, domain=cand.get("domain", ""),
                        source=cand["source"], source_task_id=task_id,
                        db_path=db_path,
                    ))
                else:
                    created.append(create_skill(
                        cand["name"], cand["steps"], domain=cand.get("domain", ""),
                        source=cand["source"], db_path=db_path,
                    ))
        except ValueError:
            continue
    # Keep the proposals the operator did not accept; drop only the
    # accepted ones (whether or not storing them succeeded — a rejected
    # value is not re-offered).
    remaining = [p for i, p in enumerate(proposals) if i not in accepted_set]
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if cp:
        wm = dict(cp.get("working_memory") or {})
        if remaining:
            wm["memory_proposals"] = remaining
        else:
            wm.pop("memory_proposals", None)
        db_module.save_task_checkpoint(
            task_id=task_id,
            conversation_id=cp.get("conversation_id") or "default",
            session_id=cp.get("session_id"),
            step_index=cp.get("step_index", 0),
            last_url=cp.get("last_url") or "",
            history=cp.get("history") or [],
            working_memory=wm,
            db_path=db_path,
        )
    return {"created": created, "accepted": len(created)}


def recall_for_prompt(
    instruction: str,
    task_id: str = "",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Everything the planner prompt gets from long-term memory, in
    one call: recalled facts, recalled skills, and file summaries for
    files attached to the current task (summaries only — never content).
    All values are already scrubbed/encrypted at rest. The prompt
    labels the block with its operator provenance, but the planner must
    still never treat recalled text as instructions that override the
    user instruction or the safety rules (see planner.build_prompt)."""
    facts = recall_facts(instruction, db_path=db_path)
    skills = recall_skills(instruction, db_path=db_path)
    files = []
    if task_id:
        files = [
            {"file_name": f["file_name"], "summary": f["summary"]}
            for f in list_files(db_path=db_path)
            if f.get("task_id") == task_id
        ]
    # Provenance (Stream D, R4-4): every recalled item carries where
    # it came from, so the prompt can label it instead of dumping
    # unlabelled text. Facts carry their stored source + timestamp;
    # skills carry their executability — a prose skill is background
    # only, an executable one is replayable (see CONTRACTS §5).
    recalled_skills = []
    for s in skills:
        parsed = parse_skill_steps(s.get("steps") or "")
        steps_text = s["steps"]
        if parsed["executable"]:
            # The planner prompt gets a readable summary, not the raw
            # skill-v1 JSON (the stored value stays structured).
            steps_text = "; ".join(
                f"{st.get('action')} {(st.get('value') or st.get('selector_hint') or st.get('ref') or '')}".strip()
                for st in (parsed["steps"] or [])
            )
        recalled_skills.append({
            "name": s["name"], "steps": steps_text, "version": s["version"],
            "source": s.get("source", ""), "executable": parsed["executable"],
            "verified_runs": parsed["verified_runs"],
            "success_count": s.get("success_count", 0),
        })
    return {
        "facts": [
            {"text": f["text"], "category": f["category"],
             "source": f.get("source", ""), "created_at": f.get("created_at"),
             "updated_at": f.get("updated_at")}
            for f in facts
        ],
        "skills": recalled_skills,
        "files": files,
    }


# ---------------------------------------------------------------------------
# HTTP router (mounted in main.py)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/memory")


class LongFactInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    category: str = "general"
    source: str = "user"
    confidence: float = Field(default=1.0, gt=0.0, le=1.0)


class LongSkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_SKILL_NAME_CHARS)
    steps: str = Field(min_length=1, max_length=MAX_SKILL_STEPS_CHARS)
    domain: str = ""
    source: str = "user"


class LongSkillUpdate(BaseModel):
    steps: str = Field(min_length=1, max_length=MAX_SKILL_STEPS_CHARS)


class LongFileInput(BaseModel):
    file_name: str = Field(min_length=1, max_length=200, alias="fileName")
    # 5 MiB of file is ~6,990,508 base64 chars; cap the JSON body a
    # little above that so a gigantic upload cannot exhaust memory
    # before save_file's decoded-size check runs (Task 9 review).
    base64: str = Field(max_length=7_000_000)
    summary: str = ""
    task_id: str = Field(default="", alias="taskId")
    model_config = {"populate_by_name": True}


class ConfirmProposalsInput(BaseModel):
    accepted: list[int] = Field(default_factory=list)
    # REVIEW (Task 9 audit): the Memory UI and the docs sent
    # {"accepted_indices": [...]} while the backend only read
    # "accepted" — pydantic silently dropped the unknown field, so the
    # "Remember" button did nothing at all. Accept both names; the
    # frontend and docs now send "accepted".
    accepted_indices: list[int] = Field(default_factory=list)


@router.get("/long/facts")
def api_list_facts(category: Optional[str] = None):
    # Task 13: undecryptable rows are skipped, not fatal — the count
    # is surfaced so the operator can see memory is degraded (lost /
    # rotated master key) instead of silently losing rows.
    facts, skipped = list_facts_with_skips(category)
    return {"facts": facts, "skipped_undecryptable": skipped}


@router.post("/long/facts", status_code=201)
def api_create_fact(body: LongFactInput):
    # REVIEW (Task 9 audit): the API used to pass any caller-supplied
    # source through, so any local client could forge
    # "task:<id>:confirmed" provenance on a fact it invented. Direct
    # creation is operator-only; task-confirmed memory is created only
    # by confirming a proposal (confirm_proposals).
    if body.source != "user":
        raise HTTPException(
            422, "direct creation is operator-only ('user'); task-confirmed "
            "memory is created only by confirming a proposal")
    try:
        fact = create_fact(body.text, category=body.category, source="user", confidence=body.confidence)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Task 10: audit — memory writes (category + id only, never the text).
    try:
        from .audit import append_event

        append_event("memory_write", f"fact created in category {body.category}",
                     task_id=None)
    except Exception:
        pass
    return fact


@router.delete("/long/facts/{identifier}")
def api_delete_fact(identifier: str):
    if not delete_fact(identifier):
        raise HTTPException(404, "fact not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "fact deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/skills")
def api_list_skills(domain: Optional[str] = None):
    # Task 13: see api_list_facts — undecryptable rows are skipped and
    # counted, never fatal to the whole list.
    skills, skipped = list_skills_with_skips(domain)
    return {"skills": skills, "skipped_undecryptable": skipped}


@router.post("/long/skills", status_code=201)
def api_create_skill(body: LongSkillInput):
    # Same provenance rule as facts: direct creation is operator-only.
    if body.source != "user":
        raise HTTPException(
            422, "direct creation is operator-only ('user'); task-confirmed "
            "memory is created only by confirming a proposal")
    try:
        skill = create_skill(body.name, body.steps, domain=body.domain, source="user")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        from .audit import append_event

        append_event("memory_write", f"skill created in domain {body.domain or 'general'}")
    except Exception:
        pass
    return skill


@router.patch("/long/skills/{identifier}")
def api_update_skill(identifier: str, body: LongSkillUpdate):
    try:
        updated = update_skill(identifier, body.steps)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "skill not found")
    # REVIEW (Task 10 audit): a skill update is a memory write — it
    # rewrites stored steps — but it emitted no audit event.
    try:
        from .audit import append_event

        append_event("memory_write", "skill updated")
    except Exception:
        pass
    return updated


@router.post("/long/skills/{identifier}/success")
def api_skill_success(identifier: str):
    if not record_skill_success(identifier):
        raise HTTPException(404, "skill not found")
    # REVIEW (Task 10 audit): the success counter is memory state;
    # record the mutation like any other memory write.
    try:
        from .audit import append_event

        append_event("memory_write", "skill success recorded")
    except Exception:
        pass
    return {"ok": True}


@router.delete("/long/skills/{identifier}")
def api_delete_skill(identifier: str):
    if not delete_skill(identifier):
        raise HTTPException(404, "skill not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "skill deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/files")
def api_list_files():
    return {"files": list_files()}


@router.post("/long/files", status_code=201)
def api_save_file(body: LongFileInput):
    try:
        saved = save_file(body.file_name, body.base64, summary=body.summary, task_id=body.task_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # REVIEW (Task 10 audit): saving a reference file is a memory
    # write, but it emitted no audit event (only its deletion did).
    try:
        from .audit import append_event

        append_event("memory_write", "reference file saved")
    except Exception:
        pass
    return saved


@router.get("/long/files/{identifier}/excerpt")
def api_file_excerpt(identifier: str, max_chars: int = 2000):
    excerpt = file_excerpt(identifier, max_chars=max(1, min(max_chars, 8000)))
    if excerpt is None:
        raise HTTPException(404, "file not found or not readable as text")
    return {"excerpt": excerpt}


@router.delete("/long/files/{identifier}")
def api_delete_file(identifier: str):
    if not delete_file(identifier):
        raise HTTPException(404, "file not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "reference file deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/proposals/{task_id}")
def api_get_proposals(task_id: str):
    return {"taskId": task_id, "proposals": get_proposals(task_id)}


@router.post("/long/proposals/{task_id}/confirm")
def api_confirm_proposals(task_id: str, body: ConfirmProposalsInput):
    result = confirm_proposals(task_id, list(body.accepted) + list(body.accepted_indices))
    # Task 10: audit — confirmed proposals write task-confirmed memory.
    try:
        from .audit import append_event

        n_accepted = result.get("accepted", 0) if isinstance(result, dict) else 0
        if n_accepted:
            append_event("memory_write",
                         f"{n_accepted} proposal(s) confirmed into memory",
                         task_id=task_id)
    except Exception:
        pass
    return result


@router.delete("")
def api_forget_all():
    """Forget everything in long-term memory (facts, skills, files)
    plus the semantic-memory tier and any pending memory proposals,
    so nothing survives to be re-confirmed afterwards."""
    result = forget_all()
    # Task 10: audit — forget-all is a destructive, security-relevant
    # event; the audit log itself is NOT wiped by it (it is the record
    # that the wipe happened).
    try:
        from .audit import append_event

        counts = ", ".join(f"{k}={v}" for k, v in sorted(result.items()))
        append_event("memory_forget_all", f"forget everything: {counts}")
    except Exception:
        pass
    return {"deleted": result}
