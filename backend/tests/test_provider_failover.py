"""Rate-limit handling: fail fast instead of sleeping through quota windows.

A live free-tier Groq key hit its 200k daily token cap. The old provider slept
8s on every 429 and then tried the other Groq models (which share the same
account-wide cap) before falling back to Gemini — roughly 40s of latency per
planning step. These tests lock in the fast path.
"""
import pytest

from app.providers import gemini as gemini_mod
from app.providers import groq as groq_mod
from app.providers.errors import ProviderUnavailable
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider, _parse_duration, _quota_scope


@pytest.fixture(autouse=True)
def _reset_provider_blocks():
    """Quota blocks are process-wide by design; tests must not leak them."""
    yield
    groq_mod._GROQ_BLOCKED_UNTIL = 0.0
    gemini_mod._MODEL_BLOCKED_UNTIL.clear()


def _payload():
    from app.contracts import BrowserObservation
    from app.privacy import classify_and_redact

    obs = BrowserObservation(
        tab_id=1, url="https://example.com", title="Example",
        interactive=[], scroll={}, dpr=1.0,
    )
    payload, _ = classify_and_redact(obs, instruction="test")
    return payload


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        return {}


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.response


def test_parse_duration_handles_minutes():
    assert _parse_duration("try again in 23m13.2s") == pytest.approx(1393.2)
    assert _parse_duration("try again in 899ms") == pytest.approx(0.899)
    assert _parse_duration("no hint here") == 0.0


def test_quota_scope_distinguishes_day_and_minute():
    assert _quota_scope("on tokens per day (TPD): Limit 200000") == "day"
    assert _quota_scope("on tokens per minute (TPM): Limit 8000") == "minute"
    assert _quota_scope("something else") == ""


def test_daily_quota_skips_remaining_groq_models(monkeypatch):
    """A TPD cap is account-wide: trying 20b/qwen only adds latency."""
    client = _Client(_Response(429, (
        '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` '
        'on tokens per day (TPD): Limit 200000, Used 198758, Requested 4467. '
        'Please try again in 23m13.2s."}}'
    )))
    provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")
    sleeps: list[float] = []
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.plan_call("test", _payload(), [])

    assert client.calls == 1, "must not cascade through sharing-the-same-cap models"
    assert sleeps == [], "must not sleep through a multi-minute window"
    assert "23m" in str(excinfo.value)


def test_minute_quota_pauses_at_most_two_seconds(monkeypatch):
    client = _Client(_Response(429, (
        '{"error":{"message":"on tokens per minute (TPM): Limit 8000. '
        'Please try again in 9.5s."}}'
    )))
    provider = GroqProvider(api_key="dummy")
    monkeypatch.setattr(provider, "_client", client)
    monkeypatch.setattr(groq_mod.config, "GEMINI_API_KEY", "")
    sleeps: list[float] = []
    monkeypatch.setattr(groq_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable):
        provider.plan_call("test", _payload(), [])

    assert client.calls == 3  # one attempt per model, none repeated
    assert sleeps and max(sleeps) <= 2.0


def test_gemini_long_quota_does_not_sleep(monkeypatch):
    class _Models:
        def __init__(self) -> None:
            self.calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED retry in 42s")

    class _GeminiClient:
        def __init__(self) -> None:
            self.models = _Models()

    provider = GeminiProvider(api_key="dummy")
    fake = _GeminiClient()
    monkeypatch.setattr(provider, "_client", fake)
    sleeps: list[float] = []
    monkeypatch.setattr("app.providers.gemini.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(ProviderUnavailable):
        provider.plan_call("test", _payload(), [])

    assert fake.models.calls == 3
    assert sleeps == []
