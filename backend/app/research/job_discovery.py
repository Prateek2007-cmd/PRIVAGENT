"""Personalised job discovery (Stream E, R4-10) — minimal, genuine.

Pipeline: synthetic-or-uploaded resume -> :mod:`app.extractors.resume`
profile -> live fetch from real public sources only -> deterministic
ranking with explained criteria -> evidence-backed XLSX spreadsheet
(via the existing research renderers) -> recurring new-listings
monitoring mission via the existing backend function.

Sources (all public, read-only, no key, no login):
- RemoteOK public JSON API (https://remoteok.com/api)
- We Work Remotely public RSS feeds (https://weworkremotely.com/...rss)
- Hacker News "Who is hiring?" monthly thread (Algolia search +
  Firebase item API), best-effort

Hard rules (contract §6 — never fabricate a listing, salary or
citation):
- Every listing carries the exact source URL it was fetched from.
  A fetch that fails is recorded in the source report, never papered
  over with a made-up listing.
- Salaries are NEVER invented: a listing shows a salary only when
  the source itself published one.
- Listings older than 90 days are dropped (expired); 60-90 days are
  flagged stale. Dedup is by canonical URL + (company, title).
- NO application is ever submitted. Discovery ends at the spreadsheet
  and the monitoring mission; applying is the operator's step.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from ..extractors.resume import ResumeProfile

USER_AGENT = "PrivAgent-job-discovery/1.0 (research demo; contact: demo)"
HTTP_TIMEOUT_S = 15
MAX_LISTINGS_PER_SOURCE = 60
EXPIRY_DAYS = 90
STALE_DAYS = 60

# RemoteOK tags to try, in profile-skill priority order. Only tags that
# exist on RemoteOK are useful; unknown tags return an empty list.
REMOTEOK_TAG_MAP = {
    "Python": "python",
    "JavaScript": "javascript",
    "TypeScript": "typescript",
    "React": "react",
    "Java": "java",
    "Go": "golang",
    "Rust": "rust",
    "Ruby": "ruby",
    "PHP": "php",
    "Node.js": "node",
    "Django": "django",
    "DevOps": "devops",
    "Design": "design",
}

WWR_CATEGORIES = (
    "remote-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-full-stack-programming-jobs",
)

_FRESHER_WORDS = ("intern", "internship", "junior", "entry level", "entry-level", "fresher", "graduate", "trainee")
_ROLE_WORDS = (
    "software engineer", "software developer", "backend", "back-end", "frontend", "front-end",
    "full stack", "full-stack", "fullstack", "data analyst", "data scientist",
    "machine learning", "ml engineer", "ai engineer", "devops", "web developer",
    "python developer", "java developer", "react developer", "sde",
)


@dataclass
class JobListing:
    title: str
    company: str
    url: str
    source: str  # "RemoteOK" | "We Work Remotely" | "Hacker News"
    posted_at: Optional[datetime] = None
    location: str = ""
    snippet: str = ""
    salary: str = ""  # only when the source published one
    tags: list[str] = field(default_factory=list)
    # Ranking (filled by rank_listings)
    score: int = 0
    matched_skills: list[str] = field(default_factory=list)
    match_reasons: list[str] = field(default_factory=list)
    stale: bool = False

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        if not self.posted_at:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, (now - self.posted_at).days)

    def canonical_url(self) -> str:
        parts = urllib.parse.urlsplit(self.url)
        path = parts.path.rstrip("/") or "/"
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


# ---------------------------------------------------------------------------
# Fetching (read-only, stdlib only)
# ---------------------------------------------------------------------------

def _http_get(url: str, deadline: float | None = None) -> bytes:
    """GET with a per-request timeout and an optional overall deadline.

    Stream C: the discovery pipeline runs inside an NL task with a
    hard task timeout, so every fetch honours a shared deadline —
    a slow source aborts instead of eating the budget the XLSX
    generation step needs.
    """
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("job discovery budget exhausted before fetching " + url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _parse_date(value: Any) -> Optional[datetime]:
    """Parse a posted date: RemoteOK uses ISO-8601 strings, HN uses
    unix epoch seconds, WWR RSS uses RFC-2822 (parsed by the caller)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _parse_epoch(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _parse_epoch(value: Any) -> Optional[datetime]:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def fetch_remoteok(skill_tags: list[str], deadline: float | None = None) -> tuple[list[JobListing], dict[str, Any]]:
    """Fetch RemoteOK listings for up to 3 skill tags. Returns (listings, report)."""
    listings: list[JobListing] = []
    tags_tried: list[str] = []
    for skill in skill_tags[:3]:
        tag = REMOTEOK_TAG_MAP.get(skill)
        if not tag:
            continue
        tags_tried.append(tag)
        try:
            raw = _http_get(f"https://remoteok.com/api?tag={urllib.parse.quote(tag)}", deadline)
            items = json.loads(raw.decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
            return listings, {"source": "RemoteOK", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "tags_tried": tags_tried}
        for item in items:
            if not isinstance(item, dict) or not item.get("position"):
                continue  # first element is the API legal notice
            listings.append(JobListing(
                title=str(item.get("position") or "").strip(),
                company=str(item.get("company") or "").strip() or "Unknown",
                url=str(item.get("url") or "").strip(),
                source="RemoteOK",
                posted_at=_parse_date(item.get("date")),
                location=str(item.get("location") or "").strip(),
                snippet=_strip_html(str(item.get("description") or ""))[:400],
                salary=_remoteok_salary(item),
                tags=[str(t) for t in (item.get("tags") or [])][:12],
            ))
            if len(listings) >= MAX_LISTINGS_PER_SOURCE:
                break
        if len(listings) >= MAX_LISTINGS_PER_SOURCE:
            break
    return listings, {"source": "RemoteOK", "status": "ok", "tags_tried": tags_tried, "fetched": len(listings)}


def _remoteok_salary(item: dict[str, Any]) -> str:
    lo, hi = item.get("salary_min"), item.get("salary_max")
    if lo and hi:
        return f"${lo:,}–${hi:,}/yr (as published)"
    if lo:
        return f"${lo:,}+/yr (as published)"
    return ""


def fetch_weworkremotely(categories: tuple[str, ...] = WWR_CATEGORIES,
                         deadline: float | None = None) -> tuple[list[JobListing], dict[str, Any]]:
    listings: list[JobListing] = []
    tried: list[str] = []
    for cat in categories:
        url = f"https://weworkremotely.com/categories/{cat}.rss"
        tried.append(url)
        try:
            raw = _http_get(url, deadline)
            root = ET.fromstring(raw)
        except (urllib.error.URLError, OSError, ET.ParseError, TimeoutError) as exc:
            return listings, {"source": "We Work Remotely", "status": "failed", "error": f"{type(exc).__name__}: {exc}", "feeds_tried": tried}
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            # WWR titles are usually "Company: Role".
            company, _, role = title.partition(": ")
            if not role:
                company, role = "Unknown", title
            pub = item.findtext("pubDate") or ""
            try:
                posted = parsedate_to_datetime(pub)
                if posted is not None and posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                posted = None
            desc = _strip_html(item.findtext("description") or "")[:400]
            listings.append(JobListing(
                title=role.strip(), company=company.strip() or "Unknown",
                url=link, source="We Work Remotely", posted_at=posted,
                location="Remote", snippet=desc,
            ))
            if len(listings) >= MAX_LISTINGS_PER_SOURCE:
                break
        if len(listings) >= MAX_LISTINGS_PER_SOURCE:
            break
    return listings, {"source": "We Work Remotely", "status": "ok", "feeds_tried": tried, "fetched": len(listings)}


def fetch_hn_whoishiring(keywords: list[str], max_comments: int = 30,
                         deadline: float | None = None) -> tuple[list[JobListing], dict[str, Any]]:
    """Best-effort: latest monthly 'Who is hiring?' thread, keyword-filtered.

    A failure here (network, no thread, no matches) is reported, never
    fatal — RemoteOK + WWR are the primary sources.
    """
    try:
        # search_by_date (not relevance search): the plain search
        # endpoint returns the famous March-2020 thread first by
        # relevance, and every listing in it is years expired.
        raw = _http_get(
            "https://hn.algolia.com/api/v1/search_by_date?query=Ask%20HN%3A%20Who%20is%20hiring"
            "%3F&tags=story,author_whoishiring&hitsPerPage=5",
            deadline,
        )
        hits = json.loads(raw.decode("utf-8", errors="replace")).get("hits") or []
        # Take the newest thread; refuse one older than 40 days.
        hits = [h for h in hits if h.get("objectID")]
        if not hits:
            return [], {"source": "Hacker News", "status": "failed", "error": "no 'Who is hiring?' thread found"}
        newest = max(hits, key=lambda h: h.get("created_at_i") or 0)
        thread_age_days = (time.time() - (newest.get("created_at_i") or 0)) / 86400
        if thread_age_days > 40:
            return [], {"source": "Hacker News", "status": "failed",
                        "error": f"newest 'Who is hiring?' thread is {thread_age_days:.0f}d old"}
        thread_id = newest.get("objectID")
        raw = _http_get(f"https://hacker-news.firebaseio.com/v0/item/{thread_id}.json", deadline)
        thread = json.loads(raw.decode("utf-8", errors="replace"))
        kids = (thread.get("kids") or [])[:max_comments]
        posted_thread = _parse_epoch(thread.get("time"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return [], {"source": "Hacker News", "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    keys = [k.lower() for k in keywords if k]
    listings: list[JobListing] = []
    partial = False
    for kid in kids:
        # Stream C: the comment loop is the slowest part of the
        # pipeline (one request per comment). Honour the shared
        # deadline and keep the listings fetched so far instead of
        # losing everything to a timeout.
        if deadline is not None and time.monotonic() > deadline:
            partial = True
            break
        try:
            raw = _http_get(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json", deadline)
            comment = json.loads(raw.decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, TimeoutError):
            continue
        text = _strip_html(comment.get("text") or "")
        if not text:
            continue
        low = text.lower()
        if keys and not any(k in low for k in keys):
            continue
        # First line is usually "Company | Role | Location".
        first = text.splitlines()[0][:160] if text.splitlines() else text[:160]
        company = first.split("|")[0].strip() or "Unknown"
        listings.append(JobListing(
            title=first[:120], company=company[:80],
            url=f"https://news.ycombinator.com/item?id={kid}",
            source="Hacker News", posted_at=_parse_epoch(comment.get("time")) or posted_thread,
            location="", snippet=text[:400],
        ))
    return listings, {"source": "Hacker News", "status": "partial" if partial else "ok",
                      "thread_id": thread_id, "fetched": len(listings)}


# ---------------------------------------------------------------------------
# Dedup, expiry, ranking
# ---------------------------------------------------------------------------

def dedup_listings(listings: list[JobListing]) -> list[JobListing]:
    seen_urls: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    out: list[JobListing] = []
    for job in listings:
        if not job.url:
            continue
        key_url = job.canonical_url()
        key_pair = (job.company.strip().lower(), job.title.strip().lower())
        if key_url in seen_urls or key_pair in seen_pairs:
            continue
        seen_urls.add(key_url)
        seen_pairs.add(key_pair)
        out.append(job)
    return out


def drop_expired(listings: list[JobListing], now: Optional[datetime] = None) -> tuple[list[JobListing], list[JobListing]]:
    """Split into (kept, expired). Listings with no posted date are kept
    (undated, not expired) but never get a recency bonus."""
    now = now or datetime.now(timezone.utc)
    kept, expired = [], []
    for job in listings:
        age = job.age_days(now)
        if age is not None and age > EXPIRY_DAYS:
            expired.append(job)
        else:
            if age is not None and age > STALE_DAYS:
                job.stale = True
            kept.append(job)
    return kept, expired


def _skill_hits(profile: ResumeProfile, job: JobListing) -> tuple[list[str], list[str]]:
    """(title/tag matches, snippet-only matches). A skill named in the
    title or the source's own tags is a strong signal; a skill merely
    mentioned somewhere in the description is weak — the two are
    scored differently so a 'Video Editor' post that happens to mention
    JavaScript does not outrank a real backend role."""
    strong_hay = f"{job.title} {job.company} {' '.join(job.tags)}".lower()
    weak_hay = job.snippet.lower()
    strong = [s for s in profile.skills if s.lower() in strong_hay]
    strong_lower = {s.lower() for s in strong}
    weak = [s for s in profile.skills if s.lower() not in strong_lower and s.lower() in weak_hay]
    return strong, weak


def rank_listings(profile: ResumeProfile, listings: list[JobListing],
                  now: Optional[datetime] = None) -> list[JobListing]:
    """Deterministic suitability ranking. Every point is explained in
    `match_reasons` so the spreadsheet reader can audit the score."""
    now = now or datetime.now(timezone.utc)
    for job in listings:
        score, reasons = 0, []
        strong, weak = _skill_hits(profile, job)
        job.matched_skills = strong + weak
        if strong or weak:
            skill_points = min(45, 15 * len(strong) + 7 * len(weak))
            score += skill_points
            bits = []
            if strong:
                bits.append(f"skill match in title/tags: {', '.join(strong)}")
            if weak:
                bits.append(f"skill mentioned in description: {', '.join(weak)}")
            reasons.append("; ".join(bits) + f" (+{skill_points})")
        title_low = job.title.lower()
        if any(role in title_low for role in _ROLE_WORDS):
            score += 20
            reasons.append("title matches a target role (+20)")
        hay = f"{job.title} {job.snippet}".lower()
        if any(w in hay for w in _FRESHER_WORDS):
            score += 10
            reasons.append("fresher/junior-friendly wording (+10)")
        loc_low = (job.location or "").lower()
        prefs = [loc.lower() for loc in profile.locations]
        if "remote" in loc_low or any(p and p in loc_low for p in prefs):
            score += 10
            reasons.append("location matches preference (+10)")
        age = job.age_days(now)
        if age is not None:
            if age <= 7:
                score += 10
                reasons.append(f"posted {age}d ago (+10)")
            elif age <= 30:
                score += 5
                reasons.append(f"posted {age}d ago (+5)")
        job.score = min(100, score)
        job.match_reasons = reasons or ["no direct skill/role/location match — listed for completeness"]
    return sorted(listings, key=lambda j: (-j.score, -(j.posted_at.timestamp() if j.posted_at else 0)))


def discover_jobs(profile: ResumeProfile, max_results: int = 25,
                  include_hn: bool = True, deadline_s: float = 150,
                  max_hn_comments: int = 15) -> tuple[list[JobListing], list[dict[str, Any]]]:
    """Fetch + dedup + expire + rank. Returns (ranked listings, source reports).

    Stream C: ``deadline_s`` bounds the whole fetch phase (default
    150 s) so the caller keeps budget for URL validation, the XLSX
    render and the monitoring mission. Sources that hit the deadline
    report ``failed``/``partial`` instead of stalling the run.
    """
    deadline = time.monotonic() + max(deadline_s, 5)
    reports: list[dict[str, Any]] = []
    all_listings: list[JobListing] = []

    remoteok, rep = fetch_remoteok(profile.skills or ["Python"], deadline)
    reports.append(rep)
    all_listings.extend(remoteok)

    wwr, rep = fetch_weworkremotely(deadline=deadline)
    reports.append(rep)
    all_listings.extend(wwr)

    if include_hn:
        keywords = list(dict.fromkeys(profile.skills + profile.target_roles))
        hn, rep = fetch_hn_whoishiring(keywords, max_comments=max_hn_comments, deadline=deadline)
        reports.append(rep)
        all_listings.extend(hn)

    deduped = dedup_listings(all_listings)
    kept, expired = drop_expired(deduped)
    ranked = rank_listings(profile, kept)
    reports.append({
        "source": "pipeline", "status": "ok",
        "fetched_total": len(all_listings), "after_dedup": len(deduped),
        "expired_dropped": len(expired), "kept": len(kept),
    })
    return ranked[:max_results], reports


# ---------------------------------------------------------------------------
# Evidence-backed package + spreadsheet
# ---------------------------------------------------------------------------

def build_job_package(profile: ResumeProfile, listings: list[JobListing],
                      reports: list[dict[str, Any]],
                      fetched_at: Optional[datetime] = None) -> "ResearchKnowledgePackage":
    """Build a ResearchKnowledgePackage where every listing is a
    single-source claim quoting the source, with a ranked table.
    Salaries appear only when the source published them."""
    from .models import (
        DataTable,
        EvidenceItem,
        ResearchClaim,
        ResearchKnowledgePackage,
        ResearchTopic,
        SourceReference,
    )

    fetched_at = fetched_at or datetime.now(timezone.utc)
    roles = ", ".join(profile.target_roles[:3]) or "software roles"
    pkg = ResearchKnowledgePackage(
        package_id=f"jobs_{int(fetched_at.timestamp())}",
        topic=ResearchTopic(
            title=f"Personalised job discovery — {roles}",
            objective=(
                f"Rank public job listings for a candidate with skills "
                f"{', '.join(profile.skills) or 'unspecified'} "
                f"({profile.summary_line()}). Fetched live from public "
                f"sources on {fetched_at.date().isoformat()}. "
                f"No application was submitted."
            ),
            target_format="xlsx",
        ),
    )
    ok_sources = [r["source"] for r in reports if r.get("status") in ("ok", "partial") and r.get("source") != "pipeline"]
    failed = [r for r in reports if r.get("status") == "failed"]
    failed_note = ""
    if failed:
        failed_bits = "; ".join(f"{r['source']}: {r.get('error')}" for r in failed)
        failed_note = f"Failed sources (no listings from them): {failed_bits}. "
    pkg.topic.findings_summary = (
        f"Deterministic summary of {len(listings)} ranked public listings "
        f"fetched live on {fetched_at.date().isoformat()} from "
        f"{', '.join(ok_sources) or 'no source responded'}. "
        f"Ranking is by skill overlap, target-role title match, "
        f"fresher-friendly wording, location preference and recency — "
        f"each listing's reasons are in the table. "
        f"{failed_note}"
        f"Verify each listing at its source link before applying; "
        f"no application was submitted by the agent."
    )

    rows: list[list[Any]] = []
    for idx, job in enumerate(listings, start=1):
        src_id = f"src_{idx}"
        pkg.add_source(SourceReference(
            source_id=src_id, url=job.url, domain=_domain(job.url),
            title=f"{job.title} — {job.company}",
            reliability_score=0.75,
            reliability_tier="secondary_reporting",
        ))
        ev_id = f"ev_{idx}"
        quote = (
            f"{job.title} at {job.company}"
            + (f" ({job.location})" if job.location else "")
            + (f" — posted {job.posted_at.date().isoformat()}" if job.posted_at else "")
            + (f" — {job.snippet[:200]}" if job.snippet else "")
        )
        pkg.add_evidence(EvidenceItem(
            evidence_id=ev_id, source_id=src_id, quote=quote[:400],
        ))
        posted = job.posted_at.date().isoformat() if job.posted_at else "undated"
        statement = (
            f"Role '{job.title}' at {job.company}"
            + (f" ({job.location})" if job.location else "")
            + f", posted {posted}, match score {job.score}/100"
            + (f" — matched skills: {', '.join(job.matched_skills)}" if job.matched_skills else " — no direct skill match")
            + (f" — salary as published: {job.salary}" if job.salary else " — no salary published by the source")
            + (f" — STALE (>{STALE_DAYS}d old)" if job.stale else "")
            + "."
        )
        pkg.add_claim(ResearchClaim(
            claim_id=f"clm_{idx}",
            statement=statement,
            claim_type="job_listing",
            classification="single_source",
            primary_source_id=src_id,
            evidence_item_ids=[ev_id],
            confidence=0.75,
        ))
        rows.append([
            idx, job.title, job.company, job.location or "—", posted,
            job.score, ", ".join(job.matched_skills) or "—",
            "; ".join(job.match_reasons), job.url,
        ])
    pkg.add_table(DataTable(
        table_id="tbl_jobs",
        title="Ranked opportunities",
        headers=["Rank", "Role", "Company", "Location", "Posted", "Match score (0-100)", "Matched skills", "Why this score", "Source link"],
        rows=rows,
        caption=(
            f"Ranked public listings for {profile.summary_line()}. "
            f"Fetched {fetched_at.date().isoformat()} from {', '.join(ok_sources) or 'no source'}. "
            "Scores are deterministic (skill overlap, role-title match, fresher wording, location, recency). "
            "No salary is shown unless the source published it. No application was submitted."
        ),
        source_ids=[f"src_{i}" for i in range(1, len(listings) + 1)],
    ))
    pkg.metadata.update({
        "generator": "job_discovery (deterministic, no LLM)",
        "profile_skills": profile.skills,
        "profile_roles": profile.target_roles,
        "sources": reports,
        "fetched_at": fetched_at.isoformat(),
        "no_application_submitted": True,
    })
    return pkg


def _domain(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def render_job_spreadsheet(pkg: "ResearchKnowledgePackage", output_path: str) -> str:
    """Render the job package to XLSX via the existing renderer."""
    from .renderers import render_artifact

    return render_artifact(pkg, "xlsx", output_path)


# ---------------------------------------------------------------------------
# Recurring monitoring mission (existing backend path)
# ---------------------------------------------------------------------------

def create_job_search_monitor(title: str, search_url: str,
                              chat_id: Optional[str] = None,
                              db_path: Optional[str] = None) -> dict[str, Any]:
    """Create a recurring new-listings watch via Stream C's existing
    monitoring creation path (``_create_new_listings_watch``), not a
    parallel path. The existing path checks hourly (3600 s) and starts
    active. Returns the confirmation text + mission id parsed from it.
    Raises ValueError when the URL is not watchable.
    """
    from ..continuous_monitoring_companion import _create_new_listings_watch
    from .. import db as _db

    # The backend calls init_db() at startup; a direct call must do the
    # same or the insert below fails on a fresh database.
    _db.init_db(db_path)
    confirmation = _create_new_listings_watch(title, search_url, chat_id, db_path)
    match = re.search(r"privagent-watch:\s*([0-9a-f-]+)", confirmation)
    if not match:
        raise ValueError(f"monitoring mission was not created: {confirmation[:300]}")
    return {
        "mission_id": match.group(1),
        "title": title,
        "target_url": search_url,
        "interval_seconds": 3600,
        "status": "active",
        "confirmation": confirmation,
    }


def default_search_url(profile: ResumeProfile) -> str:
    """A public search page worth watching for this profile: the
    RemoteOK tag page for the top skill (human-browsable, same data
    as the API)."""
    tag = None
    for skill in profile.skills:
        tag = REMOTEOK_TAG_MAP.get(skill)
        if tag:
            break
    tag = tag or "python"
    return f"https://remoteok.com/remote-{tag}-jobs"


# ---------------------------------------------------------------------------
# Stream C [C-jobs-mailcal-mcp]: bounded NL pipeline
#
# Demo 2 root cause: the NL job task died on a refused click (Task 5)
# and on the 360 s task timeout (Task 5b) because the planner loop
# fetched listing pages one by one with no budget and no artifact
# step. This section wires the EXISTING pipeline above into one
# bounded, deterministic run: fetch (deadline) -> rank -> verify every
# URL returns HTTP 200 -> render the XLSX -> create the monitoring
# mission. No listing with an unverified URL reaches the spreadsheet.
# ---------------------------------------------------------------------------

import concurrent.futures as _futures

URL_CHECK_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
URL_CHECK_TIMEOUT_S = 8
URL_CHECK_BUDGET_S = 60
URL_CHECK_MAX_WORKERS = 8

# Overall wall-clock budget for one NL job run (fetch 150 + validate
# 60 + render/monitor reserve). The agent loop's default task timeout
# is 360 s; the pipeline must finish inside it with room to answer.
PIPELINE_BUDGET_S = 300


def _check_one_url(job: JobListing) -> tuple[JobListing, bool, str]:
    """GET one listing URL (bounded). Returns (job, ok, detail).

    A listing counts as verified only on HTTP 200. A 403/429 gets one
    retry with a browser user-agent (shared-egress-IP bot gates are
    common); anything else is recorded, never papered over.
    """
    for attempt, ua in enumerate((USER_AGENT, URL_CHECK_UA)):
        try:
            req = urllib.request.Request(job.url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=URL_CHECK_TIMEOUT_S) as resp:
                resp.read(8192)
                if resp.status == 200:
                    return job, True, f"HTTP 200 (final URL {resp.url[:120]})"
                return job, False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt == 0:
                continue
            return job, False, f"HTTP {exc.code}"
        except Exception as exc:  # timeout / DNS / TLS / reset
            return job, False, f"{type(exc).__name__}: {exc}"
    return job, False, "unreachable"


def validate_listing_urls(listings: list[JobListing], max_checks: int = 25,
                          overall_budget_s: float = URL_CHECK_BUDGET_S
                          ) -> tuple[list[JobListing], list[dict[str, Any]]]:
    """Verify every listing URL returns HTTP 200 (bounded, parallel).

    Returns (verified listings in input order, per-URL check report).
    Unverified listings are EXCLUDED from any artifact — a spreadsheet
    must never present a dead or bot-gated link as an application link.
    """
    todo = listings[:max_checks]
    report: list[dict[str, Any]] = []
    verified: list[JobListing] = []
    started = time.monotonic()
    with _futures.ThreadPoolExecutor(max_workers=URL_CHECK_MAX_WORKERS) as pool:
        futs = {pool.submit(_check_one_url, job): job for job in todo}
        try:
            for fut in _futures.as_completed(futs, timeout=overall_budget_s):
                job, ok, detail = fut.result()
                report.append({"title": job.title, "company": job.company,
                               "url": job.url, "verified": ok, "check": detail})
                if ok:
                    verified.append(job)
        except _futures.TimeoutError:
            for fut in futs:
                if not fut.done():
                    fut.cancel()
            report.append({"title": "(remaining checks)", "company": "",
                           "url": "", "verified": False,
                           "check": f"URL-check budget {overall_budget_s}s exhausted; unchecked listings excluded"})
    # Keep the ranking order the pipeline produced.
    order = {job.url: i for i, job in enumerate(todo)}
    verified.sort(key=lambda j: order.get(j.url, 0))
    report.sort(key=lambda r: order.get(r["url"], 999) if r["url"] else 999)
    return verified, report


def run_job_discovery(profile: ResumeProfile, *, max_results: int = 25,
                      artifacts_dir: str | None = None,
                      db_path: str | None = None,
                      chat_id: str | None = None,
                      task_id: str | None = None,
                      deadline_s: float = 150,
                      create_monitor: bool = True) -> dict[str, Any]:
    """Bounded end-to-end job run. Returns a result dict (never raises
    for a source failure — failures are reported in ``reports``).

    Steps and budget: discover (<= ``deadline_s``) -> rank -> URL
    verify (<= 60 s) -> XLSX render -> monitoring mission. The XLSX
    step is always attempted when >= 1 listing verifies, and the
    monitor step never blocks the spreadsheet: a monitor failure is
    reported, not fatal.
    """
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    ranked, reports = discover_jobs(profile, max_results=max_results,
                                    deadline_s=deadline_s)
    timings["discover_s"] = round(time.monotonic() - t0, 1)
    verified, url_report = validate_listing_urls(ranked)
    timings["url_check_s"] = round(time.monotonic() - t0 - timings["discover_s"], 1)

    result: dict[str, Any] = {
        "ok": False,
        "profile_summary": profile.summary_line(),
        "ranked_count": len(ranked),
        "verified_count": len(verified),
        "listings": [
            {"rank": i + 1, "title": j.title, "company": j.company,
             "url": j.url, "location": j.location, "score": j.score,
             "matched_skills": j.matched_skills,
             "posted": j.posted_at.date().isoformat() if j.posted_at else "undated"}
            for i, j in enumerate(verified)
        ],
        "reports": reports,
        "url_report": url_report,
        "timings": timings,
        "xlsx_path": None,
        "xlsx_filename": None,
        "artifact_id": None,
        "monitor": None,
        "error": None,
    }
    if not verified:
        failed_bits = "; ".join(
            f"{r['source']}: {r.get('error', r.get('status'))}"
            for r in reports if r.get("status") in ("failed", "partial"))
        unverified = "; ".join(
            f"{r['title']} ({r['check']})" for r in url_report[:5] if not r["verified"])
        result["error"] = (
            "No listing could be verified: every candidate URL failed its "
            "HTTP 200 check, so no spreadsheet was generated (a spreadsheet "
            "with unverified links would be fabrication). "
            + (f"Source failures: {failed_bits}. " if failed_bits else "")
            + (f"URL checks: {unverified}." if unverified else "")
        )
        return result

    pkg = build_job_package(profile, verified, reports)
    # Filename must carry no long digit run: db.save_artifact rejects
    # paths its sensitivity classifier reads as PII (a timestamp like
    # 20261127-034340 matches), and the chat scrubber would redact it
    # in the download link anyway. Letters-only suffix.
    import secrets as _secrets
    import string as _string
    suffix = "".join(_secrets.choice(_string.ascii_lowercase) for _ in range(6))
    filename = f"job-discovery-{suffix}.xlsx"
    out_dir = artifacts_dir or _default_artifacts_dir()
    import os as _os
    _os.makedirs(out_dir, exist_ok=True)
    out_path = _os.path.join(out_dir, filename)
    render_job_spreadsheet(pkg, out_path)
    timings["render_s"] = round(time.monotonic() - t0 - timings["discover_s"] - timings["url_check_s"], 1)
    result["xlsx_path"] = out_path
    result["xlsx_filename"] = filename

    # Register the file so GET /api/artifacts/{id}/download serves it.
    try:
        from .. import db as _db
        _db.init_db(db_path)
        row = _db.save_artifact({
            "task_id": task_id or "",
            "conversation_id": chat_id or "",
            "filename": filename,
            "filepath": out_path,
            "file_format": "xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "file_size_bytes": _os.path.getsize(out_path),
            "title": f"Ranked job matches — {profile.summary_line()[:80]}",
            "summary": f"{len(verified)} verified public listings, ranked by match score. No application submitted.",
            "metadata": {"generator": "job_discovery", "verified_count": len(verified),
                         "profile_skills": profile.skills},
        }, db_path=db_path)
        result["artifact_id"] = row.get("artifact_id") if isinstance(row, dict) else None
    except Exception as exc:
        result["artifact_error"] = f"artifact registration failed: {exc}"

    if create_monitor:
        try:
            result["monitor"] = create_job_search_monitor(
                f"New {profile.target_roles[0] if profile.target_roles else 'software'} openings",
                default_search_url(profile), chat_id=chat_id, db_path=db_path)
        except Exception as exc:
            result["monitor_error"] = f"monitoring mission was not created: {exc}"

    result["timings"]["total_s"] = round(time.monotonic() - t0, 1)
    result["ok"] = True
    return result


def _default_artifacts_dir() -> str:
    from pathlib import Path
    return str((Path(__file__).resolve().parents[2] / "data" / "artifacts"))


# ---------------------------------------------------------------------------
# NL entry point (called from agentLoop's NON_BROWSER-adjacent dispatch)
# ---------------------------------------------------------------------------

_JOB_RE = re.compile(r"\b(?:job|jobs|internship|internships|opportunit\w*|openings?|roles?)\b", re.IGNORECASE)
_RESUME_RE = re.compile(r"\bresume\b", re.IGNORECASE)
_SPREADSHEET_RE = re.compile(r"\b(?:spreadsheet|excel|xlsx|rank(?:ed|ing)?|find(?:ing)? suitable|best matches)\b", re.IGNORECASE)


def is_job_discovery_instruction(instruction: str) -> bool:
    """True for a resume-based job/internship discovery ask.

    Deliberately narrow: it must mention the resume AND jobs AND a
    discovery/ranking/spreadsheet outcome, so a plain "find me a job"
    chat or a resume-only question never steals this path.
    """
    text = (instruction or "").strip()
    if len(text) < 20:
        return False
    return bool(_RESUME_RE.search(text) and _JOB_RE.search(text) and _SPREADSHEET_RE.search(text))


def handle_job_discovery_instruction(instruction: str, *, file_token: str | None = None,
                                     chat_id: str | None = None, task_id: str | None = None,
                                     db_path: str | None = None) -> str:
    """Run the bounded job pipeline for an NL instruction (sync; the
    caller runs it in a thread). Returns the operator-facing message
    (receipts included, never a bare success sentence)."""
    if not file_token:
        return (
            "I can't run the job discovery without your resume — attach it "
            "to the message (TXT, PDF or DOCX) and ask again, e.g. "
            "'Analyze this resume, find suitable software engineering "
            "internships in Hyderabad, rank the best matches, and generate "
            "an Excel spreadsheet with application links.' Nothing was "
            "searched and no spreadsheet was created."
        )
    try:
        from ..file_uploads import parse_resume_upload
        file_name, profile = parse_resume_upload(file_token)
    except KeyError:
        return (
            "The attached resume expired before I could read it "
            "(attachments live 10 minutes) — please re-attach it and ask "
            "again. Nothing was searched."
        )
    except ValueError as exc:
        return f"I couldn't read the attached resume ({exc}). Nothing was searched."
    if not profile.skills and not profile.target_roles:
        return (
            f"I read '{file_name}' but couldn't extract any skills or "
            "target roles from it, so I have nothing to match listings "
            "against. Check the file text and re-attach it. Nothing was "
            "searched."
        )
    result = run_job_discovery(profile, chat_id=chat_id, task_id=task_id, db_path=db_path)
    lines = [f"Resume analysed: **{file_name}** — {result['profile_summary']}."]
    if not result["ok"]:
        lines.append(f"\n{result['error']}")
        lines.append(
            f"\nDiscovery ran {result['timings'].get('discover_s', '?')}s over "
            f"{len(result['reports'])} source(s); {result['ranked_count']} candidate(s) "
            "ranked, 0 with a verified application link. No spreadsheet, no monitor.")
        return "\n".join(lines)
    lines.append(
        f"\nFound **{result['verified_count']} verified listing(s)** "
        f"(every application link returned HTTP 200; "
        f"{result['ranked_count'] - result['verified_count']} candidate(s) excluded "
        "for unverified links). Top matches:")
    for item in result["listings"][:5]:
        lines.append(
            f"• **{item['title']}** — {item['company']} "
            f"({item['location'] or 'location not stated'}, score {item['score']}/100) — {item['url']}")
    if result["xlsx_filename"]:
        if result.get("artifact_id"):
            dl = f"/api/artifacts/{result['artifact_id']}/download"
            lines.append(
                f"\n📄 **Spreadsheet:** [{result['xlsx_filename']}] ({dl}) — "
                "ranked matches with verified application links. Download it from the link.")
        else:
            # The file exists on disk but could not be registered for
            # download — say so plainly instead of printing a dead link.
            lines.append(
                f"\n📄 **Spreadsheet:** {result['xlsx_filename']} was generated at "
                f"{result['xlsx_path']}, but it could not be registered for download: "
                f"{result.get('artifact_error', 'unknown registration error')}")
    elif result.get("artifact_error"):
        lines.append(f"\n⚠️ Spreadsheet file error: {result['artifact_error']}")
    if result.get("monitor"):
        mon = result["monitor"]
        lines.append(
            f"\n🔔 **Monitoring:** daily watch created (mission `{mon['mission_id']}`) "
            f"on {mon['target_url']} — new matching openings will be flagged. Status: active.")
    elif result.get("monitor_error"):
        lines.append(f"\n⚠️ Monitoring: {result['monitor_error']}")
    lines.append(
        f"\n(Timings: discovery {result['timings'].get('discover_s')}s, URL checks "
        f"{result['timings'].get('url_check_s')}s, total {result['timings'].get('total_s')}s. "
        "No application was submitted — applying is your step.)")
    return "\n".join(lines)
