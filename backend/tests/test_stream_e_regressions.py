"""Stream E regression tests (NAVIGATOR audit, demo 2026-11-27).

Two groups:

A. Stream E's own fixes — these MUST pass. They pin the research
   hardening (R4-7, R4-8, R4-9), the citation-map natural sort, resume
   intake, and job discovery/ranking/dedup/expiry (R4-10). All are
   unit-level, deterministic, no network, no live provider.

B. P0 pins for fixes owned by other streams — these pin the contract
   (status vocabulary, OCR redaction, false-completion guard,
   chat-with-preference). The guard + preference pins pass on this
   worktree; the email-draft and OCR pins FAIL here by design: the
   fixes live in Stream A's worktree, already merged to main
   (privacy leak 0.0417, draft-only dispatch), so these tests pass
   once E merges into main. Do not weaken them to pass here.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# A1. Chart refuses to fabricate values (R4-8)
# ---------------------------------------------------------------------------

def test_chart_refuses_non_numeric_table(tmp_path):
    from app.research.models import DataTable
    from app.research.visuals.chart_generator import generate_chart_from_table

    table = DataTable(
        table_id="tbl_1", title="Qualitative comparison",
        headers=["Item", "Note"],
        rows=[["Alpha", "fast"], ["Beta", "slow"], ["Gamma", "medium"]],
    )
    with pytest.raises(ValueError, match="Refusing to chart"):
        generate_chart_from_table(table, str(tmp_path / "chart.png"))
    assert not (tmp_path / "chart.png").exists()


def test_chart_still_renders_numeric_table(tmp_path):
    from app.research.models import DataTable
    from app.research.visuals.chart_generator import generate_chart_from_table

    table = DataTable(
        table_id="tbl_1", title="Scores",
        headers=["Item", "Score"],
        rows=[["Alpha", "10"], ["Beta", "20"], ["Gamma", "15"]],
    )
    out = generate_chart_from_table(table, str(tmp_path / "chart.png"))
    assert Path(out).exists() and Path(out).stat().st_size > 0


# ---------------------------------------------------------------------------
# A2. Executive synthesis is labelled model text (R4-7)
# ---------------------------------------------------------------------------

def _package_with_summary() -> "object":
    from app.research.models import (
        EvidenceClassification,
        EvidenceItem,
        ResearchClaim,
        ResearchKnowledgePackage,
        ResearchTopic,
        SourceReference,
    )

    pkg = ResearchKnowledgePackage(
        package_id="pkg_e_test",
        topic=ResearchTopic(
            title="Transformer attention",
            objective="Explain attention",
            findings_summary="Attention is the core mechanism of the Transformer.",
        ),
    )
    pkg.add_source(SourceReference(
        source_id="src_1", url="https://arxiv.org/abs/1706.03762",
        domain="arxiv.org", title="Attention Is All You Need",
    ))
    pkg.add_evidence(EvidenceItem(
        evidence_id="ev_1", source_id="src_1",
        quote="Attention is all you need.",
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_1",
        statement="The Transformer relies on self-attention.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        primary_source_id="src_1", evidence_item_ids=["ev_1"],
    ))
    return pkg


def test_synthesis_labelled_model_synthesis_not_executive(tmp_path):
    # The DOCX/PDF renderers show the synthesis from the document plan;
    # the Markdown renderer omits findings_summary entirely (safe by
    # omission), so the label is verified where the text is shown.
    import docx

    from app.research.renderers import render_artifact

    pkg = _package_with_summary()
    out = render_artifact(pkg, "docx", str(tmp_path / "report.docx"))
    texts = [p.text for p in docx.Document(out).paragraphs if p.text.strip()]
    assert any("Model synthesis" in t for t in texts)
    assert "verify against the verified claims below" in " ".join(texts)
    assert not any("Executive Synthesis" in t for t in texts)


# ---------------------------------------------------------------------------
# A3. Hardcoded formulas/diagrams are labelled templates (R4-8)
# ---------------------------------------------------------------------------

def test_template_formula_and_diagram_labels(tmp_path, monkeypatch):
    from app import db as app_db
    from app.loop_research import _finalize_research_artifacts

    monkeypatch.setattr(app_db, "save_artifact", lambda *a, **k: None)
    monkeypatch.setattr(app_db, "save_research_package", lambda *a, **k: None)
    pkg = _package_with_summary()
    working_memory: dict = {
        "research_knowledge_package": pkg.to_dict(),
        "instruction": "research transformer attention",
        "requested_format": "docx",
    }
    try:
        art = _finalize_research_artifacts(
            "e_test_task", "default", working_memory,
            "Transformer attention research summary.",
        )
        assert art is not None
        out_pkg = working_memory["research_knowledge_package"]
        formulas = out_pkg.get("formulas") or []
        assert formulas, "transformer topic should still get the template formulas"
        assert all("Template formula" in (f.get("explanation") or "") for f in formulas)
        visuals = out_pkg.get("visuals") or []
        diagrams = [v for v in visuals if v.get("asset_type") == "diagram"]
        assert diagrams, "transformer topic should still get the template diagram"
        assert all("Template diagram" in (v.get("caption") or "") for v in diagrams)
    finally:
        import shutil

        shutil.rmtree(
            Path(__file__).resolve().parents[1] / "data" / "artifacts" / "e_test_task",
            ignore_errors=True,
        )


# ---------------------------------------------------------------------------
# A4. Renderer failures surface instead of vanishing (R4-9)
# ---------------------------------------------------------------------------

def test_renderer_failure_is_surfaced_in_summary(monkeypatch):
    from app import loop_research
    from app.loop_research import _finalize_research_summary

    monkeypatch.setattr(
        "app.research.renderers.render_artifact",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    pkg = _package_with_summary()
    working_memory: dict = {
        "is_research": True,
        "research_knowledge_package": pkg.to_dict(),
        "instruction": "research transformer attention",
        "requested_format": "docx",
    }
    summary = _finalize_research_summary(
        "e_test_task2", "default", working_memory, "Research done."
    )
    assert "Deliverable failed" in summary
    assert "docx" in summary.lower() or "render" in summary.lower()
    assert working_memory.get("research_artifact_errors")


# ---------------------------------------------------------------------------
# A5. Citation map is natural order (src_2 before src_10)
# ---------------------------------------------------------------------------

def test_citation_map_natural_order_with_12_sources():
    from app.research.models import ResearchKnowledgePackage, ResearchTopic, SourceReference
    from app.research.renderers.base import BaseRenderer

    pkg = ResearchKnowledgePackage(
        package_id="pkg_e_cite",
        topic=ResearchTopic(title="Twelve sources", objective="cite them"),
    )
    for i in range(1, 13):
        pkg.add_source(SourceReference(
            source_id=f"src_{i}", url=f"https://example.com/{i}",
            domain="example.com", title=f"Source {i}",
        ))
    cit_map = BaseRenderer.build_citation_map(pkg)
    assert cit_map["src_1"] == 1
    assert cit_map["src_2"] == 2
    assert cit_map["src_9"] == 9
    assert cit_map["src_10"] == 10
    assert cit_map["src_12"] == 12


# ---------------------------------------------------------------------------
# A6. Resume intake: text extraction + parsing (R4-10)
# ---------------------------------------------------------------------------

RESUME_TXT = """Aarav Sharma
B.Tech Information Technology, Vasavi College of Engineering, Hyderabad
Email: aarav.sharma.synthetic@example.com | Phone: +91 98765 43210
Location: Hyderabad, open to Remote

Skills: Python, FastAPI, SQL, JavaScript, React, Git, Docker, HTML, CSS
Experience: 0 years (fresher, 2026 graduate). Built 3 academic projects:
a REST API with FastAPI + PostgreSQL, a React dashboard, and a data
analysis notebook with Pandas.
Interests: backend developer roles, Python developer roles, software
engineer intern positions.
"""


def test_resume_txt_extraction_and_parsing():
    from app.extractors.resume import extract_text, parse_resume

    text = extract_text(RESUME_TXT.encode(), "resume.txt")
    profile = parse_resume(text)
    assert profile.name == "Aarav Sharma"
    for skill in ("Python", "FastAPI", "SQL", "React", "Docker", "Git"):
        assert skill in profile.skills
    assert "backend developer" in profile.target_roles
    assert "python developer" in profile.target_roles
    assert "B.Tech" in profile.education
    assert "Hyderabad" in profile.locations
    assert "Remote" in profile.locations


def test_resume_docx_extraction(tmp_path):
    from docx import Document

    from app.extractors.resume import extract_text, parse_resume

    path = tmp_path / "resume.docx"
    doc = Document()
    for line in RESUME_TXT.splitlines():
        doc.add_paragraph(line)
    doc.save(path)
    profile = parse_resume(extract_text(path.read_bytes(), "resume.docx"))
    assert profile.name == "Aarav Sharma"
    assert "Python" in profile.skills


def test_resume_pdf_extraction(tmp_path):
    from reportlab.pdfgen import canvas

    from app.extractors.resume import extract_text, parse_resume

    path = tmp_path / "resume.pdf"
    c = canvas.Canvas(str(path))
    y = 800
    for line in RESUME_TXT.splitlines():
        c.drawString(40, y, line)
        y -= 14
    c.save()
    profile = parse_resume(extract_text(path.read_bytes(), "resume.pdf"))
    assert profile.name == "Aarav Sharma"
    assert "FastAPI" in profile.skills


def test_resume_unsupported_type_rejected():
    from app.extractors.resume import extract_text

    with pytest.raises(ValueError):
        extract_text(b"junk", "resume.exe")


def test_file_uploads_parse_resume_round_trip():
    import base64

    from app import file_uploads

    token = file_uploads.put(
        "resume.txt", base64.b64encode(RESUME_TXT.encode()).decode()
    )
    fname, profile = file_uploads.parse_resume_upload(token)
    assert fname == "resume.txt"
    assert profile.name == "Aarav Sharma"
    assert "Python" in profile.skills
    with pytest.raises(KeyError):
        file_uploads.parse_resume_upload("no-such-token")


# ---------------------------------------------------------------------------
# A7. Job ranking / dedup / expiry (R4-10) — fixture listings, no network
# ---------------------------------------------------------------------------

def _listing(title, company, url, *, days_ago=None, tags=(), snippet="", location="Remote"):
    from app.research.job_discovery import JobListing

    posted = None
    if days_ago is not None:
        posted = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return JobListing(
        title=title, company=company, url=url, source="Test",
        posted_at=posted, location=location, snippet=snippet,
        tags=list(tags),
    )


def _profile():
    from app.extractors.resume import parse_resume

    return parse_resume(RESUME_TXT)


def test_job_dedup_by_url_and_company_title():
    from app.research.job_discovery import dedup_listings

    jobs = [
        _listing("Python Developer", "Acme", "https://example.com/jobs/1"),
        _listing("Python Developer (Remote)", "Acme", "https://example.com/jobs/1?ref=x"),
        _listing("Python Developer", "Acme", "https://example.com/jobs/2"),
        _listing("Java Developer", "Acme", "https://example.com/jobs/3"),
    ]
    assert len(dedup_listings(jobs)) == 2


def test_job_expiry_drops_old_and_flags_stale():
    from app.research.job_discovery import drop_expired

    jobs = [
        _listing("Fresh", "A", "https://example.com/1", days_ago=5),
        _listing("Stale", "B", "https://example.com/2", days_ago=70),
        _listing("Expired", "C", "https://example.com/3", days_ago=120),
        _listing("Undated", "D", "https://example.com/4"),
    ]
    kept, expired = drop_expired(jobs)
    assert [j.title for j in expired] == ["Expired"]
    assert [j.title for j in kept] == ["Fresh", "Stale", "Undated"]
    assert [j for j in kept if j.title == "Stale"][0].stale is True
    assert [j for j in kept if j.title == "Fresh"][0].stale is False


def test_job_ranking_prefers_skill_and_role_match():
    from app.research.job_discovery import rank_listings

    profile = _profile()
    jobs = [
        _listing("Video Editor", "MediaCo", "https://example.com/v",
                 days_ago=2, snippet="We use JavaScript and React on our site."),
        _listing("Python Developer", "Acme", "https://example.com/p",
                 days_ago=2, tags=["python", "fastapi"]),
        _listing("Backend Engineer", "Beta", "https://example.com/b",
                 days_ago=40, tags=["python"]),
    ]
    ranked = rank_listings(profile, jobs)
    assert ranked[0].title == "Python Developer"
    assert ranked[0].score > ranked[-1].score
    assert "Python" in ranked[0].matched_skills
    assert ranked[0].match_reasons  # every score is explained


def test_job_package_has_evidence_and_no_invented_salary():
    from app.research.job_discovery import build_job_package

    profile = _profile()
    jobs = [
        _listing("Python Developer", "Acme", "https://example.com/p",
                 days_ago=2, tags=["python"]),
        _listing("Backend Engineer", "Beta", "https://example.com/b",
                 days_ago=3, tags=["python"]),
    ]
    jobs[0].salary = "$60,000–$80,000/yr (as published)"
    jobs[0].score, jobs[1].score = 70, 55
    jobs[0].matched_skills, jobs[1].matched_skills = ["Python"], ["Python"]
    jobs[0].match_reasons = ["skill match in title/tags: Python (+15)"]
    jobs[1].match_reasons = ["skill match in title/tags: Python (+15)"]
    pkg = build_job_package(profile, jobs, [
        {"source": "Test", "status": "ok", "fetched": 2},
    ])
    assert len(pkg.claims) == 2 and len(pkg.sources) == 2
    assert len(pkg.tables) == 1 and len(pkg.tables[0].rows) == 2
    for claim in pkg.claims.values():
        assert claim.evidence_item_ids, "every job claim must carry evidence"
        assert claim.classification.value == "single_source"
    salaried = pkg.claims["clm_1"].statement
    assert "$60,000" in salaried
    assert "no salary published" in pkg.claims["clm_2"].statement


def test_job_monitor_uses_existing_creation_path(tmp_path):
    from app.research.job_discovery import create_job_search_monitor

    db_path = str(tmp_path / "monitor.db")
    mission = create_job_search_monitor(
        "Python jobs for Aarav Sharma",
        "https://remoteok.com/remote-python-jobs",
        chat_id="e-test", db_path=db_path,
    )
    assert mission["status"] == "active"
    assert mission["interval_seconds"] == 3600
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT condition_type, interval_seconds, status FROM monitoring_missions WHERE id=?",
        (mission["mission_id"],),
    ).fetchone()
    conn.close()
    assert row == ("new_listings", 3600, "active")


# ---------------------------------------------------------------------------
# B1. False-completion guard (P0 pin — passes on this worktree)
# ---------------------------------------------------------------------------

def test_false_completion_guard_rejects_sourceless_research(tmp_path):
    from app.completion_verifier import verify_task_completion

    # NOTE: the instruction must classify as RESEARCH ("search for" /
    # "extract" are the classifier's triggers). A bare "research ..."
    # instruction classifies CONVERSATIONAL and never reaches the
    # research gates — a classifier gap, flagged in the E-REPORT, not
    # fixed here (completion_verifier.py is Stream A's file).
    result = verify_task_completion(
        "search for transformer architecture papers, extract the key findings and give me a PDF report",
        "completed",
        "Research complete. Here is the report.",
        working_memory={
            "research_knowledge_package": {"sources": {}, "claims": {}, "evidence": {}},
            "requested_format": "pdf",
        },
    )
    assert result.state == "failed"
    assert result.is_verified is False


def test_false_completion_guard_accepts_verified_research(tmp_path):
    from app.completion_verifier import verify_task_completion

    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-1.4 fake but non-empty")
    result = verify_task_completion(
        "search for transformer architecture papers, extract the key findings and give me a PDF report",
        "completed",
        "Research complete. Here is the report.",
        working_memory={
            "research_knowledge_package": {
                "sources": {"src_1": {"url": "https://arxiv.org/abs/1706.03762",
                                      "domain": "arxiv.org", "reliability_score": 0.92}},
                "claims": {"clm_1": {"evidence_item_ids": ["ev_1"]}},
                "evidence": {"ev_1": {"quote": "Attention is all you need."}},
            },
            "requested_format": "pdf",
            "artifacts": [{"filepath": str(artifact), "filename": "report.pdf"}],
        },
    )
    assert result.state == "completed"
    assert result.is_verified is True


# ---------------------------------------------------------------------------
# B2. Chat-with-preference (P0 pin — passes on this worktree)
# ---------------------------------------------------------------------------

def test_chat_remember_preference_round_trip(tmp_path):
    from app.memory.tiers import SemanticMemory
    from app.memory_companion import handle_remember_instruction

    db_path = str(tmp_path / "pref.db")
    answer = handle_remember_instruction(
        "remember my preferred language is Python", db_path=db_path
    )
    assert "Python" in answer
    facts = SemanticMemory().recall(db_path=db_path)
    assert any(f["fact_value"] == "Python" for f in facts)


# ---------------------------------------------------------------------------
# B3. Email dispatch is draft-only (P0 pin — FAILS on this worktree by
# design; Stream A fixed email_dispatch.py on main to say "draft", not
# "sent". Passes once E merges into main. Do not weaken.)
# NOTE (coordinator fix): the honest message says "has NOT been sent",
# so a bare `"sent" not in reply` assertion is wrong — it would fail
# the correct wording. Assert the false claim is absent instead.
# ---------------------------------------------------------------------------

def test_email_dispatch_never_claims_sent_for_a_draft(monkeypatch):
    import app.email_dispatch as dispatch
    from app.connectors import oauth as oauth_mod

    monkeypatch.setattr(
        oauth_mod.oauth_manager, "verify_connection",
        lambda provider: {"connected": True, "account": "user@example.com",
                          "access_token": "fake-token"},
    )

    class FakeGmail:
        def __init__(self, token=None):
            self.token = token

        def draft_email(self, to, subject, body):
            return {"id": "DRAFT-1"}

    monkeypatch.setattr("app.connectors.gmail.GmailConnector", FakeGmail)
    reply = asyncio.run(dispatch.handle_email_dispatch(
        "email this comparison to friend@example.com",
        recipient="friend@example.com",
        history=[{"content": "comparison of prices across stores"}],
    ))
    assert "draft" in reply.lower(), reply
    assert "Dispatched successfully" not in reply, reply
    # The honest wording states the negative explicitly.
    assert "NOT been sent" in reply, reply
    assert "has been sent" not in reply.lower().replace("has not been sent", ""), reply


# ---------------------------------------------------------------------------
# B4. OCR text is redacted before the planner (P0 pin — FAILS on this
# worktree by design; Stream A fixed privacy.py on main to route OCR
# text through the redactor. Passes once E merges into main.)
# ---------------------------------------------------------------------------

def test_ocr_text_with_otp_is_redacted(monkeypatch):
    import base64 as _b64

    from PIL import Image

    from app.contracts import BrowserObservation
    from app.planner import build_prompt
    from app.privacy import classify_and_redact
    from app import config as app_config

    monkeypatch.setattr("app.ocr.extract_ocr_text",
                        lambda *a, **k: "Your OTP is 482910")
    monkeypatch.setattr(app_config, "ALLOW_SCREENSHOTS", True, raising=False)
    monkeypatch.setattr(app_config, "OCR_ENABLED", True, raising=False)

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, "PNG")
    shot = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": "http://test.local/page", "title": "Test page",
        "interactive": [],
        "scroll": {"y": 0, "viewport": {"w": 900, "h": 600}}, "dpr": 1,
        "pageText": "Ordinary page content.",
        "screenshot": shot,
    })
    payload, _ = classify_and_redact(obs, instruction="find headphones")
    assert payload.ocr_text, "OCR text should reach the payload (then be redacted)"
    prompt = build_prompt("find headphones", payload, [])
    assert "482910" not in prompt
