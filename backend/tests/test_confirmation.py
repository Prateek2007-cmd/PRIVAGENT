"""Confirmation gate: write actions must pause for human approval."""
import asyncio
from typing import Any

import pytest

from app import agentLoop


class ConfirmHub:
    """Fake extension hub whose planner-controlled confirm gate is exercised."""
    def __init__(self):
        self.executed: list[str] = []
        self.confirm_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.confirm_event = asyncio.Event()

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        await asyncio.sleep(0.02)
        self.executed.append(action["type"])
        return {"ok": True, "evidence": f"did {action['type']}"}


def test_write_action_requires_confirmation_then_approve():
    hub = ConfirmHub()
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "g", "steps": [{"why": "w", "action": {"type": "type", "risk": "write", "verify": "v", "timeoutMs": 100,
                                                                                      "target": {"targetId": "q"}, "params": {"text": "hi"}}}]}
            return {"goal": "g", "steps": []}

    async def scenario():
        async def approve_soon():
            await asyncio.sleep(0.2)
            hub.decision = True
            hub.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=Planner(),
                obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
                confirm_gate=hub,
            ),
            approve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    assert hub.confirm_requests and hub.confirm_requests[0]["action"]["type"] == "type"
    assert "type" in hub.executed


def test_write_action_rejected_fails_task():
    hub = ConfirmHub()

    class Planner:
        def __call__(self, i, p, h):
            if not hub.confirm_requests:
                return {"goal": "g", "steps": [{"why": "w", "action": {"type": "type", "risk": "write", "verify": "v", "timeoutMs": 100,
                                                                                      "target": {"targetId": "q"}, "params": {"text": "hi"}}}]}
            return {"goal": "g", "steps": []}

    async def scenario():
        async def reject_soon():
            await asyncio.sleep(0.2)
            hub.decision = False
            hub.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=Planner(),
                obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
                confirm_gate=hub,
            ),
            reject_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "failed"
    assert "rejected" in (state["summary"] or "")
    assert "type" not in hub.executed


def test_read_actions_skip_confirmation():
    hub = ConfirmHub()

    class Planner:
        def __call__(self, i, p, h):
            if not hub.executed:
                return {"goal": "g", "steps": [{"why": "look", "action": {"type": "scroll", "risk": "read", "verify": "scrolled", "timeoutMs": 100,
                                                                         "params": {"direction": "down"}}}]}
            return {"goal": "g", "steps": []}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
        confirm_gate=hub,
    ))
    assert state["final"] == "completed"
    assert hub.confirm_requests == []
