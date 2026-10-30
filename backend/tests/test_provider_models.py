"""Provider model contracts: only currently-live models may be defaults.

Gemini 2.0 Flash is shut down and 2.5 Flash is unavailable to new callers;
Groq's Llama 3.3/3.1 models are Enterprise-only. The defaults below are the
verified general-availability models, with env overrides for operators.
"""
from google.genai import types as gtypes
from app.contracts import BrowserObservation
from app.privacy import classify_and_redact
from app.providers import gemini as gemini_mod
from app.providers import groq as groq_mod
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider


def _payload():
    obs = BrowserObservation(
        tab_id=1,
        url="https://example.com",
        title="Example",
        interactive=[],
        scroll={},
        dpr=1.0,
    )
    payload, _ = classify_and_redact(obs, instruction="test")
    return payload


class _FakeModels:
    def __init__(self) -> None:
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs

        class _Resp:
            text = '{"goal": "g", "steps": []}'

        return _Resp()


class _FakeGeminiClient:
    def __init__(self) -> None:
        self.models = _FakeModels()


def test_gemini_uses_live_default_and_fallbacks():
    assert GeminiProvider.DEFAULT_MODEL == "gemini-3.8-flash"
    assert GeminiProvider.FALLBACK_MODELS == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    assert not any(m in {"gemini-2.0-flash", "gemini-1.5-flash"} for m in GeminiProvider.FALLBACK_MODELS)


def test_gemini_default_name_and_env_override(monkeypatch):
    assert GeminiProvider(api_key="dummy").name() == "gemini:gemini-3.8-flash"
    monkeypatch.setattr(gemini_mod.config, "GEMINI_MODEL", "gemini-3.6-flash")
    assert GeminiProvider(api_key="dummy").name() == "gemini:gemini-3.6-flash"


def test_gemini_flash_38_requests_low_thinking():
    provider = GeminiProvider(api_key="dummy")
    fake = _FakeGeminiClient()
    provider._client = fake

    result = provider.plan_call("test", _payload(), [])

    assert result == {"goal": "g", "steps": []}
    cfg = fake.models.kwargs["config"]
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.thinking_level == gtypes.ThinkingLevel.LOW


def test_gemini_older_fallback_does_not_send_38_thinking_config():
    provider = GeminiProvider(api_key="dummy", model="gemini-3.6-flash")
    fake = _FakeGeminiClient()
    provider._client = fake

    provider.plan_call("test", _payload(), [])

    cfg = fake.models.kwargs["config"]
    assert cfg.thinking_config is None


def test_groq_keeps_active_open_models():
    assert GroqProvider.DEFAULT_MODEL == "openai/gpt-oss-120b"
    assert set(GroqProvider.FALLBACK_MODELS) == {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    }
    # Llama 3.3/3.1 are Enterprise-only on Groq, never silent defaults.
    assert not any("llama" in m for m in GroqProvider.FALLBACK_MODELS)
    assert GroqProvider(api_key="dummy").name() == "groq:openai/gpt-oss-120b"


def test_groq_env_override(monkeypatch):
    monkeypatch.setattr(groq_mod.config, "GROQ_MODEL", "openai/gpt-oss-20b")
    assert GroqProvider(api_key="dummy").name() == "groq:openai/gpt-oss-20b"
