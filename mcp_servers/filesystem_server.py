#!/usr/bin/env python3
"""Filesystem / document MCP server (read-only, stdio).

Tools: list_files, read_file, analyse_file. All reads are confined to
the controlled data directory next to this script (mcp_servers/data/),
overridable with MCP_FS_DATA_DIR. Path traversal is rejected.
Python stdlib + mcp==2.3.0 only.
"""
import csv
import io
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("navigator-filesystem")

DATA_DIR = Path(os.environ.get("MCP_FS_DATA_DIR", Path(__file__).resolve().parent / "data")).resolve()
MAX_READ_BYTES = 200_000


def _safe_path(rel: str) -> Path:
    """Resolve a user-supplied relative path inside DATA_DIR or raise."""
    if not rel or rel.startswith(("/", "\\")):
        raise ValueError("path must be relative to the controlled directory")
    target = (DATA_DIR / rel).resolve()
    if target != DATA_DIR and DATA_DIR not in target.parents:
        raise ValueError("path escapes the controlled directory")
    return target


@mcp.tool()
def list_files(dir: str = "") -> str:
    """List files in the controlled document directory (relative dir, default root).

    Returns one relative path per line, with sizes in bytes.
    """
    try:
        base = _safe_path(dir) if dir else DATA_DIR
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not base.is_dir():
        return f"ERROR: not a directory: {dir!r}"
    lines = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            lines.append(f"{p.relative_to(DATA_DIR)} ({p.stat().st_size} bytes)")
    return "\n".join(lines) if lines else "(empty directory)"


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file from the controlled document directory.

    `path` is relative to the controlled directory (e.g. 'resume-synthetic.md').
    Returns the file text (capped at 200 KB). Binary files are rejected.
    """
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not target.is_file():
        return f"ERROR: file not found: {path!r}"
    if target.stat().st_size > MAX_READ_BYTES:
        return f"ERROR: file too large ({target.stat().st_size} bytes > {MAX_READ_BYTES})"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: not a UTF-8 text file: {path!r}"


@mcp.tool()
def analyse_file(path: str) -> str:
    """Analyse a document in the controlled directory.

    Returns word count, character count, line count, markdown headings,
    and (for .md tables / .csv files) table structure. Read-only.
    """
    try:
        target = _safe_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not target.is_file():
        return f"ERROR: file not found: {path!r}"
    if target.stat().st_size > MAX_READ_BYTES:
        return f"ERROR: file too large ({target.stat().st_size} bytes)"
    text = target.read_text(encoding="utf-8", errors="replace")
    words = len(text.split())
    lines = text.splitlines()
    headings = [ln.strip() for ln in lines if re.match(r"^#{1,6}\s+\S", ln.strip())]
    out = [
        f"file: {path}",
        f"bytes: {target.stat().st_size}",
        f"lines: {len(lines)}",
        f"words: {words}",
        f"characters: {len(text)}",
        f"characters (no spaces): {len(re.sub(r'\\s', '', text))}",
        f"headings ({len(headings)}):",
    ]
    out += [f"  - {h}" for h in headings] or ["  (none)"]
    if target.suffix.lower() == ".csv":
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        out.append(f"csv: {len(rows)} rows (incl. header), {len(rows[0]) if rows else 0} columns")
        if rows:
            out.append("header: " + ", ".join(rows[0]))
    else:
        table_rows = [ln for ln in lines if ln.strip().startswith("|") and "---" not in ln]
        if table_rows:
            out.append(f"markdown table rows (excl. separator/header): {max(0, len(table_rows) - 1)}")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
