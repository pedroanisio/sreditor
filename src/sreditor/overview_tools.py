"""MCP tool that reads an episode in one call (rules agentic.mcp.tools.outcomes, project.readiness).

get_episode_overview replaces the six to eight calls an agent used to need to see where an
episode stands: it digests each part with the revision to write it back with, and lists what
still holds the episode back, each gap naming the tool that fixes it.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer

from sreditor import overview_operations
from sreditor.deps import Services
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.overview_operations import EpisodeOverview, Section
from sreditor.pitch_tools import channel_access


def add_overview_tools(server: MCPServer, services: Services) -> None:
    """Register get_episode_overview."""

    @server.tool(annotations=tool_annotations("Read an episode in one call", Effect.READ))
    def get_episode_overview(
        context: Context, slug: str, number: int, include: list[Section] | None = None
    ) -> EpisodeOverview:
        """Read an episode in one call: its title, summary and status, then a compact digest of
        each section with the `revision` to pass as `expected_revision` when you write that
        part, and `readiness`, the list of gaps that still hold it back. Use it as the first
        call to read an episode, or to decide what to do next. Each readiness item has a
        severity (blocker stops an srstudio export and request_render; warning only warns), a
        message, its area and `fix_tool`, the tool that resolves it, with `fix_hint` on how.
        `skipped_areas` names what your token or role may not check. Digests hold counts,
        names and short excerpts, never whole texts: each has `read_with`, the tool that reads
        the part in full (get_research, get_screenplay, get_short_script, list_claims and so
        on); do not use this tool to read or edit a text. `include` picks sections from
        research, pitches, screenplay, short_script, style, credits, media, context, delivery,
        claims and readiness; without it you get research, screenplay, short_script, style,
        media, claims and readiness. To find which episode needs work, call list_episodes with
        readiness true first. Waivers are never included; list_waivers shows them."""
        wanted = frozenset(include) if include else None
        return reported(
            lambda: overview_operations.episode_overview(
                services,
                channel_access(services, context, slug),
                number,
                wanted,
            )
        )
