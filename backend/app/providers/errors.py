"""Provider-level failures that the control loop can report honestly."""
from __future__ import annotations


class ProviderUnavailable(RuntimeError):
    """Every configured planner provider is exhausted or unreachable.

    Raised instead of sleeping through a long rate-limit window. The message is
    written for the operator: it names the provider and the retry time.
    """
