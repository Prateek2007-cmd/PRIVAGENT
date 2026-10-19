from typing import Literal

from pydantic import BaseModel


class TaskInput(BaseModel):
    instruction: str


class ConfirmInput(BaseModel):
    decision: Literal["approve", "reject"]


class ProgressEnvelope(BaseModel):
    type: str
    taskId: str
    payload: dict
