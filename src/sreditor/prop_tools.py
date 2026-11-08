"""MCP tool that adds or changes a library prop with the srstudio rig that draws it (rules
project.srstudio.props, agentic.mcp).

One task-sized tool (agentic.mcp.tools.outcomes): a prop is only useful to beats once it names
its rig, so adding it and setting its rig is one call instead of save_library_entry followed by
a sheet tool.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from sreditor import library_operations
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.library import EntryDescription, EntryInput, EntryName, LibraryEntry, LibraryKind
from sreditor.library_operations import EntryChange
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.srstudio import PropSheet, Rig, SheetInput, Slug, SlugTaken
from sreditor.srstudio_operations import LibrarySheet, get_sheet, library_sheets, save_sheet


class PropData(BaseModel):
    """A prop to add (no prop_id) or to change, with the rig that draws it."""

    model_config = ConfigDict(frozen=True)

    name: EntryName
    description: EntryDescription = ""
    rig: Rig = Field(description="The srstudio registry name that draws it, such as laptop.")
    prop_slug: Slug | None = Field(
        default=None, description="The slug beats use; omit to keep it or take one from the name."
    )
    prop_id: UUID | None = Field(
        default=None, description="The prop to change, from list_library; omit to add one."
    )
    expected_revision: int | None = Field(
        default=None, description="The prop's revision as you read it, when changing it."
    )


def saved_entry(services: Services, channel: ChannelAccess, data: PropData) -> LibraryEntry:
    """The prop's library entry, added or renamed and described."""
    entry = EntryInput(name=data.name, description=data.description)
    if data.prop_id is None:
        return library_operations.create_entry(services, channel, LibraryKind.PROP, entry)
    change = EntryChange(
        kind=LibraryKind.PROP, entry_id=data.prop_id, expected_revision=data.expected_revision
    )
    return library_operations.replace_entry(services, channel, change, entry)


def check_slug_free(services: Services, channel: ChannelAccess, data: PropData) -> None:
    """Refuse a slug another prop holds before writing anything, so a refused call adds no prop."""
    if data.prop_slug is None:
        return
    for found in library_sheets(services, channel, LibraryKind.PROP):
        if found.sheet.slug == data.prop_slug and found.entry.id != data.prop_id:
            raise SlugTaken(data.prop_slug)


def saved_prop(services: Services, channel: ChannelAccess, data: PropData) -> LibrarySheet:
    """Save the prop's entry, then its rig and slug."""
    check_slug_free(services, channel, data)
    entry = saved_entry(services, channel, data)
    current = get_sheet(services, channel, LibraryKind.PROP, entry.id)
    change = EntryChange(kind=LibraryKind.PROP, entry_id=entry.id, expected_revision=None)
    sheet = SheetInput(slug=data.prop_slug or current.slug, sheet=PropSheet(rig=data.rig))
    saved = save_sheet(services, channel, change, sheet)
    return LibrarySheet(
        entry=services.library.get(entry.channel, LibraryKind.PROP, entry.id), sheet=saved
    )


def prop_reported[T](action: Callable[[], T]) -> T:
    """Run the tool's action; a taken slug names its fix, other refusals go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except SlugTaken as taken:
            raise ToolError(
                f"The slug {taken.slug} is already used by another prop of this channel; "
                "call list_library with kind prop to see them, or choose another prop_slug"
            ) from taken

    return reported(guarded)


def add_prop_tools(server: MCPServer, services: Services) -> None:
    """The tool that adds or changes a prop with its rig."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Save a prop", Effect.CREATE))
    def save_prop(
        context: Context, slug: str, prop: PropData, idempotency_key: IdempotencyKey = None
    ) -> Annotated[CallToolResult, LibrarySheet]:
        """Add a prop to the channel's library (no `prop_id`), or change one (its `prop_id`
        from list_library with kind prop, and its `revision` as `expected_revision`): its name,
        description, the srstudio `rig` that draws it (such as laptop or papers) and the
        `prop_slug` that short-script beats use to refer to it. Use it before writing beats
        with a prop event; to link a prop to an episode or a character, use write_context
        after. The slug is fixed once an srstudio export carried it. Needs content:write. Pass
        an `idempotency_key` when adding, so a retry returns the first prop instead of
        "exists"."""
        call = IdempotentCall(operation=f"save_prop/{slug}", arguments=prop, key=idempotency_key)
        return prop_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: saved_prop(services, access(context, slug), prop),
                LibrarySheet,
            )
        )
