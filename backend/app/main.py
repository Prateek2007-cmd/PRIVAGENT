from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any, Optional

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .hub import Hub, hub
from .schemas import TaskInput, ConfirmInput

app = FastAPI(title="NAVIGATOR backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskCreated(BaseModel):
    task_id: str = Field(alias="taskId")
    model_config = {"populate_by_name": True}


@app.post("/api/task", response_model=TaskCreated)
async def create_task(body: TaskInput) -> TaskCreated:
    task = hub.new_task(body.instruction)
    task.state = "planning"
    asyncio.create_task(_run_task_later(task.task_id))
    return TaskCreated(taskId=task.task_id)


async def _run_task_later(task_id: str) -> None:
    await asyncio.sleep(0.08)
    try:
        from .agentLoop import run_task
        await run_task(task_id)
    except Exception as exc:  # pragma: no cover — task-level isolation
        task = hub.tasks.get(task_id)
        if task:
            task.state = "failed"
            task.message = f"backend error: {exc}"
            await hub.broadcast_progress(task)


@app.get("/api/task/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    task = hub.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return {
        "taskId": task.task_id,
        "instruction": task.instruction,
        "taskState": task.state,
        "stepsDone": task.steps_done,
        "message": task.message,
        "pending_confirmation": task.pending_confirmation,
        "screenshot": task.screenshot,
    }


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    task.cancelled = True
    task.state = "cancelled"
    task.confirm_event.set()
    await hub.broadcast_progress(task)
    return {"taskState": "cancelled"}


@app.post("/api/task/{task_id}/confirm")
async def confirm_task(task_id: str, body: ConfirmInput) -> dict[str, str]:
    task = _require(task_id)
    if task.state != "awaiting_confirmation":
        raise HTTPException(409, f"task is {task.state}, not awaiting_confirmation")
    task.confirm_decision = body.decision == "approve"
    task.confirm_event.set()
    return {"received": "ok"}


def _require(task_id: str):
    task = hub.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@app.websocket("/ws/extension")
async def ws_extension(ws: WebSocket) -> None:
    await ws.accept()
    hub.set_extension(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "RESULT":
                hub.resolve_action(msg["taskId"], msg.get("payload", {}))
    except WebSocketDisconnect:
        hub.drop_extension(ws)
    except Exception:
        hub.drop_extension(ws)
        raise


@app.websocket("/ws/client")
async def ws_client(ws: WebSocket) -> None:
    await ws.accept()
    hub.client_sockets.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in hub.client_sockets:
            hub.client_sockets.remove(ws)
