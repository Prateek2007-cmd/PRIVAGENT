"""Connector execution (Task 14).

Runs a :class:`app.connector_router.ConnectorRoute` against the native
API connector. Rules, in order:

1. **Never silently fall back to the browser.** When the connector
   has no token, the outcome is an honest failure naming the connector
   and the remedy (connect it in Control Center -> Connectors, or ask
   for the browser UI explicitly). No browser navigation is attempted.
2. **Reads execute directly; writes need the operator.** Draft/trash/
   create go through the confirmation gate first (``confirm_fn``,
   injected so tests can stub it). A denied or timed-out confirmation
   fails the task honestly; nothing is executed.
3. **Egress is pinned.** Every connector call is checked against
   :data:`app.allowlist.CONNECTOR_EGRESS_HOSTS` before any request is
   made — Google APIs only.
4. **Results reach the planner as observations.** A connector read
   (or write) returns a :class:`ConnectorOutcome` that carries both
   the operator-facing ``message`` and a sanitised ``observation``
   (``<tool_result name="connector.<connector>.<operation>" ...>`` in
   the shared ToolResult shape — see :func:`run_connector_route_as_observation`
   and :meth:`ConnectorOutcome.to_tool_result`). The loop appends that
   observation to the task's working memory
   (``tool_registry.append_tool_result_to_working_memory``) and keeps
   planning, so a later step can act on the connector's data instead
   of the answer being a terminal leaf. The early return in
   ``agentLoop.run_task`` is the caller's to remove — this module
   supplies the observation; it does not decide the loop's control
   flow. The answer is still scrubbed (credential/PII patterns)
   before it is returned, so a secret inside an email body or a Drive
   file does not become a durable chat message verbatim.
   Persistence itself is the caller's job
   (:func:`app.loop_checkpoints._persist_agent_message`, which already
   honours zero retention) — this module persists nothing.
5. **Audit, metadata only.** Every call appends one audit event
   (connector + operation + outcome); summaries never carry content.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .connector_router import CONNECTOR_LABELS, ConnectorRoute
from .allowlist import CONNECTOR_EGRESS_HOSTS, egress_allowed

__all__ = ["ConnectorOutcome", "run_connector_route", "run_connector_route_as_observation",
           "connector_observation", "not_connected_message"]

_CONFIRM_TIMEOUT_S = 120.0


def not_connected_message(connector: str) -> str:
    """Honest failure for an unconfigured connector (no silent fallback)."""
    label = CONNECTOR_LABELS.get(connector, connector)
    return (
        f"### {label} connector is not connected\n\n"
        f"I can't answer this through the {label} API because no account "
        "is connected.\n\n"
        "To connect it:\n"
        "1. Open the **Connectors** tab in the Control Center.\n"
        "2. Sign in with Google OAuth or paste an access token.\n"
        "3. Ask me again.\n\n"
        "Or ask me to open it in the browser instead — e.g. "
        f"'open {label}' — and I'll drive the website UI."
    )


@dataclass
class ConnectorOutcome:
    ok: bool
    message: str
    route: ConnectorRoute
    executed: bool = False  # True only when an API call actually ran

    @property
    def observation(self) -> str:
        """The sanitised working-memory observation for this outcome
        (S3-R2). The loop appends it to the task's working memory so a
        later planner step can act on the connector's data. Bounded;
        the message is already scrubbed by the runner."""
        return connector_observation(self)

    def to_tool_result(self) -> dict[str, Any]:
        """The shared ToolResult shape (CONTRACTS.md §1) for this outcome."""
        return {
            "ok": self.ok,
            "tool": f"connector.{self.route.connector}.{self.route.operation}",
            "output": self.observation,
            "error": None if self.ok else self.message[:500],
            "provenance": f"connector:{self.route.connector}",
            "cost": {"planner_calls": 0, "ms": 0},
        }


def connector_observation(outcome: ConnectorOutcome, *, limit: int = 4000) -> str:
    """Render a ConnectorOutcome as a <tool_result> observation string.

    The body is the runner's already-scrubbed message, so no secret
    from an email body or Drive file reaches the planner verbatim.
    Status vocabulary (CONTRACTS.md §6): ``ok``/``failed`` — a draft
    is never reported as sent; see the draft message itself.
    """
    status = "ok" if outcome.ok else "failed"
    name = f"connector.{outcome.route.connector}.{outcome.route.operation}"
    body = (outcome.message or "")[:limit]
    return (
        f"<tool_result name=\"{name}\" provenance=\"connector:{outcome.route.connector}\" "
        f"status=\"{status}\">\n{body}\n</tool_result>"
    )


async def _default_confirm(message: str, action: dict[str, Any], record: Any, task_id: str) -> bool:
    """Confirmation through the loop's own gate (production path)."""
    from .loop_gates import _ConfirmGate, _request_confirmation

    gate = _ConfirmGate(record)
    return await _request_confirmation(
        gate, action, task_id, _CONFIRM_TIMEOUT_S, custom_message=message
    )


def _audit(connector: str, operation: str, outcome: str, task_id: str) -> None:
    try:
        from .audit import append_event

        append_event(
            "connector_call",
            f"connector {connector} op {operation} outcome {outcome}",
            actor="system",
            task_id=task_id,
        )
    except Exception:
        pass


def _scrub_answer(text: str) -> str:
    """Scrub credential/PII patterns from an operator-facing answer.

    The shared scrubber misses a bare labelled value ("my password
    is hunter2") — a pattern email bodies do contain — so labelled
    secrets are redacted here too. An unlabelled bare value cannot be
    told apart from ordinary text and is left alone (the operator
    owns the mailbox; this scrub is defence-in-depth for the
    persisted chat copy, not a leak to a third party).
    """
    try:
        from .privacy import scrub_persistent

        text = str(scrub_persistent(text))
    except Exception:
        pass
    try:
        import re

        text = re.sub(
            r"(?i)\b(password|passwd|pwd|api[-_ ]?key|secret|token|otp)\b"
            r"(\s*(?:is|:|=)\s*)\S+",
            r"\1\2[redacted]",
            text,
        )
    except Exception:
        pass
    return text


def _gmail_search_answer(connector: Any, query: str, limit: int = 10) -> str:
    summaries = connector.summarize_messages(query, limit=limit)
    if not summaries:
        return (
            "No emails matched"
            + (f" '{query}'" if query else "")
            + " in your Gmail inbox."
        )
    lines = ["Here is what I found in your Gmail inbox:\n"]
    for s in summaries[:10]:
        frm = (s.get("from") or "(unknown sender)").strip()
        subj = (s.get("subject") or "(no subject)").strip()
        snip = (s.get("snippet") or "").strip()
        lines.append(f"• **{subj}** — from {frm}")
        if snip:
            lines.append(f"  {snip[:240]}")
    lines.append(f"\n({len(summaries)} result(s) via the Gmail connector.)")
    return "\n".join(lines)


def _gmail_list_answer(connector: Any, limit: int = 10) -> str:
    data = connector.list_messages(query="", limit=limit)
    messages = data.get("messages", []) if isinstance(data, dict) else []
    if not messages:
        return "Your Gmail inbox is empty (no messages returned)."
    lines = ["Here is your Gmail inbox:\n"]
    for item in messages[:10]:
        try:
            msg = connector.get_message(item["id"], format="metadata")
        except Exception:
            lines.append(f"• message {item.get('id', '?')} (could not load details)")
            continue
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", [])
        }
        frm = (headers.get("from") or "(unknown sender)").strip()
        subj = (headers.get("subject") or "(no subject)").strip()
        snip = (msg.get("snippet") or "").strip()
        lines.append(f"• **{subj}** — from {frm}")
        if snip:
            lines.append(f"  {snip[:240]}")
    lines.append(f"\n({len(messages)} message(s) via the Gmail connector.)")
    return "\n".join(lines)


def _drive_search_answer(connector: Any, query: str, limit: int = 10) -> str:
    files = connector.search_documents(query, limit=limit)
    if not files:
        return f"No files matching '{query}' were found in your Google Drive."
    lines = [f"Here is what I found in your Google Drive for '{query}':\n"]
    for f in files[:10]:
        name = (f.get("name") or "(unnamed)").strip()
        link = f.get("webViewLink") or ""
        lines.append(f"• **{name}**" + (f" — {link}" if link else ""))
    lines.append(f"\n({len(files)} result(s) via the Google Drive connector.)")
    return "\n".join(lines)


async def _run_mailcal_operation(connector: Any, route: ConnectorRoute, instruction: str,
                               confirm: Any) -> str:
    """Execute one MailCal sandbox route (Stream C).

    Operation ``review`` is the multi-step NL review: list the test
    inbox -> surface deadlines -> draft a reply to the internship
    email -> prepare a calendar event for the earliest deadline.
    Draft + event are each gated on their own confirmation (the
    caller approves via the loop's confirmation gate); a declined
    step is reported, never silently skipped, and the other steps
    still run. Nothing is ever sent: the sandbox has no send
    endpoint. Individual operations (``search``/``list``/``get``/
    ``draft``/``create_event``) are the single-op forms.

    Display note: the runner's answer is scrubbed before return, and
    the shared scrubber's phone pattern matches any ISO date
    (``2026-12-01`` reads as 10 digits with hyphens) and any email
    address — so deadlines are rendered as human dates (``1 Dec
    2026``) and senders by display name (``Demo Careers``) in the
    message. The ISO dates and real addresses still go to the
    sandbox API calls unchanged; only the prose rendering differs.
    """

    def _human_date(iso: str) -> str:
        try:
            from datetime import datetime as _dt
            return _dt.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%-d %b %Y")
        except Exception:
            return str(iso)

    def _sender(m: dict) -> str:
        return (m.get("sender_name") or m.get("from") or "(unknown sender)").strip()
    op = route.operation
    if op in ("search", "list"):
        msgs = connector.search(route.query, limit=10) if route.query else connector.list_messages(limit=10)
        if not msgs:
            return "The test inbox has no messages matching that." + (f" (query: '{route.query}')" if route.query else "")
        lines = ["Here is what I found in your **test inbox** (MailCal Sandbox — synthetic data):\n"]
        for m in msgs[:10]:
            dl = (f" — deadline {_human_date(m['deadline'])} ({m.get('deadline_kind')})"
                  if m.get("deadline") else "")
            lines.append(f"• **{m.get('subject') or '(no subject)'}** — from {_sender(m)}{dl}")
            if m.get("snippet"):
                lines.append(f"  {m['snippet'][:240]}")
        lines.append(f"\n({len(msgs)} message(s) via the MailCal Sandbox connector.)")
        return "\n".join(lines)
    if op == "get":
        mid = (route.params.get("message_id") or route.query or "").strip()
        if not mid:
            return "I need a message id to read it — e.g. 'read test message m1'."
        m = connector.get_message(mid)
        if not m:
            return f"No test message with id '{mid}' exists."
        atts = m.get("attachments") or []
        out = [f"**{m.get('subject') or '(no subject)'}** — from {m.get('from')}"]
        if m.get("deadline"):
            out.append(f"Deadline: {_human_date(m['deadline'])} ({m.get('deadline_kind')})")
        if m.get("body"):
            out.append(f"\n{m['body'][:2000]}")
        if atts:
            out.append("\nAttachments: " + ", ".join(f"{a.get('filename')} ({a.get('id')})" for a in atts))
        return "\n".join(out)
    if op == "draft":
        to = (route.params.get("to") or "").strip()
        subject = (route.params.get("subject") or "").strip()
        body = (route.params.get("body") or "").strip()
        if not to:
            return "I couldn't tell who the draft is for."
        ok = await confirm(
            f"Create a test-inbox draft to {to}"
            + (f" with subject '{subject}'" if subject else "")
            + "? The draft is stored in the MailCal Sandbox only; nothing is sent.",
            {"type": "connector_draft", "connector": "mailcal_sandbox", "to": to, "subject": subject},
        )
        if not ok:
            return "Draft not created — you declined the confirmation."
        draft = connector.create_draft(to, subject, body or "(no body)")
        return (f"Draft created in the test inbox to {to}"
                + (f" (draft id {draft.get('id', '')})." if draft.get("id") else ".")
                + " Nothing was sent — the sandbox cannot send mail.")
    if op == "create_event":
        title = (route.params.get("title") or "").strip()
        start = (route.params.get("start") or "").strip()
        end = (route.params.get("end") or "").strip()
        if not title or not start or not end:
            return "I need a title, start and end for the calendar event."
        ok = await confirm(
            f"Create this test-calendar event?\n• Event: {title}\n• When: {start} to {end}",
            {"type": "connector_create", "connector": "mailcal_sandbox"},
        )
        if not ok:
            return "Calendar event not created — you declined the confirmation."
        ev = connector.create_event(title, start, end,
                                    location=route.params.get("location") or "",
                                    description=route.params.get("description") or "")
        return (f"Calendar event created in the test calendar: '{ev.get('title')}'"
                + (f" — receipt: event id {ev.get('id')}." if ev.get("id") else "."))

    if op != "review":
        return f"Unsupported MailCal Sandbox operation '{op}'."

    # -- review: inbox -> deadlines -> draft reply -> calendar event --
    messages = connector.list_messages(limit=50)
    unread = [m for m in messages if not m.get("is_read")]
    deadlines = sorted(
        (m for m in messages if m.get("deadline")),
        key=lambda m: str(m.get("deadline")),
    )
    lines = [
        f"Reviewed your **test inbox** (MailCal Sandbox, demo@demo.test — synthetic data): "
        f"{len(messages)} message(s), {len(unread)} unread.",
    ]
    if deadlines:
        lines.append("\n**Important deadlines:**")
        for m in deadlines:
            lines.append(
                f"• {_human_date(m['deadline'])} — {m.get('subject') or '(no subject)'} "
                f"({m.get('deadline_kind') or 'deadline'}, from {_sender(m)})")
    else:
        lines.append("\nNo messages in the test inbox carry a deadline.")
    receipt_lines: list[str] = []

    # Draft a reply to the internship email (the application-deadline
    # message, or the newest internship message). The reply target is
    # the message's real sender, resolved from the inbox — never
    # guessed from the instruction text.
    intern = next((m for m in deadlines if (m.get("deadline_kind") or "") == "application"),
                  None)
    if intern is None:
        found = connector.search("internship", limit=5)
        intern = found[0] if found else None
    if intern is not None:
        full = connector.get_message(intern["id"]) or intern
        to = (full.get("from") or "").strip()
        subject = (full.get("subject") or "").strip()
        if subject.lower().startswith("re:"):
            subject = subject[3:].strip()
        deadline = full.get("deadline") or "the stated deadline"
        body = (
            "Hi Demo Careers Team,\n\n"
            "Thank you for the test internship update. I noted that "
            f"applications close on {deadline} and I am preparing my "
            "application for the Software Intern role (Test Cohort 2027).\n\n"
            "Could you confirm the next steps after I submit my resume?\n\n"
            "Thanks,\nDemo User\n"
            "(Draft only — prepared in the MailCal Sandbox test inbox; never sent.)"
        )
        approved = await confirm(
            f"Create a draft reply to the internship email?\n"
            f"• To: {_sender(full)}\n• Subject: Re: {subject}\n"
            f"• Body (first line): {body.splitlines()[2][:120]}\n"
            "The draft is stored in the MailCal Sandbox only; nothing is sent.",
            {"type": "connector_draft", "connector": "mailcal_sandbox", "to": to, "subject": f"Re: {subject}"},
        )
        if approved:
            draft = connector.create_draft(to, f"Re: {subject}", body)
            did = draft.get("id", "") if isinstance(draft, dict) else ""
            receipt_lines.append(
                f"Draft reply created to {_sender(full)} (draft id {did}). Nothing was sent — the sandbox cannot send mail.")
        else:
            receipt_lines.append("Draft reply NOT created — you declined the confirmation.")
    else:
        receipt_lines.append("No internship email found in the test inbox, so no draft was created.")

    # Prepare a calendar event for the earliest deadline, for approval.
    if deadlines:
        first = deadlines[0]
        day = str(first["deadline"])
        title = f"Deadline: {first.get('subject') or 'test inbox deadline'}"
        start, end = f"{day}T09:00:00+05:30", f"{day}T10:00:00+05:30"
        day_human = _human_date(day)
        approved = await confirm(
            f"Create this test-calendar event?\n• Event: {title}\n• When: {day_human}, 09:00–10:00 IST\n"
            "• Source: the earliest deadline in your test inbox. The event is created "
            "in the MailCal Sandbox calendar only.",
            {"type": "connector_create", "connector": "mailcal_sandbox"},
        )
        if approved:
            ev = connector.create_event(
                title, start, end,
                description=f"Prepared from test inbox message '{first.get('subject')}' "
                            f"(from {_sender(first)}, deadline {day_human}). For approval — demo data only.")
            eid = ev.get("id", "") if isinstance(ev, dict) else ""
            receipt_lines.append(
                f"Calendar event created: '{title}' on {day_human} 09:00–10:00 IST — receipt: event id {eid}.")
        else:
            receipt_lines.append("Calendar event NOT created — you declined the confirmation.")
    else:
        receipt_lines.append("No deadline found, so no calendar event was prepared.")

    lines.append("\n**Actions:**")
    lines.extend(f"• {r}" for r in receipt_lines)
    lines.append("\nReal Gmail / Google Calendar were not used — no test account is connected.")
    return "\n".join(lines)


async def run_connector_route(
    route: ConnectorRoute,
    *,
    task_id: str = "",
    instruction: str = "",
    record: Any = None,
    confirm_fn: Optional[Callable[[str, dict[str, Any]], Awaitable[bool]]] = None,
    oauth_manager: Any = None,
) -> ConnectorOutcome:
    """Execute one routed connector operation.

    ``instruction`` is the raw user instruction (the calendar handler
    parses title/time from it). ``confirm_fn(message, action)`` is
    awaited for mutating operations; when omitted, the production
    confirmation gate on ``record`` is used (``record`` is then
    required). ``oauth_manager`` is injectable for tests; the
    process-wide manager is the default.
    """
    label = CONNECTOR_LABELS.get(route.connector, route.connector)

    async def _confirm(message: str, action: dict[str, Any]) -> bool:
        if confirm_fn is not None:
            return bool(await confirm_fn(message, action))
        if record is None:
            return False
        return await _default_confirm(message, action, record, task_id)

    # -- Stream C: MailCal sandbox -------------------------------------
    # The sandbox is not an OAuth provider (oauth_manager has no
    # "mailcal_sandbox" entry) — it is a loopback demo with a fixed
    # demo token, so it is instantiated directly here. Its origin is
    # 127.0.0.1, which is exempt from the Google-only egress
    # allow-list: a loopback demo call is not external egress. Real
    # Gmail / Google Calendar are NOT used by this branch and remain
    # BLOCKED without a configured test account.
    if route.connector == "mailcal_sandbox":
        from .connectors.mailcal_sandbox import MailCalSandboxConnector

        try:
            _mc = MailCalSandboxConnector()
            _mc.health()
        except Exception as exc:
            _audit(route.connector, route.operation, "unreachable", task_id)
            return ConnectorOutcome(
                False,
                "The MailCal Sandbox (test inbox) is not reachable at "
                "http://127.0.0.1:8126 — start it first "
                "(demo-sandboxes/mailcal: uvicorn app:app --port 8126). "
                f"Health check failed: {exc}",
                route,
            )
        try:
            _msg = await _run_mailcal_operation(_mc, route, instruction, _confirm)
        except Exception as exc:
            _audit(route.connector, route.operation, "failed", task_id)
            return ConnectorOutcome(
                False, f"The MailCal Sandbox call failed: {exc}", route
            )
        _audit(route.connector, route.operation, "ok", task_id)
        return ConnectorOutcome(True, _scrub_answer(_msg), route, executed=True)

    if oauth_manager is None:
        from .connectors.oauth import oauth_manager as _manager

        oauth_manager = _manager

    try:
        connector = oauth_manager.connector(route.connector)
    except Exception:
        _audit(route.connector, route.operation, "unknown_connector", task_id)
        return ConnectorOutcome(False, not_connected_message(route.connector), route)

    if not getattr(connector, "configured", False):
        _audit(route.connector, route.operation, "not_connected", task_id)
        return ConnectorOutcome(False, not_connected_message(route.connector), route)

    origin = getattr(connector, "origin", "")
    if origin and not egress_allowed(origin, extra_hosts=list(CONNECTOR_EGRESS_HOSTS)):
        _audit(route.connector, route.operation, "egress_denied", task_id)
        return ConnectorOutcome(
            False,
            f"The {label} API host is not on the connector egress allow-list; "
            "refusing to call it. This is a configuration error, not a "
            "connection problem.",
            route,
        )

    # -- Mutating operations: confirm first, execute only on approval.
    if route.mutating:
        if route.connector == "gmail" and route.operation == "draft":
            to = (route.params.get("to") or "").strip()
            subject = (route.params.get("subject") or "").strip()
            if not to:
                return ConnectorOutcome(
                    False,
                    "I couldn't tell who the draft is for — say who to email, "
                    "e.g. 'draft an email to priya@example.com about the report'.",
                    route,
                )
            approved = await _confirm(
                f"Create a Gmail draft to {to}"
                + (f" with subject '{subject}'" if subject else " (no subject yet)")
                + "? The draft is created in your account; nothing is sent.",
                {"type": "connector_draft", "connector": "gmail", "to": to, "subject": subject},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Draft not created — you declined the confirmation.", route)
            try:
                res = connector.draft_email(to, subject, route.params.get("body") or "")
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Creating the Gmail draft failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            draft_id = res.get("id", "") if isinstance(res, dict) else ""
            return ConnectorOutcome(
                True,
                f"Draft created in Gmail to {to}"
                + (f" with subject '{subject}'" if subject else "")
                + (f" (draft id {draft_id})." if draft_id else ".")
                + " Nothing was sent — open Gmail to review and send it.",
                route,
                executed=True,
            )

        if route.connector == "gmail" and route.operation == "trash":
            try:
                listing = connector.list_messages(query=route.query, limit=1)
                messages = listing.get("messages", []) if isinstance(listing, dict) else []
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Searching Gmail failed: {exc}", route)
            if not messages:
                return ConnectorOutcome(
                    False, f"No email matching '{route.query}' was found, so nothing was trashed.", route
                )
            message_id = messages[0].get("id", "")
            try:
                msg = connector.get_message(message_id, format="metadata")
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Loading the email failed: {exc}", route)
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in msg.get("payload", {}).get("headers", [])
            }
            subj = (headers.get("subject") or "(no subject)").strip()
            frm = (headers.get("from") or "(unknown sender)").strip()
            approved = await _confirm(
                f"Move this email to Trash?\n• Subject: {subj}\n• From: {frm}\n"
                "Trash is reversible for 30 days; the email is not permanently deleted.",
                {"type": "connector_trash", "connector": "gmail", "subject": subj},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Nothing was trashed — you declined the confirmation.", route)
            try:
                connector.trash_message(message_id)
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Moving the email to Trash failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            return ConnectorOutcome(
                True,
                f"Moved to Trash: '{subj}' from {frm}. You can restore it from Gmail Trash within 30 days.",
                route,
                executed=True,
            )

        if route.connector == "google_calendar" and route.operation == "create":
            # REVIEW (Task 14 audit): parse BEFORE confirming. The
            # operator must approve the actual event (title + the time
            # that will be booked), and an instruction that does not
            # parse as a create must fail here — the handler falls
            # through to a listing for such input, so confirming it
            # would approve a creation and deliver a list of existing
            # events instead. The named time is shown as written AND
            # as booked: the parser interprets the day, clock time and
            # timezone (IST default), falling back to tomorrow 10:00
            # IST when the instruction gives no usable time, so the
            # two can differ and the operator must see that before
            # approving.
            from .connectors.calendar_companion import parse_calendar_create

            _parsed = parse_calendar_create(instruction)
            if _parsed is None:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(
                    False,
                    "I couldn't tell what event to create from that — say the "
                    "title and when, e.g. 'schedule a meeting called Standup "
                    "on Monday at 10am'. Nothing was created.",
                    route,
                )
            _when = _parsed["start"].strftime("%A, %B %d, %Y at %I:%M %p %Z")
            _attendees = _parsed["attendees"]
            approved = await _confirm(
                "Create this calendar event?\n"
                f"• Event: {_parsed['title']}\n"
                f"• When: {_when} (you wrote: '{_parsed['time_text']}')"
                + (f"\n• Attendees: {', '.join(_attendees)}" if _attendees else "")
                + "\nThe event is created on your primary calendar and invites are sent to any attendees you named.",
                {"type": "connector_create", "connector": "google_calendar"},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Calendar event not created — you declined the confirmation.", route)
            try:
                from .connectors.calendar_companion import handle_calendar_instruction

                answer = await handle_calendar_instruction(instruction, chat_id=None)
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Creating the calendar event failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            return ConnectorOutcome(True, _scrub_answer(answer), route, executed=True)

        return ConnectorOutcome(False, f"Unsupported mutating operation '{route.operation}' for {label}.", route)

    # -- Reads: execute directly (read-only; no gate needed, like the
    #    MCP registry's read-only tools).
    try:
        if route.connector == "gmail":
            if route.operation == "search":
                answer = _gmail_search_answer(connector, route.query)
            else:
                answer = _gmail_list_answer(connector)
        elif route.connector == "google_calendar":
            from .connectors.calendar_companion import handle_calendar_instruction

            answer = await handle_calendar_instruction(instruction, chat_id=None)
        elif route.connector == "google_drive":
            answer = _drive_search_answer(connector, route.query)
        else:
            return ConnectorOutcome(False, f"Unknown connector '{route.connector}'.", route)
    except Exception as exc:
        _audit(route.connector, route.operation, "failed", task_id)
        return ConnectorOutcome(
            False,
            f"The {label} API call failed: {exc}. If this keeps happening, "
            "reconnect the account in Control Center → Connectors.",
            route,
        )
    _audit(route.connector, route.operation, "ok", task_id)
    return ConnectorOutcome(True, _scrub_answer(answer), route, executed=True)


async def run_connector_route_as_observation(
    route: ConnectorRoute,
    *,
    task_id: str = "",
    instruction: str = "",
    record: Any = None,
    confirm_fn: Optional[Callable[[str, dict[str, Any]], Awaitable[bool]]] = None,
    oauth_manager: Any = None,
    working_memory: dict[str, Any] | None = None,
) -> tuple[ConnectorOutcome, str]:
    """Run a routed connector operation and return its observation.

    Same arguments as :func:`run_connector_route`, plus an optional
    ``working_memory`` dict: when given, the outcome's sanitised
    ``<tool_result>`` observation is appended to it (via
    ``tool_registry.append_tool_result_to_working_memory``) so the next
    planner step can consume the connector's data. Returns
    ``(outcome, observation)``. The caller — the agent loop — decides
    what happens next: it must NOT return the message as a terminal
    answer when the task has further steps (S3-R2); the observation is
    what lets the loop continue.
    """
    outcome = await run_connector_route(
        route, task_id=task_id, instruction=instruction, record=record,
        confirm_fn=confirm_fn, oauth_manager=oauth_manager,
    )
    if working_memory is not None:
        from .tool_registry import append_tool_result_to_working_memory

        append_tool_result_to_working_memory(working_memory, outcome.to_tool_result())
    return outcome, outcome.observation
