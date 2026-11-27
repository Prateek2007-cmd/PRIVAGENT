"""Compositional orchestration for monitoring / scheduling objectives.

Stream 3 (NAVIGATOR intent-routing fix, CONTRACTS4).

A monitor objective is executed as a coordinated pipeline, driven by
the structured :class:`Objective` (Stream 1, ``backend/app/objective.py``)
and :class:`WorkingContext` (Stream 2, ``backend/app/working_context.py``)
— never by re-parsing raw text. Each step produces a receipt; the final
step verifies creation against the DB by re-read, per CONTRACTS4 §3.

Pipeline for a monitor objective::

    resolve product identity (objective entities, else working context)
      -> discover relevant public product sources (target_url)
      -> validate identity / variant (URL gate + page product name)
      -> extract current price + reference price (baseline observation)
      -> resolve the discount condition (never substitute one for another)
      -> create the persistent monitoring mission
      -> schedule recurring observations (next_run_at, scheduler claim)
      -> register notification conditions (condition row, verified)
      -> verify creation + baseline (completion verifier, §3)

The same compositional principle is applied to the objectives whose
pieces already exist: scheduling (``scheduler.create_job``), monitor
updates (pause/resume/cancel/update by re-read), and email-polling
configuration (``handle_email_interval_change``). Research, browser
tasks, artifacts, MCP/connector tasks and job discovery are NOT
re-implemented here — they keep their existing handlers; this module
only *verifies* their outcomes through the single completion verifier
(:func:`app.completion_verifier.verify_objective_completion`).

Monitoring vs scheduling vs email polling — the semantics this module
enforces, and the regression tests prove:

* monitoring a product/website/new listings/inbox creates a
  ``monitoring_missions`` (or ``email_monitoring_missions``) row;
* updating a monitoring interval updates ONLY that watch's row;
* creating/changing a scheduled task touches ONLY ``scheduled_jobs``;
* configuring email polling touches ONLY ``email_monitoring_missions``.
  A product watch creation must never change any email setting.
* a condition is never silently substituted: an absolute-price ask
  stays absolute, a %-discount ask stays a percent, a price-change ask
  stays a change. A missing or unknown condition is ``awaiting_input``,
  not a guessed default.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import db

# ---------------------------------------------------------------------------
# Objective / WorkingContext access (duck-typed: Stream 1's Objective
# dataclass does not exist at base d423388, so accept a dict or any
# object carrying the CONTRACTS4 §1 fields).
# ---------------------------------------------------------------------------

_MONITOR_INTENTS = {
    "monitor_product", "monitor_website", "monitor_new_listings",
    "monitor_inbox", "update_monitor", "cancel_monitor",
    "pause_monitor", "resume_monitor", "list_monitors",
}
_SCHEDULE_INTENTS = {"schedule_task", "update_schedule", "cancel_schedule"}
_EMAIL_POLLING_INTENTS = {"configure_email_polling"}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _objective_intent(objective: Any) -> str:
    return str(_field(objective, "primary_intent", "") or "")


# ---------------------------------------------------------------------------
# Interval parsing — one canonical parser for every cadence variant.
# ---------------------------------------------------------------------------

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}
_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)"
_UNIT = r"(?:minutes?|mins?|hours?|hrs?)"
_INTERVAL_RE = re.compile(
    rf"\bevery\s+(?:({_NUM})\s*)?({_UNIT})\b", re.IGNORECASE)
_ONCE_RE = re.compile(
    rf"\bonce\s+in\s+({_NUM})\s*({_UNIT})\b|\bin\s+({_NUM})\s*({_UNIT})\b.*\bonce\b",
    re.IGNORECASE)
_IN_RE = re.compile(rf"\bin\s+({_NUM})\s*({_UNIT})\b", re.IGNORECASE)
_DAILY_RE = re.compile(
    r"\b(?:daily|every\s+day|everyday|every\s+morning|each\s+morning)\b"
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE)


@dataclass
class IntervalSpec:
    """A normalised cadence. Exactly one of interval/once/daily.

    kind "interval": check every ``interval_seconds``.
    kind "once":    a single check after ``delay_seconds`` — this is a
                    one-off scheduled job, NOT a recurring watch.
    kind "daily":   a cron check at a clock time (UTC cron string).
    """
    kind: str  # "interval" | "once" | "daily"
    interval_seconds: int | None = None
    delay_seconds: int | None = None
    cron: str | None = None
    display: str = ""

    def as_schedule_string(self) -> str:
        """The ``scheduler.parse_schedule`` string for this cadence."""
        if self.kind == "once":
            secs = self.delay_seconds or 60
            # scheduler.parse_schedule only accepts whole minutes for
            # one-off delays — round (parse_interval only ever yields
            # whole minutes anyway).
            return f"in {max(1, round(secs / 60))} minutes"
        if self.kind == "daily" and self.cron:
            parts = self.cron.split()
            return f"daily {parts[1]}:{parts[0]}"
        secs = self.interval_seconds or 300
        if secs % 86400 == 0:
            return f"every {secs // 86400}d"
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        return f"every {max(1, secs // 60)}m"


def _word_to_int(token: str | None) -> int | None:
    if token is None:
        return None
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def parse_interval(text: str) -> IntervalSpec | None:
    """Parse every supported cadence variant into one canonical spec.

    Variants: "every five minutes", "every 5 mins", "once in 10
    minutes", "daily at 9am", "check every hour", "every 2 hours".
    Returns None when the text carries no cadence at all. A bare
    "every hour" / "every day" means one unit. Interval seconds are
    clamped to the mission table's 60-second floor.
    """
    raw = text or ""
    # Daily first: "daily at 9am" also contains no "every N unit".
    daily = _DAILY_RE.search(raw)
    if daily:
        from .continuous_monitoring_companion import parse_daily_time

        parsed = parse_daily_time(raw)
        if parsed is not None:
            cron, display = parsed
            return IntervalSpec(kind="daily", cron=cron, display=f"daily at {display}")
        return IntervalSpec(kind="daily", cron="30 3 * * *", display="daily at 9:00 AM IST")
    once = _ONCE_RE.search(raw)
    if once:
        num_tok = once.group(1) or once.group(3)
        unit = (once.group(2) or once.group(4) or "minutes").lower()
        num = _word_to_int(num_tok) or 1
        secs = num * 3600 if unit.startswith(("hour", "hr")) else num * 60
        secs = max(60, secs)
        return IntervalSpec(kind="once", delay_seconds=secs,
                            display=f"once in {num} {'hour' if unit.startswith(('hour', 'hr')) else 'minute'}{'s' if num != 1 else ''}")
    # A bare future delay ("remind me in 10 minutes", "do this in an
    # hour") is a one-off too — the scheduler's "in N minutes" form.
    # It is checked AFTER "once in ..." so the explicit form wins.
    delay = _IN_RE.search(raw)
    if delay:
        num = _word_to_int(delay.group(1)) or 1
        unit = delay.group(2).lower()
        secs = max(60, num * 3600 if unit.startswith(("hour", "hr")) else num * 60)
        unit_word = "hour" if unit.startswith(("hour", "hr")) else "minute"
        return IntervalSpec(kind="once", delay_seconds=secs,
                            display=f"in {num} {unit_word}{'s' if num != 1 else ''}")
    m = _INTERVAL_RE.search(raw)
    if not m:
        return None
    num = _word_to_int(m.group(1)) or 1
    unit = m.group(2).lower()
    secs = num * 3600 if unit.startswith(("hour", "hr")) else num * 60
    secs = max(60, secs)
    unit_word = "hour" if unit.startswith(("hour", "hr")) else "minute"
    return IntervalSpec(kind="interval", interval_seconds=secs,
                        display=f"every {num} {unit_word}{'s' if num != 1 else ''}")


# ---------------------------------------------------------------------------
# Condition resolution — never substitute one condition for another.
# ---------------------------------------------------------------------------

# CONTRACTS4 §1 condition kinds -> (condition_type, params builder).
# "price_change" is any price move: the stored schema represents it as
# price_drop with min_percent 0, stated explicitly in the result —
# that is the schema's representation, not a silent substitution.
# "deadline" is not a watch condition at all: it routes to a scheduled
# reminder, and resolve_condition says so instead of inventing a price
# condition for it.
_CONDITION_KINDS = ("percent_discount", "absolute_price", "price_change",
                    "availability", "new_listing", "deadline")


def resolve_condition(condition: dict | None) -> tuple[str, dict] | None:
    """Map a structured condition to (condition_type, condition_params).

    Returns None when no condition was given (the caller must ask,
    not guess). Raises ValueError on an unknown kind or an
    operator the stored schema cannot represent — a condition the
    schema cannot hold must fail loudly, never become a different
    condition.
    """
    if not condition:
        return None
    kind = str(condition.get("kind") or "").strip()
    value = condition.get("value")
    operator = str(condition.get("operator") or "").strip().lower()
    if kind == "percent_discount":
        if value is None:
            raise ValueError("percent_discount condition needs a percent value")
        return "price_drop", {"min_percent": float(value)}
    if kind == "absolute_price":
        if value is None:
            raise ValueError("absolute_price condition needs a price value")
        if operator and operator not in ("below", "<", "<=", "under"):
            raise ValueError(
                f"absolute_price with operator {operator!r} cannot be stored: "
                "this schema only watches for the price falling below a threshold")
        return "below_price", {"threshold": float(value)}
    if kind == "price_change":
        return "price_drop", {"min_percent": 0}
    if kind == "availability":
        keyword = str(value or "").strip()
        if keyword:
            return "regex", {"pattern": re.escape(keyword)}
        return "text_change", {}
    if kind == "new_listing":
        return "new_listings", {}
    if kind == "deadline":
        raise ValueError(
            "deadline is not a watch condition: it needs a scheduled reminder, "
            "not a price/availability watch")
    raise ValueError(f"unknown condition kind {kind!r} (known: {', '.join(_CONDITION_KINDS)})")


# ---------------------------------------------------------------------------
# Orchestration result
# ---------------------------------------------------------------------------

@dataclass
class OrchestrationResult:
    """One pipeline run. ``state`` uses the CONTRACTS4 §4 vocabulary."""
    state: str  # completed | partially_completed | blocked | failed | awaiting_input | awaiting_approval
    intent: str
    message: str
    mission_id: str | None = None
    job_id: str | None = None
    baseline: dict | None = None  # {"price","currency","source","observed_at"} or {"status": "baseline_pending", "reason": ...}
    steps: list[dict] = field(default_factory=list)  # [{"step","ok","detail"}]
    next_actions: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})


# ---------------------------------------------------------------------------
# Helpers shared by the pipelines
# ---------------------------------------------------------------------------

def _read_watch(mission_id: str, *, db_path: str | None = None) -> dict | None:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
    return dict(row) if row else None


def _resolve_product(objective: Any, context: Any) -> str | None:
    """Product identity: objective entities first, then the most
    recent product entity in working context (deictic "this")."""
    entities = _field(objective, "entities", {}) or {}
    product = entities.get("product")
    if product:
        return str(product)
    ctx_entities = _field(context, "entities", []) or []
    for ent in ctx_entities:
        if isinstance(ent, dict) and ent.get("kind") == "product" and ent.get("label"):
            return str(ent["label"])
    return None


def _resolve_source_url(objective: Any, context: Any) -> str | None:
    url = _field(objective, "target_url", None)
    if url:
        return str(url)
    ctx_entities = _field(context, "entities", []) or []
    for ent in ctx_entities:
        if isinstance(ent, dict) and ent.get("kind") in ("source", "result"):
            detail = ent.get("detail") or {}
            if isinstance(detail, dict) and detail.get("url"):
                return str(detail["url"])
    results = _field(context, "last_results", []) or []
    for res in results:
        if isinstance(res, dict) and res.get("url"):
            return str(res["url"])
    return None


# ---------------------------------------------------------------------------
# Monitor pipeline
# ---------------------------------------------------------------------------

async def orchestrate_monitor(objective: Any, context: Any = None, *,
                              chat_id: str | None = None,
                              db_path: str | None = None) -> OrchestrationResult:
    """Run the monitor pipeline for a structured objective.

    Steps (each receipted): resolve product identity -> discover
    sources -> validate identity/variant -> extract price + reference
    price (baseline) -> resolve condition -> create mission -> schedule
    recurring observations -> register notification conditions ->
    verify creation + baseline. A step that cannot be satisfied stops
    the pipeline with an honest state instead of guessing.
    """
    from .change_detector import validate_monitor_url
    from .continuous_monitoring_companion import upsert_monitor_mission
    from .monitor_check import fetch_observation
    from .change_detector import evaluate_observation

    intent = _objective_intent(objective) or "monitor_product"
    result = OrchestrationResult(state="failed", intent=intent, message="")
    chat = chat_id or _field(context, "chat_id", None) or "default"

    # 1. Resolve product identity.
    product = _resolve_product(objective, context)
    if not product:
        result.state = "awaiting_input"
        result.message = ("I don't have a product to watch yet — which product "
                          "should I watch? Tell me the product name or paste its link.")
        result.step("resolve_product_identity", False, "no product in objective or working context")
        result.next_actions = ["Reply with the product name or paste the product page link."]
        return result
    result.step("resolve_product_identity", True, product)

    # 2. Discover relevant public product sources.
    url = _resolve_source_url(objective, context)
    if not url:
        result.state = "awaiting_input"
        result.message = (f"I know the product ({product}) but I don't have a page to watch it on — "
                          "paste the product page link and I'll set up the watch.")
        result.step("discover_sources", False, "no target_url in objective or working context")
        result.next_actions = [f"Paste the product page link for {product}."]
        return result
    result.step("discover_sources", True, url)

    # 3. Validate identity / variant (URL gate).
    ok, gate_reason = validate_monitor_url(url)
    if not ok:
        dns_transient = bool(gate_reason and "does not resolve" in gate_reason)
        if not dns_transient:
            result.state = "failed"
            result.message = f"I can't watch {url} — {gate_reason}"
            result.step("validate_identity_variant", False, gate_reason or "URL refused")
            result.next_actions = ["Give me a public http(s) product page URL."]
            return result
        result.step("validate_identity_variant", True, f"DNS transient, will retry: {gate_reason}")
    else:
        result.step("validate_identity_variant", True, "URL passes the monitor gate")

    # 4. Resolve the discount condition — never substitute.
    try:
        resolved = resolve_condition(_field(objective, "condition", None))
    except ValueError as exc:
        result.state = "failed"
        result.message = str(exc)
        result.step("resolve_condition", False, str(exc))
        result.next_actions = ["State the condition plainly, e.g. 'below ₹5000' or 'when it is 10% off'."]
        return result
    if resolved is None:
        result.state = "awaiting_input"
        result.message = (f"When should I alert you about {product} — when the price falls "
                          "below a value, when it drops by a percent, or on any price change?")
        result.step("resolve_condition", False, "no condition in objective")
        result.next_actions = ["Say the condition, e.g. 'below ₹5000' or 'when it drops 10%'."]
        return result
    condition_type, condition_params = resolved
    if condition_type == "new_listings":
        return await _orchestrate_new_listings(objective, context, product, url,
                                               chat_id=chat, db_path=db_path)
    result.step("resolve_condition", True, f"{condition_type} {condition_params}")

    # 5. Cadence: structured interval, else the documented 300s default.
    interval_seconds = _field(objective, "interval_seconds", None)
    if interval_seconds is None:
        interval_seconds = 300
        result.step("schedule_recurring_observations", True, "no interval in objective — 300s default")
    else:
        interval_seconds = max(60, int(interval_seconds))
        result.step("schedule_recurring_observations", True, f"every {interval_seconds}s")
    watch_title = product if condition_type in ("text_change", "regex") else f"Price Watch: {product}"

    # 6. Create the persistent monitoring mission (shared core with
    # the NL handler — one creation path, not two).
    upsert = upsert_monitor_mission(
        product_title=product, watch_title=watch_title, target_url=url,
        trigger_type="interval", schedule_cron=None,
        interval_seconds=interval_seconds, condition_type=condition_type,
        condition_params={**condition_params, "target": product},
        chat_id=chat, db_path=db_path,
    )
    mission_id = upsert["mission_id"]
    result.mission_id = mission_id
    result.step("create_persistent_mission", True,
                f"{upsert['outcome']} mission {mission_id}")
    result.evidence["watch_id"] = mission_id
    result.evidence["expected"] = {
        "product": product, "target_url": url,
        "interval_seconds": interval_seconds,
        "condition_type": condition_type,
        "condition_params": {**condition_params, "target": product},
    }

    # 7. Baseline: one immediate read-only check. The page's own
    # product name must match the watched product (identity check);
    # the price becomes the baseline. Any failure leaves an honest
    # baseline_pending — never a claimed baseline.
    baseline: dict | None = None
    try:
        observation = await asyncio.to_thread(fetch_observation, url)
        page_name = (observation.get("product_name") or "").strip()
        if page_name and product.lower() not in page_name.lower() \
                and page_name.lower() not in product.lower():
            result.state = "blocked"
            result.message = (f"The page identifies its product as '{page_name}', not '{product}' — "
                              "the watch is saved but I have not taken a baseline from the wrong product.")
            result.step("extract_price_baseline", False, f"identity mismatch: page says {page_name!r}")
            result.next_actions = [
                "Check the link is the right product page, or correct the product name.",
                f"Say 'watch {page_name}' to watch the product this page actually sells.",
            ]
            result.baseline = {"status": "baseline_pending", "reason": "identity mismatch"}
            return result
        event = await asyncio.to_thread(
            evaluate_observation, mission_id, observation, db_path=db_path)
        price = observation.get("price")
        price_info = observation.get("price_info") or {}
        baseline = {
            "price": price,
            "currency": price_info.get("currency"),
            "source": price_info.get("source"),
            "product_name": page_name or None,
            "observed_at": time.time(),
            "alert_fired_on_baseline": bool(event),
        }
        result.step("extract_price_baseline", True,
                    f"baseline price {price} ({price_info.get('source') or 'no structured price'})")
    except Exception as exc:  # noqa: BLE001 — baseline failure is pending, not fatal
        baseline = {"status": "baseline_pending", "reason": str(exc)}
        result.step("extract_price_baseline", False, f"baseline pending: {exc}")
    result.baseline = baseline
    result.evidence["baseline"] = baseline

    # 8. Verify creation + baseline by re-read (CONTRACTS4 §3).
    from .completion_verifier import verify_objective_completion

    verdict = verify_objective_completion(
        {"primary_intent": intent, "entities": {"product": product}},
        result.evidence, db_path=db_path)
    if verdict.state == "completed":
        result.state = "completed"
        base_note = ""
        if isinstance(baseline, dict) and baseline.get("status") == "baseline_pending":
            base_note = (f" Baseline pending ({baseline.get('reason') or 'first check not run yet'}) — "
                         "the first scheduled check will record it.")
        price_note = ""
        if isinstance(baseline, dict) and baseline.get("price") is not None:
            price_note = f" Current price ₹{baseline['price']:g}."
        result.message = (
            f"Watch active for **{product}** — checking every {interval_seconds // 60} minute(s), "
            f"alerting on {condition_type.replace('_', ' ')}.{price_note}{base_note} "
            f"Watch ID: `{mission_id}`."
        )
        result.step("verify_creation_baseline", True, verdict.reason)
    else:
        result.state = verdict.state
        result.message = verdict.message
        result.step("verify_creation_baseline", False, verdict.reason)
        from .completion_verifier import suggest_next_actions

        result.next_actions = suggest_next_actions(verdict.reason, intent)
    return result


async def _orchestrate_new_listings(objective: Any, context: Any, product: str,
                                    url: str, *, chat_id: str,
                                    db_path: str | None) -> OrchestrationResult:
    """New-listings watch: same pipeline, different condition kind."""
    from .continuous_monitoring_companion import _create_new_listings_watch

    result = OrchestrationResult(state="failed", intent="monitor_new_listings", message="")
    result.step("resolve_product_identity", True, product)
    result.step("discover_sources", True, url)
    result.step("resolve_condition", True, "new_listings")
    reply = _create_new_listings_watch(product, url, chat_id, db_path)
    marker = re.search(r"privagent-watch:\s*([0-9a-f-]+)", reply or "")
    if not marker:
        result.state = "failed"
        result.message = reply or "Could not create the listings watch."
        result.step("create_persistent_mission", False, "no watch id in creation reply")
        result.next_actions = ["Paste the search-results page URL to watch."]
        return result
    mission_id = marker.group(1)
    result.mission_id = mission_id
    result.step("create_persistent_mission", True, f"created mission {mission_id}")
    row = _read_watch(mission_id, db_path=db_path)
    if row and row.get("status") == "active":
        result.state = "completed"
        result.message = reply
        result.baseline = {"status": "baseline_pending",
                           "reason": "first listing scan runs on the next tick"}
        result.step("verify_creation_baseline", True, "watch row active on re-read")
    else:
        result.state = "failed"
        result.message = "The listings watch did not persist — please retry."
        result.step("verify_creation_baseline", False, "watch row missing or not active on re-read")
        result.next_actions = ["Retry creating the watch, or paste the search page link again."]
    return result


# ---------------------------------------------------------------------------
# Schedule pipeline (create / update / cancel a scheduled task)
# ---------------------------------------------------------------------------

def orchestrate_schedule(objective: Any, *, chat_id: str | None = None,
                         db_path: str | None = None) -> OrchestrationResult:
    """Create (or update/cancel) a scheduled job from a structured
    objective, then verify by re-read. A scheduled job is NOT a watch:
    it writes only ``scheduled_jobs`` and never touches any monitor or
    email row.
    """
    from .scheduler import create_job, get_job, update_job, set_job_status

    intent = _objective_intent(objective) or "schedule_task"
    result = OrchestrationResult(state="failed", intent=intent, message="")
    entities = _field(objective, "entities", {}) or {}
    raw = _field(objective, "raw", "") or ""
    job_ref = entities.get("job_id") or entities.get("job_ref")

    if intent == "cancel_schedule":
        if not job_ref:
            result.state = "awaiting_input"
            result.message = "Which scheduled job should I cancel? Give me its name or ID."
            result.next_actions = ["Reply with the job name or ID to cancel."]
            return result
        job = get_job(str(job_ref), db_path=db_path)
        if job is None:
            result.state = "failed"
            result.message = f"No scheduled job {job_ref} exists."
            result.next_actions = ["List scheduled jobs to see their IDs, then cancel by ID."]
            return result
        set_job_status(str(job_ref), "cancelled", db_path=db_path)
        reread = get_job(str(job_ref), db_path=db_path)
        if reread and reread.get("status") == "cancelled":
            result.state = "completed"
            result.job_id = str(job_ref)
            result.message = f"Cancelled scheduled job {job_ref}."
            result.step("cancel_schedule", True, "status cancelled on re-read")
        else:
            result.state = "failed"
            result.message = "The cancel did not persist — the job is still active."
            result.next_actions = ["Retry cancelling the job."]
        return result

    if intent == "update_schedule":
        if not job_ref:
            result.state = "awaiting_input"
            result.message = "Which scheduled job should I change? Give me its name or ID."
            result.next_actions = ["Reply with the job name or ID to change."]
            return result
        spec = parse_interval(raw) or _interval_from_seconds(_field(objective, "interval_seconds", None))
        if spec is None:
            result.state = "awaiting_input"
            result.message = "What should the new schedule be — e.g. 'every 30 minutes' or 'daily at 9am'?"
            result.next_actions = ["Reply with the new cadence, e.g. 'every 30 minutes'."]
            return result
        try:
            updated = update_job(str(job_ref), schedule=spec.as_schedule_string(), db_path=db_path)
        except ValueError as exc:
            result.state = "failed"
            result.message = str(exc)
            result.next_actions = ["Reply with a supported cadence, e.g. 'every 30 minutes'."]
            return result
        if updated is None:
            result.state = "failed"
            result.message = f"No scheduled job {job_ref} exists."
            result.next_actions = ["List scheduled jobs to see their IDs, then update by ID."]
            return result
        result.state = "completed"
        result.job_id = str(job_ref)
        result.message = f"Updated scheduled job {job_ref} to {spec.display or spec.as_schedule_string()}."
        result.step("update_schedule", True, "job row updated on re-read")
        return result

    # schedule_task: create.
    instruction = entities.get("instruction") or raw
    if not instruction:
        result.state = "awaiting_input"
        result.message = "What should the scheduled task do?"
        result.next_actions = ["Say what the task should do, and when it should run."]
        return result
    spec = parse_interval(raw) or _interval_from_seconds(_field(objective, "interval_seconds", None))
    if spec is None:
        result.state = "awaiting_input"
        result.message = "When should it run — e.g. 'in 10 minutes', 'every hour', or 'daily at 9am'?"
        result.next_actions = ["Reply with the schedule, e.g. 'every hour' or 'daily at 9am'."]
        return result
    try:
        job = create_job(instruction, spec.as_schedule_string(), db_path=db_path)
    except ValueError as exc:
        result.state = "failed"
        result.message = str(exc)
        result.next_actions = ["Reply with a supported schedule, e.g. 'every 30 minutes'."]
        return result
    reread = get_job(job["id"], db_path=db_path)
    if reread and reread.get("next_run_at"):
        result.state = "completed"
        result.job_id = job["id"]
        result.message = (f"Scheduled: {instruction} — {spec.display or spec.as_schedule_string()}. "
                          f"Job ID: `{job['id']}`.")
        result.step("create_schedule", True, f"job {job['id']} next_run_at={reread['next_run_at']}")
        result.evidence["job_id"] = job["id"]
    else:
        result.state = "failed"
        result.message = "The scheduled job did not persist with a next run time."
        result.next_actions = ["Retry creating the scheduled task."]
    return result


def _interval_from_seconds(seconds: Any) -> IntervalSpec | None:
    if seconds is None:
        return None
    try:
        secs = max(60, int(seconds))
    except (TypeError, ValueError):
        return None
    return IntervalSpec(kind="interval", interval_seconds=secs,
                        display=f"every {secs // 60} minute(s)")


# ---------------------------------------------------------------------------
# Monitor-update pipeline (pause / resume / cancel / interval update)
# ---------------------------------------------------------------------------

def orchestrate_monitor_update(objective: Any, context: Any = None, *,
                               chat_id: str | None = None,
                               db_path: str | None = None) -> OrchestrationResult:
    """Pause, resume, cancel or re-interval a watch, verified by
    re-read. Updates ONLY the named watch row — no other watch, job
    or email setting is touched.
    """
    intent = _objective_intent(objective) or "update_monitor"
    result = OrchestrationResult(state="failed", intent=intent, message="")
    entities = _field(objective, "entities", {}) or {}
    watch_ref = entities.get("watch_id") or entities.get("watch_ref")
    row: dict | None = None
    if watch_ref:
        row = _read_watch(str(watch_ref), db_path=db_path)
    if row is None:
        product = entities.get("product") or _resolve_product(objective, context)
        if product:
            db.init_db(db_path)
            with db.get_connection(db_path) as conn:
                found = conn.execute(
                    """SELECT * FROM monitoring_missions WHERE conversation_id=? AND status='active'
                       AND (title LIKE ? OR title LIKE ?) ORDER BY created_at DESC LIMIT 1""",
                    (chat_id or "default", f"%{product}%", f"%Price Watch: {product}%"),
                ).fetchone()
            row = dict(found) if found else None
    # Stream 3 (S5 D10): a relative update names no product and no
    # watch ref — the objective carries only the factor. When exactly
    # one active watch exists it is the referent; several active
    # watches with no referent is ambiguous, never a guess.
    relative = entities.get("relative_interval")
    if row is None and intent == "update_monitor" and relative in ("twice_as_often", "half_as_often"):
        db.init_db(db_path)
        with db.get_connection(db_path) as conn:
            active = conn.execute(
                "SELECT * FROM monitoring_missions WHERE status='active' "
                "ORDER BY created_at DESC"
            ).fetchall()
        if len(active) == 1:
            row = dict(active[0])
        elif len(active) > 1:
            result.state = "awaiting_input"
            result.message = ("That matches more than one watch — tell me which one to check "
                              f"{'twice' if relative == 'twice_as_often' else 'half'} as often, "
                              "by name or id. No watch was changed.")
            result.next_actions = ["Reply with the product name or the watch ID."]
            return result
    if row is None:
        result.state = "awaiting_input"
        result.message = "Which watch should I change? Give me the product name or watch ID."
        result.next_actions = ["Reply with the product name or the watch ID."]
        return result
    mission_id = row["id"]

    if intent == "cancel_monitor":
        new_status = "cancelled"
    elif intent == "pause_monitor":
        new_status = "paused"
    elif intent == "resume_monitor":
        new_status = "active"
    elif intent == "update_monitor":
        new_status = row["status"]
    else:
        result.state = "failed"
        result.message = f"Unsupported monitor update intent {intent!r}."
        return result

    interval_seconds = _field(objective, "interval_seconds", None)
    # Stream 3 (S5 D10): a relative interval is a factor, never a
    # guessed number — the new interval is derived from the row's
    # CURRENT interval: twice as often halves it, half as often
    # doubles it (60 s floor). The row is updated in place; no new
    # watch is ever created for an update.
    target_interval: int | None = None
    old_interval = row.get("interval_seconds") or 300
    if intent == "update_monitor":
        if interval_seconds is not None:
            target_interval = max(60, int(interval_seconds))
        elif relative in ("twice_as_often", "half_as_often"):
            from .continuous_monitoring_companion import _relative_interval_seconds

            try:
                target_interval = _relative_interval_seconds(relative, old_interval)
            except ValueError:
                result.state = "failed"
                result.message = (f"Unknown relative interval {relative!r} — say the new "
                                  "interval directly, e.g. 'check every 30 minutes'.")
                return result
    with db.get_connection(db_path) as conn:
        if intent == "update_monitor" and target_interval is not None:
            conn.execute(
                """UPDATE monitoring_missions
                   SET trigger_type='interval', schedule_cron=NULL,
                       interval_seconds=?, next_run_at=? WHERE id=?""",
                (target_interval, time.time(), mission_id))
        else:
            conn.execute(
                "UPDATE monitoring_missions SET status=? WHERE id=?", (new_status, mission_id))
        conn.commit()
    reread = _read_watch(mission_id, db_path=db_path)
    ok = bool(reread) and (
        (reread.get("interval_seconds") == target_interval)
        if intent == "update_monitor" and target_interval is not None
        else reread.get("status") == new_status)
    if ok:
        result.state = "completed"
        result.mission_id = mission_id
        result.evidence["watch_id"] = mission_id
        if target_interval is not None:
            result.evidence["interval_seconds"] = target_interval
        verb = {"cancel_monitor": "Cancelled", "pause_monitor": "Paused",
                "resume_monitor": "Resumed", "update_monitor": "Updated"}[intent]
        if target_interval is not None:
            from .continuous_monitoring_companion import _describe_interval

            result.message = (
                f"{verb} the watch for {row.get('title') or mission_id} — it was checking "
                f"{_describe_interval(old_interval)}, now checking {_describe_interval(target_interval)}.")
        else:
            result.message = f"{verb} the watch for {row.get('title') or mission_id}."
        result.step(intent, True, "watch row matches on re-read")
    else:
        result.state = "failed"
        result.message = "The watch change did not persist — the stored row is unchanged."
        result.next_actions = ["Retry the change, or check the watch in the dashboard."]
    return result


# ---------------------------------------------------------------------------
# Email-polling configuration (guarded: monitor intents must never land here)
# ---------------------------------------------------------------------------

async def orchestrate_email_polling(objective: Any, *, chat_id: str | None = None,
                                   db_path: str | None = None) -> OrchestrationResult:
    """Change how often email is checked. This touches ONLY the
    ``email_monitoring_missions`` row, verified by re-read — and it
    REFUSES any objective whose intent is a monitor intent, so a
    product-watch interval can never silently rewrite email polling.
    """
    from .email_intelligence.mission_setup import (
        handle_email_interval_change, get_active_email_mission,
    )

    intent = _objective_intent(objective) or "configure_email_polling"
    result = OrchestrationResult(state="failed", intent=intent, message="")
    if intent in _MONITOR_INTENTS or intent in _SCHEDULE_INTENTS:
        result.state = "failed"
        result.message = (f"Refused: a {intent} objective must never change email polling. "
                          "Email polling is only changed by a configure_email_polling objective.")
        result.step("guard_monitor_vs_email", False, f"intent {intent} is not email polling")
        result.next_actions = ["Say 'check email every N minutes' to change email polling."]
        return result
    raw = _field(objective, "raw", "") or ""
    before = get_active_email_mission(chat_id, db_path=db_path)
    before_freq = before.get("frequency_seconds") if before else None
    reply = await handle_email_interval_change(raw, chat_id=chat_id, db_path=db_path)
    after = get_active_email_mission(chat_id, db_path=db_path)
    after_freq = after.get("frequency_seconds") if after else None
    if before is not None and after is not None and before_freq != after_freq:
        result.state = "completed"
        result.message = reply
        result.step("configure_email_polling", True,
                    f"frequency {before_freq} -> {after_freq} on re-read")
        result.evidence["mission_id"] = after["id"]
        result.evidence["frequency_seconds"] = after_freq
    elif "no active email monitoring mission" in reply.lower() or before is None:
        result.state = "awaiting_input"
        result.message = reply
        result.step("configure_email_polling", False, "no active email mission")
        result.next_actions = ["Set up email monitoring first, e.g. 'scan my mails and notify me for important mails'."]
    else:
        result.state = "failed"
        result.message = reply
        result.step("configure_email_polling", False, "stored frequency unchanged on re-read")
        result.next_actions = ["Retry, e.g. 'check email every 10 minutes'."]
    return result


# ---------------------------------------------------------------------------
# Dispatcher — one entry point for a structured objective
# ---------------------------------------------------------------------------

async def run_objective(objective: Any, context: Any = None, *,
                        chat_id: str | None = None,
                        db_path: str | None = None) -> OrchestrationResult:
    """Route a structured objective to its compositional pipeline.

    Intents with a pipeline here: monitor_*, update/pause/resume/
    cancel_monitor, schedule_task, update/cancel_schedule,
    configure_email_polling. Every other intent (research,
    simple_search, browser_task, artifact_request, mcp_task,
    connector_task, memory_*, greet, conversational, ...) keeps its
    existing handler — this dispatcher does NOT invent a pipeline for
    it and says so honestly.
    """
    intent = _objective_intent(objective)
    if intent in ("monitor_product", "monitor_website", "monitor_inbox",
                  "monitor_new_listings"):
        return await orchestrate_monitor(objective, context, chat_id=chat_id, db_path=db_path)
    if intent in ("update_monitor", "pause_monitor", "resume_monitor", "cancel_monitor"):
        return orchestrate_monitor_update(objective, context, chat_id=chat_id, db_path=db_path)
    if intent in _SCHEDULE_INTENTS:
        return orchestrate_schedule(objective, chat_id=chat_id, db_path=db_path)
    if intent in _EMAIL_POLLING_INTENTS:
        return await orchestrate_email_polling(objective, chat_id=chat_id, db_path=db_path)
    result = OrchestrationResult(
        state="failed", intent=intent or "unknown",
        message=(f"No compositional pipeline owns intent {intent!r} in this module — "
                 "it keeps its existing handler; nothing was executed here."))
    result.next_actions = ["Use the existing handler for this intent."]
    return result


# ---------------------------------------------------------------------------
# Email-settings isolation check (used by the regression test and by
# the monitor pipeline's own audit trail)
# ---------------------------------------------------------------------------

def snapshot_email_missions(*, db_path: str | None = None) -> dict[str, dict]:
    """{mission_id: {frequency_seconds, status, ...}} for every email
    mission — snapshot before a monitor creation, compare after."""
    from .db import list_email_missions

    return {m["id"]: {"frequency_seconds": m.get("frequency_seconds"),
                      "status": m.get("status"),
                      "account_email": m.get("account_email")}
            for m in list_email_missions(db_path=db_path)}


def email_missions_untouched(before: dict[str, dict], *,
                             db_path: str | None = None) -> tuple[bool, list[str]]:
    """True when every email mission in *before* is byte-identical
    now (same rows, same frequency/status). Returns (ok, diffs)."""
    after = snapshot_email_missions(db_path=db_path)
    diffs: list[str] = []
    if set(after) != set(before):
        diffs.append(f"email mission set changed: {sorted(set(before) ^ set(after))}")
    for mid, snap in before.items():
        now = after.get(mid)
        if now is None:
            continue
        for key, value in snap.items():
            if now.get(key) != value:
                diffs.append(f"email mission {mid[:8]}: {key} {value!r} -> {now.get(key)!r}")
    return (not diffs), diffs
