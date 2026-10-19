"""Test Groq provider initialization, naming, and mock payload handling."""
import pytest
from app.providers.groq import GroqProvider
from app.privacy import PrivacyFilteredPayload
from app.contracts import BrowserObservation


def test_groq_provider_name():
    p = GroqProvider(api_key="dummy_key", model="openai/gpt-oss-120b")
    assert p.name() == "groq:openai/gpt-oss-120b"


def test_groq_provider_fallback_models():
    assert "openai/gpt-oss-120b" in GroqProvider.FALLBACK_MODELS
    assert "openai/gpt-oss-20b" in GroqProvider.FALLBACK_MODELS
    assert "qwen/qwen3.8-27b" in GroqProvider.FALLBACK_MODELS


def test_groq_plan_call_parses_json(monkeypatch):
    class FakeResponse:
        status_code = 200
        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": '{"goal": "test goal", "steps": []}'
                    }
                }]
            }

    class FakeClient:
        def post(self, *args, **kwargs):
            return FakeResponse()

    p = GroqProvider(api_key="dummy_key")
    monkeypatch.setattr(p, "_client", FakeClient())

    obs = BrowserObservation(
        tab_id=1,
        url="http://example.com",
        title="Example",
        interactive=[],
        scroll={},
        dpr=1.0
    )
    from app.privacy import classify_and_redact
    payload, _ = classify_and_redact(obs, instruction="test instruction")
    res = p.plan_call("test instruction", payload, [])
    assert res["goal"] == "test goal"
    assert res["steps"] == []
