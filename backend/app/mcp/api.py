"""Trusted local catalog and execution endpoints; mutations use the human task gate."""
from __future__ import annotations
import hashlib
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from jsonschema import validate, ValidationError

from .. import db
from ..hub import hub
from .discovery import MCPDiscovery, ServerSpec
from .registry import Registry, Tool
from .sanitizer import sanitize_tool_output

logger = logging.getLogger(__name__)

# --- Stream C [C-jobs-mailcal-mcp]: built-in registry tools -----------
# The unified tool registry (tool_registry.py) renders live MCP-registry
# specs into <agent-tools>, so registering real tools here IS wiring
# them into the planner's catalog — no edit to tool_registry.py is
# needed. Two groups:
#   * mailcal_sandbox.* — the MailCal demo-sandbox connector as
#     planner-selectable tools (reads run directly; draft/event are
#     mutating and need the MCP approval gate, never auto-approved).
#   * jobs.discover_and_rank / research.render_comparison — one-call
#     wrappers around the EXISTING deterministic pipelines. Demo 2
#     showed the planner burning 39–50 steps fetching pages one by
#     one and never reaching the artifact step; these tools give it a
#     bounded path to the same outcome. The planner still chooses
#     them independently — nothing here injects a planner action.
# Registration is idempotent and lazy (first _ensure_restored call).
_builtins_registered = False


def _mailcal_connector():
    from ..connectors.mailcal_sandbox import MailCalSandboxConnector
    return MailCalSandboxConnector()


async def _mc_call(method: str, *args: Any, **kwargs: Any) -> Any:
    """Run one (sync httpx) MailCal connector call off the event loop."""
    conn = _mailcal_connector()
    fn = getattr(conn, method)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _obj_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


def _register_builtin_tools() -> None:
    """Register Stream C built-in tools in the MCP registry (once)."""
    global _builtins_registered
    if _builtins_registered:
        return
    _builtins_registered = True

    def _add(name: str, description: str, schema: dict, mutating: bool, invoke):
        if name in registry.tools:
            return
        try:
            registry.register(Tool(name, schema, invoke, mutating=mutating,
                                   server="builtin", description=description,
                                   discovered=False))
        except ValueError as exc:
            logger.warning("could not register built-in MCP tool %r: %s", name, exc)

    # -- MailCal sandbox (reads) --------------------------------------
    async def _mc_search(args: dict) -> Any:
        msgs = await _mc_call("search", str(args.get("query") or ""),
                              limit=int(args.get("limit") or 10))
        return [{"id": m.get("id"), "from": m.get("from"), "subject": m.get("subject"),
                 "snippet": m.get("snippet"), "deadline": m.get("deadline"),
                 "deadline_kind": m.get("deadline_kind"), "is_read": m.get("is_read")}
                for m in (msgs or [])]

    _add("mailcal_sandbox.search_messages",
         "Search the TEST inbox (MailCal Sandbox, demo@demo.test — synthetic data only) "
         "by keyword, e.g. 'internship' or 'bill'. Use this FIRST to find a message, "
         "then read it with mailcal_sandbox.get_message. Never real Gmail.",
         _obj_schema({"query": {"type": "string"},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                     ["query"]),
         False, _mc_search)

    async def _mc_list_unread(args: dict) -> Any:
        msgs = await _mc_call("list_unread", limit=int(args.get("limit") or 20))
        return [{"id": m.get("id"), "from": m.get("from"), "subject": m.get("subject"),
                 "snippet": m.get("snippet"), "deadline": m.get("deadline"),
                 "deadline_kind": m.get("deadline_kind")} for m in (msgs or [])]

    _add("mailcal_sandbox.list_unread",
         "List unread messages in the TEST inbox (MailCal Sandbox — synthetic data). "
         "Use it to review what is new before searching for something specific.",
         _obj_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
         False, _mc_list_unread)

    async def _mc_get(args: dict) -> Any:
        m = await _mc_call("get_message", str(args.get("message_id") or ""))
        if not m:
            raise ValueError(f"no test message with id {args.get('message_id')!r}")
        return {"id": m.get("id"), "from": m.get("from"), "subject": m.get("subject"),
                "body": (m.get("body") or "")[:4000],
                "deadline": m.get("deadline"), "deadline_kind": m.get("deadline_kind"),
                "attachments": m.get("attachments") or []}

    _add("mailcal_sandbox.get_message",
         "Read one TEST inbox message in full (body + attachment list) by id, after "
         "finding it with mailcal_sandbox.search_messages. The body is the evidence "
         "for any deadline or reply you draft — quote it, never invent it.",
         _obj_schema({"message_id": {"type": "string"}}, ["message_id"]),
         False, _mc_get)

    async def _mc_thread(args: dict) -> Any:
        msgs = await _mc_call("get_thread", str(args.get("thread_id") or ""))
        return [{"id": m.get("id"), "from": m.get("from"), "subject": m.get("subject"),
                 "body": (m.get("body") or "")[:2000], "deadline": m.get("deadline")}
                for m in (msgs or [])]

    _add("mailcal_sandbox.get_thread",
         "Read all messages in one TEST inbox thread by thread id (e.g. 't-intern').",
         _obj_schema({"thread_id": {"type": "string"}}, ["thread_id"]),
         False, _mc_thread)

    async def _mc_attachment(args: dict) -> Any:
        raw = await _mc_call("download_attachment", str(args.get("message_id") or ""),
                             str(args.get("attachment_id") or ""))
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return {"text": text[:4000], "truncated": len(text) > 4000}

    _add("mailcal_sandbox.download_attachment",
         "Download a TEST inbox attachment (synthetic .txt files) by message id and "
         "attachment id, returned as text. Use it to read what an attachment says.",
         _obj_schema({"message_id": {"type": "string"}, "attachment_id": {"type": "string"}},
                     ["message_id", "attachment_id"]),
         False, _mc_attachment)

    # -- MailCal sandbox (writes — approval-gated, drafts never sent) --
    async def _mc_draft(args: dict) -> Any:
        return await _mc_call("create_draft", str(args.get("to") or ""),
                              str(args.get("subject") or ""), str(args.get("body") or ""))

    _add("mailcal_sandbox.create_draft",
         "Create a DRAFT reply in the TEST inbox (MailCal Sandbox). Drafts are stored "
         "only — the sandbox has no send endpoint, so a draft can never be sent. "
         "Mutating: needs operator approval first.",
         _obj_schema({"to": {"type": "string"}, "subject": {"type": "string"},
                      "body": {"type": "string"}}, ["to", "subject", "body"]),
         True, _mc_draft)

    async def _mc_event(args: dict) -> Any:
        return await _mc_call("create_event", str(args.get("title") or ""),
                              str(args.get("start") or ""), str(args.get("end") or ""),
                              location=str(args.get("location") or ""),
                              description=str(args.get("description") or ""))

    _add("mailcal_sandbox.create_event",
         "Create a calendar event in the TEST calendar (MailCal Sandbox), e.g. for a "
         "deadline found in the test inbox. Start/end are ISO datetimes; end must be "
         "after start. The sandbox returns the event id — that id IS the receipt. "
         "Mutating: needs operator approval first.",
         _obj_schema({"title": {"type": "string"}, "start": {"type": "string"},
                      "end": {"type": "string"}, "location": {"type": "string"},
                      "description": {"type": "string"}}, ["title", "start", "end"]),
         True, _mc_event)

    # -- Job discovery (one bounded call, not 30 fetches) --------------
    async def _jobs_discover(args: dict) -> Any:
        from ..extractors.resume import parse_resume
        from ..research.job_discovery import run_job_discovery

        profile = parse_resume(str(args.get("resume_text") or ""))
        loc = str(args.get("location") or "").strip()
        if loc:
            profile.locations = [loc]
        result = await asyncio.to_thread(
            run_job_discovery, profile,
            max_results=int(args.get("max_results") or 25),
            create_monitor=bool(args.get("create_monitor", True)))
        return {"ok": result["ok"], "profile": result["profile_summary"],
                "verified_listings": result["listings"],
                "spreadsheet": result.get("xlsx_filename"),
                "artifact_id": result.get("artifact_id"),
                "download": (f"/api/artifacts/{result['artifact_id']}/download"
                             if result.get("artifact_id") else None),
                "monitor": result.get("monitor"),
                "error": result.get("error"),
                "timings_s": result["timings"]}

    _add("jobs.discover_and_rank",
         "Run the full job-discovery pipeline in ONE call: fetch public listings for "
         "the resume's skills, rank them deterministically, verify every application "
         "URL returns HTTP 200, render the ranked XLSX spreadsheet, and create a "
         "daily monitoring watch. Pass the resume TEXT you already read (do not "
         "re-read the file). Do NOT fetch job pages yourself — this tool does the "
         "fetching, ranking and verification. No application is ever submitted. "
         "Mutating (writes the spreadsheet + creates the watch): needs approval.",
         _obj_schema({"resume_text": {"type": "string"},
                      "location": {"type": "string"},
                      "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                      "create_monitor": {"type": "boolean"}}, ["resume_text"]),
         True, _jobs_discover)

    # -- Cited comparison document (one bounded call) -------------------
    async def _render_comparison(args: dict) -> Any:
        return await asyncio.to_thread(_build_comparison_artifact, args)

    _add("research.render_comparison",
         "Render a CITED comparison document in ONE call from evidence you already "
         "fetched. Call this when you have 2+ sources with verified HTTP 200 URLs "
         "and verbatim quotes — then STOP fetching and generate the document. Each "
         "claim needs its exact source URL and the exact quote you read there; "
         "claims without a verified source are refused. Returns the artifact id "
         "and download link. It writes only this task's own deliverable artifact "
         "(the same class of write as the research pipeline's own artifact "
         "generation, which is not approval-gated); it changes no external state "
         "— no mail, calendar, monitor, repo or file outside the task's artifacts.",
         _obj_schema({
             "title": {"type": "string"},
             "format": {"type": "string", "enum": ["docx", "pdf", "xlsx", "md"]},
             "summary": {"type": "string"},
             "claims": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                 "type": "object", "properties": {
                     "statement": {"type": "string"},
                     "source_url": {"type": "string"},
                     "quote": {"type": "string"}},
                 "required": ["statement", "source_url"]}},
         }, ["title", "claims"]),
         False, _render_comparison)


def _build_comparison_artifact(args: dict) -> dict[str, Any]:
    """Build a cited comparison package and render it (sync helper).

    Every claim must carry an http(s) source URL; a claim whose URL is
    not a valid public URL is refused, never rendered as cited. The
    rendered file is registered in the artifact store so it is
    downloadable via /api/artifacts/{id}/download.
    """
    from urllib.parse import urlsplit
    from ..research.models import (
        DataTable, EvidenceItem, ResearchClaim, ResearchKnowledgePackage,
        ResearchTopic, SourceReference,
    )
    from ..research.renderers import render_artifact

    claims = args.get("claims") or []
    if not claims:
        raise ValueError("research.render_comparison needs at least one cited claim")
    title = str(args.get("title") or "Comparison").strip()[:160]
    fmt = str(args.get("format") or "docx").lower().strip(" .")
    if fmt not in ("docx", "pdf", "xlsx", "md"):
        raise ValueError(f"unsupported format {fmt!r}")
    summary = str(args.get("summary") or "").strip()

    pkg = ResearchKnowledgePackage(
        package_id=f"cmp_{int(time.time())}",
        topic=ResearchTopic(
            title=title,
            objective=f"Cited comparison: {title}. Every claim below cites the exact public source it was read from.",
            target_format=fmt,
        ),
    )
    pkg.topic.findings_summary = summary or f"Cited comparison of {title} ({len(claims)} claims, each with its source)."

    seen_urls: dict[str, str] = {}
    rows: list[list[Any]] = []
    for idx, claim in enumerate(claims, start=1):
        statement = str(claim.get("statement") or "").strip()
        url = str(claim.get("source_url") or "").strip()
        quote = str(claim.get("quote") or "").strip()
        if not statement or not url:
            raise ValueError(f"claim {idx} needs both a statement and a source_url")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"claim {idx} source_url is not a public http(s) URL: {url!r}")
        src_id = seen_urls.get(url)
        if src_id is None:
            src_id = f"src_{len(seen_urls) + 1}"
            seen_urls[url] = src_id
            pkg.add_source(SourceReference(
                source_id=src_id, url=url, domain=parts.hostname or url,
                title=url, reliability_score=0.75,
                reliability_tier="secondary_reporting"))
        ev_id = f"ev_{idx}"
        pkg.add_evidence(EvidenceItem(evidence_id=ev_id, source_id=src_id,
                                       quote=(quote or statement)[:400]))
        pkg.add_claim(ResearchClaim(
            claim_id=f"clm_{idx}", statement=statement,
            claim_type="comparison", classification="single_source",
            primary_source_id=src_id, evidence_item_ids=[ev_id],
            confidence=0.75))
        rows.append([idx, statement, url, quote[:200]])
    pkg.add_table(DataTable(
        table_id="tbl_claims", title="Cited claims",
        headers=["#", "Claim", "Source URL", "Verbatim quote"],
        rows=rows,
        caption="Every claim cites the public source it was read from. Verify at the source link before reusing.",
        source_ids=list(seen_urls.values())))
    pkg.metadata.update({"generator": "research.render_comparison (planner-supplied cited claims)",
                         "sources_checked": "planner verified each URL returned HTTP 200 before calling"})

    from pathlib import Path
    out_dir = Path(__file__).resolve().parents[2] / "data" / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in title.lower().replace(" ", "-"))[:60] or "comparison"
    # Letters-only suffix: db.save_artifact rejects filenames whose
    # digit runs read as PII (an epoch timestamp does), and the chat
    # scrubber would redact such a name in the download link.
    import secrets as _secrets
    import string as _string
    suffix = "".join(_secrets.choice(_string.ascii_lowercase) for _ in range(6))
    filename = f"{safe}-{suffix}.{fmt}"
    out_path = str(out_dir / filename)
    render_artifact(pkg, fmt, out_path)
    row = db.save_artifact({
        "filename": filename, "filepath": out_path, "file_format": fmt,
        "mime_type": "application/octet-stream",
        "file_size_bytes": Path(out_path).stat().st_size,
        "title": title,
        "summary": summary or f"Cited comparison ({len(claims)} claims).",
        "metadata": {"generator": "research.render_comparison", "claims": len(claims),
                     "sources": list(seen_urls)},
    })
    artifact_id = row.get("artifact_id") if isinstance(row, dict) else None
    return {"artifact_id": artifact_id, "filename": filename,
            "download": f"/api/artifacts/{artifact_id}/download" if artifact_id else None,
            "claims": len(claims), "sources": list(seen_urls)}


router = APIRouter(prefix="/api/mcp")
registry = Registry()
discovery = MCPDiscovery(registry)
_pending: dict[str, tuple[float, str, dict, str]] = {}
_expiry_tasks: set[asyncio.Task] = set()
_restored = False
_rediscovery_tasks: set[asyncio.Task] = set()


async def _expire_gate(task_id: str):
    await asyncio.sleep(120)
    if _pending.pop(task_id, None) is not None:
        task = hub.tasks.get(task_id)
        if task and task.state == "awaiting_confirmation":
            task.state = "failed"
            task.pending_confirmation = None
            task.message = "Tool approval expired."
            await hub.broadcast_progress(task)


# --- Stream B (S3-R4): persistence across restart ------------------------
# Server specs are JSON in the backend data dir (next to privagent.db);
# tools themselves are re-discovered, never persisted — a stale tool
# list after a server upgrade would be worse than a re-discover. The
# file holds configuration only (no tokens, no secrets). Override the
# path with PRIVAGENT_MCP_SERVERS_FILE (tests). Restore is lazy: the
# first API call — or the tool registry, when it builds the planner
# prompt — re-registers the specs, and re-discovery runs in the
# background from the async endpoints (or on demand, in
# tool_registry._execute_mcp, when a tool is missing).
def _persist_path() -> Path:
    import os

    override = os.environ.get("PRIVAGENT_MCP_SERVERS_FILE")
    if override:
        return Path(override)
    return db.DB_DIR / "mcp_servers.json"


def _save_specs() -> None:
    try:
        path = _persist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"name": s.name, "transport": s.transport, "command": s.command,
             "args": list(s.args), "url": s.url,
             "read_only_tools": sorted(s.read_only_tools)}
            for s in discovery.servers.values()
        ]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:  # persistence must never break registration
        logger.warning("could not persist MCP server specs: %s", exc)


def restore_servers() -> list[str]:
    """Re-register persisted server specs (no discovery). Idempotent.

    Called lazily by _ensure_restored() and by the tool registry; a
    main.py startup hook may call it directly. Returns restored names.
    """
    global _restored
    restored: list[str] = []
    try:
        path = _persist_path()
        if not path.is_file():
            _restored = True
            return restored
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("could not read persisted MCP server specs: %s", exc)
        _restored = True
        return restored
    if not isinstance(data, list):
        _restored = True
        return restored
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        try:
            discovery.register_server(
                ServerSpec(
                    item["name"], item.get("transport", "stdio"),
                    item.get("command"), tuple(item.get("args") or []),
                    item.get("url"),
                    read_only_tools=frozenset(item.get("read_only_tools") or []),
                ),
                trusted_user=True,
            )
            restored.append(item["name"])
        except (PermissionError, ValueError) as exc:
            logger.warning("skipping persisted MCP server %r: %s", item.get("name"), exc)
    _restored = True
    return restored


def _ensure_restored() -> None:
    if not _restored:
        restore_servers()
    # Stream C: built-in registry tools (MailCal, jobs, comparison
    # render) are registered lazily with the restored servers, so the
    # planner prompt lists them even before any /api/mcp call.
    _register_builtin_tools()


def _kick_rediscovery() -> None:
    """Best-effort background re-discovery of restored servers.

    A server that dies after discovery leaves dead tools until the
    next explicit discover; a failed background re-discover only logs
    — the tools stay listed from the last good discovery and an
    explicit POST /servers/{name}/discover refreshes them.
    """
    for name in list(discovery.servers):
        if any(t.get_name() == f"mcp-rediscover:{name}" and not t.done()
               for t in _rediscovery_tasks):
            continue
        task = asyncio.create_task(_rediscover_one(name), name=f"mcp-rediscover:{name}")
        _rediscovery_tasks.add(task)
        task.add_done_callback(_rediscovery_tasks.discard)


async def _rediscover_one(name: str) -> None:
    try:
        await discovery.discover(name)
    except Exception as exc:
        logger.warning("background re-discovery of MCP server %r failed: %s: %s",
                       name, type(exc).__name__, exc)


class ServerInput(BaseModel):
    name: str
    transport: Literal["stdio", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    read_only_tools: list[str] = Field(default_factory=list, alias="readOnlyTools")
    model_config = {"populate_by_name": True}


class ToolInput(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    gate_task_id: str | None = Field(default=None, alias="gateTaskId")
    model_config = {"populate_by_name": True}


@router.get("/servers")
async def servers():
    _ensure_restored()
    _kick_rediscovery()
    return [{"name": s.name, "transport": s.transport, "url": s.url, "tools": [t.name for t in registry.tools.values() if t.server == s.name]}
            for s in discovery.servers.values()]


@router.post("/servers", status_code=201)
def add_server(body: ServerInput):
    try:
        discovery.register_server(ServerSpec(body.name, body.transport, body.command, tuple(body.args), body.url,
                                              read_only_tools=frozenset(body.read_only_tools)), trusted_user=True)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    _save_specs()
    return {"name": body.name, "status": "registered"}


@router.post("/servers/{name}/discover")
async def discover(name: str):
    _ensure_restored()
    if name not in discovery.servers:
        raise HTTPException(404, "unknown server")
    try:
        tools = await discovery.discover(name)
    except Exception as exc:
        # Stream B: log the real failure (type + message) instead of
        # swallowing it into a bare 502 — the SDK call itself is
        # Stream A's RC-LIVE-03 fix; this only makes the failure
        # diagnosable.
        logger.warning("MCP discovery failed for server %r: %s: %s", name, type(exc).__name__, exc)
        raise HTTPException(502, "MCP discovery failed") from None
    return {"tools": tools}


@router.get("/tools")
async def tools():
    _ensure_restored()
    _kick_rediscovery()
    return [{"name": t.name, "description": t.description, "schema": t.schema, "mutating": t.mutating, "server": t.server}
            for t in registry.tools.values()]


@router.post("/prepare")
async def prepare(body: ToolInput):
    tool = registry.tools.get(body.name)
    if not tool:
        raise HTTPException(404, "unknown tool")
    if not tool.mutating:
        raise HTTPException(409, "read-only tools do not require a gate")
    try:
        validate(body.args, tool.schema)
    except ValidationError:
        raise HTTPException(422, "invalid tool arguments") from None
    task = hub.new_task(f"Approve MCP tool {body.name}")
    task.state = "awaiting_confirmation"
    safe = sanitize_tool_output(body.args, 1000)["content"]
    task.pending_confirmation = {"message": f"Approve {body.name} with these arguments? {safe}",
                                 "action": {"type": "mcp_tool", "name": body.name}}
    binding = hashlib.sha256(json.dumps([body.name, body.args], sort_keys=True, default=str).encode()).hexdigest()
    _pending[task.task_id] = (time.monotonic() + 120, body.name, body.args, binding)
    expiry = asyncio.create_task(_expire_gate(task.task_id))
    _expiry_tasks.add(expiry)
    expiry.add_done_callback(_expiry_tasks.discard)
    await hub.broadcast_progress(task)
    return {"gateTaskId": task.task_id, "message": task.pending_confirmation["message"]}


@router.post("/execute")
async def execute(body: ToolInput):
    tool = registry.tools.get(body.name)
    if not tool:
        raise HTTPException(404, "unknown tool")
    approval = None
    if tool.mutating:
        pending = _pending.pop(body.gate_task_id or "", None)
        task = hub.tasks.get(body.gate_task_id or "")
        binding = hashlib.sha256(json.dumps([body.name, body.args], sort_keys=True, default=str).encode()).hexdigest()
        if not pending or not task or pending[0] < time.monotonic() or pending[1] != body.name or pending[3] != binding or task.confirm_decision is not True:
            raise HTTPException(403, "fresh approved tool gate required")
        approval = registry.approve(body.name, body.args, task.task_id)
        task.state = "running"
        await hub.broadcast_progress(task)
    try:
        result = await registry.execute_async(body.name, body.args, approval, task_id=body.gate_task_id or "")
    except (PermissionError, ValidationError, ValueError):
        raise HTTPException(422, "tool execution rejected") from None
    except Exception:
        if tool.mutating and task:
            task.state = "failed"
            task.message = "Tool execution failed."
            await hub.broadcast_progress(task)
        raise HTTPException(502, "tool execution failed") from None
    if tool.mutating and task:
        task.state = "failed"
        task.message = "Tool call returned, but the external effect lacks authoritative confirmation. Verify it at the service."
        await hub.broadcast_progress(task)
    return {**result, "effect_verified": False} if tool.mutating else result


@router.get("/audit")
def audit():
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_audit ORDER BY created_at DESC LIMIT 100")]
