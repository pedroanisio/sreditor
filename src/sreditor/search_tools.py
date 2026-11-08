"""MCP tool that searches a channel's writing (rules project.search, agentic.mcp).

One task-sized tool (agentic.mcp.tools.outcomes): 'where did we talk about X?' is one call that
reaches every kind of text, instead of a list tool per kind that the agent must page through.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from sreditor import search_operations as searching
from sreditor.deps import Services, access_for
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit
from sreditor.search_operations import QUERY_MAX_LENGTH, ContentQuery, SearchResult

SearchWords = Annotated[
    str,
    Field(
        min_length=1,
        max_length=QUERY_MAX_LENGTH,
        description="Words to look for: every word must appear and the last may be a prefix. "
        "Quotes, AND, OR, NEAR, * and parentheses are plain text, not search syntax.",
    ),
]


def add_search_tools(server: MCPServer, services: Services) -> None:
    """The tool that finds text across a channel."""

    @server.tool(annotations=tool_annotations("Search a channel", Effect.READ))
    def search_channel(
        context: Context,
        slug: str,
        query: SearchWords,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[SearchResult]:
        """Search one channel's writing for words, to answer 'where did we talk about X?'.

        It reads episode titles and summaries, research notes, sources (title, claim, link),
        screenplays, short scripts (speech and on-screen text), pitches with their notes and
        sources, library characters, locations, props and styles, third-party media (title,
        creator, credit, rights note) and context answers. Accents and case are ignored. Each
        result gives its kind, id, the episode number or pitch it applies to, a snippet with
        the match between « and », and `open_with`: the tool and arguments that read the item
        in full. Best matches come first; pass `next_cursor` as `cursor` for more. Use it
        when you do not know which episode or pitch holds a text; to list episodes, pitches or
        library entries without a search, use list_episodes, list_pitches or list_library.
        It never reads waivers, and it needs the channels:read scope."""

        def find() -> Page[SearchResult]:
            who = principal(context)
            access = access_for(services, slug, who.user, who.token)
            return searching.search_channel(
                services, access, ContentQuery(text=query, cursor=cursor, limit=limit)
            )

        return reported(find)
