from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from mcp.server import Server
from mcp.types import Tool

from mcp_server.context import McpContext

_API_BASE = "https://git.streamboard.tv/api/v4/projects/19/wikis"
_WEB_BASE = "https://git.streamboard.tv/common/simplebuild4/-/wikis"


def _fetch_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise ValueError(f"GitLab API error {exc.code}: {exc.reason}") from exc
    except OSError as exc:
        raise ValueError(f"Network error: {exc}") from exc


def _list_wiki_pages_impl(ctx: McpContext) -> list[dict[str, str]]:
    pages = _fetch_json(_API_BASE)
    return [
        {"slug": p["slug"], "title": p["title"], "url": f"{_WEB_BASE}/{p['slug']}"}
        for p in pages
        if p.get("slug") != "_sidebar"
    ]


def _get_wiki_page_impl(ctx: McpContext, slug: str) -> dict[str, str]:
    slug = slug.strip().strip("/")
    if not slug:
        raise ValueError("slug must not be empty")
    page = _fetch_json(f"{_API_BASE}/{slug}")
    return {
        "title": page["title"],
        "slug": page["slug"],
        "content": page["content"],
        "url": f"{_WEB_BASE}/{page['slug']}",
    }


def register(server: Server, ctx: McpContext) -> list[Tool]:
    return [
        Tool(
            name="list_wiki_pages",
            description=(
                "List all pages in the simplebuild4 GitLab wiki. "
                "Returns slug, title, and URL for each page."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_wiki_page",
            description=(
                "Fetch the full Markdown content of a simplebuild4 wiki page by slug "
                "(e.g. 'core/toolchains', 'extensions/mcp-server'). "
                "Use list_wiki_pages to discover available slugs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Wiki page slug, e.g. 'core/toolchains'",
                    },
                },
                "required": ["slug"],
                "additionalProperties": False,
            },
        ),
    ]


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_wiki_pages":
        return _list_wiki_pages_impl(ctx)
    if name == "get_wiki_page":
        return _get_wiki_page_impl(ctx, args["slug"])
    raise KeyError(name)
