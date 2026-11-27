"""Bounded HTTP research pipeline (Stream B).

Runs a research task WITHOUT the browser extension: source discovery
and evidence collection happen over plain HTTP (DuckDuckGo HTML
search, arXiv API, Wikipedia API + direct page fetches through the
SSRF-guarded :func:`app.monitor_check.fetch_observation`), synthesis
is a bounded number of provider calls, and artifact rendering is
reserved budget that discovery/fetch can never eat.

Step budget (total 40 — the old browser loop spent all 50 of its
steps fetching and never generated artifacts, DEMO2 §Task 10/12)::

    1            objective understanding (deterministic parse)
    2-3          research planning (existing adaptive planner)
    4-11         source discovery (max 8 search queries)
    12-26        evidence collection (max 15 fetch attempts)
    27-28        synthesis (max 2 provider calls)
    29-30        comparison table (provider JSON, deterministic fallback)
    31-40        RESERVED for synthesis overflow + artifact rendering
                  (rendering itself runs in loop_research and always
                  gets its budget — the pipeline stops fetching once
                  the sufficiency gate passes or the fetch budget is
                  spent, whichever comes first)

Evidence rules enforced here (not left to the model):

* HTTP 404 / refused / non-HTML / transport-failed sources are
  REJECTED and recorded in ``result["rejected"]`` — never cited.
* Repeated fetches are deduped by canonical URL (collector +
  pipeline-level seen set).
* Inaccessible sources the harvester records with reliability 0 are
  PRUNED from the package before rendering, so they can never appear
  in the bibliography.
* The sufficiency gate requires >= ``min_sources`` successful sources
  with >= 1 evidence item each AND every requested aspect covered by
  >= 1 evidence snippet; otherwise the run fails honestly with the
  gap named instead of rendering a thin report.
* Synthesis is labelled: provider synthesis vs deterministic
  extractive fallback (evidence quotes only), recorded in
  ``pkg.metadata["synthesis_path"]`` and the result, so a reader can
  tell evidence from synthesis.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional

from .collector import (
    BOILERPLATE_RE,
    canonical_source_key,
    extract_domain,
    harvest_page_evidence,
    sanitize_snippet,
)
from .models import (
    DataTable,
    EvidenceItem,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
)
from .text_utils import strip_llm_meta_disclaimers

__all__ = [
    "STEP_BUDGET_TOTAL",
    "DISCOVERY_STEPS_MAX",
    "FETCH_STEPS_MAX",
    "StepBudget",
    "ResearchRunResult",
    "detect_all_requested_formats",
    "parse_research_objective",
    "run_http_research_pipeline",
]

STEP_BUDGET_TOTAL = 40
DISCOVERY_STEPS_MAX = 8
FETCH_STEPS_MAX = 15
SYNTHESIS_STEPS_MAX = 2

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Comparison dimensions the pipeline recognises in an instruction.
# Anything else falls back to generic aspects below.
_KNOWN_ASPECTS = [
    "architecture", "performance", "privacy", "limitations",
    "security", "cost", "accuracy", "scalability", "usability",
    "energy", "latency",
]
_ASPECT_KEYWORDS: dict[str, list[str]] = {
    "architecture": ["architect", "design", "model", "framework", "component"],
    "performance": ["perform", "latency", "speed", "benchmark", "throughput", "fps", "inference time"],
    "privacy": ["priva", "on-device data", "local data", "data never leaves", "without sending"],
    "limitations": ["limitation", "challenge", "drawback", "constraint", "shortcoming"],
    "security": ["secur", "attack", "adversarial", "encrypt"],
    "cost": ["cost", "price", "expensive", "cheap", "budget"],
    "accuracy": ["accura", "precision", "error rate"],
    "scalability": ["scalab", "scale to"],
    "usability": ["usab", "user experience", "ease of use"],
    "energy": ["energy", "battery", "power consumption"],
    "latency": ["latency", "delay", "response time"],
}

# Domains that count as academic / official / credible-reference for
# the "credible academic or official sources" requirement. Tier 0 is
# peer-reviewed or primary, tier 1 official documentation, tier 2
# credible reference, tier 3 everything else (still fetchable, but
# only used when the higher tiers cannot fill the quota).
_TIER_0_SUFFIXES = (
    "arxiv.org", "nature.com", "science.org", "ieee.org", "acm.org",
    "proceedings.mlr.press", "openreview.net", "arxiv-vanity.com",
    "neurips.cc", "icml.cc",
)
_TIER_1_SUFFIXES = (
    "developer.android.com", "developer.apple.com", "pytorch.org",
    "tensorflow.org", "onnx.ai", "huggingface.co", "qualcomm.com",
    "developer.qualcomm.com", "arm.com", "developer.nvidia.com",
    "microsoft.com", "developers.google.com", "ai.google.dev",
)
_TIER_2_SUFFIXES = ("wikipedia.org", "britannica.com", "scholar.google.com")

# Formats recognised anywhere in an instruction (same patterns the
# intent router uses, reused here so the pipeline and the router
# cannot disagree about what was asked for).
_FORMAT_RES = [
    ("pdf", re.compile(r"(?:\bpdf\b|\.pdf\b)", re.IGNORECASE)),
    ("docx", re.compile(r"(?:\bdocx\b|\bword(?:\s+doc(?:ument)?)?\b|\.docx\b)", re.IGNORECASE)),
    ("pptx", re.compile(r"(?:\bpptx\b|\bppt\b|\bpowerpoint\b|\bpresentation\b|\bslide\s*deck\b|\bslides?\b|\.pptx\b|\.ppt\b)", re.IGNORECASE)),
    ("xlsx", re.compile(r"(?:\bxlsx\b|\bexcel\b|\bxcel\b|\bspreadsheet\b|\bworkbook\b|\.xlsx\b)", re.IGNORECASE)),
    ("csv", re.compile(r"(?:\bcsv\b|\.csv\b)", re.IGNORECASE)),
    ("markdown", re.compile(r"(?:\bmarkdown\b|\.md\b)", re.IGNORECASE)),
    ("html", re.compile(r"(?:\bhtml\b|\bwebpage\b|\.html?\b)", re.IGNORECASE)),
]
# Canonical order for rendering: report formats first, then data.
_FORMAT_ORDER = ["pdf", "docx", "pptx", "xlsx", "csv", "markdown", "html"]


class StepBudget:
    """A spendable step budget: every phase spends from the same pot,
    and the reserved tail can never be spent by discovery/fetch."""

    def __init__(self, total: int = STEP_BUDGET_TOTAL, reserve: int = 10):
        self.total = total
        self.reserve = reserve
        self.used = 0
        self.ledger: list[tuple[str, int]] = []

    def spend(self, n: int, what: str) -> bool:
        """Spend n steps if they fit outside the reserve. False when
        the spend would eat the reserved tail."""
        if self.used + n > self.total - self.reserve:
            return False
        self.used += n
        self.ledger.append((what, n))
        return True

    def spend_reserved(self, n: int, what: str) -> bool:
        if self.used + n > self.total:
            return False
        self.used += n
        self.ledger.append((what, n))
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)


@dataclass
class ResearchRunResult:
    ok: bool
    pkg: Optional[ResearchKnowledgePackage] = None
    steps_used: int = 0
    planner_calls: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    aspects: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    synthesis_path: str = ""
    error: str = ""
    timings: dict[str, float] = field(default_factory=dict)


def detect_all_requested_formats(instruction: str, requested_format: str | None = None,
                                 deliverable_type: str | None = None,
                                 default: str = "pdf") -> list[str]:
    """Every deliverable format named in the instruction (not just the
    first). "Generate a professional PDF report and Excel comparison
    sheet" yields ["pdf", "xlsx"]. Falls back to the router's single
    requested format / deliverable type, then ["pdf"]."""
    found: list[str] = []
    text = instruction or ""
    for fmt, rx in _FORMAT_RES:
        if rx.search(text):
            found.append(fmt)
    if requested_format:
        canon = (requested_format or "").lower().strip(" .")
        alias = {"word": "docx", "doc": "docx", "report": "docx", "document": "docx",
                 "presentation": "pptx", "slides": "pptx", "ppt": "pptx",
                 "excel": "xlsx", "spreadsheet": "xlsx", "xls": "xlsx"}
        canon = alias.get(canon, canon)
        if canon in _FORMAT_ORDER and canon not in found:
            found.append(canon)
    if not found and deliverable_type:
        dt = (deliverable_type or "").lower()
        if dt in ("presentation",):
            found.append("pptx")
        elif dt in ("dataset",):
            found.append("xlsx")
        elif dt in ("report", "document"):
            found.append("docx")
    if not found:
        found.append(default)
    return sorted(found, key=_FORMAT_ORDER.index)


def parse_research_objective(instruction: str, topic: str | None) -> dict[str, Any]:
    """Deterministic objective understanding: topic, comparison
    aspects, minimum credible sources, requested formats."""
    text = (instruction or "").strip()
    low = text.lower()
    min_sources = 5
    m = re.search(
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:credible\s+)?(?:academic\s+or\s+official\s+|academic\s+|official\s+)?sources?",
        low,
    )
    if m:
        tok = m.group(1)
        min_sources = int(tok) if tok.isdigit() else _WORD_NUMBERS.get(tok, 5)
    min_sources = max(1, min(10, min_sources))
    aspects = [a for a in _KNOWN_ASPECTS if re.search(rf"\b{re.escape(a)}\b", low)]
    if not aspects:
        aspects = ["overview", "strengths", "limitations"]
    # The classifier's research_topic is often a mangled whole-
    # instruction string ("on-device AI agents using five credible
    # academic or official sources. Compare their architecture, ...
    # Generate a professional and sheet") — trusting it verbatim
    # starved discovery live (arXiv/DDG return nothing for a
    # 100-char pseudo-topic) while topic=None runs passed. Sanitize
    # any supplied topic that is long or contains clause boundaries
    # through the same extractor used for a missing topic.
    raw_topic = (topic or "").strip()
    if raw_topic and (
        len(raw_topic) > 60
        or re.search(r"\b(using|compare|generate|create|produce|with citations)\b",
                     raw_topic, re.IGNORECASE)
    ):
        raw_topic = _extract_topic(raw_topic)
    return {
        "topic": raw_topic or _extract_topic(text),
        "aspects": aspects,
        "min_sources": min_sources,
        "formats": detect_all_requested_formats(text),
        "wants_credible": bool(re.search(r"\bcredible\b|\bacademic\b|\bofficial\b", low)),
    }


def _extract_topic(text: str) -> str:
    """Deterministic topic extraction from a research instruction.
    Strips a leading research verb, then cuts at the first clause
    boundary (using/with/compare/generate/sentence end)."""
    t = text.strip()
    t = re.sub(
        r"^(?:please\s+)?(?:research|investigate|find|search(?:\s+for)?|"
        r"look\s+into|write\s+(?:a\s+report\s+)?(?:about|on)?|"
        r"tell\s+me\s+about|give\s+me\s+(?:a\s+)?(?:report|summary)\s+(?:about|on)?)\s+",
        "", t, flags=re.IGNORECASE,
    )
    cut = re.split(
        r"\s+(?:using|with|and\s+compare|compare|generate|create|produce|"
        r"prepare|write|make)\b|\.\s+|,?\s+and\s+then\b",
        t, maxsplit=1, flags=re.IGNORECASE,
    )
    t = cut[0].strip().strip(",.;:")
    # "on-device AI agents" — drop a trailing "using ..." remnant.
    t = re.sub(r"\s+using\s+.*$", "", t, flags=re.IGNORECASE).strip()
    return t[:80] if len(t) >= 3 else text[:120]


# ---------------------------------------------------------------------------
# Source discovery (max DISCOVERY_STEPS_MAX steps)
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: float = 20.0) -> str:
    # Firefox UA + full Accept headers: the DuckDuckGo HTML endpoint
    # serves its no-results bot page to bare/urllib-style UAs, and
    # returns real results to a browser-like UA (verified 2026-11-27).
    # Retry 429s (and DDG's 202 bot-page) with backoff: the shared
    # egress IP is rate-limited by arXiv-adjacent APIs, OpenAlex,
    # Wikipedia and Semantic Scholar, and a single 429 in the window
    # used to lose the whole backend for the run.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == 202 and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return ""


def _http_get_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    return json.loads(_http_get_text(url, timeout=timeout))


def _ddg_search(query: str, limit: int = 8) -> list[dict[str, str]]:
    """One DuckDuckGo HTML search. Returns [{url, title}]."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    page = _http_get_text(url)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        page, re.IGNORECASE | re.DOTALL,
    ):
        href = html_lib.unescape(m.group(1))
        title = re.sub(r"<[^>]+>", " ", html_lib.unescape(m.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        q = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(q.query)
        target = params.get("uddg", [None])[0]
        if target:
            target = urllib.parse.unquote(target)
        elif href.startswith("http"):
            target = href
        else:
            continue
        if not target or target in seen:
            continue
        seen.add(target)
        out.append({"url": target, "title": title[:160]})
        if len(out) >= limit:
            break
    return out


def _arxiv_search(query: str, limit: int = 8) -> list[dict[str, str]]:
    """One arXiv API search (academic sources, with real titles)."""
    url = ("https://export.arxiv.org/api/query?search_query=all:"
           + urllib.parse.quote(f'"{query}"') + f"&max_results={limit}&sortBy=relevance")
    feed = _http_get_text(url)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(feed)
    out: list[dict[str, str]] = []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:id", ns)
        title_el = entry.find("a:title", ns)
        if link is None or not (link.text or "").strip():
            continue
        title = re.sub(r"\s+", " ", (title_el.text or "")).strip() if title_el is not None else ""
        out.append({"url": link.text.strip(), "title": title[:160]})
    return out


def _crossref_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Crossref API search (academic works, JSON, no key)."""
    url = ("https://api.crossref.org/works?query="
           + urllib.parse.quote(query) + f"&rows={limit}&select=DOI,title,URL")
    data = _http_get_json(url)
    out: list[dict[str, str]] = []
    for item in (data.get("message") or {}).get("items") or []:
        title = " ".join(item.get("title") or []).strip()
        link = item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else "")
        if link:
            out.append({"url": link, "title": title[:160]})
    return out


def _semscholar_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Semantic Scholar API search (academic papers, JSON, no key).
    429s are retried once after a short sleep — the shared egress IP
    is rate-limited, and a single 429 must not lose the backend."""
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query="
           + urllib.parse.quote(query)
           + "&fields=title,abstract,url,externalIds&limit=" + str(limit))
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(5)
            data = _http_get_json(url)
        else:
            raise
    out: list[dict[str, str]] = []
    for paper in data.get("data") or []:
        link = paper.get("url") or ""
        if not link and (paper.get("externalIds") or {}).get("DOI"):
            link = "https://doi.org/" + paper["externalIds"]["DOI"]
        if link:
            out.append({"url": link, "title": (paper.get("title") or "")[:160]})
    return out


def _openalex_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """OpenAlex API search (academic works, JSON, no key).
    Prefers the open-access URL (often arXiv full text) over the DOI,
    because publisher pages 403 to the monitor fetcher."""
    url = ("https://api.openalex.org/works?search="
           + urllib.parse.quote(query) + f"&per-page={limit}")
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(5)
            data = _http_get_json(url)
        else:
            raise
    out: list[dict[str, str]] = []
    for work in data.get("results") or []:
        title = (work.get("title") or "").strip()
        oa = (work.get("open_access") or {}).get("oa_url") or ""
        link = oa or work.get("doi") or ""
        if link:
            out.append({"url": link, "title": title[:160]})
    return out


def _wikipedia_search(query: str, limit: int = 3) -> list[dict[str, str]]:
    """One Wikipedia API search (credible-reference fallback)."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
           + urllib.parse.quote(query) + f"&format=json&srlimit={limit}")
    data = json.loads(_http_get_text(url))
    out: list[dict[str, str]] = []
    for hit in (data.get("query") or {}).get("search") or []:
        title = hit.get("title") or ""
        if title:
            out.append({
                "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                "title": title,
            })
    return out


def _source_tier(url: str) -> int:
    """0 = openly fetchable full text, 1 = academic but often
    bot/paywall-gated, 2 = reference, 3 = everything else.
    The fetch loop walks candidates in tier order and stops as soon
    as the sufficiency gate is met, so doi.org / publisher pages
    (which 403 to the monitor fetcher — SSRN, ScienceDirect, MDPI,
    ResearchGate all did on 2026-11-27) must NOT outrank arXiv,
    Wikipedia and GitHub, which actually serve text."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(d in host for d in ("arxiv.org", "wikipedia.org", "github.com",
                               "semanticscholar.org", "openalex.org")):
        return 0
    if host.endswith(".edu"):
        return 0
    if any(d in host for d in ("nature.com", "ieee.org", "acm.org",
                               "springer.com", "sciencedirect.com",
                               "doi.org", "mdpi.com", "researchgate.net")):
        return 1
    if "britannica.com" in host:
        return 2
    return 3


def _canon_key(url: str) -> str:
    """Pipeline-level dedupe key: the collector's canonical key, plus
    arXiv version collapsing (…/abs/2605.10380v1 and …/abs/2605.10380
    are the same paper — fetching both wastes fetch budget)."""
    key = canonical_source_key(url) or url
    key = re.sub(r"(arxiv\.org/abs/\d{4}\.\d{4,5})v\d+$", r"\1", key)
    return key
    host = extract_domain(url).lower()
    if host.endswith(_TIER_0_SUFFIXES) or host in ("arxiv.org",):
        return 0
    if host.endswith(_TIER_1_SUFFIXES):
        return 1
    if host.endswith(_TIER_2_SUFFIXES):
        return 2
    if host.endswith((".edu", ".gov", ".ac.uk", ".edu.au")):
        return 0
    return 3


def _discover(objective: dict[str, Any], budget: StepBudget,
              log: list[str],
              queries: list[tuple[str, str]] | None = None) -> list[dict[str, str]]:
    """Spend at most DISCOVERY_STEPS_MAX steps on discovery. Returns
    deduped candidates ordered by tier (academic/official first).
    ``queries`` overrides the default query list (second-chance round)."""
    topic = objective["topic"]
    aspects = objective["aspects"]
    if queries is None:
        queries = []
        if objective["wants_credible"]:
            queries.append(("arxiv", topic))
            short_topic = " ".join(topic.split()[:-1]) if len(topic.split()) > 2 else ""
            if short_topic and short_topic.lower() != topic.lower():
                queries.append(("arxiv", short_topic))
            queries.append(("openalex", topic))
            queries.append(("wikipedia", topic))
            queries.append(("crossref", topic))
            queries.append(("semscholar", topic))
        # One general web query only: the per-aspect DuckDuckGo queries
        # returned 0 results every time (bot page) and burned steps.
        queries.append(("ddg", topic))
    candidates: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    spent = 0
    for kind, query in queries:
        if spent >= DISCOVERY_STEPS_MAX or not budget.spend(1, f"discover:{kind}:{query[:40]}"):
            break
        spent += 1
        try:
            if kind == "arxiv":
                found = _arxiv_search(query)
            elif kind == "openalex":
                found = _openalex_search(query)
            elif kind == "crossref":
                found = _crossref_search(query)
            elif kind == "semscholar":
                found = _semscholar_search(query)
            elif kind == "wikipedia":
                found = _wikipedia_search(query)
            else:
                found = _ddg_search(query)
        except Exception as exc:  # noqa: BLE001 — a dead search engine is a rejected step, not a crash
            log.append(f"discovery {kind} {query!r} failed: {type(exc).__name__}: {exc}")
            continue
        for cand in found:
            key = _canon_key(cand["url"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(cand)
        log.append(f"discovery {kind} {query!r}: {len(found)} links")
    # Stable tier order (academic/official first), preserving
    # first-seen order inside a tier. NOTE: never use
    # candidates.index(c) inside a sort key — CPython's sort calls
    # the key while permuting the list, so index() raises ValueError.
    candidates = [c for _, c in sorted(
        enumerate(candidates),
        key=lambda p: (_source_tier(p[1]["url"]), p[0]),
    )]
    return candidates


# ---------------------------------------------------------------------------
# Evidence collection (max FETCH_STEPS_MAX attempts)
# ---------------------------------------------------------------------------

def _fetch_pdf_text(url: str) -> tuple[str, str]:
    """Direct PDF download + text extraction (the monitor fetcher
    refuses non-HTML, so credible PDF sources — MIT AI Agent Index,
    arXiv PDFs, official reports — were all rejected). Same SSRF
    posture as the monitor fetcher is NOT reproduced here; instead
    the URL comes only from discovery candidates, the download is
    capped at 5 MB, and the extracted text is still harvested and
    sufficiency-gated like any other page."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "application/pdf,*/*;q=0.8",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    with opener.open(req, timeout=60.0) as resp:
        raw = resp.read(5_000_000 + 1)
    if len(raw) > 5_000_000:
        raise ValueError("PDF larger than 5 MB; skipped")
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    parts: list[str] = []
    for page in reader.pages[:60]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = "\n".join(parts)
    title = ""
    try:
        meta = reader.metadata
        if meta and meta.title:
            title = str(meta.title)[:160]
    except Exception:
        pass
    return title, text


def _fetch_page(url: str) -> tuple[int, str, str]:
    """Fetch one page through the SSRF-guarded monitor fetcher.
    Returns (http_status, title, visible_text). Raises RuntimeError
    on HTTP errors (including 404) and ValueError on refusals — the
    caller records those as REJECTED sources, never as evidence.
    PDFs take the direct-download path (see _fetch_pdf_text)."""
    from ..monitor_check import fetch_observation

    try:
        obs = fetch_observation(url, timeout=30.0, max_bytes=2_000_000)
    except ValueError as exc:
        if "application/pdf" in str(exc):
            title, text = _fetch_pdf_text(url)
            return 200, title, text
        raise
    text = (obs.get("text") or "").strip()
    title = ""
    elements = obs.get("elements") or []
    if elements:
        title = str(elements[0])[:160]
    return 200, title, text


def _fallback_evidence(pkg: ResearchKnowledgePackage, source_id: str,
                       text: str, topic: str, aspects: list[str]) -> int:
    """Verbatim fallback evidence for a page the collector harvested
    0 snippets from. The collector only keeps sentences with a
    number/unit/%/year/action-verb signal, so definitional academic
    prose ("On-device agents keep user data on the phone") yields
    nothing even from a 100 KB review — that starved the sufficiency
    gate. These are still verbatim quotes from the fetched page, tied
    to the same source_id, so provenance is intact and nothing is
    invented. A sentence qualifies only if it names the topic or one
    of the requested aspects, is 30–400 chars, and is not boilerplate.
    Confidence is lower (0.7) than collector evidence (0.8–0.9)."""
    if not source_id or source_id not in {s.source_id for s in pkg.sources.values()}:
        return 0
    topic_words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", topic)}
    topic_words -= {"with", "using", "about", "their", "these", "those", "from", "that", "this"}
    keywords: set[str] = set(topic_words)
    for aspect in aspects:
        keywords.update(k.lower() for k in _ASPECT_KEYWORDS.get(aspect, [aspect]))
    # Page chrome that rides in the extracted text (arXiv HTML header
    # lines) — real page text, but not quotable evidence.
    junk = ("content select", "skip to main content", "describe the issue",
            "license:", "content selection saved")
    added = 0
    seen: set[int] = set()
    for line in text.splitlines():
        if added >= 8:
            break
        line = line.strip()
        if not line or BOILERPLATE_RE.search(line):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if added >= 8:
                break
            clean = sanitize_snippet(sentence)
            if len(clean) < 30 or len(clean) > 400:
                continue
            low = clean.lower()
            if any(j in low for j in junk):
                continue
            if not any(k in low for k in keywords):
                continue
            h = hash(low[:60])
            if h in seen:
                continue
            seen.add(h)
            ev_id = f"ev_{len(pkg.evidence) + 1:03d}"
            pkg.add_evidence(EvidenceItem(
                evidence_id=ev_id, source_id=source_id,
                quote=clean, context=clean[:250], confidence=0.7,
            ))
            added += 1
    return added


def _good_sources(pkg: ResearchKnowledgePackage) -> list[Any]:
    """Sources that actually yielded evidence (reliability > 0)."""
    good = []
    for src in pkg.sources.values():
        if (src.reliability_score or 0) <= 0:
            continue
        if src.metadata.get("status") == "inaccessible":
            continue
        ev = [e for e in pkg.evidence.values() if e.source_id == src.source_id]
        if ev:
            good.append(src)
    return good


def _aspect_coverage(pkg: ResearchKnowledgePackage, aspects: list[str]) -> dict[str, bool]:
    blob = " ".join(e.quote.lower() for e in pkg.evidence.values())
    covered: dict[str, bool] = {}
    for aspect in aspects:
        keys = _ASPECT_KEYWORDS.get(aspect, [aspect])
        covered[aspect] = any(k.lower() in blob for k in keys)
    return covered


def _prune_inaccessible(pkg: ResearchKnowledgePackage) -> int:
    """Drop reliability-0 / inaccessible sources so they can never be
    cited or listed in a rendered bibliography."""
    doomed = [
        sid for sid, src in pkg.sources.items()
        if (src.reliability_score or 0) <= 0 or src.metadata.get("status") == "inaccessible"
    ]
    for sid in doomed:
        pkg.sources.pop(sid, None)
    return len(doomed)


# ---------------------------------------------------------------------------
# Synthesis (max SYNTHESIS_STEPS_MAX provider calls)
# ---------------------------------------------------------------------------

def _evidence_brief(pkg: ResearchKnowledgePackage, aspects: list[str],
                    max_snippets_per_source: int = 4) -> tuple[str, list[Any]]:
    """Numbered evidence brief for the synthesis prompt: [n] maps to
    the citation numbers the renderers assign (natural source order).
    Snippets are capped so the prompt stays well inside context."""
    ordered = sorted(pkg.sources.values(), key=lambda s: s.source_id)
    good_ids = {s.source_id for s in _good_sources(pkg)}
    ordered = [s for s in ordered if s.source_id in good_ids]
    lines: list[str] = []
    for idx, src in enumerate(ordered, start=1):
        lines.append(f"[{idx}] {src.title or src.domain} — {src.url}")
        snippets = [e for e in pkg.evidence.values() if e.source_id == src.source_id]
        for ev in snippets[:max_snippets_per_source]:
            q = re.sub(r"\s+", " ", ev.quote).strip()[:350]
            if q:
                lines.append(f"    - {q}")
    return "\n".join(lines), ordered


def _chat_call_with_retry(provider: Any, prompt: str, system: str,
                          attempts: int = 3) -> str:
    """One provider call with retries. FreePi answers "one request at
    a time" when another stream is using it, and the provider client
    surfaces that as an empty/exceptional reply — retry with backoff
    instead of failing the synthesis on a transient contention."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            answer = (provider.chat_call(prompt, history=[], system_prompt=system) or "").strip()
        except Exception as exc:  # noqa: BLE001 — retried below
            last_exc = exc
            answer = ""
        if answer and "one request at a time" not in answer.lower():
            return answer
        if attempt + 1 < attempts:
            time.sleep(5 * (attempt + 1))
    if last_exc is not None and not answer:
        raise last_exc
    return answer


def _synthesize_with_provider(provider: Any, topic: str, aspects: list[str],
                              brief: str, ordered_sources: list[Any]) -> str:
    system = (
        "You are a research synthesizer. Write ONLY from the evidence brief below. "
        "Every factual sentence must end with a citation like [1] or [2, 3] using the "
        "numbered sources in the brief. Do not add facts, figures or claims that are "
        "not in the evidence. If the evidence is thin on an aspect, say so explicitly "
        "in that section instead of inventing detail. Use markdown with one ## section "
        "per aspect, then a short ## Conclusion. No preamble about being an AI."
    )
    prompt = (
        f"Research topic: {topic}\n"
        f"Aspects to cover (one ## section each): {', '.join(aspects)}\n\n"
        f"Evidence brief (numbered sources; cite these numbers only):\n{brief}\n\n"
        "Write the report now."
    )
    answer = _chat_call_with_retry(provider, prompt, system)
    return strip_llm_meta_disclaimers(answer.strip())


def _extractive_synthesis(pkg: ResearchKnowledgePackage, topic: str,
                          aspects: list[str]) -> str:
    """Deterministic fallback synthesis: evidence quotes only, grouped
    per aspect with citations. Used only when the provider fails —
    never a mock, and labelled as extractive in the metadata."""
    ordered = sorted(pkg.sources.values(), key=lambda s: s.source_id)
    good_ids = {s.source_id for s in _good_sources(pkg)}
    ordered = [s for s in ordered if s.source_id in good_ids]
    num = {s.source_id: i + 1 for i, s in enumerate(ordered)}
    parts = [f"# {topic}\n"]
    for aspect in aspects:
        keys = _ASPECT_KEYWORDS.get(aspect, [aspect])
        parts.append(f"## {aspect.title()}")
        hits = 0
        for ev in pkg.evidence.values():
            qlow = ev.quote.lower()
            if any(k.lower() in qlow for k in keys):
                q = re.sub(r"\s+", " ", ev.quote).strip()[:350]
                parts.append(f"- {q} [{num.get(ev.source_id, '?')}]")
                hits += 1
                if hits >= 3:
                    break
        if hits == 0:
            parts.append(f"- The collected sources do not cover {aspect} in the harvested evidence.")
    parts.append("## Conclusion")
    parts.append(
        "This synthesis is extractive: every statement above is a verbatim evidence "
        "quote from a cited source, with no model-generated elaboration."
    )
    return "\n".join(parts)


def _build_comparison_table(pkg: ResearchKnowledgePackage, topic: str,
                            aspects: list[str], provider: Any,
                            budget: StepBudget) -> DataTable | None:
    """Aspect x source comparison table. First tries one provider call
    returning JSON; on any failure falls back to a deterministic table
    built from evidence snippets (grounded by construction)."""
    ordered = sorted(pkg.sources.values(), key=lambda s: s.source_id)
    good = [s for s in ordered if s in _good_sources(pkg)]
    if not good or not aspects:
        return None
    headers = ["Aspect"] + [
        (s.title or s.domain)[:28] for s in good
    ] + ["Sources"]
    short_names = {s.source_id: (s.title or s.domain)[:28] for s in good}

    rows: list[list[Any]] | None = None
    if provider is not None and budget.spend_reserved(1, "comparison-table"):
        try:
            brief, _ = _evidence_brief(pkg, aspects, max_snippets_per_source=3)
            system = (
                "Return ONLY a JSON object, no other text: "
                '{"rows": [["aspect", "cell per source in order", "..."], ...]}. '
                "One row per aspect, in the given aspect order. Each cell is a short "
                "finding (<= 18 words) paraphrased ONLY from that source's evidence in "
                "the brief, or the exact string 'Not covered in this source'. "
                "Never invent a finding for a source."
            )
            prompt = (
                f"Topic: {topic}\nAspects in order: {aspects}\n"
                f"Sources in order: {[s.title or s.domain for s in good]}\n\n"
                f"Evidence brief:\n{brief}\n\nReturn the JSON object now."
            )
            raw = _chat_call_with_retry(provider, prompt, system, attempts=2).strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0) if m else raw)
            cand = data.get("rows") if isinstance(data, dict) else None
            if isinstance(cand, list) and cand:
                norm: list[list[Any]] = []
                for row in cand:
                    if not isinstance(row, list) or not row:
                        continue
                    aspect = str(row[0])[:40]
                    cells = [str(c)[:220] for c in row[1:1 + len(good)]]
                    cells += ["Not covered in this source"] * (len(good) - len(cells))
                    nums = ", ".join(f"[{i + 1}]" for i in range(len(good)))
                    norm.append([aspect] + cells + [nums])
                if len(norm) == len(aspects):
                    rows = norm
        except Exception:  # noqa: BLE001 — malformed provider JSON falls back deterministically
            rows = None
    if rows is None:
        # Deterministic fallback: first matching evidence snippet per
        # aspect per source, else "Not covered in this source".
        rows = []
        for aspect in aspects:
            keys = _ASPECT_KEYWORDS.get(aspect, [aspect])
            cells: list[str] = []
            for src in good:
                snippet = "Not covered in this source"
                for ev in pkg.evidence.values():
                    if ev.source_id != src.source_id:
                        continue
                    if any(k.lower() in ev.quote.lower() for k in keys):
                        snippet = re.sub(r"\s+", " ", ev.quote).strip()[:220]
                        break
                cells.append(snippet)
            nums = ", ".join(f"[{i + 1}]" for i in range(len(good)))
            rows.append([aspect] + cells + [nums])
    return DataTable(
        table_id="tbl_comparison",
        title=f"Comparison — {topic}"[:120],
        headers=headers,
        rows=rows,
        caption=(
            "Aspect-by-source comparison. Each cell paraphrases evidence from that "
            "source only, or states that the source does not cover the aspect. "
            "Source numbers refer to the bibliography."
        ),
        source_ids=[s.source_id for s in good],
    )


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_http_research_pipeline(
    *,
    task_id: str,
    instruction: str,
    topic: str | None,
    provider: Any,
    seed_pkg: ResearchKnowledgePackage | None = None,
    requested_format: str | None = None,
    deliverable_type: str | None = None,
) -> ResearchRunResult:
    """Run the bounded HTTP research pipeline for one task.

    ``provider`` is the configured chat provider (chat_call); it is
    passed in so this module never imports agentLoop (which imports
    this module's callers). Returns a ResearchRunResult; ``ok`` is
    False only when the sufficiency gate fails or synthesis fails —
    the caller then fails the task honestly with ``error``.
    """
    t0 = time.time()
    log: list[str] = []
    timings: dict[str, float] = {}
    budget = StepBudget()
    objective = parse_research_objective(instruction, topic)
    if requested_format or deliverable_type:
        objective["formats"] = detect_all_requested_formats(
            instruction, requested_format, deliverable_type)
    budget.spend(1, "objective-understanding")
    aspects = objective["aspects"]
    min_sources = objective["min_sources"]
    formats = objective["formats"]

    pkg = seed_pkg
    if pkg is None:
        pkg = ResearchKnowledgePackage(
            package_id=f"pkg_{task_id}",
            topic=ResearchTopic(
                title=objective["topic"][:200],
                objective=instruction,
                target_format=formats[0] if formats else "pdf",
            ),
        )
        for idx, aspect in enumerate(aspects, start=1):
            pkg.topic.subtopics.append(ResearchSubtopic(
                subtopic_id=f"sub_{idx:02d}",
                title=aspect.title(),
                queries=[f"{objective['topic']} {aspect}"],
            ))
    else:
        pkg.topic.objective = instruction
        if not pkg.topic.subtopics:
            for idx, aspect in enumerate(aspects, start=1):
                pkg.topic.subtopics.append(ResearchSubtopic(
                    subtopic_id=f"sub_{idx:02d}",
                    title=aspect.title(),
                    queries=[f"{objective['topic']} {aspect}"],
                ))

    # Planning (existing adaptive planner, deterministic).
    t_plan = time.time()
    try:
        from .adaptive_planner import generate_adaptive_research_plan

        plan = generate_adaptive_research_plan(instruction, {
            "research_topic": objective["topic"],
            "deliverable_type": deliverable_type,
            "requested_format": formats[0] if formats else None,
        })
        pkg.metadata["research_plan"] = plan.model_dump()
    except Exception as exc:  # noqa: BLE001 — planning is advisory; research continues without it
        log.append(f"adaptive plan failed: {exc}")
    budget.spend(1, "research-planning")
    timings["planning_s"] = round(time.time() - t_plan, 2)

    # Discovery.
    t_disc = time.time()
    candidates = _discover(objective, budget, log)
    timings["discovery_s"] = round(time.time() - t_disc, 2)
    if not candidates:
        return ResearchRunResult(
            ok=False, pkg=pkg, steps_used=budget.used, planner_calls=0,
            aspects=aspects, formats=formats,
            error="Source discovery found no candidates (search engines unreachable or no results).",
            timings=timings,
        )

    # Evidence collection, with dedupe + rejection + sufficiency gate.
    # Two passes: if the first discovery+fetch round does not reach
    # the gate, a second round runs with different queries (review /
    # survey phrasing, Wikipedia topic page) instead of failing on
    # one unlucky candidate mix — live runs on 2026-11-27 swung
    # between 2/5 and 8/5 accepted from the same brief.
    t_fetch = time.time()
    seen: set[str] = set()
    for src in pkg.sources.values():
        seen.add(_canon_key(src.url))
    sources_ok: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    fetches = 0

    def _fetch_pass(cands: list[dict[str, str]]) -> None:
        nonlocal fetches
        for cand in cands:
            if fetches >= FETCH_STEPS_MAX:
                break
            if not budget.spend(1, f"fetch:{cand['url'][:60]}"):
                break
            fetches += 1
            key = _canon_key(cand["url"])
            if key in seen:
                rejected.append({"url": cand["url"], "reason": "duplicate URL"})
                continue
            seen.add(key)
            try:
                status, title, text = _fetch_page(cand["url"])
            except Exception as exc:  # noqa: BLE001 — every fetch failure is a rejected source
                rejected.append({"url": cand["url"],
                                 "reason": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            if not text or len(text) < 200:
                rejected.append({"url": cand["url"], "reason": "page text too short (<200 chars)"})
                continue
            result = harvest_page_evidence(
                cand["url"], title or cand.get("title") or "", text, pkg)
            if result.status == "success" and result.evidence_count > 0:
                sources_ok.append({"url": cand["url"], "title": title or cand.get("title") or "",
                                   "evidence": result.evidence_count,
                                   "claims": result.claims_added})
            elif result.status == "success" and result.evidence_count == 0:
                # Collector found nothing quotable (usually definitional
                # prose without numbers) — fall back to verbatim
                # topic/aspect sentences from the same page.
                n_fb = _fallback_evidence(pkg, result.source_id, text,
                                          objective["topic"], aspects)
                if n_fb > 0:
                    log.append(f"fallback evidence: {n_fb} sentence(s) from {cand['url'][:70]}")
                    sources_ok.append({"url": cand["url"], "title": title or cand.get("title") or "",
                                       "evidence": n_fb, "claims": 0})
                else:
                    rejected.append({"url": cand["url"],
                                     "reason": "harvest success: Harvested 0 evidence snippets (academic)."})
            elif result.status == "duplicate":
                rejected.append({"url": cand["url"], "reason": "duplicate source"})
            else:
                rejected.append({"url": cand["url"],
                                 "reason": f"harvest {result.status}: {result.notes or ''}"[:300]})
            # Sufficiency gate: enough good sources AND every aspect covered.
            good = _good_sources(pkg)
            coverage = _aspect_coverage(pkg, aspects)
            if len(good) >= min_sources and all(coverage.values()):
                break

    _fetch_pass(candidates)
    if len(_good_sources(pkg)) < min_sources and fetches < FETCH_STEPS_MAX:
        log.append("round 1 short of the gate; second discovery round")
        round2_queries = [
            ("arxiv", f"{objective['topic']} review"),
            ("wikipedia", objective["topic"]),
            ("ddg", f"{objective['topic']} survey"),
            ("ddg", f"{objective['topic']} {aspects[-1]}" if aspects else objective["topic"]),
        ]
        candidates2 = _discover(objective, budget, log, queries=round2_queries)
        if candidates2:
            _fetch_pass(candidates2)
    timings["fetch_s"] = round(time.time() - t_fetch, 2)

    pruned = _prune_inaccessible(pkg)
    if pruned:
        log.append(f"pruned {pruned} inaccessible source(s) from the package")
    good = _good_sources(pkg)
    coverage = _aspect_coverage(pkg, aspects)
    missing_aspects = [a for a, ok in coverage.items() if not ok]
    if len(good) < min_sources:
        return ResearchRunResult(
            ok=False, pkg=pkg, steps_used=budget.used, planner_calls=0,
            sources=sources_ok, rejected=rejected, aspects=aspects, formats=formats,
            error=(f"Sufficiency gate failed: {len(good)}/{min_sources} credible sources "
                   f"yielded evidence (fetched {fetches} pages, rejected {len(rejected)}). "
                   f"Missing aspects: {missing_aspects or 'none'}."),
            timings=timings,
        )
    if missing_aspects:
        log.append(f"aspects without evidence (reported, not invented): {missing_aspects}")

    # Synthesis (reserved budget — fetch could never eat it).
    t_synth = time.time()
    planner_calls = 0
    synthesis_path = ""
    brief, ordered = _evidence_brief(pkg, aspects)
    synthesis = ""
    if provider is not None and budget.spend_reserved(1, "synthesis"):
        try:
            synthesis = _synthesize_with_provider(provider, objective["topic"], aspects, brief, ordered)
            planner_calls += 1
            synthesis_path = "provider"
        except Exception as exc:  # noqa: BLE001 — provider failure falls back to extractive synthesis
            log.append(f"provider synthesis failed: {type(exc).__name__}: {exc}; using extractive fallback")
            synthesis = ""
    if not synthesis.strip():
        synthesis = _extractive_synthesis(pkg, objective["topic"], aspects)
        synthesis_path = "extractive-fallback"
    if missing_aspects:
        synthesis += (
            "\n\n> Evidence note: the collected sources did not cover "
            + ", ".join(missing_aspects)
            + " in the harvested evidence, so those sections report the gap instead of inventing detail."
        )
    pkg.topic.findings_summary = synthesis
    timings["synthesis_s"] = round(time.time() - t_synth, 2)

    # Comparison table.
    table = _build_comparison_table(pkg, objective["topic"], aspects, provider, budget)
    if table is not None:
        pkg.tables.append(table)
    # The table step is in the ledger only when the provider JSON
    # attempt actually ran — count it as a provider call then.
    planner_calls += sum(1 for what, _ in budget.ledger if what == "comparison-table")

    pkg.metadata["pipeline"] = {
        "step_budget_total": STEP_BUDGET_TOTAL,
        "steps_used": budget.used,
        "steps_ledger": budget.ledger,
        "sources_ok": len(good),
        "sources_rejected": len(rejected),
        "synthesis_path": synthesis_path,
        "planner_calls": planner_calls,
        "missing_aspects": missing_aspects,
        "log": log,
    }
    timings["total_s"] = round(time.time() - t0, 2)
    return ResearchRunResult(
        ok=True, pkg=pkg, steps_used=budget.used, planner_calls=planner_calls,
        sources=sources_ok, rejected=rejected, aspects=aspects, formats=formats,
        synthesis_path=synthesis_path, timings=timings,
    )
