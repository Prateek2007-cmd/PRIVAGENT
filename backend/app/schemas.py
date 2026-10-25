from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import config


class TaskInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=config.MAX_INSTRUCTION_CHARS)

    @field_validator("instruction")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction must not be blank")
        return value


class ConfirmInput(BaseModel):
    decision: Literal["approve", "reject"]


class ProgressEnvelope(BaseModel):
    type: str
    taskId: str
    payload: dict
