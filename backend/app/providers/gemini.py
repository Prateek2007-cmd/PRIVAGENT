"""Gemini provider — real model calls via google-genai.

Retries transient failures (429/5xx) up to 3 times with 2s backoff.
Never logs the key; errors surfaced as exceptions for the loop to handle.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from google import genai
from google.genai import types as gtypes

from app.privacy import PrivacyFilteredPayload
from ..planner import SYSTEM_FRAME, build_prompt


class GeminiProvider:
    FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout_s: float = 45.0) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._timeout_s = timeout_s

    def name(self) -> str:
        return f"gemini:{self._model}"

    def plan_call(self, instruction: str, payload: PrivacyFilteredPayload, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        prompt = f"{SYSTEM_FRAME}\n\n{build_prompt(instruction, payload, history, working_memory=working_memory)}"
        contents: list[Any] = []
        if getattr(payload, "screenshot", None):
            try:
                import base64
                s_data = payload.screenshot
                if "," in s_data:
                    s_data = s_data.split(",", 1)[1]
                img_bytes = base64.b64decode(s_data)
                contents.append(gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
            except Exception:
                pass
        contents.append(prompt)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None

        for attempt in range(5):
            curr_model = models_to_try[min(attempt, len(models_to_try) - 1)]
            try:
                resp = self._client.models.generate_content(
                    model=curr_model,
                    contents=contents if len(contents) > 1 else prompt,
                    config=gtypes.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                        max_output_tokens=2048,
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
