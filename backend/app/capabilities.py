"""PrivAgent Capability Registry and Awareness System.

Defines supported capabilities, unsupported operation detection,
and honest system awareness prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .connectors import GmailConnector, OutlookConnector, GoogleCalendarConnector, GoogleDriveConnector, SlackConnector, GitHubConnector, LocalFileSystemAdapter


class Capability(str, Enum):
    BROWSER_AUTOMATION = "BROWSER_AUTOMATION"
    PRIVACY_PRESERVATION = "PRIVACY_PRESERVATION"
    AUTH_HANDOFF = "AUTH_HANDOFF"
    MULTI_SITE_RESEARCH = "MULTI_SITE_RESEARCH"
    CONVERSATIONAL_REASONING = "CONVERSATIONAL_REASONING"


@dataclass
class CapabilityDeclaration:
    capability: Capability
    name: str
    description: str
    supported: bool = True


SUPPORTED_CAPABILITIES: dict[Capability, CapabilityDeclaration] = {
    Capability.BROWSER_AUTOMATION: CapabilityDeclaration(
        capability=Capability.BROWSER_AUTOMATION,
        name="Real-Browser Automation",
        description="Autonomous navigation, DOM interaction (clicking, typing, scrolling), multi-tab management, web search, webmail (Gmail/Outlook), e-commerce research, and LeetCode inspection via connected Chrome extension.",
        supported=True,
    ),
    Capability.PRIVACY_PRESERVATION: CapabilityDeclaration(
        capability=Capability.PRIVACY_PRESERVATION,
        name="Fail-Closed Privacy Filter",
        description="Client-side and server-side fail-closed PII redaction of passwords, credit cards, SSNs, Aadhaar numbers, and private authentication tokens before DOM data reaches models.",
        supported=True,
    ),
    Capability.AUTH_HANDOFF: CapabilityDeclaration(
        capability=Capability.AUTH_HANDOFF,
        name="Human Authentication Handoff",
        description="Secure pause and handoff for CAPTCHAs, MFA, two-factor challenges, and sign-in screens, allowing the user to sign in manually while preserving tab session authorization.",
        supported=True,
    ),
    Capability.MULTI_SITE_RESEARCH: CapabilityDeclaration(
        capability=Capability.MULTI_SITE_RESEARCH,
        name="Multi-Site Cross-Store Research",
        description="Structured product search and price comparison across multiple retailers (Amazon, Flipkart, Samsung, etc.), tracking verified listings, unavailable stores, and unvisited sites.",
        supported=True,
    ),
    Capability.CONVERSATIONAL_REASONING: CapabilityDeclaration(
        capability=Capability.CONVERSATIONAL_REASONING,
        name="Conversational & Analytical Reasoning",
        description="Fast-path conversational answers, algorithms, coding assistance, calculations, and explanations without launching browser sessions.",
        supported=True,
    ),
}

# Patterns of explicitly unsupported system/desktop operations
UNSUPPORTED_SYSTEM_PATTERNS = [
    re.compile(r"\b(?:reboot|restart|shut\s*down|power\s*off)\s+(?:(?:my|the|this)\s+)?(?:physical\s+)?(?:computer|pc|laptop|windows|machine|system|device)\b", re.IGNORECASE),
    re.compile(r"\b(?:format|wipe|erase)\s+(?:(?:my|the|this)\s+)?(?:hard\s*drive|disk|drive|partition|c:)\b", re.IGNORECASE),
    re.compile(r"\b(?:hack|crack|bypass|steal)\s+(?:into\s+)?(?:someone's|any|a|the)?\s*(?:bank|account|password|database|wifi)\b", re.IGNORECASE),
    re.compile(r"\b(?:install|run|execute)\s+(?:malware|virus|trojan|ransomware|keylogger)\b", re.IGNORECASE),
    re.compile(r"\b(?:open|launch|run)\s+(?:calculator|notepad|paint|excel|word|terminal|cmd|powershell)\s+(?:on\s+my\s+desktop|app|application|locally)\b", re.IGNORECASE),
]

# Patterns where user explicitly asks about agent capabilities
CAPABILITY_QUERY_PATTERN = re.compile(
    r"^(?:what\s+can\s+you\s+do|what\s+are\s+your\s+capabilities|what\s+are\s+you\s+capable\s+of|tell\s+me\s+what\s+you\s+can\s+do|who\s+are\s+you|what\s+is\s+privagent)\b",
    re.IGNORECASE,
)

# One-off timer / countdown (fixC N1). There is NO timer implementation
# in the backend — no timer table, no one-shot fire path — and the
# scheduler's one-off jobs are reminders (REMINDER_JOB_PREFIX), not
# timers. A bare "set a timer for 20 minutes" used to fall through to
# the model, which invented a queued one-shot check that would never
# fire (live 2026-11-27). Detect it deterministically here and answer
# with the honest boundary instead of letting the model sink claim a
# job. Deliberately narrow: a monitoring instruction that happens to
# mention a timer ("watch … on a timer") is a watch, not a timer
# request, and "remind me in N minutes" is a reminder (the reminder
# branch owns it) — neither matches these patterns.
_TIMER_SET_RE = re.compile(
    r"\b(?:set|start|create|make)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?:\d+\s*[-\u2013]?\s*(?:minute|min|hour|hr|second|sec)s?\s+)?"
    r"(?:timer|countdown)\b",
    re.IGNORECASE,
)
_TIMER_FOR_RE = re.compile(
    r"\b(?:timer|countdown)\b\s+(?:for|in)\s+\d+\s*"
    r"(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)\b",
    re.IGNORECASE,
)
_TIMER_MONITOR_GUARD_RE = re.compile(
    r"\b(?:watch|monitor|track|alert|notify)\b", re.IGNORECASE
)

TIMER_UNSUPPORTED_MESSAGE = (
    "I can't set a one-off timer — PrivAgent has no timer or alarm, "
    "so nothing was set and nothing will ring. "
    "What I can do instead: say 'remind me in 30 minutes to …' and "
    "I'll create a real scheduled reminder that posts here when it fires, "
    "or set the timer on your phone for a reliable alarm."
)


def is_one_off_timer_request(text: str) -> bool:
    """True for a bare one-off timer/countdown request ("set a timer
    for 20 minutes", "start a 10-minute countdown"). False for
    monitoring instructions and reminders, which own their own paths."""
    raw = (text or "").strip()
    if not raw or _TIMER_MONITOR_GUARD_RE.search(raw):
        return False
    return bool(_TIMER_SET_RE.search(raw) or _TIMER_FOR_RE.search(raw))

SYSTEM_CAPABILITY_PROMPT = """You are PrivAgent, a privacy-preserving AI browser automation and web co-pilot.

YOUR CAPABILITIES — WHAT IS TRUE RIGHT NOW:
1. Browser Automation: ONLY when the Chrome extension is connected (check the live capability overview before claiming it). With it connected, you can open websites, navigate, click, type, search, and extract information through the browser. Without it, browser tasks cannot start — say so instead of pretending.
2. Continuous Background Monitoring Engine: an active SQLite interval monitoring scheduler in the backend continuously checks product prices, DOM changes, and emails in the background. When something changes it posts an alert message right here in the workspace conversation and in the Control Center monitoring view — that is the only alert channel. There is no phone call, SMS, or push ping. Never describe monitoring as running on a port: a port number is only the address of this workspace's web UI, not a notification channel. NEVER claim you cannot do background monitoring.
3. Native API & OAuth Connectors: connectors (Gmail, Google Calendar, Google Drive, Slack, GitHub, Outlook) work ONLY for accounts that are actually connected — check the live capability overview first and never claim an account is connected unless it says so. Email is draft-only: you can create a Gmail draft for the operator to review and send — never say an email was "sent" or "dispatched" when only a draft was created. Slack and GitHub are reachable through the capabilities API, not through chat.
4. Web research and shopping: you can search the web and compare listings on sites you can actually reach. Some large retailers bot-gate automated browsing (Amazon direct pages are a known example) — never promise unrestricted access to any retailer, and report honestly when a site blocks you or a price could not be verified.
5. Privacy Redaction: passwords, credit cards, Aadhaar, SSNs, and private tokens are filtered fail-closed before DOM data reaches models. That filter is best-effort, not perfect — never claim perfect privacy or security, and never ask for or repeat secrets.
6. Conversational Co-Pilot Voice: you are interactive, friendly, and natural. Speak with genuine warmth and relatable cadence. Avoid cold robotic disclaimers, dry monospace dumps, or forced comedy jokes.
7. Honesty: be clear about verified facts versus unconfirmed listings, but always guide the user proactively on the next best move. Never present a capability from this prompt as currently working when the live capability overview says it is not connected or not configured.
"""


# ---------------------------------------------------------------------------
# Live capability state (Stream 4).
#
# The capability answer a user gets in chat must be derived from live
# state — the registered tool registry, the actual provider selection,
# the extension connection, the OAuth connectors that really have
# credentials, the MCP servers actually registered, and the runtime
# permission rules — never from a static list. Every source below is
# read best-effort: a source that cannot be read reports "unknown"
# instead of a guessed value, so a missing import can never turn into
# a false "connected".
# ---------------------------------------------------------------------------

# Connector names (oauth_manager keys) in the order they are shown.
_CONNECTOR_DISPLAY: tuple[tuple[str, str], ...] = (
    ("gmail", "Gmail"),
    ("outlook", "Outlook"),
    ("google_calendar", "Google Calendar"),
    ("google_drive", "Google Drive"),
    ("slack", "Slack"),
    ("github", "GitHub"),
)

# Restrictions that are true regardless of live state. These are
# product facts, not per-run claims: keep them short and checkable.
_KNOWN_RESTRICTIONS: tuple[str, ...] = (
    "Email is draft-only: drafts are created for the operator to review and send — nothing is sent without approval.",
    "Monitoring alerts arrive only in this workspace conversation / Control Center — no phone call, SMS, or push notification.",
    "Some large retailers bot-gate automated browsing (Amazon direct pages are a known example) — access is verified per run, never promised.",
    "Purchases, reservations, and payments always stop at an approval step; demo sandboxes never charge.",
    "Privacy redaction is fail-closed but best-effort, not perfect — never paste secrets, and never claim a page is fully private.",
)


@dataclass
class LiveCapabilityState:
    """One snapshot of what PrivAgent can actually do right now."""

    provider: str = "unknown"
    provider_model: str | None = None
    provider_configured: bool | None = None
    provider_note: str | None = None
    browser_connected: bool | None = None
    connectors: dict[str, bool | None] | None = None
    tool_counts: dict[str, int] | None = None
    tool_registry_available: bool = False
    mcp_servers: dict[str, int] | None = None
    approval_required_for: tuple[str, ...] = (
        "consequential tool calls",
        "native connector mutations",
        "navigation outside the domain allow-list",
        "purchases, reservations, and payments",
    )


def gather_live_capability_state(
    *,
    browser_connected: bool | None = None,
    provider_state: dict[str, Any] | None = None,
) -> LiveCapabilityState:
    """Read the live capability sources. Callers (tests, the API) may
    inject ``browser_connected`` / ``provider_state``; otherwise the
    values are read from the running process. Nothing here raises:
    an unreadable source becomes ``None`` / "unknown"."""
    state = LiveCapabilityState()

    # -- Planner provider (runtime selection wins over static config) --
    try:
        if provider_state is None:
            from .providers import provider_settings

            provider_state = provider_settings.public_state()
        state.provider = str(provider_state.get("provider") or "unknown")
        state.provider_model = provider_state.get("model") or None
        state.provider_configured = bool(provider_state.get("keyConfigured"))
        if state.provider == "mock":
            state.provider_note = (
                "Mock provider: answers are canned, not a real model. "
                "Configure a real provider before treating replies as AI output."
            )
        elif state.provider_configured is False:
            state.provider_note = (
                f"Provider '{state.provider}' is selected but has no key/model configured."
            )
    except Exception:
        try:
            from . import config as _config

            state.provider = str(getattr(_config, "PROVIDER", "unknown") or "unknown")
        except Exception:
            pass
        state.provider_configured = None
        state.provider_note = "Provider status could not be read."

    # -- Browser extension connection (live sockets, not a setting) --
    if browser_connected is not None:
        state.browser_connected = bool(browser_connected)
    else:
        try:
            from .hub import hub

            state.browser_connected = bool(hub.extension_sockets)
        except Exception:
            state.browser_connected = None

    # -- Connected applications: OAuth credentials actually stored --
    connectors: dict[str, bool | None] = {}
    try:
        from .connectors.oauth import oauth_manager

        for _key, _label in _CONNECTOR_DISPLAY:
            try:
                connectors[_key] = bool(oauth_manager.configured(_key))
            except Exception:
                connectors[_key] = None
    except Exception:
        for _key, _label in _CONNECTOR_DISPLAY:
            connectors[_key] = None
    state.connectors = connectors

    # -- Registered tools (the planner's real catalog, by kind) --
    try:
        from .tool_registry import list_tool_specs

        _specs = list_tool_specs()
        _counts: dict[str, int] = {}
        _mcp: dict[str, int] = {}
        for _spec in _specs:
            _kind = str(_spec.get("kind") or "unknown")
            _counts[_kind] = _counts.get(_kind, 0) + 1
            if _kind == "mcp":
                _server = str(_spec.get("source") or "mcp:unknown")
                _mcp[_server] = _mcp.get(_server, 0) + 1
        state.tool_counts = _counts
        state.tool_registry_available = True
        state.mcp_servers = _mcp
    except Exception:
        state.tool_counts = None
        state.tool_registry_available = False
        state.mcp_servers = None

    return state


def format_live_capability_overview(state: LiveCapabilityState) -> str:
    """Render a live capability overview from a gathered state. Every
    claim names its source; anything unknown says so."""
    lines: list[str] = ["### PrivAgent Capabilities — live status\n"]

    # Provider / model.
    if state.provider == "mock":
        lines.append(
            "- **Planner model: mock provider (canned answers, not a real model).** "
            "Configure a real provider before treating replies as AI output."
        )
    elif state.provider_configured is False:
        lines.append(
            f"- **Planner model: {state.provider} is selected but not configured "
            f"(no key/model).** Browser and chat planning cannot run until it is configured."
        )
    elif state.provider_configured is True:
        _model = f" (model: {state.provider_model})" if state.provider_model else ""
        lines.append(f"- **Planner model: {state.provider}{_model} — configured.**")
    else:
        lines.append(
            f"- **Planner model: {state.provider} — status could not be verified.**"
        )
    if state.provider_note and state.provider != "mock" and state.provider_configured is not False:
        lines.append(f"  ({state.provider_note})")

    # Browser.
    if state.browser_connected is True:
        lines.append("- **🌐 Real-Browser Automation: connected.** The Chrome extension is live — navigation, clicking, typing, and page reading work.")
    elif state.browser_connected is False:
        lines.append("- **🌐 Real-Browser Automation: NOT connected.** The Chrome extension is not connected, so browser tasks cannot start. Load the extension and connect it first.")
    else:
        lines.append("- **🌐 Real-Browser Automation: connection status unknown.** Browser tasks will report the real state when they start.")

    # Connected applications — only actually-configured ones are "connected".
    _labels = dict(_CONNECTOR_DISPLAY)
    _connected = [(_k, _labels[_k]) for _k in _labels if (state.connectors or {}).get(_k) is True]
    _not_connected = [(_k, _labels[_k]) for _k in _labels if (state.connectors or {}).get(_k) is False]
    _unknown = [(_k, _labels[_k]) for _k in _labels if (state.connectors or {}).get(_k) is None]
    if _connected:
        lines.append(
            "- **Connected apps (credentials stored):** "
            + ", ".join(_label for _key, _label in _connected)
            + ". These work through their native connectors; connection health is verified on each call."
        )
    if _not_connected:
        lines.append(
            "- **Not connected:** "
            + ", ".join(_label for _key, _label in _not_connected)
            + ". I cannot read mail, calendar, drive, chat, or repos for these until you connect them."
        )
    if _unknown:
        lines.append(
            "- **Connection status unknown:** "
            + ", ".join(_label for _key, _label in _unknown)
            + "."
        )
    if not _connected and not _not_connected and not _unknown:
        lines.append("- **Connected apps:** status could not be read.")

    # Registered tools + MCP servers.
    if state.tool_registry_available and state.tool_counts:
        _kinds = ", ".join(f"{_k}: {_n}" for _k, _n in sorted(state.tool_counts.items()))
        lines.append(f"- **Registered tools:** {sum(state.tool_counts.values())} in the tool registry ({_kinds}).")
    else:
        lines.append("- **Registered tools:** the tool registry could not be read, so no tool list is claimed.")
    if state.mcp_servers:
        _servers = ", ".join(f"{_s} ({_n} tools)" for _s, _n in sorted(state.mcp_servers.items()))
        lines.append(f"- **MCP servers:** {_servers}.")
    elif state.mcp_servers == {}:
        lines.append("- **MCP servers:** none registered.")
    else:
        lines.append("- **MCP servers:** status could not be read.")

    # Monitoring + privacy (capability shape, not a per-run claim).
    lines.append(
        "- **📊 Monitoring:** background watches for price, page, listing, and inbox changes; "
        "alerts arrive only in this conversation / Control Center."
    )
    lines.append(
        "- **🔒 Privacy:** fail-closed redaction of credentials, payment cards, SSNs, and private tokens "
        "before page data reaches a model — best-effort, not perfect."
    )
    lines.append(
        "- **🔑 Human Authentication Handoff:** when a site needs login, 2FA, or CAPTCHA, "
        "the task pauses for you to complete it — I never ask for your password."
    )

    # Approvals + restrictions.
    lines.append(
        "- **Approvals required for:** " + ", ".join(state.approval_required_for) + "."
    )
    lines.append("\n**Known limits:**")
    for _restriction in _KNOWN_RESTRICTIONS:
        lines.append(f"- {_restriction}")
    return "\n".join(lines)


def check_supported_capability(instruction: str) -> tuple[bool, Optional[str]]:
    """Check if an instruction requests an unsupported OS-level or harmful capability.

    Returns (is_supported, explanation_if_unsupported).
    """
    text = (instruction or "").strip()
    if not text:
        return True, None

    for pat in UNSUPPORTED_SYSTEM_PATTERNS:
        if pat.search(text):
            return False, (
                "PrivAgent is an autonomous web browser and privacy assistant. "
                "It cannot perform physical desktop OS operations (such as rebooting your computer, formatting drives, or launching desktop applications) "
                "or bypass security controls."
            )

    return True, None


def is_capability_query(instruction: str) -> bool:
    """True if user is asking what PrivAgent can do."""
    return bool(CAPABILITY_QUERY_PATTERN.search((instruction or "").strip()))


def format_capability_overview(state: LiveCapabilityState | None = None) -> str:
    """Return a capability overview derived LIVE from the registered
    tool registry, provider status, browser connection, connected
    applications, MCP servers, and runtime permissions.

    Never claims an app is connected unless its credentials are
    actually stored, never promises unrestricted retailer access, and
    never claims perfect privacy/security. An optional pre-gathered
    ``state`` (tests / API) is accepted; otherwise state is gathered
    from the running process now."""
    if state is None:
        state = gather_live_capability_state()
    return format_live_capability_overview(state)


@dataclass(frozen=True)
class CapabilityRoute:
    capability: str
    adapter: str
    method: str
    operation: str
    available: bool
    url: str | None = None


class BrowserAdapter:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class CapabilityRegistry:
    """Intent maps to an adapter without giving a planner credentials or tools."""

    _OPERATIONS = {
        "EmailCapability": {
            "search_threads": False, "summarize_messages": False, "list_messages": False, "get_message": False,
            "draft_email": True, "manage_labels": True, "trash_message": True, "untrash_message": True,
        },
        "CalendarCapability": {"list_events": False, "check_availability": False, "create_invite": True},
        "DocumentCapability": {"search_documents": False, "extract_text": False, "upload_deliverable": True},
        "TeamChatCapability": {"query_channels": False, "summarize_discussions": False, "post_notification": True},
        "CodeRepoCapability": {"query_issues": False, "inspect_prs": False, "inspect_commit_diff": False},
    }

    def __init__(self, adapters: dict[str, list[Any]] | None = None, *, browser_available: bool = False):
        self.browser_available = browser_available
        from .mcp.policy import ApprovalPolicy
        self._policy = ApprovalPolicy()
        from .connectors.oauth import oauth_manager
        from pathlib import Path
        self.adapters = adapters or {
            "EmailCapability": [oauth_manager.connector("gmail"), oauth_manager.connector("outlook"), BrowserAdapter("BrowserEmailAdapter", "https://mail.google.com")],
            "CalendarCapability": [oauth_manager.connector("google_calendar"), BrowserAdapter("BrowserCalendarAdapter", "https://calendar.google.com")],
            "DocumentCapability": [oauth_manager.connector("google_drive"), LocalFileSystemAdapter(Path(__file__).resolve().parents[1] / "data" / "artifacts"), BrowserAdapter("BrowserDriveAdapter", "https://drive.google.com")],
            "TeamChatCapability": [oauth_manager.connector("slack"), BrowserAdapter("BrowserChatAdapter", "https://app.slack.com/client")],
            "CodeRepoCapability": [oauth_manager.connector("github")],
        }

    def resolve(self, capability: str, operation: str) -> CapabilityRoute:
        if capability not in self._OPERATIONS or operation not in self._OPERATIONS[capability]:
            raise ValueError("unsupported capability operation")
        for adapter in self.adapters.get(capability, []):
            if getattr(adapter, "configured", False):
                return CapabilityRoute(capability, type(adapter).__name__, "native", operation, True)
        for adapter in self.adapters.get(capability, []):
            if isinstance(adapter, BrowserAdapter) and self.browser_available:
                return CapabilityRoute(capability, adapter.name, "browser", operation, True, adapter.url)
        return CapabilityRoute(capability, "", "unavailable", operation, False)

    def approve_native(self, capability: str, operation: str, params: dict[str, Any], task_id: str) -> str:
        """Called only by a trusted user-confirmation handler."""
        route = self.resolve(capability, operation)
        if route.method != "native" or not self._OPERATIONS[capability][operation]:
            raise ValueError("no mutable native route")
        return self._policy.approve(f"{capability}.{operation}", params, task_id)

    def execute_native(self, capability: str, operation: str, params: dict[str, Any], *,
                       approval_token: str | None = None, task_id: str = "") -> Any:
        route = self.resolve(capability, operation)
        if route.method != "native":
            raise RuntimeError("native connector not configured; use browser route or connect an account")
        if self._OPERATIONS[capability][operation]:
            self._policy.authorize(f"{capability}.{operation}", params, approval_token, task_id)
        adapter = next(a for a in self.adapters[capability] if type(a).__name__ == route.adapter)
        from .mcp.sanitizer import sanitize_tool_output
        result = getattr(adapter, operation)(**params)
        return sanitize_tool_output(result)
