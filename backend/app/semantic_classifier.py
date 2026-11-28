"""Provider-backed semantic classifier (Stream 1, CONTRACTS5).

The production callable for :func:`app.objective.classify_semantic`:
one short JSON prompt (``SEMANTIC_PROMPT`` in objective.py), one
successful classification per ambiguous turn, timeout-bounded.
Transient rate limits (HTTP 429 from the shared one-at-a-time
gateway) are retried at most twice with backoff — a 429 produces no
classification, so this still yields one classification per turn.
The provider is resolved lazily at call time via
``agentLoop._provider_for_config()`` so the runtime provider
selection applies and no import cycle is created (agentLoop imports
this module lazily; this module imports agentLoop lazily, inside the
factory).

Mock refusal: the mock provider's ``chat_call`` returns canned prose,
never a real classification. A semantic result built on it would be
presented as model judgement when it is not, so the classifier
REFUSES a mock provider (raises :class:`MockProviderRefused`) before
any call is made. :func:`app.objective.classify_semantic` lets that
refusal propagate (other failures still return None) so
:func:`app.objective.build_objective` can tell structural
unavailability apart from a transient failure: a mock refusal marks
the objective ``semantic_unavailable`` (legacy routing, no question
asked), while a real provider's failure marks ``semantic_failed``
(dispatch asks instead of guessing). The distinction matters: in a
mock-provider environment every ambiguous turn would otherwise block
300 s in the ask gate (Stream B regression, found in the suite).

Logging (CONTRACTS5): every invocation logs exactly one
``SEMANTIC_CLASSIFY task_id=<id> model=<model> latency_ms=<n>
result=<primary_intent|none>`` line at INFO. A second invocation for
the same turn (the agentLoop dispatch re-resolves the objective once
after classification) is served from the per-turn cache and logged
with ``latency_ms=0`` plus a ``cached=true`` marker — one model call
per ambiguous turn, two honest log lines.

No secrets are logged. The prompt carries only the user's
instruction (via SEMANTIC_PROMPT); no page data, no credentials.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Optional

logger = logging.getLogger("app.semantic_classifier")

# Same visibility fix as app.objective: uvicorn leaves the root
# logger at WARNING, so without this the SEMANTIC_CLASSIFY INFO
# lines would never reach the backend log (verified live 2026-11-27).
if not logger.handlers:
    _route_handler = logging.StreamHandler()
    _route_handler.setLevel(logging.INFO)
    logger.addHandler(_route_handler)
logger.setLevel(logging.INFO)

#: Wall-clock bound for one model call. The provider's own HTTP
#: timeout still applies underneath; this bound is what keeps a hung
#: gateway from stalling the turn.
DEFAULT_TIMEOUT_S = 30.0

#: Transient rate limits only: the shared FreePi gateway serves one
#: request at a time and 429s the rest, so a single attempt would
#: fail most ambiguous turns while other backends are busy. A 429
#: produces no classification, so retrying it still yields at most
#: one successful classification per turn (the per-turn cache
#: enforces that). Anything that is not a rate limit fails fast.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (5.0, 10.0)
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "one request at a time",
    "quota exceeded",
)


def _is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)

#: Per-turn cache TTL: the dispatch re-resolves the objective once
#: after classification, within the same turn.
_CACHE_TTL_S = 120.0

#: Classifier-only system prompt: short, JSON-only. Overrides the
#: provider's long default capability prompt so the call stays cheap.
_CLASSIFIER_SYSTEM_PROMPT = "You are a precise intent classifier. Reply with JSON only."

_cache: dict[str, tuple[float, str, str]] = {}
_cache_lock = threading.Lock()


class MockProviderRefused(RuntimeError):
    """The configured provider is the mock provider: no real
    classification exists, so the classifier refuses rather than
    presenting canned prose as model judgement."""


def is_mock_provider(provider: Any) -> bool:
    """True when ``provider`` is the mock provider (by class or by
    name), including test doubles derived from it."""
    try:
        from .providers.mock import MockProvider

        if isinstance(provider, MockProvider):
            return True
    except Exception:
        pass
    try:
        name = provider.name() if callable(getattr(provider, "name", None)) else ""
    except Exception:
        name = ""
    return str(name or "").strip().lower() == "mock"


def _model_label(provider: Any) -> str:
    """Best-effort model label for the log line (never raises)."""
    for attr in ("_model", "model", "model_name"):
        try:
            value = getattr(provider, attr, None)
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value
    try:
        name = provider.name()
    except Exception:
        name = ""
    return str(name or "unknown") or "unknown"


def _extract_primary_intent(raw: str) -> str:
    """Best-effort primary_intent from a raw model reply, for the log
    line only (validation still happens in classify_semantic).
    Returns "none" when the reply is missing, malformed, or names an
    intent outside the canonical vocabulary."""
    if not raw:
        return "none"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return "none"
        try:
            data = json.loads(m.group(0))
        except (ValueError, TypeError):
            return "none"
    if not isinstance(data, dict):
        return "none"
    primary = data.get("primary_intent")
    if not isinstance(primary, str):
        return "none"
    try:
        from .objective import INTENTS

        if primary not in INTENTS:
            return "none"
    except Exception:
        pass
    return primary


def _default_provider_factory() -> Any:
    """Resolve the configured provider, lazily (avoids the
    agentLoop import cycle at module import time)."""
    from .agentLoop import _provider_for_config

    return _provider_for_config()


def _cache_key(task_id: Optional[str], prompt: str) -> str:
    return hashlib.sha256(f"{task_id or 'unknown'}\0{prompt}".encode()).hexdigest()


def clear_cache() -> None:
    """Empty the per-turn cache (tests)."""
    with _cache_lock:
        _cache.clear()


def make_semantic_classifier(
    *,
    task_id: Optional[str] = None,
    provider_factory: Optional[Callable[[], Any]] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Callable[[str], str]:
    """Build the ``prompt -> raw model reply`` callable that
    :func:`app.objective.classify_semantic` expects.

    ``provider_factory`` defaults to the configured provider
    (``agentLoop._provider_for_config``); tests pass a fake provider
    factory instead — the classifier itself is never mocked, only the
    provider boundary behind it.
    """
    factory = provider_factory or _default_provider_factory

    def _classify(prompt: str) -> str:
        key = _cache_key(task_id, prompt)
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S:
                _, cached_model, raw = hit
                logger.info(
                    "SEMANTIC_CLASSIFY task_id=%s model=%s latency_ms=0 result=%s cached=true",
                    task_id or "unknown",
                    cached_model,
                    _extract_primary_intent(raw),
                )
                return raw

        t0 = time.monotonic()
        try:
            provider = factory()
        except Exception as exc:
            logger.info(
                "SEMANTIC_CLASSIFY task_id=%s model=unknown latency_ms=%d result=none",
                task_id or "unknown",
                int((time.monotonic() - t0) * 1000),
            )
            logger.warning(
                "SEMANTIC_CLASSIFY_FAILED task_id=%s reason=provider_unavailable error=%r",
                task_id or "unknown",
                exc,
            )
            raise
        model = _model_label(provider)
        if is_mock_provider(provider):
            logger.warning(
                "SEMANTIC_CLASSIFY_REFUSED task_id=%s reason=mock_provider "
                "model=%s — mock output is never a real classification",
                task_id or "unknown",
                model,
            )
            logger.info(
                "SEMANTIC_CLASSIFY task_id=%s model=mock latency_ms=%d result=none",
                task_id or "unknown",
                int((time.monotonic() - t0) * 1000),
            )
            raise MockProviderRefused(
                "semantic classification refused: the configured provider "
                "is the mock provider — no real model is available"
            )
        # One attempt per call, except transient rate limits (see
        # _MAX_ATTEMPTS): a 429 produces no classification, so
        # retrying it still yields at most one successful
        # classification per turn.
        raw: Optional[str] = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                executor = ThreadPoolExecutor(max_workers=1)
                try:
                    future = executor.submit(
                        provider.chat_call,
                        prompt,
                        None,
                        _CLASSIFIER_SYSTEM_PROMPT,
                    )
                    raw = future.result(timeout=timeout_s)
                finally:
                    # Do not wait: a hung gateway thread must not stall
                    # the turn. It is abandoned, not leaked into the
                    # response path.
                    executor.shutdown(wait=False, cancel_futures=True)
                break
            except FuturesTimeout:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "SEMANTIC_CLASSIFY task_id=%s model=%s latency_ms=%d result=none",
                    task_id or "unknown",
                    model,
                    latency_ms,
                )
                logger.warning(
                    "SEMANTIC_CLASSIFY_FAILED task_id=%s reason=timeout timeout_s=%s model=%s",
                    task_id or "unknown",
                    timeout_s,
                    model,
                )
                raise TimeoutError(
                    f"semantic classification timed out after {timeout_s}s "
                    f"(model {model})"
                ) from None
            except Exception as exc:
                if _is_rate_limit(exc) and attempt < _MAX_ATTEMPTS:
                    logger.warning(
                        "SEMANTIC_CLASSIFY_RETRY task_id=%s attempt=%d/%d reason=rate_limited model=%s",
                        task_id or "unknown",
                        attempt,
                        _MAX_ATTEMPTS,
                        model,
                    )
                    time.sleep(_RETRY_BACKOFF_S[attempt - 1])
                    continue
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "SEMANTIC_CLASSIFY task_id=%s model=%s latency_ms=%d result=none",
                    task_id or "unknown",
                    model,
                    latency_ms,
                )
                logger.warning(
                    "SEMANTIC_CLASSIFY_FAILED task_id=%s reason=provider_error model=%s error=%r",
                    task_id or "unknown",
                    model,
                    exc,
                )
                raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = _extract_primary_intent(raw or "")
        logger.info(
            "SEMANTIC_CLASSIFY task_id=%s model=%s latency_ms=%d result=%s",
            task_id or "unknown",
            model,
            latency_ms,
            result,
        )
        with _cache_lock:
            _cache[key] = (time.monotonic(), model, raw or "")
        return raw or ""

    return _classify
