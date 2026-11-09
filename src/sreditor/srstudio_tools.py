"""MCP tools for srstudio's sheets, episode slug and relationships (rules project.srstudio.*, agentic.mcp).

Task-sized tools (agentic.mcp.tools.outcomes): export checks and renders name what srstudio
lacks, such as a character's world or kind, and these tools are how an agent fills it in:
sheets of characters, locations and props, an episode's slug and seed, and the relationships
between characters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import status
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field

from sreditor import srstudio_operations as srstudio
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.library import EntryNotFound, LibraryKind
from sreditor.library_operations import EntryChange
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, Removed, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit, decode_cursor, page
from sreditor.problems import ProblemError
from sreditor.srstudio import (
    EpisodeSlug,
    EpisodeSlugInput,
    Relationship,
    RelationshipInput,
    RelationshipNotFound,
    SameCharacter,
    SheetInput,
    SheetMismatch,
    SlugLocked,
    SlugTaken,
)
from sreditor.srstudio_operations import (
    FactNotPublished,
    LibrarySheet,
    RelationshipChange,
    UnknownToonRig,
)

RELATIONSHIP_KIND = "relationship"
RELATIONSHIP_ONLY_FIELDS = {"relationship_id", "expected_revision"}


class RelationshipData(RelationshipInput):
    """A relationship to record (no relationship_id) or to replace (its id and revision)."""

    relationship_id: UUID | None = Field(
        default=None,
        description="The relationship to replace, from list_relationships; omit to add one.",
    )
    expected_revision: int | None = Field(
        default=None, description="The relationship's revision as you read it, when replacing it."
    )


def srstudio_error_message(error: Exception, slug_field: str) -> str | None:
    """How to fix a refused srstudio call (rule agentic.mcp.errors); None for other errors."""
    if isinstance(error, SlugTaken):
        return (
            f"The slug {error.slug} is already used by another item of this channel; choose "
            f"another {slug_field}. get_library_sheet (or check_srstudio_export for episodes) "
            "shows the slugs in use"
        )
    if isinstance(error, SlugLocked):
        return (
            f"{error}. Send {error.slug} as {slug_field} (an srstudio export fixed it), or "
            "leave it out where it is optional"
        )
    if isinstance(error, SheetMismatch):
        return (
            f"{error}. Send a sheet whose kind is {error.expected}; list_library with kind "
            f"{error.expected} lists the entries of that kind"
        )
    if isinstance(error, FactNotPublished):
        return (
            f"{error}. Cite only published episodes (list_episodes shows each status), or "
            "leave that fact out"
        )
    if isinstance(error, UnknownToonRig):
        return f"{error}. Fix sheet.sheet.rig and call save_library_sheet again"
    if isinstance(error, RelationshipNotFound):
        return f"Relationship {error} not found; call list_relationships for the relationship ids"
    if isinstance(error, SameCharacter):
        return (
            "A relationship pairs two different characters; send two different ids as first "
            "and second, from list_library with kind character"
        )
    return None


SRSTUDIO_REFUSALS = (
    SlugTaken,
    SlugLocked,
    SheetMismatch,
    FactNotPublished,
    UnknownToonRig,
    RelationshipNotFound,
    SameCharacter,
)


def srstudio_reported[T](action: Callable[[], T], slug_field: str = "sheet_slug") -> T:
    """Run a tool's action; srstudio refusals name their fix, other refusals go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except SRSTUDIO_REFUSALS as refusal:
            raise ToolError(
                srstudio_error_message(refusal, slug_field) or str(refusal)
            ) from refusal

    return reported(guarded)


def sheet_reported[T](action: Callable[[], T], kind: LibraryKind) -> T:
    """Run a sheet tool's action; a missing entry says which kind's list holds valid ids."""

    def guarded() -> T:
        try:
            return action()
        except EntryNotFound as missing:
            raise ToolError(
                f"No {kind} with id {missing} in this channel's library; list_library with kind "
                f"{kind} gives its entry ids (the sheet's kind must be the entry's kind)"
            ) from missing

    return srstudio_reported(guarded)


def library_sheet(
    services: Services, channel: ChannelAccess, kind: LibraryKind, entry_id: UUID
) -> LibrarySheet:
    """A library entry together with its srstudio sheet."""
    found = srstudio.get_sheet(services, channel, kind, entry_id)
    return LibrarySheet(
        entry=services.library.get(channel.channel.slug, kind, entry_id), sheet=found
    )


def relationship_input(data: RelationshipData) -> RelationshipInput:
    """The relationship's own fields, without the ids that say which one to replace."""
    return RelationshipInput.model_validate(data.model_dump(exclude=RELATIONSHIP_ONLY_FIELDS))


def saved_relationship(
    services: Services, channel: ChannelAccess, data: RelationshipData
) -> Relationship:
    """Add the relationship, or replace the one `relationship_id` names."""
    fields = relationship_input(data)
    if data.relationship_id is None:
        return srstudio.add_relationship(services, channel, fields)
    change = RelationshipChange(
        relationship_id=data.relationship_id, expected_revision=data.expected_revision
    )
    return srstudio.replace_relationship(services, channel, change, fields)


def relationships_page(
    rows: list[Relationship], cursor: str | None, limit: int
) -> Page[Relationship]:
    """One page of relationships in the order they were recorded, after the cursor's id."""
    after = decode_cursor(cursor)
    ids = [str(row.id) for row in rows]
    if after is not None and after not in ids:
        raise ProblemError(status.HTTP_400_BAD_REQUEST, "Invalid cursor", "invalid-cursor")
    start = ids.index(after) + 1 if after is not None else 0
    return page(rows[start : start + limit + 1], limit, lambda row: str(row.id))


def add_srstudio_tools(server: MCPServer, services: Services) -> None:
    """Tools to fill in what srstudio needs: sheets, an episode's slug and seed, relationships."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Read a library sheet", Effect.READ))
    def get_library_sheet(
        context: Context, slug: str, kind: LibraryKind, entry_id: UUID
    ) -> LibrarySheet:
        """Read the srstudio sheet of a library character, location, prop or style by its id
        from list_library: the entry's name and description and its `sheet` (world, status,
        kind, rig, look, voice, canon facts and so on, by kind), `slug`, `revision` and
        `exported_at` (set once an srstudio export carried the slug, which then never
        changes). Revision 0 with a slug proposed from the name means nobody saved a sheet
        yet. Use it before save_library_sheet, which replaces the whole sheet, and when
        check_srstudio_export or request_render says an entry has no srstudio world or no
        kind. Needs channels:read."""
        return sheet_reported(
            lambda: library_sheet(services, access(context, slug), kind, entry_id), kind
        )

    @server.tool(annotations=tool_annotations("Save a library sheet", Effect.REPLACE))
    def save_library_sheet(
        context: Context,
        slug: str,
        entry_id: UUID,
        sheet: SheetInput,
        expected_revision: int | None = None,
    ) -> LibrarySheet:
        """Replace the srstudio sheet of a library character, location or prop (a style's
        sheet is easier with save_visual_style). `sheet` holds the `slug` (lowercase words
        joined by hyphens, unique per channel and kind; send the one get_library_sheet gave
        to keep it) and the kind's `sheet`, whose `kind` must be the entry's kind: a
        character's world, status (canon or placeholder), character_kind (person, animal,
        object, archetype), rig, look, voice, who, comic role, never, open questions and
        canon facts; a location's world, rig and cites; a prop's rig. Use it to fix a
        character with no srstudio world or no kind, which blocks exports and renders. The
        entry must exist: add it with save_library_entry first. Everything not sent is
        cleared, so call get_library_sheet first and send what you keep. Facts may cite only
        published episodes. The slug is fixed once an srstudio export carried it. Pass the
        sheet's `revision` as `expected_revision` (0 before the first save). Fields are
        plain text, not Markdown. Needs content:write."""
        change = EntryChange(
            kind=LibraryKind(sheet.sheet.kind),
            entry_id=entry_id,
            expected_revision=expected_revision,
        )

        def save() -> LibrarySheet:
            channel = access(context, slug)
            srstudio.save_sheet(services, channel, change, sheet)
            return library_sheet(services, channel, change.kind, entry_id)

        return sheet_reported(save, change.kind)

    @server.tool(annotations=tool_annotations("Set an episode's srstudio slug", Effect.REPLACE))
    def set_episode_slug(
        context: Context,
        slug: str,
        number: int,
        srstudio_slug: EpisodeSlugInput,
        expected_revision: int | None = None,
    ) -> EpisodeSlug:
        """Set the slug srstudio names an episode by (`srstudio_slug.slug`: lowercase words
        joined by hyphens, unique in the channel) and its `seed` (an integer that makes a
        rebuild draw the same frames). check_srstudio_export shows the current values. Once an
        srstudio export carried the slug it is fixed and only the same slug is accepted; the
        seed stays editable. The episode must exist (create_episode). Pass the revision the
        answer gave as `expected_revision` to avoid overwriting a change you have not seen
        (0 before the first save; left out, it takes the current one). Needs content:write."""
        return srstudio_reported(
            lambda: srstudio.save_episode_slug(
                services, access(context, slug), number, srstudio_slug, expected_revision
            ),
            "srstudio_slug.slug",
        )

    @server.tool(annotations=tool_annotations("List relationships", Effect.READ))
    def list_relationships(
        context: Context, slug: str, cursor: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> Page[Relationship]:
        """List how the channel's library characters relate (srstudio's relationships.json):
        each one's id, the two character ids, relation, how they know each other, dynamic,
        tension, where they meet, status and history facts, and its revision. Use it before
        save_relationship to replace one, or remove_relationship. Pass `next_cursor` as `cursor`
        for more. Needs channels:read."""
        return reported(
            lambda: relationships_page(
                srstudio.list_relationships(services, access(context, slug)), cursor, limit
            )
        )

    @server.tool(annotations=tool_annotations("Save a relationship", Effect.CREATE))
    def save_relationship(
        context: Context,
        slug: str,
        relationship: RelationshipData,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Relationship]:
        """Record how two different library characters relate (no `relationship_id`), or
        replace one (its `relationship_id` and `revision` as `expected_revision`, both from
        list_relationships; everything not sent is cleared). `first` and `second` are character
        ids from list_library with kind character, so add the characters first. Texts are plain
        text, not Markdown; `status` is proposal (default) or canon; `history` facts may cite only published episodes. To delete one use
        remove_relationship. Needs content:write. Pass an `idempotency_key` when adding, so a
        retry does not record it twice."""
        call = IdempotentCall(
            operation=f"save_relationship/{slug}", arguments=relationship, key=idempotency_key
        )
        return srstudio_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: saved_relationship(services, access(context, slug), relationship),
                Relationship,
            )
        )

    @server.tool(annotations=tool_annotations("Remove a relationship", Effect.REMOVE))
    def remove_relationship(
        context: Context, slug: str, relationship_id: UUID, expected_revision: int | None = None
    ) -> Removed:
        """Delete a relationship by its id from list_relationships, for example one recorded by
        mistake; the two characters stay. Pass its `revision` as `expected_revision` so a
        change you have not seen is not lost. Needs an owner or editor and, for a token, the
        channels:write scope."""
        change = RelationshipChange(
            relationship_id=relationship_id, expected_revision=expected_revision
        )

        def remove() -> Removed:
            srstudio.delete_relationship(services, access(context, slug), change)
            return Removed(kind=RELATIONSHIP_KIND, id=relationship_id)

        return srstudio_reported(remove)
