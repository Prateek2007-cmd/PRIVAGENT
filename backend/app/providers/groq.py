"""Groq provider — ultra-fast LPU inference via OpenAI-compatible endpoint.

Models: openai/gpt-oss-120b, openai/gpt-oss-20b, qwen/qwen3.8-27b.
Retries transient failures (429/5xx) and auto-cascades to fallback models.
Never logs the API key.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx

from app.privacy import PrivacyFilteredPayload
from ..planner import SYSTEM_FRAME, build_prompt


class GroqProvider:
    FALLBACK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-120b", timeout_s: float = 45.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._client = httpx.Client(timeout=timeout_s)

    def name(self) -> str:
        return f"groq:{self._model}"

    def plan_call(
        self,
        instruction: str,
        payload: PrivacyFilteredPayload,
        history: list[dict[str, Any]],
        working_memory: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(instruction, payload, history, working_memory=working_memory)
        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None

        for attempt in range(len(models_to_try) + 1):
            curr_model = models_to_try[min(attempt, len(models_to_try) - 1)]
            try:
                resp = self._client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": curr_model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_FRAME},
                            {"role": "user", "content": prompt},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1,
                    },
                )
                if resp.status_code != 200:
                    err_text = resp.text
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_exc = RuntimeError(f"Groq {resp.status_code}: {err_text}")
                        delay = 4.0
                        match = re.search(r"try again in ([\d\.]+)s", err_text, re.IGNORECASE)
                        if match:
                            try:
                                delay = float(match.group(1)) + 0.8
                            except Exception:
                                delay = 4.0
                        elif "retry-after" in resp.headers:
                            try:
                                delay = float(resp.headers["retry-after"]) + 0.5
                            except Exception:
                                delay = 4.0
                        time.sleep(min(delay, 8.0))
                        continue
                    raise RuntimeError(f"Groq API error {resp.status_code}: {err_text}")

                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                text = (content or "").strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    text = "\n".join(lines).strip()
                return json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"PLAN_INVALID: Groq returned non-JSON: {exc}") from exc
            except Exception as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))

        # Seamless fallback to Gemini if configured
        from .. import config
        if config.GEMINI_API_KEY:
            try:
                from .gemini import GeminiProvider
                fallback_provider = GeminiProvider(api_key=config.GEMINI_API_KEY)
                return fallback_provider.plan_call(instruction, payload, history, working_memory=working_memory)
            except Exception as gemini_exc:
                raise RuntimeError(f"Groq failed ({last_exc}) and Gemini fallback failed ({gemini_exc})")

        raise RuntimeError(f"Groq call failed after retries: {last_exc}")
