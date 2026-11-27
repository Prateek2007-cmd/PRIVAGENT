#!/usr/bin/env python3
"""GitHub public-repo MCP server (read-only, stdio, no token).

Tools: repo_info, list_repo_files, search_public_repos. Uses only the
unauthenticated api.github.com endpoints. If GitHub rate-limits or
blocks the request, the failure (status + rate-limit headers) is
reported honestly — never faked. Python stdlib + mcp==2.3.0 only.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("navigator-github")

TIMEOUT_S = 15
USER_AGENT = "NavigatorDemo/1.0 (+demo; unauthenticated public reads)"


def _get(path: str) -> str:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    # Explicit opener: honour proxy env vars when the spawner passes
    # them through (the MCP SDK's stdio client does NOT inherit proxy
    # vars by default — see backend discovery patch in the demo copy).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies()))
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            return json.dumps(payload, indent=2) + f"\n[rate-limit-remaining: {remaining}]"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        remaining = exc.headers.get("X-RateLimit-Remaining", "?")
        reset = exc.headers.get("X-RateLimit-Reset", "?")
        return (f"ERROR: GitHub API returned HTTP {exc.code} for {url}\n"
                f"rate-limit-remaining: {remaining}; reset (unix): {reset}\n"
                f"body: {body}\n"
                f"(Unauthenticated reads are limited to 60 requests/hour.)")
    except urllib.error.URLError as exc:
        return f"ERROR: network failure reaching api.github.com: {exc.reason}"


@mcp.tool()
def repo_info(owner: str, repo: str) -> str:
    """Return public metadata for a GitHub repository (owner/repo).

    Unauthenticated api.github.com read. Returns description, stars,
    language, license, topics, URLs — or the honest API error.
    """
    owner = urllib.parse.quote(owner.strip(), safe="")
    repo = urllib.parse.quote(repo.strip(), safe="")
    return _get(f"/repos/{owner}/{repo}")


@mcp.tool()
def list_repo_files(owner: str, repo: str, path: str = "") -> str:
    """List files in a public GitHub repository path (default: repo root).

    Unauthenticated api.github.com read. Returns names/types/sizes —
    or the honest API error.
    """
    owner = urllib.parse.quote(owner.strip(), safe="")
    repo = urllib.parse.quote(repo.strip(), safe="")
    path = urllib.parse.quote(path.strip().strip("/"), safe="/")
    suffix = f"/{path}" if path else ""
    return _get(f"/repos/{owner}/{repo}/contents{suffix}")


@mcp.tool()
def search_public_repos(query: str) -> str:
    """Search public GitHub repositories by keyword.

    Unauthenticated api.github.com read, top 5 by stars. Returns
    full_name, description, stars, URL per hit — or the honest API error.
    """
    q = urllib.parse.quote(query.strip())
    return _get(f"/search/repositories?q={q}&sort=stars&order=desc&per_page=5")


if __name__ == "__main__":
    mcp.run()
