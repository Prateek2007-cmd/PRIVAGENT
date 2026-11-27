"""CONTRACTS4 §3 named entry point for the completion verifier.

There is exactly ONE verifier implementation: it lives in
:mod:`app.completion_verifier` (already called by ``agentLoop`` at its
three completion sites). This module is a thin alias that re-exports
it under the CONTRACTS4 §3 name so future streams find it — no second
implementation, no duplicated criteria.
"""
from __future__ import annotations

from .completion_verifier import (  # noqa: F401
    TASK_STATES,
    VerificationResult,
    _verify_monitor_completion,
    suggest_next_actions,
    verify_objective_completion,
    verify_task_completion,
)

__all__ = [
    "TASK_STATES",
    "VerificationResult",
    "suggest_next_actions",
    "verify_objective_completion",
    "verify_task_completion",
]
