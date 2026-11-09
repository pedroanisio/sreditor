"""MCP tool that reads the change feed (rules agentic.events, agentic.mcp.tools.outcomes).

list_events tells an agent what changed in a channel since its last cursor, so it reacts to a
finished render or a revised script without polling every resource.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer

from sreditor import event_operations
from sreditor.deps import Services
from sreditor.events import EventPage
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, ToolLimit
from sreditor.pitch_tools import channel_access


def add_event_tools(server: MCPServer, services: Services) -> None:
    """Register list_events."""

    @server.tool(annotations=tool_annotations("List what changed in a channel", Effect.READ))
    def list_events(
        context: Context, slug: str, since: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> EventPage:
        """List what changed in a channel since a cursor, oldest first: use it after doing work,
        or when you come back, to see what others (people, agents, render workers) changed
        since your last cursor, instead of polling every resource. Each event has its
        `operation` (such as short_script.write, source.create, render.status), the
        `episode_number` or `item_id` and `status` (a render job's id and status: done, failed,
        canceled) it concerns, the `actor` and `read_with`, the tool that reads the item. Events
        carry no content: never expect the text that was written, read the item with the named
        tool. Keep `next_cursor`, which is always set, and pass it as `since` next time; call
        again at once while `has_more` is true. Without `since` you get the latest events and a
        cursor to continue from. A cursor older than the 30 days kept is refused: call without
        `since` and re-read what you track. Events about waivers need the waivers:read scope.
        Do not use it to read an item or to wait for a render; get_render reports one job."""
        return reported(
            lambda: event_operations.list_events(
                services, channel_access(services, context, slug), since, limit
            )
        )
