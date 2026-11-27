"""Lightweight scheduled tasks (Task 9).

An in-process, SQLite-backed scheduler: a job is an instruction plus
a human schedule (``"every 6h"``, ``"every 30m"``, ``"daily 09:00"``
UTC, or a 5-field cron). A background runner thread claims due jobs
and creates real tasks from them.

Honest limits, stated in code and docs:

* The runner is in-process. A backend restart loses nothing (jobs and
  ``next_run_at`` live in SQLite) but a job due during downtime runs
  late on the next tick, and two backend processes would double-claim
  (``BEGIN IMMEDIATE`` only serialises within one process). A real
  deployment should use the OS scheduler instead — see
  docs/MEMORY_LONG_HORIZON.md.
* A scheduled run is a normal task: it goes through the SAME gates as
  an interactive task — domain allow-list, confirmation gate,
  takeover, payment limits. A scheduled job can never silently buy,
  log in, or exfiltrate; it halts for the operator like any task.
  Scheduled jobs therefore always run with ``background=True`` and
  the operator's configured allow-list.
* Under pytest the lifespan does not start the loop (tests drive
  ``claim_due_jobs`` / ``execute_job`` directly).

Stream B (2026-11-27): the lifecycle is now closed. The schedule
definition (``schedule`` / ``schedule_kind`` / ``cron`` /
``interval_seconds``) is stored separately from the objective being
executed (``objective`` = the operator's wording, ``instruction`` =
the concrete executable instruction — see
:func:`concretise_schedule_instruction`). Every run records its
start (``execution_started_at``), completion
(``execution_completed_at``), outcome, error, a short run summary
and the artifacts it produced (``last_artifacts`` JSON) on the job
row, and posts a completion or failure notification to the outbox
(+ live WS broadcast; the missed-event pull delivers it after a
reconnect). ``start_job`` attaches the same finalizer to run-now
runs, so run-now is no longer a silent path. A restart runs
:func:`recover_interrupted_runs` before the loop starts: a run that
was in flight when the backend died is recorded as failed with the
cause (a claimed one-off job is re-queued to run on the next tick
instead of being lost).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from . import config, db
from . import secrets as secrets_store
from .hub import hub

logger = logging.getLogger("navigator.scheduler")

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([mhd])$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)
# Stream C: one-off schedules from NL ("in 2 minutes", "once in 2 hours").
# A once job runs a single time and never repeats.
_ONCE_RE = re.compile(r"^(?:once\s+)?in\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)$", re.IGNORECASE)

# Stream C (R-06): how long a scheduled run may sit waiting for the
# operator (awaiting_confirmation / awaiting_takeover) before the run
# times out, records a blocked outcome, and notifies. Default 10
# minutes; override with PRIVAGENT_SCHEDULED_CONFIRM_TIMEOUT_S (the
# demo/test sets it small).
import os as _os

SCHEDULED_CONFIRM_TIMEOUT_S = float(_os.getenv("PRIVAGENT_SCHEDULED_CONFIRM_TIMEOUT_S", "600"))


# ---------------------------------------------------------------------------
# Stream B: scheduler lifecycle schema + instruction concretisation
# ---------------------------------------------------------------------------

# Columns added idempotently by _ensure_scheduler_schema (PRAGMA check +
# ALTER TABLE, same pattern as monitoring_scheduler.ensure_stream_c_schema).
# They live here — not in monitoring_scheduler — so the scheduler owns its
# own lifecycle columns and a DB created by either path converges.
_SCHEDULER_COLUMNS: list[tuple[str, str]] = [
    ("objective", "TEXT"),              # operator's wording; instruction is the executable form
    ("execution_started_at", "REAL"),   # run start (UTC epoch)
    ("execution_completed_at", "REAL"), # run completion (UTC epoch)
    ("last_artifacts", "TEXT"),         # JSON list of {artifact_id, filename, file_format, filepath, file_size_bytes}
    ("last_run_summary", "TEXT"),       # one-line human summary of the last run
    ("run_condition", "TEXT"),          # JSON condition; null/{} means "always run"
]
_SCHEMA_DONE: set[str | None] = set()


def _ensure_scheduler_schema(db_path: str | None = None) -> None:
    """Add the Stream B lifecycle columns if the DB lacks them.

    The done-cache is keyed by the RESOLVED path (``db._get_db_path``),
    not the passed argument: callers usually pass None and the real
    path comes from PRIVAGENT_DB_PATH, which tests re-point per test —
    keying on None would migrate the first DB and silently skip the
    next one (the INSERT would then fail with "no column named
    objective").
    """
    resolved = db._get_db_path(db_path)
    if resolved in _SCHEMA_DONE:
        return
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        try:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(scheduled_jobs)").fetchall()}
        except Exception:
            _SCHEMA_DONE.add(resolved)
            return
        for name, decl in _SCHEDULER_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {name} {decl}")
    _SCHEMA_DONE.add(resolved)


# A scheduled run has no operator present to answer questions, so the
# instruction it executes must be self-contained. Vague NL wordings
# ("give me a summary", "check my stuff") used to re-enter the
# conversational path at run time and come back as a clarifying
# question instead of a result (DEMO2 §Task 7b). Concretisation is
# deterministic and pattern-based — never model-generated — and runs at
# creation/edit time, never at run time.
_RUN_TIME_RULE = (
    " Execute this directly at run time. Do not ask clarifying questions;"
    " if some information is missing, state what is missing and complete"
    " as much as possible."
)
_SUMMARY_OF_RE = re.compile(
    r"\b(?:give|send|show|provide|make|prepare|create)\b.{0,40}?\b"
    r"(?:summary|summar(?:y|ise|ize)|digest|round[- ]?up)\b.{0,80}?\b"
    r"(?:of|for)\b.{0,40}?\b(?:my\s+)?(watches|jobs|missions|tasks|alerts|monitors)\b",
    re.IGNORECASE,
)


def concretise_schedule_instruction(instruction: str) -> tuple[str, str]:
    """Split a schedule's objective from its executable instruction.

    Returns ``(objective, executable_instruction)``. ``objective`` is
    the operator's wording verbatim; ``executable_instruction`` is the
    concrete instruction the run executes. A vague summary wording is
    rewritten to the deterministic summary operation (list watches
    with latest observations + jobs with statuses and last outcomes);
    every other instruction keeps its wording and gains the run-time
    rule that it must execute without asking questions.
    """
    objective = (instruction or "").strip()
    executable = objective
    if _SUMMARY_OF_RE.search(objective):
        executable = (
            "List my watches with their latest observations and list my scheduled jobs "
            "with their statuses and last outcomes, and present it as a short daily summary. "
            "Do not ask clarifying questions; if there are no watches or jobs, say so explicitly."
        )
    if "do not ask clarifying questions" not in executable.lower():
        executable = executable + _RUN_TIME_RULE
    return objective, executable


# Supported run conditions (``run_condition`` JSON on the job row).
# A condition is evaluated in execute_job before the run starts; when
# it is not met the run is skipped, the skip is recorded on the job
# row (outcome 'completed' + summary), and the next run proceeds on
# schedule. Unknown kinds fail closed: the run is recorded 'failed'
# with the cause instead of running unconditionally.
#   {"kind": "always"}                              — default, run
#   {"kind": "has_active_watches"}                  — run only if >=1 active watch exists
#   {"kind": "unread_events_at_least", "value": N}  — run only if >=N unread events exist
def evaluate_run_condition(condition: Any, *, db_path: str | None = None) -> tuple[bool, str]:
    """Return (may_run, reason). ``condition`` is the stored JSON value
    (dict, JSON string, or None)."""
    if condition is None or condition == "" or condition == {}:
        return True, "no condition"
    if isinstance(condition, str):
        try:
            condition = json.loads(condition)
        except (json.JSONDecodeError, TypeError):
            return False, f"run condition is not valid JSON: {condition!r}"
    if not isinstance(condition, dict):
        return False, f"run condition must be an object, got {type(condition).__name__}"
    kind = str(condition.get("kind") or "always")
    db.init_db(db_path)
    if kind == "always":
        return True, "condition 'always' met"
    if kind == "has_active_watches":
        with db.get_connection(db_path) as conn:
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM monitoring_missions WHERE status='active'"
                ).fetchone()[0]
            except Exception:
                n = 0
        return (n > 0), f"active watches: {n}"
    if kind == "unread_events_at_least":
        try:
            need = int(condition.get("value", 1))
        except (TypeError, ValueError):
            return False, "run condition 'unread_events_at_least' needs an integer 'value'"
        from .monitoring_scheduler import count_unread_events

        n = count_unread_events(db_path=db_path)
        return (n >= need), f"unread events: {n} (need >= {need})"
    return False, f"unknown run condition kind: {kind!r}"


def parse_schedule(schedule: str) -> dict[str, Any]:
    """Parse a human schedule into the stored form.

    Returns ``{"schedule_kind", "cron", "interval_seconds"}``.
    Raises ValueError on anything unsupported — a job that cannot be
    parsed must never be created silently."""
    raw = (schedule or "").strip()
    match = _ONCE_RE.fullmatch(raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        seconds = amount * 3600 if unit.startswith(("hour", "hr")) else amount * 60
        if not 60 <= seconds <= 7 * 86400:
            raise ValueError("one-off delay must be between 1 minute and 7 days")
        return {"schedule_kind": "once", "cron": None, "interval_seconds": seconds}
    match = _EVERY_RE.fullmatch(raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
        if not 60 <= seconds <= 7 * 86400:
            raise ValueError("interval must be between 1 minute and 7 days")
        return {"schedule_kind": "interval", "cron": None, "interval_seconds": seconds}
    match = _DAILY_RE.fullmatch(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("daily time must be HH:MM in 24-hour UTC")
        return {"schedule_kind": "cron", "cron": f"{minute} {hour} * * *", "interval_seconds": None}
    # A raw 5-field cron is accepted as-is (validated by next_due_at).
    if len(raw.split()) == 5:
        from .monitoring_scheduler import next_due_at

        next_due_at("cron", raw, 0, time.time())  # raises on invalid cron
        return {"schedule_kind": "cron", "cron": raw, "interval_seconds": None}
    raise ValueError(
        "unsupported schedule: use 'in 5 minutes' (once), 'every 30m' / 'every 6h' / 'every 1d', "
        "'daily 09:00' (UTC), or a 5-field cron"
    )


def _next_run(parsed: dict[str, Any], now: float, *, initial: bool = False) -> float | None:
    from .monitoring_scheduler import next_due_at

    if parsed["schedule_kind"] == "once":
        # A one-off job runs once: at creation its next run is
        # now + delay; after the claim, next_run_at becomes None so
        # it is never claimed again.
        return now + int(parsed["interval_seconds"]) if initial else None
    if parsed["schedule_kind"] == "interval":
        return now + int(parsed["interval_seconds"])
    return next_due_at("cron", parsed["cron"], 0, now)


def _row_to_job(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["allowed_domains"] = json.loads(d.get("allowed_domains") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["allowed_domains"] = []
    d["long_horizon"] = bool(d.get("long_horizon"))
    # Stream B: objective / condition / artifacts are stored as
    # text; surface them parsed, defaulting to the executable
    # instruction / no condition / no artifacts on old rows.
    d["objective"] = d.get("objective") or d.get("instruction")
    try:
        d["run_condition"] = json.loads(d.get("run_condition") or "null")
    except (json.JSONDecodeError, TypeError):
        d["run_condition"] = None
    try:
        d["last_artifacts"] = json.loads(d.get("last_artifacts") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["last_artifacts"] = []
    return d


def create_job(
    instruction: str,
    schedule: str,
    *,
    allowed_domains: Optional[list[str]] = None,
    long_horizon: bool = False,
    objective: Optional[str] = None,
    run_condition: Optional[dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create a scheduled job.

    Stream B: the stored ``instruction`` is the CONCRETE executable
    instruction (see :func:`concretise_schedule_instruction`); the
    operator's wording is kept separately as ``objective`` so the
    schedule definition and the objective stay distinguishable.
    ``run_condition`` (optional dict, see :func:`evaluate_run_condition`)
    makes the job conditional.
    """
    instruction = (instruction or "").strip()
    if not instruction or len(instruction) > config.MAX_INSTRUCTION_CHARS:
        raise ValueError("instruction must be 1-8000 characters")
    # REVIEW (Task 9 audit): a non-string entry (e.g. a number from a
    # hand-built JSON body) used to reach the DB and only fail later
    # inside effective_allowlist when the job ran — the job would then
    # never execute, silently. Reject it at creation.
    if allowed_domains is not None and (
        not isinstance(allowed_domains, list)
        or any(not isinstance(d, str) or not d.strip() for d in allowed_domains)
    ):
        raise ValueError("allowed_domains must be a list of non-empty domain strings")
    if run_condition is not None and not isinstance(run_condition, dict):
        raise ValueError("run_condition must be an object")
    parsed = parse_schedule(schedule)
    objective_text, executable = concretise_schedule_instruction(instruction)
    if objective is not None and str(objective).strip():
        objective_text = str(objective).strip()
    now = time.time()
    job_id = uuid.uuid4().hex
    db.init_db(db_path)
    from .monitoring_scheduler import ensure_stream_c_schema

    ensure_stream_c_schema(db_path)
    _ensure_scheduler_schema(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs(id, instruction, objective, schedule, schedule_kind, cron, interval_seconds,"
            " status, next_run_at, created_at, updated_at, allowed_domains, long_horizon, run_condition)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, executable, objective_text, schedule.strip(), parsed["schedule_kind"], parsed["cron"],
                parsed["interval_seconds"], "active", _next_run(parsed, now, initial=True), now, now,
                json.dumps(list(allowed_domains) if allowed_domains is not None else list(config.ALLOWED_DOMAINS)),
                1 if long_horizon else 0,
                json.dumps(run_condition) if run_condition is not None else None,
            ),
        )
        conn.commit()
    return get_job(job_id, db_path=db_path) or {"id": job_id}


def get_job(job_id: str, *, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM scheduled_jobs ORDER BY created_at DESC").fetchall()
    return [_row_to_job(row) for row in rows]


def delete_job(job_id: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)).rowcount > 0


def set_job_status(job_id: str, status: str, *, db_path: Optional[str] = None) -> bool:
    # Stream C: 'cancelled' is a terminal job state (contract §6
    # vocabulary). A cancelled job is never claimed again; use
    # delete_job only when the row itself should disappear.
    if status not in {"active", "paused", "cancelled"}:
        raise ValueError("status must be 'active', 'paused' or 'cancelled'")
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE scheduled_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), job_id),
        )
        conn.commit()
        return cur.rowcount > 0


def update_job(job_id: str, *, instruction: str | None = None, schedule: str | None = None,
               run_condition: dict[str, Any] | None = None,
               db_path: Optional[str] = None) -> dict[str, Any] | None:
    """Edit a job's instruction and/or schedule (Stream C, task 6).

    A new schedule re-parses and recomputes next_run_at from now; a
    paused job keeps its paused status. Raises ValueError on a bad
    schedule or an empty instruction. Returns the updated job, or
    None when the job does not exist.

    Stream B: a new instruction is concretised like at creation
    (objective kept verbatim, instruction stored executable); a new
    run_condition replaces the old one.
    """
    db.init_db(db_path)
    _ensure_scheduler_schema(db_path)
    job = get_job(job_id, db_path=db_path)
    if job is None:
        return None
    if instruction is None:
        new_objective, new_instruction = job.get("objective"), job["instruction"]
    else:
        new_objective, new_instruction = concretise_schedule_instruction(instruction)
    if not new_instruction or len(new_instruction) > config.MAX_INSTRUCTION_CHARS:
        raise ValueError("instruction must be 1-8000 characters")
    if run_condition is not None and not isinstance(run_condition, dict):
        raise ValueError("run_condition must be an object")
    sets: list[str] = ["instruction = ?", "objective = ?", "updated_at = ?"]
    params: list[Any] = [new_instruction, new_objective, time.time()]
    if schedule is not None:
        parsed = parse_schedule(schedule)
        sets += ["schedule = ?", "schedule_kind = ?", "cron = ?", "interval_seconds = ?",
                 "next_run_at = ?"]
        params += [schedule.strip(), parsed["schedule_kind"], parsed["cron"],
                   parsed["interval_seconds"], _next_run(parsed, time.time(), initial=True)]
    if run_condition is not None:
        sets += ["run_condition = ?"]
        params += [json.dumps(run_condition)]
    params.append(job_id)
    with db.get_connection(db_path) as conn:
        conn.execute(
            f"UPDATE scheduled_jobs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    return get_job(job_id, db_path=db_path)


def record_job_start(job_id: str, task_id: str, *, db_path: Optional[str] = None) -> None:
    """Record a run's start on the job row (Stream B).

    Sets ``execution_started_at`` / ``last_run_at`` / ``last_task_id``.
    The completion side is :func:`record_job_outcome`; together they
    separate "the job ran" from "the job finished, with this outcome".
    """
    from .monitoring_scheduler import ensure_stream_c_schema

    db.init_db(db_path)
    ensure_stream_c_schema(db_path)
    _ensure_scheduler_schema(db_path)
    now = time.time()
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET execution_started_at=?, last_run_at=?,"
            " last_task_id=?, updated_at=? WHERE id=?",
            (now, now, task_id, now, job_id),
        )
        conn.commit()


def record_job_outcome(job_id: str, outcome: str, error: str | None = None,
                       *, artifacts: list[dict[str, Any]] | None = None,
                       summary: str | None = None,
                       db_path: Optional[str] = None) -> None:
    """Record a run's outcome on the job row (Stream C, R-06).

    outcome is one of: completed | failed | cancelled | blocked.
    'blocked' means the run timed out waiting for the operator
    (awaiting_confirmation / awaiting_takeover). The row's `status`
    stays the job's enabled state (active/paused/cancelled) — the
    outcome is what the LAST run did.

    Stream B: also records ``execution_completed_at``, the generated
    artifacts (``last_artifacts`` JSON) and a one-line run summary, so
    the job row alone answers "did it run, what happened, what did it
    produce".
    """
    from .monitoring_scheduler import ensure_stream_c_schema

    db.init_db(db_path)
    ensure_stream_c_schema(db_path)
    _ensure_scheduler_schema(db_path)
    now = time.time()
    with db.get_connection(db_path) as conn:
        try:
            conn.execute(
                "UPDATE scheduled_jobs SET last_outcome=?, last_error=?,"
                " last_run_outcome_at=?, last_run_at=?, execution_completed_at=?,"
                " last_artifacts=?, last_run_summary=?, updated_at=? WHERE id=?",
                (outcome, error, now, now, now,
                 json.dumps(artifacts or []), summary, now, job_id),
            )
        except Exception:
            # A DB without the outcome columns (pre-migration) still
            # records the run time.
            conn.execute(
                "UPDATE scheduled_jobs SET last_run_at=?, updated_at=? WHERE id=?",
                (now, now, job_id),
            )
        conn.commit()


async def _watch_stalled_run(job_id: str, task_id: str, timeout_s: float,
                             *, db_path: Optional[str] = None) -> bool:
    """Watch one scheduled run for an operator stall (Stream C, R-06).

    Polls the task record every 2 s. If it sits in
    awaiting_confirmation / awaiting_takeover for longer than
    timeout_s, the run is timed out: the task is cancelled (mirroring
    POST /api/task/{id}/cancel — wake the gates, clear the takeover),
    the job row records outcome 'blocked' with the cause, and a
    notification goes to the outbox + live WS clients. The job itself
    stays active and runs again at its next scheduled time — the
    timeout ends the RUN, not the job. Never stalls silently forever.
    Returns True when it timed out a stall, False when the run reached
    a terminal state on its own.
    """
    from .monitoring_scheduler import broadcast_outbox_notification, post_outbox_notification
    from .hub import TERMINAL_STATES

    waited = 0.0
    poll_s = 2.0
    while True:
        await asyncio.sleep(poll_s)
        record = hub.tasks.get(task_id)
        if record is None or record.state in TERMINAL_STATES:
            return False
        if record.state in ("awaiting_confirmation", "awaiting_takeover"):
            waited += poll_s
            if waited >= timeout_s:
                state = record.state
                reason = (
                    f"Scheduled run waited {int(waited)}s for the operator "
                    f"({state}) and timed out — no confirmation/takeover answer arrived."
                )
                try:
                    from .agentLoop import request_cancel

                    request_cancel(task_id)
                except Exception:
                    pass
                record.cancelled = True
                record.state = "cancelled"
                record.pending_confirmation = None
                record.confirm_decision = False
                record.confirm_event.set()
                record.takeover = None
                record.takeover_decision = False
                record.takeover_event.set()
                record.message = reason
                _mark_finalized(task_id)
                record_job_outcome(job_id, "blocked", reason, db_path=db_path)
                note_id = post_outbox_notification(
                    "scheduled_blocked", job_id,
                    "Scheduled job blocked",
                    reason,
                    diff_data={"task_id": task_id, "state": state},
                    db_path=db_path,
                )
                try:
                    await broadcast_outbox_notification(note_id, db_path=db_path)
                except Exception:
                    pass
                logger.warning("scheduled job %s blocked: %s", job_id, reason)
                return True
        else:
            waited = 0.0


# ---------------------------------------------------------------------------
# Stream B: run finalization — one place records a run's outcome AND
# notifies, for BOTH the scheduler loop and run-now.
#
# DEMO2 §Task 7b: run-now called start_job directly, so the run
# produced a summary message on the background task and nothing else —
# the job row kept last_outcome null and no notification existed.
# The finalizer below is attached to every run task (see start_job)
# and is also awaited by execute_job, so every path that starts a run
# ends with a recorded outcome + an outbox notification (+ WS
# broadcast; the missed-event pull is the durable channel for a
# closed frontend). The _finalized set dedupes: whichever path runs
# first wins, the other is a no-op — so a run notifies exactly once.
# ---------------------------------------------------------------------------

_finalized_runs: set[str] = set()


def _mark_finalized(task_id: str) -> None:
    _finalized_runs.add(task_id)
    if len(_finalized_runs) > 5000:  # bounded; task ids are unique
        _finalized_runs.clear()


def _collect_run_artifacts(task_id: str, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        rows = db.list_task_artifacts(task_id, db_path=db_path)
    except Exception:
        return []
    return [
        {
            "artifact_id": r.get("artifact_id"),
            "filename": r.get("filename"),
            "file_format": r.get("file_format"),
            "filepath": r.get("filepath"),
            "file_size_bytes": r.get("file_size_bytes"),
        }
        for r in rows
    ]


def _run_summary_for(outcome: str, record: Any, artifacts: list[dict[str, Any]]) -> str:
    if outcome == "completed":
        if artifacts:
            names = ", ".join(a["filename"] for a in artifacts[:3])
            extra = f" (+{len(artifacts) - 3} more)" if len(artifacts) > 3 else ""
            return f"Completed; produced {len(artifacts)} artifact(s): {names}{extra}."
        msg = (record.message or "").strip().replace("\n", " ")[:200]
        return f"Completed. {msg}" if msg else "Completed."
    if outcome == "cancelled":
        return "Run cancelled."
    if outcome == "blocked":
        return "Run blocked waiting for the operator."
    err = (record.message or "").strip().replace("\n", " ")[:200] if record is not None else ""
    return f"Failed. {err}" if err else "Failed."


def _record_run_completion_sync(job_id: str, task_id: str, *,
                                db_path: Optional[str] = None) -> dict[str, Any] | None:
    """Synchronously record one run's outcome + post its notification.

    Returns ``{"outcome": ..., "note_id": ...}`` or None when this
    task was already finalized (exactly-once notification). The WS
    broadcast is the caller's job (async); the outbox row is the
    durable part and is written here.
    """
    if task_id in _finalized_runs:
        return None
    _mark_finalized(task_id)
    from .monitoring_scheduler import post_outbox_notification

    record = hub.tasks.get(task_id)
    state = (record.state if record is not None else "failed")
    if state in ("completed", "failed", "cancelled"):
        outcome = state
    elif state in ("awaiting_confirmation", "awaiting_takeover"):
        outcome = "blocked"
    else:
        outcome = "failed"
    error = None
    if outcome in ("failed", "blocked") and record is not None and record.message:
        error = record.message[:500]
    artifacts = _collect_run_artifacts(task_id, db_path=db_path)
    summary = _run_summary_for(outcome, record, artifacts)
    record_job_outcome(job_id, outcome, error, artifacts=artifacts,
                       summary=summary, db_path=db_path)
    job = get_job(job_id, db_path=db_path) or {}
    objective = job.get("objective") or job.get("instruction") or job_id
    if outcome == "completed":
        kind, title = "scheduled_completed", "Scheduled job completed"
        text = f"Scheduled job completed: {objective}. {summary}"
    elif outcome == "cancelled":
        kind, title = "scheduled_cancelled", "Scheduled job cancelled"
        text = f"Scheduled run cancelled: {objective}."
    elif outcome == "blocked":
        kind, title = "scheduled_blocked", "Scheduled job blocked"
        text = f"Scheduled job blocked: {objective}. {error or ''}".strip()
    else:
        kind, title = "scheduled_failed", "Scheduled job failed"
        text = f"Scheduled job failed: {objective}. {error or ''}".strip()
    note_id = post_outbox_notification(
        kind, job_id, title, text,
        diff_data={"task_id": task_id, "outcome": outcome,
                   "artifacts": [a["filename"] for a in artifacts]},
        db_path=db_path,
    )
    logger.info("scheduled job %s run %s: %s", job_id, task_id, outcome)
    return {"outcome": outcome, "note_id": note_id}


async def _finalize_scheduled_run(job: dict[str, Any], task_id: str, *,
                                  db_path: Optional[str] = None) -> dict[str, Any] | None:
    """Record a run's outcome + notify (async wrapper).

    The sync part runs first (exactly-once); the WS broadcast follows
    best-effort. A closed frontend loses nothing — the outbox row is
    returned by the missed-event pull on reconnect.
    """
    result = _record_run_completion_sync(job["id"], task_id, db_path=db_path)
    if result is None:
        return None
    try:
        from .monitoring_scheduler import broadcast_outbox_notification

        await broadcast_outbox_notification(result["note_id"], db_path=db_path)
    except Exception:
        pass
    return result


def _attach_run_finalizer(job: dict[str, Any], task_id: str,
                          task: "asyncio.Task", *, db_path: Optional[str] = None) -> None:
    """Attach the outcome finalizer to a run task (Stream B).

    Fires when the task settles, whichever path started it
    (scheduler loop or run-now). The sync record + outbox write run
    inline in the callback; the WS broadcast is scheduled on the
    running loop. If execute_job already finalized the run, this is a
    no-op via the _finalized set.
    """
    def _on_done(_t: "asyncio.Task") -> None:
        try:
            result = _record_run_completion_sync(job["id"], task_id, db_path=db_path)
        except Exception:
            return
        if result is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            from .monitoring_scheduler import broadcast_outbox_notification

            loop.create_task(broadcast_outbox_notification(result["note_id"], db_path=db_path))
        except Exception:
            pass

    task.add_done_callback(_on_done)


def claim_due_jobs(now: Optional[float] = None, limit: int = 5, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Atomically claim due jobs and advance their next run. A claimed
    job is never claimed twice by this process; a job whose execution
    crashes keeps its advanced next_run_at (it runs again on schedule,
    not in a tight loop)."""
    current = time.time() if now is None else now
    db.init_db(db_path)
    claims: list[dict[str, Any]] = []
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE status='active' AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT ?",
            (current, limit),
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            parsed = {
                "schedule_kind": job["schedule_kind"],
                "cron": job.get("cron"),
                "interval_seconds": job.get("interval_seconds"),
            }
            conn.execute(
                "UPDATE scheduled_jobs SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                (_next_run(parsed, current), current, job["id"]),
            )
            claims.append(job)
    # Task 10: audit — every claim (job id + schedule kind only; the
    # instruction text is never written to the audit log).
    if claims:
        try:
            from .audit import append_event

            for job in claims:
                append_event("scheduler_claim",
                             f"scheduled job {job['id']} claimed ({job['schedule_kind']})",
                             actor="system")
        except Exception:
            pass
    return claims


async def execute_job(job: dict[str, Any], *, db_path: Optional[str] = None,
                      confirm_timeout_s: float | None = None) -> Optional[str]:
    """Run one scheduled job as a normal background task.

    The task is created with the job's allow-list (or the global one)
    and ``background=True``; it goes through run_task, which wires the
    confirmation gate, takeover gate, and payment limits exactly like
    an interactive task. A scheduled run that needs the operator halts
    in ``awaiting_confirmation`` / ``awaiting_takeover`` until the
    operator answers — it never self-approves.

    Stream C (R-06): the halt is now BOUNDED. A watchdog watches the
    run: if it waits for the operator longer than confirm_timeout_s
    (default SCHEDULED_CONFIRM_TIMEOUT_S, 10 min), the run is timed
    out, the job row records outcome 'blocked' with the cause, and a
    notification is posted — never a silent stall forever. A run that
    settles normally records its outcome (completed / failed /
    cancelled) on the job row, visible via GET /api/scheduled.

    Stream B: before starting, the job's run_condition is evaluated
    (see :func:`evaluate_run_condition`); a run whose condition is
    not met is skipped and the skip is recorded instead of executing.
    Outcome recording + completion/failure notification go through
    :func:`_finalize_scheduled_run` — the same finalizer the run-now
    path uses via start_job — so a scheduled run notifies exactly
    once however it was started.
    """
    may_run, cond_reason = evaluate_run_condition(job.get("run_condition"), db_path=db_path)
    if not may_run:
        reason = f"Skipped — run condition not met ({cond_reason})."
        if "unknown run condition" in cond_reason:
            # Fail closed: an unknown condition must not silently
            # skip forever, nor run unconditionally.
            record_job_outcome(job["id"], "failed", reason,
                               summary=reason, db_path=db_path)
        else:
            record_job_outcome(job["id"], "completed", None,
                               summary=reason, db_path=db_path)
        logger.info("scheduled job %s skipped: %s", job["id"], reason)
        return None
    task_id, task = start_job(job, db_path=db_path)
    timeout = SCHEDULED_CONFIRM_TIMEOUT_S if confirm_timeout_s is None else confirm_timeout_s
    stalled: dict[str, bool] = {"blocked": False}

    async def _watch() -> None:
        stalled["blocked"] = await _watch_stalled_run(job["id"], task_id, timeout, db_path=db_path)

    watcher = asyncio.create_task(_watch())
    try:
        await task
    except asyncio.CancelledError:
        # The scheduler loop is shutting down (or the run task was
        # cancelled from outside): the run task is already done —
        # awaiting it again would raise CancelledError a second time.
        pass
    except Exception as exc:  # noqa: BLE001 — a failed scheduled run must not kill the loop
        logger.warning("scheduled job %s failed: %s", job["id"], exc)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    if stalled["blocked"]:
        # The watchdog already recorded outcome 'blocked' + notified.
        return task_id
    # Exactly-once outcome + completion/failure notification (the
    # run-now done-callback is the same finalizer; whichever runs
    # first wins).
    await _finalize_scheduled_run(job, task_id, db_path=db_path)
    return task_id


# Fire-and-forget scheduled runs kept alive for the process lifetime.
_job_tasks: set[asyncio.Task] = set()


def start_job(job: dict[str, Any], *, db_path: Optional[str] = None) -> tuple[str, "asyncio.Task"]:
    """Create the task for a job and start run_task in the background.
    Returns (task_id, asyncio.Task). The scheduler loop awaits the
    task; the run-now endpoint returns the id immediately.

    Stream B: records the run start on the job row and attaches the
    outcome finalizer (see :func:`_attach_run_finalizer`), so a
    run-now run records its outcome and notifies exactly like a
    loop-claimed run instead of vanishing silently.
    """
    from .agentLoop import run_task
    from .allowlist import effective_allowlist

    instruction = job["instruction"]
    effective = effective_allowlist(job.get("allowed_domains") or None, config.ALLOWED_DOMAINS)
    record = hub.new_task(
        instruction,
        chat_id=None,
        allowed_domains=effective,
        long_horizon=bool(job.get("long_horizon")),
        background=True,
    )
    record.state = "planning"
    record_job_start(job["id"], record.task_id, db_path=db_path)
    task = asyncio.create_task(run_task(record.task_id))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    _attach_run_finalizer(job, record.task_id, task, db_path=db_path)
    return record.task_id, task


def recover_interrupted_runs(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Restart recovery for scheduled runs (Stream B).

    After a backend restart the in-memory hub is empty, so a job whose
    last run started (``execution_started_at`` set) but never recorded
    an outcome (``last_run_outcome_at`` is null or older than the
    start) was interrupted mid-run. Record it as failed with the
    cause, post a failure notification, and — for a one-off job whose
    next run was already consumed by the claim — re-queue it to run
    on the next tick instead of losing it. Recurring jobs keep their
    already-advanced next run. Returns the recovered job rows.
    """
    from .monitoring_scheduler import ensure_stream_c_schema, post_outbox_notification

    db.init_db(db_path)
    ensure_stream_c_schema(db_path)
    _ensure_scheduler_schema(db_path)
    recovered: list[dict[str, Any]] = []
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE status='active'"
            " AND execution_started_at IS NOT NULL"
            " AND (last_run_outcome_at IS NULL OR last_run_outcome_at < execution_started_at)"
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            recovered.append(job)
            now = time.time()
            reason = "Backend restarted during this scheduled run; the run did not complete."
            conn.execute(
                "UPDATE scheduled_jobs SET last_outcome='failed', last_error=?,"
                " last_run_outcome_at=?, execution_completed_at=?,"
                " last_run_summary=?, updated_at=? WHERE id=?",
                (reason, now, now,
                 "Failed — backend restarted during the run.", now, job["id"]),
            )
            if job.get("schedule_kind") == "once" and job.get("next_run_at") is None:
                conn.execute(
                    "UPDATE scheduled_jobs SET next_run_at=? WHERE id=?",
                    (now, job["id"]),
                )
            conn.commit()
            post_outbox_notification(
                "scheduled_failed", job["id"], "Scheduled job failed",
                f"Scheduled job failed: {job.get('objective') or job.get('instruction')}. {reason}",
                diff_data={"task_id": job.get("last_task_id"), "outcome": "failed",
                           "recovered": True},
                db_path=db_path,
            )
            logger.warning("scheduled job %s recovered after restart: %s", job["id"], reason)
    return recovered


async def scheduler_loop(stop: asyncio.Event, tick_seconds: float | None = None) -> None:
    tick = tick_seconds if tick_seconds is not None else config.SCHEDULER_TICK_S
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        try:
            for job in claim_due_jobs(limit=max(0, 5 - len(running))):
                task = asyncio.create_task(execute_job(job))
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception as exc:  # noqa: BLE001 — the loop must survive a bad claim
            logger.warning("scheduler tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    if running:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def scheduler_lifespan(_app):
    """Start the in-process scheduler. Skipped under pytest: tests
    drive claim_due_jobs / execute_job directly, and a background loop
    claiming jobs would make test runs non-deterministic.

    Stream B: before the loop starts, recover runs interrupted by a
    restart (see :func:`recover_interrupted_runs`) so a run that died
    with the backend is recorded + notified instead of looking like
    it never happened."""
    if secrets_store.is_pytest():
        yield
        return
    try:
        recover_interrupted_runs()
    except Exception as exc:  # noqa: BLE001 — recovery must not block startup
        logger.warning("scheduled-run recovery failed: %s", exc)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await task
