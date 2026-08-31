"""MCP-source ingester: enumerate resources from MCP servers into Doc nodes.

Deterministic, heading-chunked, zero LLM — mirrors the docsite.py pattern
(KGB-2) but over the MCP resources/list + resources/read protocol (KGB-12).

Public sync surface
-------------------
  fetch_resources(config) -> list[McpPage]

Async surface (used directly by tests via create_connected_server_and_client_session)
------------------------------------------------------------------------------------
  _ingest_from_session(session, config) -> list[McpPage]
  _parse_sections(text, uri)            -> (title, list[Section])
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.client.session import ClientSession


@dataclass
class Section:
    heading: str
    level: int   # 0 = preamble, 1–4 = h1–h4
    text: str


@dataclass
class McpPage:
    uri: str            # MCP resource URI (stable identity key)
    title: str
    sections: list[Section] = field(default_factory=list)
    fetched_at: str = ""        # ISO-8601 datetime string
    content_hash: str = ""      # "sha256:…"


@dataclass
class McpSourceConfig:
    name: str                           # human label → McpSource title + kg:mcpServer literal
    command: list[str] | None = None    # stdio transport: [executable, *args]
    max_resources: int = 200


# ── Section parser ─────────────────────────────────────────────────────────────

def _parse_sections(text: str, uri: str) -> tuple[str, list[Section]]:
    """Split markdown/plain text into (title, sections).

    Sections with level=0 are preamble (text before the first heading, or
    body text directly under an h1).  Callers that want to store preamble
    as ``kg:detail`` can filter on ``section.level == 0``.
    """
    lines = text.splitlines()

    # Collect raw chunks: (level, heading, body_lines)
    raw: list[tuple[int, str, list[str]]] = []
    cur_level, cur_heading, cur_body = 0, "", []

    for line in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            raw.append((cur_level, cur_heading, cur_body))
            cur_level = len(m.group(1))
            cur_heading = m.group(2).strip()
            cur_body = []
        else:
            cur_body.append(line)
    raw.append((cur_level, cur_heading, cur_body))

    title = ""
    sections: list[Section] = []

    for level, heading, body_lines in raw:
        body = "\n".join(body_lines).strip()
        if level == 0:
            if body:
                sections.append(Section("", 0, body))
        elif level == 1 and not title:
            title = heading
            if body:
                sections.append(Section("", 0, body))
        else:
            if heading or body:
                sections.append(Section(heading, level, body))

    if not title:
        # No h1 found — derive title from the URI tail
        title = uri.rstrip("/").rsplit("/", 1)[-1] or uri
        if not any(s.level > 0 for s in sections):
            # No headings at all: emit the whole text as a single section
            body = text.strip()
            if body:
                sections.clear()
                sections.append(Section(title, 1, body))

    return title, sections


# ── Content hash ───────────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Async resource enumeration + ingestion ────────────────────────────────────

async def _enumerate_resources(session: "ClientSession", max_resources: int) -> list:
    """Paginate resources/list until exhausted or capped at max_resources."""
    resources = []
    cursor: str | None = None

    while True:
        result = await session.list_resources(cursor=cursor)
        resources.extend(result.resources)
        if len(resources) >= max_resources:
            resources = resources[:max_resources]
            break
        cursor = result.nextCursor
        if not cursor:
            break

    return resources


async def _ingest_from_session(
    session: "ClientSession",
    config: McpSourceConfig,
) -> list[McpPage]:
    """Read all resources from an already-initialised ClientSession.

    Called by tests (in-process) and by _fetch_resources_async (subprocess/SSE).
    Returns one McpPage per readable text resource; non-text resources are skipped.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    resources = await _enumerate_resources(session, config.max_resources)
    pages: list[McpPage] = []

    for resource in resources:
        uri_str = str(resource.uri)
        try:
            result = await session.read_resource(uri=resource.uri)
        except Exception:
            continue

        text: str | None = None
        for content in result.contents:
            mime = getattr(content, "mimeType", None) or ""
            if mime.startswith("text/") or not mime:
                text = getattr(content, "text", None)
                if text is not None:
                    break

        if text is None:
            continue

        title, sections = _parse_sections(text, uri_str)
        pages.append(McpPage(
            uri=uri_str,
            title=title,
            sections=sections,
            fetched_at=now,
            content_hash=_content_hash(text),
        ))

    return pages


# ── Sync public surface ────────────────────────────────────────────────────────

def fetch_resources(config: McpSourceConfig) -> list[McpPage]:
    """Connect to an MCP server and ingest all text resources.

    Uses stdio transport (subprocess) when ``config.command`` is set.
    Wraps async in ``asyncio.run()`` to present a sync surface to cli.py
    (option (a) from planning risk #6 — avoids making main() async).
    """
    return asyncio.run(_fetch_resources_async(config))


async def _fetch_resources_async(config: McpSourceConfig) -> list[McpPage]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    if config.command:
        params = StdioServerParameters(
            command=config.command[0],
            args=config.command[1:],
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await _ingest_from_session(session, config)

    raise ValueError(
        f"McpSourceConfig for '{config.name}' has no 'command' — "
        "set command: [executable, arg, …] in sources.yml mcp_sources"
    )
