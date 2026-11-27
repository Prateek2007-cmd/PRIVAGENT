#!/usr/bin/env python3
"""Public-fetch MCP server (read-only, stdio).

Tools: fetch_url, extract_headings. Real HTTP GET with a 15 s timeout
and a 1 MB size cap; returns status + extracted text. Failures are
reported honestly (status code / error) — never bypassed, never faked.
Python stdlib + mcp==2.3.0 only. Private/loopback hosts are refused.
"""
import html as html_lib
import ipaddress
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("navigator-public-fetch")

TIMEOUT_S = 15
MAX_BYTES = 1_000_000
USER_AGENT = "NavigatorDemo/1.0 (+demo; read-only fetch)"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0  # nesting depth inside script/style

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._skip == 0:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self) -> str:
        return re.sub(r"[ \t]+", " ", " ".join(self.parts)).strip()


def _check_public(url: str) -> urllib.parse.ParseResult:
    """Refuse non-http(s) URLs and literal private/loopback hosts.

    NOTE: no DNS-based check — this sandbox's DNS resolves every public
    host to a 198.18/15 egress address (Python marks it private), so a
    resolved-IP check would refuse the whole public internet. Literal
    private hosts are still refused, and actual egress goes through the
    sandbox egress proxy, which only allows public destinations.
    """
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("URL has no host")
    if host in ("localhost",):
        raise ValueError("refusing non-public host localhost")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return parts  # hostname: let the proxy/DNS resolve it
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(f"refusing non-public host {host} ({ip})")
    return parts


def _get(url: str) -> tuple[int, str, bytes]:
    _check_public(url)
    # Explicit opener: honour proxy env vars when the spawner passes
    # them through (the MCP SDK's stdio client does NOT inherit proxy
    # vars by default — see backend discovery patch in the demo copy).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read(MAX_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ValueError(f"fetch failed: {exc.reason}") from exc
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES]
    return status, ctype, body


@mcp.tool()
def fetch_url(url: str) -> str:
    """Fetch a public URL with a real HTTP GET (15 s timeout, 1 MB cap).

    Returns the HTTP status, content type, and the page's extracted
    visible text (scripts/styles stripped). On failure returns the
    honest error (status code or network error) — never a fabricated page.
    """
    try:
        status, ctype, body = _get(url)
    except ValueError as exc:
        return f"ERROR: {exc}"
    text = body.decode("utf-8", errors="replace")
    if "html" in ctype or text.lstrip().lower().startswith("<"):
        parser = _TextExtractor()
        parser.feed(text)
        text = parser.text()
    text = html_lib.unescape(text)
    truncated = len(body) >= MAX_BYTES
    out = [f"url: {url}", f"http_status: {status}", f"content_type: {ctype}",
           f"bytes_fetched: {len(body)}" + (" (capped at 1 MB)" if truncated else ""),
           "text:", text[:20000]]
    return "\n".join(out)


@mcp.tool()
def extract_headings(url: str) -> str:
    """Fetch a public URL and return its h1–h3 headings in order.

    Returns the HTTP status plus the headings; on failure returns the
    honest error instead of invented headings.
    """
    try:
        status, ctype, body = _get(url)
    except ValueError as exc:
        return f"ERROR: {exc}"
    text = body.decode("utf-8", errors="replace")
    headings = []
    for match in re.finditer(r"<h([1-3])[^>]*>(.*?)</h\1>", text, re.IGNORECASE | re.DOTALL):
        raw = re.sub(r"<[^>]+>", "", match.group(2))
        clean = html_lib.unescape(re.sub(r"\s+", " ", raw)).strip()
        if clean:
            headings.append(f"h{match.group(1)}: {clean}")
    out = [f"url: {url}", f"http_status: {status}", f"headings ({len(headings)}):"]
    out += headings if headings else ["(none found)"]
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
