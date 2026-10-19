"""Bounded agent control loop: observe → plan → validate → act → verify.

Injectable dependencies (hub, planner, obs_factory) so tests drive the loop
without Chrome. Production wiring (main.py) passes the real hub and planner.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from . import config
from .contracts import PrivacyViolation
from .hub import Hub, hub as real_hub, TaskRecord
from .planner import plan_from_observation
from .privacy import classify_and_redact

MAX_STEPS = config.MAX_STEPS
MAX_RETRIES = config.MAX_RETRIES
TASK_TIMEOUT_S = config.TASK_TIMEOUT_S

_cancelled: set[str] = set()


def request_cancel(task_id: str) -> None:
    _cancelled.add(task_id)


def _noop_progress(record: TaskRecord | dict[str, Any], *_: Any) -> None:
    pass


async def run_with_injection(
    ext_hub: Any,
    *,
    planner: Callable[[str, dict[str, Any], list[dict[str, Any]]], dict[str, Any]],
    obs_factory: Callable[[], dict[str, Any]],
    task_id: str = "test-task",
    on_progress: Optional[Callable[[], None]] = None,
    confirm_gate: Optional[Any] = None,
    confirm_timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Test entrypoint mirroring run_task's flow with injectable parts."""
    _cancelled.discard(task_id)
    history: list[dict[str, Any]] = []
    penalties: list[str] = []
    working_memory: dict[str, Any] = {}
    steps_done = 0
    start = time.monotonic()

    def progress(msg: str, state: str = "running", screenshot: Optional[str] = None) -> None:
        if on_progress:
            try:
                on_progress(steps_done, msg, screenshot=screenshot)
            except TypeError:
                try:
                    on_progress(steps_done, msg)
                except TypeError:
                    on_progress()

    while steps_done < MAX_STEPS:
        if task_id in _cancelled:
            return {"final": "cancelled", "steps": steps_done, "summary": None}
        if time.monotonic() - start > TASK_TIMEOUT_S:
            return {"final": "budget_exceeded", "steps": steps_done, "summary": "timeout"}

        _maybe = obs_factory()
        raw_obs = await _maybe if asyncio.iscoroutine(_maybe) else _maybe
        try:
            payload, _decision = classify_and_redact(_to_obs(raw_obs), instruction="loop")
        except PrivacyViolation:
            return {"final": "failed", "steps": steps_done, "summary": "privacy violation — payload not sent"}

        h_view = _history_view(history, penalties, working_memory)
        try:
            raw_plan = planner("loop", payload.model_dump() if hasattr(payload, "model_dump") else payload, h_view, working_memory=working_memory)
        except TypeError:
            raw_plan = planner("loop", payload.model_dump() if hasattr(payload, "model_dump") else payload, h_view)

        new_notes = raw_plan.get("notes")
        if new_notes:
            working_memory["notes"] = str(new_notes)

        steps = raw_plan.get("steps", [])
        if not steps:
            final_ans = raw_plan.get("answer") or raw_plan.get("summary")
            summary_msg = final_ans if final_ans else _summary(raw_obs, history)
            return {"final": "completed", "steps": steps_done,
                    "summary": summary_msg}

        action = steps[0]["action"]

        # Loop prevention & WhatsApp / chat message auto-completion:
        if history and history[-1].get("result_ok"):
            last_act = history[-1].get("action", {})
            last_target_str = str(last_act.get("target", {})).lower()
            is_prev_composer_submit = (
                last_act.get("type") == "type"
                and bool(last_act.get("params", {}).get("submit"))
                and any(w in last_target_str for w in ("type a message", "message", "composer", "conversation"))
            )
            if is_prev_composer_submit:
                new_act_type = action.get("type")
                new_target_str = str(action.get("target", {})).lower()
                if new_act_type == "type" or any(w in new_target_str for w in ("send", "voice", "ptt")):
                    notes_summary = working_memory.get("notes") or "Message successfully sent to recipient."
                    return {
                        "final": "completed",
                        "steps": steps_done,
                        "summary": f"Completed: {notes_summary[:160]}"
                    }

            def _actions_match(a1: dict[str, Any], a2: dict[str, Any]) -> bool:
                if a1.get("type") != a2.get("type"):
                    return False
                t1 = a1.get("target") or {}
                t2 = a2.get("target") or {}
                p1 = a1.get("params") or {}
                p2 = a2.get("params") or {}
                if t1 and t2:
                    if t1.get("name") and t1.get("name") == t2.get("name"):
                        return True
                    if t1.get("targetId") and t1.get("targetId") == t2.get("targetId"):
                        return True
                if p1.get("text") and p1.get("text") == p2.get("text"):
                    return True
                if p1.get("url") and p1.get("url") == p2.get("url"):
                    return True
                return False

            consecutive_repeats = 0
            for h in reversed(history):
                if h.get("result_ok") and _actions_match(h.get("action", {}), action):
                    consecutive_repeats += 1
                else:
                    break

            if consecutive_repeats >= 3 and action.get("type") != "scroll":
                return {
                    "final": "budget_exceeded",
                    "steps": steps_done,
                    "summary": f"Loop detected: action '{action.get('type')}' repeated {consecutive_repeats + 1} times without progress."
                }
            elif consecutive_repeats >= 1 and action.get("type") != "scroll":
                penalties.append(f"loop warning: action '{action.get('type')}' was already completed. Do not repeat it; proceed to next step or conclude.")

        raw_url = str(getattr(raw_obs, "url", None) or (raw_obs.get("url") if isinstance(raw_obs, dict) else "")).lower()
        is_auth_page = any(x in raw_url for x in ("signin", "login", "ap/signin", "authenticate", "auth", "otp", "checkout/pay"))
        is_wa_qr = "whatsapp.com" in raw_url and any("qr" in str(getattr(e, "name", "")).lower() or "scan" in str(getattr(e, "name", "")).lower() for e in (getattr(raw_obs, "interactive", []) or []))
        is_strict = getattr(confirm_gate, "strict_write_confirm", True) if confirm_gate is not None else False
        needs_confirm = action.get("risk") == "sensitive" or is_auth_page or is_wa_qr or (
            is_strict and action.get("risk") == "write" and action.get("type") not in ("navigate", "scroll", "zoom", "wait", "observe", "select_tab")
        )
        if confirm_gate is not None and needs_confirm:
            gate_msg = (
                "WhatsApp Web requires QR code scan. Please scan the QR code in your WhatsApp tab to log in, then click Continue."
                if is_wa_qr else (
                    "Site requires Sign-In or Payment Credentials. Please sign in or complete authentication in your browser tab, then click Continue."
                    if is_auth_page else None
                )
            )
            approved = await _request_confirmation(confirm_gate, action, task_id, confirm_timeout_s, custom_message=gate_msg)
            if approved is False:
                return {"final": "failed", "steps": steps_done,
                        "summary": f"user rejected action: {action.get('type')}"}
        result = await _execute_with_retries(ext_hub, action)
        steps_done += 1
        evidence = result.get("evidence")
        if not evidence:
            if result.get("ok"):
                evidence = "ok"
            else:
                err_msg = result.get("error", {}).get("message", "failed")
        raw_screenshot = raw_obs.get("screenshot") if isinstance(raw_obs, dict) else getattr(raw_obs, "screenshot", None)
        progress(f"Step {steps_done}: {action.get('type')} -> {evidence}", screenshot=raw_screenshot)

        entry = {"action": action, "result_ok": result.get("ok"), "evidence": result.get("evidence")}
        if not result.get("ok"):
            err = result.get("error", {})
            entry["error"] = err.get("message", "unknown")
            if not err.get("recoverable", False):
                return {"final": "failed", "steps": steps_done,
                        "summary": f"non-recoverable error: {err.get('message')}"}
            penalty = _same_failure(history, entry)
            if penalty:
                penalties.append(penalty)
        history.append(entry)

        if steps_done >= MAX_STEPS:
            break

    if task_id in _cancelled:
        return {"final": "cancelled", "steps": steps_done, "summary": None}
    final_obs = obs_factory()
    if asyncio.iscoroutine(final_obs):
        try:
            final_obs = await final_obs
        except Exception:
            final_obs = {}
    return {"final": "budget_exceeded", "steps": steps_done, "summary": _summary(final_obs, history)}


async def _request_confirmation(gate: Any, action: dict[str, Any], task_id: str, timeout_s: float, custom_message: Optional[str] = None) -> bool:
    """Pause and await a human decision. Returns True if approved.
    gate contract: confirm_requests list + asyncio.Event confirm_event + decision:bool|None."""
    req = {
        "taskId": task_id,
        "action": action,
        "message": custom_message or f"NAVIGATOR wants to {action.get('type')} (risk {action.get('risk')})"
    }
    if hasattr(gate, "add_request"):
        gate.add_request(req)
    else:
        gate.confirm_requests.append(req)
    gate.decision = None
    gate.confirm_event.clear()
    try:
        await asyncio.wait_for(gate.confirm_event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return bool(gate.decision)


def _summary(obs: Any, history: list[dict[str, Any]]) -> str:
    last_ok = next((h.get("evidence") for h in reversed(history) if h.get("result_ok") and h.get("evidence")), None)
    url = getattr(obs, "url", None) or (obs.get("url") if isinstance(obs, dict) else "")
    return f"final url: {url} | last evidence: {last_ok or 'none'}"


def _same_failure(history: list[dict[str, Any]], entry: dict[str, Any]) -> Optional[str]:
    for prev in reversed(history):
        if prev.get("result_ok"):
            return None
        if prev.get("error") == entry.get("error") and prev["action"].get("type") == entry["action"].get("type"):
            return f"repeated failure: {entry['error']} on {entry['action'].get('type')}"
    return None


def _history_view(history: list[dict[str, Any]], penalties: list[str], working_memory: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    out = list(history[-5:])
    if working_memory and working_memory.get("notes"):
        out.append({"working_memory": working_memory["notes"]})
    for p in penalties[-3:]:
        out.append({"penalty": p})
    return out


async def _execute_with_retries(ext_hub: Any, action: dict[str, Any]) -> dict[str, Any]:
    act = dict(action)
    if "timeoutMs" not in act:
        act["timeoutMs"] = act.get("timeout_ms", 10000)
    for attempt in range(MAX_RETRIES + 1):
        res = await ext_hub.send_action("task", act)
        if res.get("ok"):
            return res
        err = res.get("error", {})
        if not err.get("recoverable", False) or attempt == MAX_RETRIES:
            return res
        await asyncio.sleep(0.2 * (attempt + 1))
    return res


def _to_obs(raw: dict[str, Any]):
    from .contracts import BrowserObservation
    if isinstance(raw, BrowserObservation):
        return raw
    return BrowserObservation.model_validate(raw)


async def run_task(task_id: str) -> None:
    """Production entrypoint — wires real hub, real planner, live observation."""
    record: TaskRecord = real_hub.tasks[task_id]

    def on_step_progress(steps: int = 0, msg: str = "", screenshot: Optional[str] = None) -> None:
        record.steps_done = steps
        record.message = msg
        record.state = "running"
        if screenshot:
            record.screenshot = screenshot
        asyncio.create_task(real_hub.broadcast_progress(record))

    try:
        state = await run_with_injection(
            real_hub,
            planner=_real_planner(record),
            obs_factory=_live_observation_factory(record),
            task_id=task_id,
            on_progress=on_step_progress,
            confirm_gate=_ConfirmGate(record),
        )
        final_state = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
                       "budget_exceeded": "failed"}.get(state["final"], "failed")
        record.state = final_state  # type: ignore[assignment]
        record.message = state.get("summary") or ""
        await real_hub.broadcast_progress(record)
    except Exception as exc:
        record.state = "failed"  # type: ignore[assignment]
        record.message = f"loop error: {exc}"
        await real_hub.broadcast_progress(record)


class _ConfirmGate:
    """Bridges the loop's confirm contract onto the Hub TaskRecord a REST
    client posts to. Event + decision live on the record itself."""
    def __init__(self, record: TaskRecord) -> None:
        self._record = record
        self.confirm_requests = []
        self.strict_write_confirm = False

    @property
    def confirm_event(self) -> asyncio.Event:
        return self._record.confirm_event

    @property
    def decision(self) -> Optional[bool]:
        return self._record.confirm_decision

    @decision.setter
    def decision(self, v: Optional[bool]) -> None:
        self._record.confirm_decision = v

    def add_request(self, req: dict[str, Any]) -> None:
        self.confirm_requests.append(req)
        self._record.pending_confirmation = req
        self._record.state = "awaiting_confirmation"
        self._record.message = req.get("message", "Awaiting confirmation")
        asyncio.create_task(real_hub.broadcast_progress(self._record))


class _LiveObservation:
    """Observation factory: asks the extension for the active tab view each
    round. Raises if the extension socket is gone (loop fails cleanly)."""
    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    async def __call__(self) -> dict[str, Any]:
        if not real_hub.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: no extension socket")
        for attempt in range(2):
            res = await real_hub.send_action(self._task_id, {
                "type": "observe", "risk": "read", "verify": "fresh observation", "timeoutMs": 25000,
            })
            if res.get("ok") and res.get("observation"):
                return res["observation"]
            if attempt == 0:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"observation failed: {res.get('error')}")


def _live_observation_factory(record: TaskRecord) -> _LiveObservation:
    return _LiveObservation(record.task_id)


def _real_planner(record: TaskRecord):
    from . import config
    from .providers.mock import MockProvider
    instruction = record.instruction
    if config.PROVIDER == "groq" and config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        provider = GroqProvider(api_key=config.GROQ_API_KEY)
    elif config.PROVIDER == "gemini" and config.GEMINI_API_KEY:
        from .providers.gemini import GeminiProvider
        provider = GeminiProvider(api_key=config.GEMINI_API_KEY)
    elif config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        provider = GroqProvider(api_key=config.GROQ_API_KEY)
    elif config.GEMINI_API_KEY:
        from .providers.gemini import GeminiProvider
        provider = GeminiProvider(api_key=config.GEMINI_API_KEY)
    else:
        provider = MockProvider()

    def planner(_instr: str, payload: Any, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None):
        from .planner import plan_from_observation
        from .privacy import PrivacyFilteredPayload
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        return plan_from_observation(instruction=instruction, payload=pf, provider=provider, history=history, working_memory=working_memory).model_dump(by_alias=True)

    return planner

