"""MCP tools that read and save a library visual style as data (rules project.visual-style, agentic.mcp).

Two task-sized tools (agentic.mcp.tools.outcomes): read a style with its palette, fonts, cut
rhythm, smallest text and safe zone, and save them, so episode 5 can look like episode 4.
Picking a style for an episode stays with set_episode_style.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.library import LibraryKind
from sreditor.library_operations import EntryChange
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.srstudio import SheetInput, Slug, SlugTaken, StyleSheet
from sreditor.srstudio_operations import LibrarySheet, get_sheet, save_sheet


class VisualStyleData(StyleSheet):
    """What a visual style holds as data; a field left out is cleared. `style_slug` renames it."""

    style_slug: Slug | None = None


def style_reported[T](action: Callable[[], T]) -> T:
    """Run a style tool's action; a taken slug names its fix, other refusals go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except SlugTaken as taken:
            raise ToolError(
                f"The slug {taken.slug} is already used by another style of this channel; "
                "call list_library with kind style, then get_visual_style for each, or "
                "choose another style_slug"
            ) from taken

    return reported(guarded)


def style_input(data: VisualStyleData, current_slug: str) -> SheetInput:
    """The sheet to save: the style's data, under the slug sent or the one it already has."""
    sheet = StyleSheet.model_validate(data.model_dump(exclude={"style_slug"}))
    return SheetInput(slug=data.style_slug or current_slug, sheet=sheet)


def add_style_data_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and save a library visual style's structured data."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Read a visual style", Effect.READ))
    def get_visual_style(context: Context, slug: str, style_id: UUID) -> LibrarySheet:
        """Read a library visual style by its id from list_library (kind style): its name and
        description, and its sheet with the srstudio style, palette (#RRGGBB by role), fonts by
        role, cut rhythm, smallest text (px) and safe zone (percent per edge). Revision 0 means
        nobody saved its data yet. Use it before save_visual_style, which replaces all of it."""

        def read() -> LibrarySheet:
            channel = access(context, slug)
            found = get_sheet(services, channel, LibraryKind.STYLE, style_id)
            entry = services.library.get(channel.channel.slug, LibraryKind.STYLE, style_id)
            return LibrarySheet(entry=entry, sheet=found)

        return style_reported(read)

    @server.tool(annotations=tool_annotations("Save a visual style's data", Effect.REPLACE))
    def save_visual_style(
        context: Context,
        slug: str,
        style_id: UUID,
        data: VisualStyleData,
        expected_revision: int | None = None,
    ) -> LibrarySheet:
        """Replace a visual style's data: srstudio style, palette (ink, white, shade and
        background as #RRGGBB), fonts (heading, body, caption: family and weight 100-900), cut
        rhythm (shortest_shot_s and average_shot_s, shortest not above average), min_text_px and
        safe_zone (top, right, bottom, left, 0-50 percent each). Every field is optional, and one
        left out is cleared, so call get_visual_style first and send what you keep. The style
        must exist: add it with save_library_entry first. Pass the sheet's `revision` as
        `expected_revision` (0 before the first save). Needs content:write."""

        def save() -> LibrarySheet:
            channel = access(context, slug)
            current = get_sheet(services, channel, LibraryKind.STYLE, style_id)
            change = EntryChange(
                kind=LibraryKind.STYLE, entry_id=style_id, expected_revision=expected_revision
            )
            saved = save_sheet(services, channel, change, style_input(data, current.slug))
            entry = services.library.get(channel.channel.slug, LibraryKind.STYLE, style_id)
            return LibrarySheet(entry=entry, sheet=saved)

        return style_reported(save)
