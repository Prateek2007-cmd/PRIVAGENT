"""Mail/Calendar Demo Sandbox connector (NAVIGATOR demo only).

Talks to the local mailcal demo sandbox over real HTTP — no mocked
responses, no in-process fakes. The sandbox holds synthetic test data
only (senders @demo.test); drafts created here are stored in the sandbox
and NEVER sent (the sandbox has no send endpoint).

Default origin http://127.0.0.1:8126, default demo token
"demo-mailcal-token" (override with MAILCAL_DEMO_TOKEN on the sandbox
side, or pass token=/base_url= here). Real Gmail / Google Calendar are
NOT used by this connector and remain BLOCKED without a configured test
account — see the S4 report.

Not wired into connector_router.py / capabilities.py: that would need
edits to shared files outside this stream's ownership. Wiring is
documented in ~/workspace/navigator-audit/worktrees2/S4-REPORT.md.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import ConnectorError, NativeConnector

DEFAULT_ORIGIN = "http://127.0.0.1:8126"
DEFAULT_TOKEN = "demo-mailcal-token"


class MailCalSandboxConnector(NativeConnector):
    origin = DEFAULT_ORIGIN
    service = "MailCal Sandbox"

    def __init__(self, token: str | None = DEFAULT_TOKEN, *,
                 base_url: str | None = None,
                 client: Any | None = None, refresh_hook: Any = None):
        super().__init__(token, client=client, refresh_hook=refresh_hook)
        if base_url:
            self.origin = base_url.rstrip("/")

    # ------------------------------------------------------------------ auth
    def login(self, email: str, password: str) -> dict:
        """Exchange demo credentials for the sandbox token (demo only)."""
        if not self._token:
            raise ConnectorError(f"{self.service} is not connected")
        # The sandbox accepts only demo@demo.test; the call itself is the
        # credential check — the returned token is stored for later calls.
        data = self._request("POST", "/api/v1/auth/login",
                             json={"email": email, "password": password})
        token = data.get("token")
        if not token:
            raise ConnectorError(f"{self.service} login did not return a token")
        self._token = token
        return data

    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    # ------------------------------------------------------------------ mail
    def list_unread(self, limit: int = 50) -> list[dict]:
        data = self._request("GET", "/api/v1/messages",
                             params={"unread": "true", "limit": min(max(limit, 1), 100)})
        return data.get("messages", [])

    def search(self, query: str, limit: int = 20) -> list[dict]:
        data = self._request("GET", "/api/v1/messages",
                             params={"q": (query or "")[:300], "limit": min(max(limit, 1), 100)})
        return data.get("messages", [])

    def list_messages(self, query: str = "", unread: bool = False, limit: int = 50) -> list[dict]:
        params: dict = {"limit": min(max(limit, 1), 100)}
        if query:
            params["q"] = (query or "")[:300]
        if unread:
            params["unread"] = "true"
        data = self._request("GET", "/api/v1/messages", params=params)
        return data.get("messages", [])

    def get_message(self, message_id: str) -> dict:
        data = self._request("GET", f"/api/v1/messages/{quote(message_id, safe='')}")
        return data.get("message", {})

    def get_thread(self, thread_id: str) -> list[dict]:
        data = self._request("GET", f"/api/v1/threads/{quote(thread_id, safe='')}")
        return data.get("messages", [])

    def mark_read(self, message_id: str) -> dict:
        return self._request("POST", f"/api/v1/messages/{quote(message_id, safe='')}/read")

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        data = self._request(
            "GET",
            f"/api/v1/messages/{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}/download")
        if isinstance(data, bytes):
            return data
        raise ConnectorError(f"{self.service} attachment download did not return bytes")

    # ---------------------------------------------------------------- drafts
    def list_drafts(self) -> list[dict]:
        data = self._request("GET", "/api/v1/drafts")
        return data.get("drafts", [])

    def create_draft(self, to: str, subject: str, body: str) -> dict:
        """Create a draft in the sandbox. Drafts are stored only — the
        sandbox has no send endpoint, so a draft can never be sent."""
        if not to or not subject or not body:
            raise ConnectorError("create_draft requires to, subject and body")
        data = self._request("POST", "/api/v1/drafts",
                             json={"to": to, "subject": subject, "body": body})
        return data.get("draft", {})

    # -------------------------------------------------------------- calendar
    def list_events(self, start: str | None = None, end: str | None = None) -> list[dict]:
        params: dict = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/api/v1/events", params=params or None)
        return data.get("events", [])

    def get_event(self, event_id: str) -> dict:
        data = self._request("GET", f"/api/v1/events/{quote(event_id, safe='')}")
        return data.get("event", {})

    def create_event(self, title: str, start: str, end: str,
                     location: str = "", description: str = "") -> dict:
        if not title or not start or not end:
            raise ConnectorError("create_event requires title, start and end")
        if start >= end:
            raise ConnectorError("create_event requires end after start")
        data = self._request("POST", "/api/v1/events",
                             json={"title": title, "start": start, "end": end,
                                   "location": location, "description": description})
        return data.get("event", {})

    # ------------------------------------------------------------ invitations
    def list_invitations(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else None
        data = self._request("GET", "/api/v1/invitations", params=params)
        return data.get("invitations", [])

    def respond_invitation(self, invitation_id: str, status: str) -> dict:
        if status not in ("accepted", "declined"):
            raise ConnectorError("respond_invitation status must be 'accepted' or 'declined'")
        data = self._request(
            "POST", f"/api/v1/invitations/{quote(invitation_id, safe='')}/respond",
            json={"status": status})
        return data.get("invitation", {})
