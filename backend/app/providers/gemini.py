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
import re
import time
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.privacy import PrivacyFilteredPayload
from .. import config
from ..planner import SYSTEM_FRAME, build_prompt

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


class GeminiProvider:
    DEFAULT_MODEL = "gemini-3.8-flash"
    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    def __init__(self, api_key: str, model: Optional[str] = None, timeout_s: float = 45.0) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model or getattr(config, "GEMINI_MODEL", "") or self.DEFAULT_MODEL
        self._timeout_s = timeout_s

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

        for attempt in range(5):
            curr_model = models_to_try[min(attempt, len(models_to_try) - 1)]
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
                return json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"PLAN_INVALID: Gemini returned non-JSON: {exc}") from exc
            except Exception as exc:  # transient network / quota
                last_exc = exc
                err_str = str(exc)
                is_429 = "429" in err_str or getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in err_str
                is_transient = is_429 or getattr(exc, "code", None) in (500, 502, 503) or "deadline" in err_str.lower()
                if not is_transient or attempt == 4:
                    raise

                if is_429:
                    delay = 18.0
                    match = re.search(r"retry in ([\d\.]+)s", err_str, re.IGNORECASE) or re.search(r"'retryDelay':\s*'(\d+)s'", err_str)
                    if match:
                        try:
                            delay = float(match.group(1)) + 1.0
                        except Exception:
                            delay = 18.0
                    # If we can switch to a fallback model, do so with a brief pause
                    if attempt + 1 < len(models_to_try) and models_to_try[attempt + 1] != curr_model:
                        time.sleep(1.0)
                    else:
                        time.sleep(min(delay, 20.0))
                else:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Gemini call failed after retries: {last_exc}")
