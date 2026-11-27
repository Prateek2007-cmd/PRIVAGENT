"""User-registered MCP stdio and legacy SSE servers via the official Python SDK."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from .registry import Registry, Tool

logger = logging.getLogger("navigator.mcp.discovery")

# Stream C [C-jobs-mailcal-mcp]: proxy env vars forwarded to spawned
# stdio servers. The MCP Python SDK's stdio client only inherits a
# small allow-list (HOME/PATH/... on POSIX — see
# mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS), so a spawned server
# never sees the sandbox egress proxy vars and every real HTTP fetch
# fails (observed: SSL WRONG_VERSION_NUMBER, direct TLS to the
# intercepting middlebox). The SDK merges StdioServerParameters.env
# OVER its default env (get_default_environment() | env), so passing
# these here is additive, never a replacement. The list is
# connectivity-only: no credential, token or secret var is ever
# forwarded (never add *KEY/*TOKEN/*SECRET/*PASSWORD here).
PROXY_ENV_VARS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "NODE_USE_ENV_PROXY",
)


def proxy_env_for_stdio() -> dict[str, str]:
    """Proxy env vars present in this process, for a spawned server.

    Names only are logged by callers; values (proxy URLs, which may
    carry credentials) are never logged.
    """
    return {k: v for k, v in os.environ.items() if k in PROXY_ENV_VARS and v}


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    read_only_tools: frozenset[str] = frozenset()


class MCPDiscovery:
    def __init__(self, registry: Registry | None = None):
        self.registry = registry or Registry()
        self.servers: dict[str, ServerSpec] = {}

    def register_server(self, spec: ServerSpec, *, trusted_user: bool = False) -> None:
        if not trusted_user:
            raise PermissionError("MCP server registration requires trusted user configuration")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", spec.name) or spec.name in self.servers:
            raise ValueError("invalid or duplicate server name")
        if spec.transport == "stdio":
            path = Path(spec.command or "")
            if not path.is_absolute() or not path.is_file() or not path.stat().st_mode & 0o111:
                raise ValueError("stdio server must be an absolute executable path")
            if any("\x00" in arg for arg in spec.args):
                raise ValueError("invalid stdio argument")
        elif spec.transport == "sse":
            parts = urlsplit(spec.url or "")
            if parts.username or parts.password or parts.fragment or not parts.hostname:
                raise ValueError("invalid MCP SSE URL")
            if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
                if parts.scheme not in ("http", "https"):
                    raise ValueError("local MCP requires http(s)")
            else:
                from ..website_resolver import is_safe_public_url
                if parts.scheme != "https" or not is_safe_public_url(spec.url or "")[0]:
                    raise ValueError("remote MCP requires a safe public HTTPS URL")
        else:
            raise ValueError("unsupported MCP transport")
        self.servers[spec.name] = spec

    @asynccontextmanager
    async def _session(self, spec: ServerSpec):
        if spec.transport == "stdio":
            # Stream C: forward the egress proxy vars so the spawned
            # server can reach the internet (SDK drops them by
            # default). Names only in the log — never proxy values.
            forwarded = proxy_env_for_stdio()
            if forwarded:
                logger.debug(
                    "MCP stdio server %r: forwarding proxy env vars %s",
                    spec.name, sorted(forwarded),
                )
            server = StdioServerParameters(
                command=spec.command or "", args=list(spec.args), env=forwarded or None
            )
            transport = stdio_client(server)
        else:
            transport = sse_client(spec.url or "", headers=spec.headers, timeout=8, sse_read_timeout=15)
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def discover(self, name: str) -> list[str]:
        spec = self.servers[name]
        async def collect():
            names = []
            async with self._session(spec) as session:
                cursor = None
                for _ in range(10):
                    # RC-LIVE-03: pinned mcp==2.3.0 has no `cursor`
                    # keyword — ClientSession.list_tools(*, params=None).
                    # Pagination goes through params={"cursor": ...}.
                    page = await session.list_tools(
                        params={"cursor": cursor} if cursor else None
                    )
                    for item in page.tools:
                        registered = f"{name}.{item.name}"
                        async def invoke(args, server_name=name, tool_name=item.name):
                            return await self.call(server_name, tool_name, args)
                        # mcp 2.3.0 field names are snake_case
                        # (input_schema / next_cursor); the camelCase
                        # aliases exist only for wire serialisation.
                        self.registry.replace(Tool(registered, item.input_schema, invoke,
                                                   mutating=item.name not in spec.read_only_tools,
                                                   server=name, description=item.description or "",
                                                   discovered=True))
                        names.append(registered)
                    cursor = page.next_cursor
                    if not cursor: break
            return names
        try:
            return await asyncio.wait_for(collect(), timeout=20)
        except Exception as exc:
            # Never swallow discovery failures silently: log the
            # exception with its type so a 502 at the API layer is
            # diagnosable instead of opaque.
            logger.exception(
                "MCP discovery failed for server %r: %s: %s",
                name, type(exc).__name__, exc,
            )
            raise

    async def call(self, server_name: str, tool_name: str, arguments: dict):
        spec = self.servers[server_name]
        async def invoke():
            async with self._session(spec) as session:
                result = await session.call_tool(tool_name, arguments)
                return result.model_dump(mode="json")
        return await asyncio.wait_for(invoke(), timeout=30)
