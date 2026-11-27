# MCP servers — NAVIGATOR demo (Stream S5)

Stdio MCP servers in this directory. Python stdlib + `mcp==2.3.0` only.
All tools are **read-only**; register them with the tool names in
`readOnlyTools` so no approval gate is needed.

## Servers

| Server | Script | Tools |
|---|---|---|
| `fs-docs` | `filesystem_server.py` | `list_files`, `read_file`, `analyse_file` |
| `public-fetch` | `public_fetch_server.py` | `fetch_url`, `extract_headings` |
| `github-public` | `github_server.py` | `repo_info`, `list_repo_files`, `search_public_repos` |

Seed documents live in `data/` (synthetic only — fictional resume,
sample report, sample CSV). `filesystem_server.py` confines every read
to that directory (`MCP_FS_DATA_DIR` override); `..` escapes are rejected.

## Registration (against the demo2 backend on port 8000)

The servers need a Python with `mcp` installed — the demo2-backend venv:

```bash
PY=~/workspace/navigator-audit/live/demo2-backend/venv/bin/python
BASE=~/workspace/navigator-audit/worktrees2/S5/mcp_servers
for spec in "fs-docs filesystem_server.py list_files,read_file,analyse_file" \
            "public-fetch public_fetch_server.py fetch_url,extract_headings" \
            "github-public github_server.py repo_info,list_repo_files,search_public_repos"; do
  set -- $spec
  curl -s -X POST localhost:8000/api/mcp/servers -H 'Content-Type: application/json' \
    -d "{\"name\":\"$1\",\"transport\":\"stdio\",\"command\":\"$PY\",\"args\":[\"$BASE/$2\"],\"readOnlyTools\":[\"$(echo $3 | sed 's/,/","/g')\"]}"
done
curl -s -X POST localhost:8000/api/mcp/servers/fs-docs/discover
curl -s -X POST localhost:8000/api/mcp/servers/public-fetch/discover
curl -s -X POST localhost:8000/api/mcp/servers/github-public/discover
curl -s localhost:8000/api/mcp/tools
```

Tool names in the planner registry are prefixed: `fs-docs.list_files`,
`public-fetch.fetch_url`, `github-public.repo_info`, etc. The planner
calls them with an `mcp_tool` action (`{"type": "mcp_tool", "risk": "write",
"params": {"tool": "<full name>", "arguments": {...}}}` — note: the
planner validation requires risk=write for `mcp_tool` even for
read-only tools; the registry itself treats them as non-mutating).
