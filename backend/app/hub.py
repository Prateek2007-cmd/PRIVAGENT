"""Task registry + WebSocket hub.

Broadcasts TaskProgress to /ws/client, routes EXECUTE_ACTION to the single
extension socket on /ws/extension, and matches RESULTs back to waiting
loops via asyncio.Event per taskId.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket


@dataclass
class TaskRecord:
    task_id: str
    instruction: str
    state: str = "planning"
    steps_done: int = 0
    message: str = ""
    cancelled: bool = False
    pending_confirmation: Optional[dict[str, Any]] = None
    confirm_event: asyncio.Event = field(default_factory=asyncio.Event)
    confirm_decision: Optional[bool] = None
    screenshot: Optional[str] = None


class Hub:
    def __init__(self) -> None:
        self.extension_sockets: list[WebSocket] = []
        self.client_sockets: list[WebSocket] = []
        self.tasks: dict[str, TaskRecord] = {}
        self._action_results: dict[str, ActionResultSlot] = {}
        self._pending: list[tuple[str, str]] = []  # queued action ids while extension offline

    @property
    def extension_ws(self) -> Optional[WebSocket]:
        """Most recent live extension socket, or None."""
        for ws in reversed(self.extension_sockets):
            if ws in self.extension_sockets:
                return ws
        return None

    def set_extension(self, ws: Optional[WebSocket]) -> None:
        if ws is not None and ws not in self.extension_sockets:
            self.extension_sockets.append(ws)

    def drop_extension(self, ws: WebSocket) -> None:
        if ws in self.extension_sockets:
            self.extension_sockets.remove(ws)

    def new_task(self, instruction: str) -> TaskRecord:
        task = TaskRecord(task_id=uuid.uuid4().hex, instruction=instruction)
        self.tasks[task.task_id] = task
        return task

    async def send_action(self, task_id: str, action: dict[str, Any], timeout_s: float = 45.0):
        if not self.extension_sockets:
            for _ in range(30):
                await asyncio.sleep(0.1)
                if self.extension_sockets:
                    break
        if not self.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: Chrome extension not connected. Ensure NAVIGATOR extension is enabled at chrome://extensions")
        action_id = uuid.uuid4().hex
        slot = self._action_results.setdefault(action_id, ActionResultSlot())
        ws = self.extension_sockets[-1]
        try:
            await ws.send_json({
                "type": "EXECUTE_ACTION",
                "taskId": action_id,
                "payload": action,
            })
        except Exception as exc:
            self.drop_extension(ws)
            raise RuntimeError(f"EXTENSION_DISCONNECTED: send failed: {exc}") from exc
        try:
            result = await asyncio.wait_for(slot.future, timeout_s)
        except asyncio.TimeoutError:
            self._action_results.pop(action_id, None)
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "extension action timeout", "recoverable": True}}
        return result

    async def broadcast_progress(self, task: TaskRecord) -> None:
        payload = {
            "type": "PROGRESS",
            "taskId": task.task_id,
            "payload": {
                "taskId": task.task_id,
                "taskState": task.state,
                "stepsDone": task.steps_done,
                "message": task.message,
                "screenshot": task.screenshot,
            },
        }
        for ws in list(self.client_sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                self.client_sockets.remove(ws)

    def resolve_action(self, action_id: str, payload: dict[str, Any]) -> None:
        slot = self._action_results.get(action_id)
        if slot is not None:
            slot.future.set_result(payload)


class ActionResultSlot:
    def __init__(self) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()


hub = Hub()
