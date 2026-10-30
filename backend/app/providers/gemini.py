"""Gemini provider — real model calls via google-genai.

Default is the current stable Flash model (gemini-3.8-flash) with low thinking
for planning latency. Fallbacks stay on live 3.x models: 2.0-flash is shut
down and 2.5-flash is no longer available to new callers.

Retries transient failures (429/5xx) with backoff and cascades across models.
Never logs the key; errors surfaced as exceptions for the loop to handle.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.privacy import PrivacyFilteredPayload
from .. import config
from ..planner import SYSTEM_FRAME, build_prompt
from .errors import ProviderUnavailable
from .limits import parse_retry_seconds, quota_scope

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
_MAX_PAUSE_S = 2.0

# model -> monotonic unblock time. Avoids re-probing a model that just told us
# its quota is exhausted (each probe also burns the shared request budget).
_MODEL_BLOCKED_UNTIL: dict[str, float] = {}


def _model_blocked(model: str) -> bool:
    return _MODEL_BLOCKED_UNTIL.get(model, 0.0) > time.monotonic()


def _block_model(model: str, seconds: float) -> None:
    _MODEL_BLOCKED_UNTIL[model] = time.monotonic() + max(seconds, 30.0)


class GeminiProvider:
    DEFAULT_MODEL = "gemini-3.8-flash"
    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    def __init__(self, api_key: str, model: Optional[str] = None, timeout_s: float = 45.0) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model or getattr(config, "GEMINI_MODEL", "") or self.DEFAULT_MODEL
        self._timeout_s = timeout_s
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: Optional[int] = None

    def name(self) -> str:
        return f"gemini:{self._model}"

    def plan_call(self, instruction: str, payload: PrivacyFilteredPayload, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        prompt = f"{SYSTEM_FRAME}\n\n{build_prompt(instruction, payload, history, working_memory=working_memory)}"
        contents: list[Any] = []
        # Defense in depth: privacy.py already drops screenshots unless the
        # operator opted in. Re-check here so a caller that bypasses the
        # privacy layer still cannot ship a rendered page to a remote model.
        if getattr(config, "ALLOW_SCREENSHOTS", False) and getattr(payload, "screenshot", None):
            try:
                s_data = payload.screenshot
                mime = "image/jpeg"
                if s_data.startswith("data:"):
                    header, _, s_data = s_data.partition(",")
                    mime = header[5:].split(";", 1)[0] or mime
                if mime not in ALLOWED_IMAGE_MIMES:
                    raise ValueError(f"unsupported image mime {mime!r}")
                img_bytes = base64.b64decode(s_data, validate=True)
                if 0 < len(img_bytes) <= MAX_SCREENSHOT_BYTES:
                    contents.append(gtypes.Part.from_bytes(data=img_bytes, mime_type=mime))
            except Exception:
                # Invalid/oversized image: send text only, never the raw blob.
                pass
        contents.append(prompt)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None
        quota_hint = ""

        available = [m for m in models_to_try if not _model_blocked(m)]
        if not available:
            wait_s = min(
                _MODEL_BLOCKED_UNTIL.get(m, 0.0) - time.monotonic() for m in models_to_try
            )
            raise ProviderUnavailable(
                f"Gemini quota exhausted; retry in about {int(wait_s // 60) + 1}m."
            )

        for index, curr_model in enumerate(available):
            started = time.perf_counter()
            try:
                # Flash 3.8 defaults to a higher thinking budget than a bounded
                # JSON plan needs; "low" keeps the schema while cutting tokens.
                tuning: dict[str, Any] = {}
                if curr_model.startswith("gemini-3.8"):
                    tuning["thinking_config"] = gtypes.ThinkingConfig(thinking_level="low")
                resp = self._client.models.generate_content(
                    model=curr_model,
                    contents=contents if len(contents) > 1 else prompt,
                    config=gtypes.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                        max_output_tokens=2048,
                        **tuning,
                    ),
                )
                text = (resp.text or "").strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    text = "\n".join(lines).strip()
                usage = getattr(resp, "usage_metadata", None)
                if usage is not None:
                    self.last_usage = {
                        "prompt_tokens": getattr(usage, "prompt_token_count", None),
                        "completion_tokens": getattr(usage, "candidates_token_count", None),
                        "total_tokens": getattr(usage, "total_token_count", None),
                    }
                self.last_latency_ms = int((time.perf_counter() - started) * 1000)
                return json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"PLAN_INVALID: Gemini returned non-JSON: {exc}") from exc
            except Exception as exc:  # transient network / quota
                last_exc = exc
                err_str = str(exc)
                is_429 = "429" in err_str or getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in err_str
                is_transient = is_429 or getattr(exc, "code", None) in (500, 502, 503) or "deadline" in err_str.lower()
                if not is_transient:
                    raise

                retry_in = parse_retry_seconds(err_str)
                daily = quota_scope(err_str) == "day"

                if is_429:
                    if retry_in >= 30 or daily:
                        hint = retry_in or 3600.0
                        quota_hint = (
                            f"Gemini quota exhausted; retry in about "
                            f"{int(round(hint / 60)) or 1}m."
                        )
                    # Never sleep through a long window: the next model (or the
                    # caller's fallback) is faster than waiting 18-20 seconds.
                    if retry_in >= 5 or daily:
                        _block_model(curr_model, retry_in or 3600.0)
                    if index + 1 < len(available):
                        if retry_in < 5 and not daily:
                            time.sleep(min(retry_in or 1.0, 2.0))
                        continue
                else:
                    time.sleep(min(2.0 * (index + 1), _MAX_PAUSE_S))

        raise ProviderUnavailable(
            quota_hint or f"Gemini call failed after retries: {last_exc}"
        ) from last_exc
