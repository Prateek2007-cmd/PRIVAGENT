"""Privacy layer — redacts sensitive values BEFORE any model payload exists.

Fail-closed: internal redactors raise through; classify_and_redact wraps only
the assembly step so a crash anywhere yields PrivacyViolation, never a
partially-redacted payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

from .contracts import BrowserObservation, InteractiveElement, PrivacyDecision, PrivacyViolation

import re

MAX_NAME_LEN = 120
SENSITIVE_INPUT_TYPES = {"password", "hidden"}
SENSITIVE_NAME_TOKENS = (
    "password", "passwd", "pwd", "token", "secret", "api key", "apikey",
    "credit card", "creditcard", "card number", "cvv", "cvc", "ssn", "social security",
    "pin", "auth", "bearer", "private key"
)

PATTERNS = {
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "api_key": re.compile(r"(?:bearer\s+[a-zA-Z0-9_\-\.]{16,}|(?:ghp|sk|AIza|gho|glpat|eyJ)[a-zA-Z0-9_\-\.]{15,})", re.IGNORECASE),
}


def _has_sensitive_value(val: str) -> bool:
    for pattern in PATTERNS.values():
        if pattern.search(val):
            return True
    return False


def _redact_element(el: InteractiveElement) -> InteractiveElement:
    """Pure per-element redactor. Raises through on bad input."""
    value: Optional[str] = el.value
    if el.input_type in SENSITIVE_INPUT_TYPES:
        value = None
    name_l = (el.name or "").lower()
    if any(tok in name_l for tok in SENSITIVE_NAME_TOKENS):
        value = None
    if value is not None and _has_sensitive_value(value):
        value = None
    return el.model_copy(update={
        "value": value,
        "name": (el.name or "")[:MAX_NAME_LEN],
    })


@dataclass
class PrivacyFilteredPayload:
    """Sanitized observation ready for the planner, plus provenance counts."""
    tab_id: int
    url: str
    title: str
    interactive: list[InteractiveElement]
    scroll: dict
    dpr: float
    elements_original: int
    elements_sent: int
    screenshot: Optional[str] = None

    def to_context_block(self) -> str:
        """Render as an UNTRUSTED-DATA block for the planner prompt."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        def _pri(e: InteractiveElement) -> int:
            nm = (e.name or "").lower()
            if any(k in nm for k in ("search", "type a message", "send", "skip", "close", "submit", "groups", "group", "cancel search")):
                return 0
            if e.role in ("listitem", "row") and e.name:
                return 1
            if e.role in ("textbox", "input", "button", "ad-skip"):
                return 2
            if e.name:
                return 3
            return 4

        sorted_els = sorted(self.interactive, key=_pri)
        for el in sorted_els[:35]:
            bits = [f"id={el.target_id}", el.role, f'name="{el.name}"', "disabled" if not el.enabled else ""]
            if el.bounds:
                bits.append(f'bounds=[{int(el.bounds.get("x", 0))},{int(el.bounds.get("y", 0))},{int(el.bounds.get("width", 0))}x{int(el.bounds.get("height", 0))}]')
            if el.value is not None:
                bits.append(f'value="{el.value}"')
            lines.append("- " + " ".join(b for b in bits if b))
        return "\n".join(lines)


def classify_and_redact(
    obs: BrowserObservation, *, instruction: str
) -> tuple[PrivacyFilteredPayload, PrivacyDecision]:
    _ = instruction  # reserved: relevance scoring arrives with the planner
    try:
        redacted = [_redact_element(e) for e in obs.interactive]
        # Fail-closed privacy: if any element on page is sensitive (password/auth/card), omit screenshot
        has_sensitive_element = any(
            e.input_type in SENSITIVE_INPUT_TYPES or any(tok in (e.name or "").lower() for tok in SENSITIVE_NAME_TOKENS)
            for e in obs.interactive
        )
        safe_screenshot = None if has_sensitive_element else getattr(obs, "screenshot", None)
        payload = PrivacyFilteredPayload(
            tab_id=obs.tab_id,
            url=obs.url,
            title=obs.title,
            interactive=redacted,
            scroll=obs.scroll,
            dpr=obs.dpr,
            elements_original=len(obs.interactive),
            elements_sent=len(redacted),
            screenshot=safe_screenshot,
        )
        decision = PrivacyDecision(send=True, reason="redaction succeeded", violations=[])
        return payload, decision
    except Exception as exc:  # fail-closed: any filter crash blocks the send
        raise PrivacyViolation(f"privacy filter failed; payload NOT sent: {exc}") from exc