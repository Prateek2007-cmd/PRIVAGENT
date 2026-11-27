"""Stream C [C-jobs-mailcal-mcp]: router/runner MailCal wiring, bounded
job pipeline, MCP proxy forwarding, comparison artifact builder.

No network except a loopback HTTP server for URL validation; no real
provider; no sandbox needed (MailCal is exercised through a fake
connector with the sandbox's real message shapes).
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.connector_router import route_instruction
from app.connector_runner import run_connector_route
from app.connector_router import ConnectorRoute
from app.mcp.discovery import proxy_env_for_stdio
from app.research.job_discovery import (
    JobListing,
    handle_job_discovery_instruction,
    is_job_discovery_instruction,
    validate_listing_urls,
)


# ---------------------------------------------------------------- router

def test_mailcal_marker_routes_to_sandbox_review():
    route = route_instruction(
        "Review my test inbox, identify important deadlines, draft a reply "
        "to the internship email, and prepare a calendar event for my approval.")
    assert route is not None
    assert route.connector == "mailcal_sandbox"
    assert route.operation == "review"


def test_mailcal_marker_beats_google_calendar():
    # "prepare a calendar event" alone routes to Google Calendar; with a
    # test-inbox marker the sandbox wins for the whole instruction.
    assert route_instruction("prepare a calendar event").connector == "google_calendar"
    route = route_instruction("Check my demo inbox and prepare a calendar event")
    assert route.connector == "mailcal_sandbox"


def test_plain_inbox_still_routes_to_real_gmail():
    route = route_instruction("check my inbox")
    assert route is not None and route.connector == "gmail"


def test_mailcal_account_marker():
    # A targeted single-message search falls through to the planner,
    # which has the mailcal_sandbox.* tools in the registry — the
    # review op would wrongly create an unrelated draft + event.
    assert route_instruction("search demo@demo.test for the bill") is None
    # ...while a review-shaped request still takes the review op.
    route = route_instruction(
        "Review my test inbox, identify important deadlines, draft a reply")
    assert route is not None and route.connector == "mailcal_sandbox"
    assert route.operation == "review"


# ---------------------------------------------------------------- runner

class FakeMailCal:
    """Sandbox-shaped fake: the S4 seed's messages, deadlines, ids."""

    def __init__(self, *, healthy=True):
        self.healthy = healthy
        self.drafts: list[dict] = []
        self.events: list[dict] = []
        self.messages = [
            {"id": "m1", "thread_id": "t-intern", "from": "careers@demo.test",
             "sender_name": "Demo Careers",
             "subject": "Test Internship — Application deadline 5 Dec 2026 [TEST]",
             "snippet": "Applications close on 5 Dec 2026.",
             "body": "Applications for the test Software Intern role close on 5 Dec 2026.",
             "deadline": "2026-12-05", "deadline_kind": "application",
             "is_read": False, "attachments": [{"id": "a1", "filename": "internship-role-test.txt"}]},
            {"id": "m4", "thread_id": "t-bill", "from": "billing@demo.test",
             "sender_name": "Demo Billing",
             "subject": "Your demo utility bill is due 1 Dec 2026 [TEST]",
             "snippet": "Test bill of 1250 due 1 Dec 2026.",
             "body": "Amount due: 1250. Due date: 1 December 2026.",
             "deadline": "2026-12-01", "deadline_kind": "bill",
             "is_read": False, "attachments": []},
        ]

    def health(self):
        if not self.healthy:
            raise ConnectionError("connection refused")
        return {"ok": True}

    def list_messages(self, query="", unread=False, limit=50):
        return list(self.messages)

    def search(self, query, limit=20):
        q = (query or "").lower()
        return [m for m in self.messages if q in (m["subject"] + m["body"]).lower()]

    def get_message(self, message_id):
        for m in self.messages:
            if m["id"] == message_id:
                return dict(m)
        return {}

    def create_draft(self, to, subject, body):
        did = f"d-test{len(self.drafts) + 1}"
        self.drafts.append({"id": did, "to": to, "subject": subject, "body": body})
        return {"id": did, "to": to, "subject": subject, "body": body}

    def create_event(self, title, start, end, location="", description=""):
        eid = f"e-test{len(self.events) + 1}"
        self.events.append({"id": eid, "title": title, "start": start, "end": end})
        return {"id": eid, "title": title, "start": start, "end": end}


def _mailcal_route():
    return ConnectorRoute(connector="mailcal_sandbox", operation="review")


def test_mailcal_review_creates_draft_and_event_with_receipts(monkeypatch):
    fake = FakeMailCal()
    monkeypatch.setattr(
        "app.connectors.mailcal_sandbox.MailCalSandboxConnector",
        lambda *a, **k: fake)
    approved: list[str] = []

    async def confirm(message, action):
        approved.append(action.get("type", ""))
        return True

    outcome = asyncio.run(run_connector_route(
        _mailcal_route(), task_id="t1",
        instruction="Review my test inbox, identify important deadlines, draft a reply "
                    "to the internship email, and prepare a calendar event for my approval.",
        confirm_fn=confirm))
    assert outcome.ok
    assert outcome.executed
    assert len(fake.drafts) == 1
    assert fake.drafts[0]["to"] == "careers@demo.test"
    assert "Re: Test Internship" in fake.drafts[0]["subject"]
    assert len(fake.events) == 1
    assert fake.events[0]["start"].startswith("2026-12-01")  # earliest deadline: the bill
    assert "d-test1" in outcome.message  # draft receipt
    assert "e-test1" in outcome.message  # event receipt
    assert "Nothing was sent" in outcome.message
    # Human dates survive the shared scrubber (ISO dates read as phone
    # numbers and are redacted); the API calls still carry ISO dates.
    assert "5 Dec 2026" in outcome.message and "1 Dec 2026" in outcome.message
    assert "2026-12-05" not in outcome.message
    assert "Demo Careers" in outcome.message
    assert approved.count("connector_draft") == 1
    assert approved.count("connector_create") == 1


def test_mailcal_review_declined_creates_nothing(monkeypatch):
    fake = FakeMailCal()
    monkeypatch.setattr(
        "app.connectors.mailcal_sandbox.MailCalSandboxConnector",
        lambda *a, **k: fake)

    async def deny(message, action):
        return False

    outcome = asyncio.run(run_connector_route(
        _mailcal_route(), task_id="t1", instruction="Review my test inbox",
        confirm_fn=deny))
    assert outcome.ok  # the review itself ran; the writes were declined
    assert fake.drafts == [] and fake.events == []
    assert "declined" in outcome.message


def test_mailcal_sandbox_down_fails_honestly(monkeypatch):
    fake = FakeMailCal(healthy=False)
    monkeypatch.setattr(
        "app.connectors.mailcal_sandbox.MailCalSandboxConnector",
        lambda *a, **k: fake)
    outcome = asyncio.run(run_connector_route(
        _mailcal_route(), task_id="t1", instruction="Review my test inbox"))
    assert not outcome.ok
    assert "8126" in outcome.message

# ------------------------------------------------------- job URL checks

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            body = b"<html><body>job</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_validate_listing_urls_keeps_only_200s(local_server):
    jobs = [
        JobListing(title="Good", company="A", url=f"{local_server}/ok", source="T"),
        JobListing(title="Gone", company="B", url=f"{local_server}/nope", source="T"),
        JobListing(title="Down", company="C", url="http://127.0.0.1:1/nope", source="T"),
    ]
    verified, report = validate_listing_urls(jobs)
    assert [j.title for j in verified] == ["Good"]
    by_title = {r["title"]: r for r in report}
    assert by_title["Good"]["verified"] is True
    assert "200" in by_title["Good"]["check"]
    assert by_title["Gone"]["verified"] is False
    assert by_title["Down"]["verified"] is False


def test_job_instruction_detector():
    assert is_job_discovery_instruction(
        "Analyze this resume, find suitable software engineering internships "
        "in Hyderabad, rank the best matches, generate an Excel spreadsheet "
        "with application links, and monitor new matching opportunities daily.")
    assert not is_job_discovery_instruction("what is the weather today")
    assert not is_job_discovery_instruction("find me a job")  # no resume, no spreadsheet
    assert not is_job_discovery_instruction("analyze this resume")  # no jobs ask


def test_job_handler_without_resume_asks_for_upload():
    msg = handle_job_discovery_instruction(
        "Analyze this resume, find internships, rank them, generate a spreadsheet.")
    assert "attach" in msg.lower()
    assert "Nothing was searched" in msg


def test_job_handler_reports_stubbed_run(monkeypatch):
    from app import file_uploads
    import base64
    resume = (
        "Asha Test\n"
        "B.Tech IT student, Hyderabad\n"
        "Skills: Python, JavaScript, React, FastAPI, SQL\n"
        "Seeking: software engineer intern\n"
    )
    token = file_uploads.put("resume-test.txt", base64.b64encode(resume.encode()).decode())
    canned = {
        "ok": True, "profile_summary": "targeting software engineer intern; skills: Python",
        "ranked_count": 3, "verified_count": 2,
        "listings": [
            {"rank": 1, "title": "Software Engineer Intern", "company": "Acme",
             "url": "https://example.com/jobs/1", "location": "Hyderabad",
             "score": 85, "matched_skills": ["Python"]},
            {"rank": 2, "title": "Backend Intern", "company": "Beta",
             "url": "https://example.com/jobs/2", "location": "Remote",
             "score": 70, "matched_skills": ["FastAPI"]},
        ],
        "reports": [], "url_report": [],
        "timings": {"discover_s": 1.0, "url_check_s": 0.5, "total_s": 2.0},
        "xlsx_path": "/tmp/job-discovery.xlsx", "xlsx_filename": "job-discovery-TEST.xlsx",
        "artifact_id": "art1", "monitor": {"mission_id": "mon-1",
                                            "target_url": "https://remoteok.com/remote-python-jobs"},
        "error": None,
    }
    monkeypatch.setattr(
        "app.research.job_discovery.run_job_discovery", lambda *a, **k: canned)
    msg = handle_job_discovery_instruction(
        "Analyze this resume, find internships, rank them, generate a spreadsheet.",
        file_token=token, chat_id="c1", task_id="t1")
    assert "job-discovery-TEST.xlsx" in msg
    assert "/api/artifacts/art1/download" in msg
    assert "mon-1" in msg
    assert "No application was submitted" in msg


# ------------------------------------------------------- proxy forwarding

def test_proxy_env_forwarded_and_secrets_never(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.test:3128")
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    monkeypatch.setenv("MY_SECRET_TOKEN", "should-never-forward")
    monkeypatch.setenv("ANTSEED_API_KEY", "should-never-forward")
    env = proxy_env_for_stdio()
    assert env["https_proxy"] == "http://proxy.test:3128"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert "MY_SECRET_TOKEN" not in env and "ANTSEED_API_KEY" not in env


# ------------------------------------------------------- comparison render

def test_build_comparison_artifact_refuses_unverified_url(tmp_path, monkeypatch):
    from app.mcp.api import _build_comparison_artifact
    with pytest.raises(ValueError, match="not a public http"):
        _build_comparison_artifact({
            "title": "X vs Y",
            "claims": [{"statement": "X is fast", "source_url": "not-a-url", "quote": "fast"}],
        })


def test_build_comparison_artifact_renders_and_registers(tmp_path, monkeypatch):
    from app.mcp.api import _build_comparison_artifact

    def fake_render(pkg, fmt, out_path, **kwargs):
        # Write the real output path (the builder stats it afterwards);
        # the file is removed below so the worktree stays clean.
        with open(out_path, "wb") as fh:
            fh.write(b"fake-docx")
        return str(out_path)

    monkeypatch.setattr("app.research.renderers.render_artifact", fake_render)
    monkeypatch.setattr("app.db.save_artifact",
                        lambda art, db_path=None: {"artifact_id": "cmp-1"})
    gh = "https://github.com/"
    out = _build_comparison_artifact({
        "title": "Llama.cpp vs Ollama",
        "format": "docx",
        "summary": "Cited comparison.",
        "claims": [
            {"statement": "llama.cpp runs LLMs on CPU.",
             "source_url": gh + "ggml-org/llama.cpp",
             "quote": "Inference of LLaMA model in pure C/C++"},
            {"statement": "Ollama runs models locally.",
             "source_url": gh + "ollama/ollama",
             "quote": "Get up and running with large language models."},
        ],
    })
    assert out["artifact_id"] == "cmp-1"
    assert out["claims"] == 2
    assert out["download"] == "/api/artifacts/cmp-1/download"
    assert len(out["sources"]) == 2
    # The fake wrote the real output path; remove it so the worktree stays clean.
    from pathlib import Path as _Path
    produced = _Path(__file__).resolve().parents[1] / "data" / "artifacts" / out["filename"]
    assert produced.exists()
    produced.unlink()
