"""Stream 4 regression tests: capability answers derived LIVE.

The overview must reflect the registered tool registry, the actual
provider selection, the browser connection, and the OAuth connectors
that really have credentials — never a static list, never an
unrestricted-retailer promise, never perfect privacy/security.
"""
import pytest

from app.capabilities import (
    LiveCapabilityState,
    format_capability_overview,
    format_live_capability_overview,
    gather_live_capability_state,
)


def _state(**overrides):
    base = LiveCapabilityState(
        provider="antseed",
        provider_model="deepseek-v4.1-flash",
        provider_configured=True,
        browser_connected=True,
        connectors={
            "gmail": True,
            "outlook": False,
            "google_calendar": False,
            "google_drive": False,
            "slack": False,
            "github": True,
        },
        tool_counts={"browser": 17, "connector": 20, "mcp": 10},
        tool_registry_available=True,
        mcp_servers={"mcp:builtin": 9},
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_connected_apps_only_when_credentials_stored():
    text = format_live_capability_overview(_state())
    assert "Gmail" in text
    assert "GitHub" in text
    # The connected line names only Gmail + GitHub; the rest are "not connected".
    connected_line = [ln for ln in text.splitlines() if ln.startswith("- **Connected apps")][0]
    assert "Gmail" in connected_line and "GitHub" in connected_line
    assert "Slack" not in connected_line
    not_connected = [ln for ln in text.splitlines() if ln.startswith("- **Not connected")][0]
    assert "Slack" in not_connected and "Outlook" in not_connected
    assert "Gmail" not in not_connected


def test_nothing_connected_says_so():
    text = format_live_capability_overview(_state(
        connectors={k: False for k in ("gmail", "outlook", "google_calendar", "google_drive", "slack", "github")}
    ))
    assert "Not connected:" in text
    assert "Connected apps (credentials stored)" not in text


def test_mock_provider_is_labelled_mock():
    text = format_live_capability_overview(_state(
        provider="mock", provider_model=None, provider_configured=False
    ))
    assert "mock provider (canned answers, not a real model)" in text


def test_unconfigured_provider_is_not_claimed_live():
    text = format_live_capability_overview(_state(provider_configured=False))
    assert "not configured" in text


def test_browser_disconnected_is_stated():
    text = format_live_capability_overview(_state(browser_connected=False))
    assert "NOT connected" in text
    assert "browser tasks cannot start" in text


def test_no_unrestricted_retailer_promise_and_no_perfect_privacy():
    text = format_live_capability_overview(_state())
    assert "bot-gate" in text
    assert "never promised" in text
    assert "best-effort, not perfect" in text
    assert "perfect privacy" not in text.lower().replace("not perfect", "")
    assert "unrestricted" not in text.lower()


def test_tool_counts_come_from_registry():
    text = format_live_capability_overview(_state())
    assert "Registered tools:" in text
    assert "browser: 17" in text
    assert "mcp: 10" in text
    assert "MCP servers:" in text
    assert "mcp:builtin (9 tools)" in text


def test_gather_reads_live_sources(monkeypatch):
    # Provider + browser injected; connectors read from oauth_manager.
    monkeypatch.setattr(
        "app.connectors.oauth.oauth_manager.configured",
        lambda provider: provider == "gmail",
    )
    state = gather_live_capability_state(
        browser_connected=False,
        provider_state={"provider": "antseed", "keyConfigured": True, "model": "deepseek-v4.1-flash"},
    )
    assert state.provider == "antseed"
    assert state.provider_model == "deepseek-v4.1-flash"
    assert state.provider_configured is True
    assert state.browser_connected is False
    assert state.connectors["gmail"] is True
    assert state.connectors["slack"] is False
    # The real registry is read: counts are live, not fixtures.
    assert state.tool_registry_available is True
    assert state.tool_counts and sum(state.tool_counts.values()) > 0
    text = format_capability_overview(state)
    assert "Gmail" in text
    assert "NOT connected" in text  # browser


def test_format_capability_overview_no_arg_smoke():
    text = format_capability_overview()
    assert isinstance(text, str) and "PrivAgent Capabilities" in text
    # In this environment nothing is connected and the provider is mock;
    # the answer must say so instead of claiming otherwise.
    assert "mock provider" in text or "not configured" in text
    assert "Not connected:" in text


def test_capabilities_api_overview_endpoint():
    from app.capabilities_api import overview as overview_endpoint
    import app.capabilities_api as api

    body = overview_endpoint()
    assert isinstance(body, dict)
    assert body["browser_connected"] in (True, False, None)
    assert isinstance(body["connectors"], dict)
    assert "overview" in body and "PrivAgent Capabilities" in body["overview"]
    assert body["tool_registry_available"] is True
