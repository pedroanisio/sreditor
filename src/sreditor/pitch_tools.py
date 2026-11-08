"""MCP tools for a channel's pitches (pautas) and the pitch an episode uses (rule agentic.mcp).

A few task-sized tools (agentic.mcp.tools.outcomes): find pitches, read one with its research,
create, update or delete one, write its notes, cite or drop a source, and point an episode at its pitches.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor import pitch_operations as pitches
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, Removed, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit
from sreditor.pitch_operations import PitchChange, PitchQuery, PitchSourceChange
from sreditor.pitches import (
    MAX_EPISODE_PITCHES,
    EpisodePitch,
    EpisodePitches,
    Pitch,
    PitchChoice,
    PitchInput,
    PitchInUse,
    PitchList,
    PitchNotes,
    PitchNotesInput,
    PitchNotFound,
    PitchSource,
    PitchStatus,
)
from sreditor.research import SourceInput, SourceNotFound


class PitchWithResearch(BaseModel):
    """A pitch with its notes, a page of its sources and the episodes that use it."""

    model_config = ConfigDict(frozen=True)

    pitch: Pitch
    notes: PitchNotes
    sources: Page[PitchSource]
    episodes: list[int]


def pitch_error_message(error: Exception) -> str | None:
    """How to fix a refused pitch call (agentic.mcp.errors); None for other errors."""
    if isinstance(error, PitchNotFound):
        return f"Pitch {error} not found; call list_pitches for the channel's pitch ids"
    if isinstance(error, PitchInUse):
        numbers = ", ".join(str(number) for number in error.episodes)
        return (
            f"Pitch {error} is used by episodes {numbers}; call get_pitch to list them again, "
            "then set_episode_pitches without this pitch for each of them first"
        )
    if isinstance(error, SourceNotFound):
        return f"Source {error} not found; call get_pitch for the pitch's source ids"
    return None


def pitch_reported[T](action: Callable[[], T]) -> T:
    """Run a pitch tool's action; pitch refusals name their own fix, others go to `reported`.

    The pitch errors are caught first, because `reported` would point a missing source at the
    episode tool get_research.
    """

    def guarded() -> T:
        try:
            return action()
        except (PitchNotFound, PitchInUse, SourceNotFound) as refusal:
            raise ToolError(pitch_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def pitch_list(pitch_ids: list[UUID]) -> PitchList:
    """The list of pitches an agent sent, or a tool error saying what to change."""
    try:
        return PitchList(pitch_ids=pitch_ids)
    except ValidationError as invalid:
        raise ToolError(
            f"pitch_ids is not valid ({invalid.errors()[0]['msg']}); send each pitch id once, "
            f"at most {MAX_EPISODE_PITCHES} of them, from list_pitches"
        ) from invalid


def add_pitch_tools(server: MCPServer, services: Services) -> None:
    """Tools to find, read and write pitches, and to point an episode at one."""
    add_pitch_reads(server, services)
    add_pitch_writes(server, services)
    add_pitch_research_tools(server, services)


def channel_access(services: Services, context: Context, slug: str) -> ChannelAccess:
    """The caller's access to the channel, from the token behind the MCP request."""
    who = principal(context)
    return access_for(services, slug, who.user, who.token)


def add_pitch_reads(server: MCPServer, services: Services) -> None:
    """Tools to list pitches and read one with its research."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("List pitches", Effect.READ))
    def list_pitches(
        context: Context,
        slug: str,
        status: PitchStatus | None = None,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[Pitch]:
        """List a channel's pitches (pautas: story ideas for episodes), oldest first. Pass
        `status` (idea, research, approved, discarded) to narrow, and `next_cursor` as `cursor`
        for more. Read one with its research using get_pitch."""
        query = PitchQuery(status=status, cursor=cursor, limit=limit)
        return pitch_reported(lambda: pitches.list_pitches(services, access(context, slug), query))

    @server.tool(annotations=tool_annotations("Read a pitch", Effect.READ))
    def get_pitch(
        context: Context,
        slug: str,
        pitch_id: UUID,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> PitchWithResearch:
        """Read a pitch with its research notes, its sources and the episodes that use it,
        before writing about it. Notes at revision 0 were never written. Pass
        `sources.next_cursor` as `cursor` for more sources."""

        def read() -> PitchWithResearch:
            channel = access(context, slug)
            return PitchWithResearch(
                pitch=pitches.get_pitch(services, channel, pitch_id),
                notes=pitches.get_notes(services, channel, pitch_id),
                sources=pitches.list_sources(services, channel, pitch_id, cursor, limit),
                episodes=pitches.pitch_episodes(services, channel, pitch_id),
            )

        return pitch_reported(read)


def add_pitch_writes(server: MCPServer, services: Services) -> None:
    """Tools to create and update pitches and to point an episode at one."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Create a pitch", Effect.CREATE))
    def create_pitch(
        context: Context, slug: str, pitch: PitchInput, idempotency_key: IdempotencyKey = None
    ) -> Annotated[CallToolResult, Pitch]:
        """Add a story idea to a channel: a title, a summary and a status (idea by default).
        The summary may use Markdown. Needs content:write. Each call adds another pitch unless you pass an `idempotency_key`
        (a retry with the same key and pitch returns the first one), so check list_pitches
        first; to change one, use update_pitch."""
        call = IdempotentCall(
            operation=f"create_pitch/{slug}", arguments=pitch, key=idempotency_key
        )
        return pitch_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: pitches.create_pitch(services, access(context, slug), pitch),
                Pitch,
            )
        )

    @server.tool(annotations=tool_annotations("Update a pitch", Effect.REPLACE))
    def update_pitch(
        context: Context,
        slug: str,
        pitch_id: UUID,
        pitch: PitchInput,
        expected_revision: int | None = None,
    ) -> Pitch:
        """Replace a pitch's title, summary and status, for example to move it from idea to
        research or approved. The summary may use Markdown. Needs content:write. Pass the pitch's `revision` from get_pitch
        as `expected_revision` so you never overwrite a change you have not read."""
        change = PitchChange(pitch_id=pitch_id, expected_revision=expected_revision)
        return pitch_reported(
            lambda: pitches.replace_pitch(services, access(context, slug), change, pitch)
        )

    @server.tool(annotations=tool_annotations("Delete a pitch", Effect.REMOVE))
    def delete_pitch(
        context: Context, slug: str, pitch_id: UUID, expected_revision: int | None = None
    ) -> Removed:
        """Delete a pitch for good with its notes and sources. Refused while episodes still
        use it: get_pitch lists their numbers in `episodes`, and set_episode_pitches without this
        pitch (or set_episode_pitch with pitch_id null) drops it from each; to keep the idea
        without deleting it, update_pitch to status discarded instead. Needs owner or editor
        role and the channels:write scope. Pass the pitch's `revision` from get_pitch as
        `expected_revision` to refuse the delete if the pitch changed."""
        change = PitchChange(pitch_id=pitch_id, expected_revision=expected_revision)

        def delete() -> Removed:
            pitches.delete_pitch(services, access(context, slug), change)
            return Removed(kind="pitch", id=pitch_id)

        return pitch_reported(delete)

    @server.tool(annotations=tool_annotations("Set an episode's pitch", Effect.REPLACE))
    def set_episode_pitch(
        context: Context,
        slug: str,
        number: int,
        pitch_id: UUID | None,
        expected_revision: int | None = None,
    ) -> EpisodePitch:
        """Set an episode's pitches to this one pitch of the channel (from list_pitches), or
        pass pitch_id null for none. It replaces any pitches the episode had; to build an
        episode on several pitches, use set_episode_pitches instead. Pass get_research's
        `pitch_list.revision` (0 when never set) as `expected_revision`. Needs content:write."""
        choice = PitchChoice(pitch_id=pitch_id)
        return pitch_reported(
            lambda: pitches.choose_episode_pitch(
                services, access(context, slug), number, choice, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Set an episode's pitches", Effect.REPLACE))
    def set_episode_pitches(
        context: Context,
        slug: str,
        number: int,
        pitch_ids: list[UUID],
        expected_revision: int | None = None,
    ) -> EpisodePitches:
        """Set the pitches (from list_pitches) an episode is built on, in order: the first is
        its main pitch, and an empty list clears them. This replaces the whole list, so send
        every pitch you want to keep. The episode shows each pitch's notes and sources as they
        are, never a copy. Pass the list's `revision` (get_research's `pitch_list.revision`, 0
        when never set) as `expected_revision`. Needs content:write."""
        chosen = pitch_list(pitch_ids)
        return pitch_reported(
            lambda: pitches.set_episode_pitches(
                services, access(context, slug), number, chosen, expected_revision
            )
        )


def add_pitch_research_tools(server: MCPServer, services: Services) -> None:
    """Tools to write a pitch's notes and cite or drop its sources."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Write pitch notes", Effect.REPLACE))
    def write_pitch_notes(
        context: Context,
        slug: str,
        pitch_id: UUID,
        notes: PitchNotesInput,
        expected_revision: int | None = None,
    ) -> PitchNotes:
        """Replace a pitch's research notes with the whole new text; to cite where a fact
        comes from, use add_pitch_source instead. Write them in Markdown (headings, emphasis,
        lists, quotes, links), which pages and PDF books show formatted. Needs content:write. Pass the notes'
        `revision` from get_pitch as `expected_revision` (0 when empty)."""
        change = PitchChange(pitch_id=pitch_id, expected_revision=expected_revision)
        return pitch_reported(
            lambda: pitches.write_notes(services, access(context, slug), change, notes)
        )

    @server.tool(annotations=tool_annotations("Cite a pitch source", Effect.CREATE))
    def add_pitch_source(
        context: Context,
        slug: str,
        pitch_id: UUID,
        source: SourceInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, PitchSource]:
        """Cite a source for one claim of a pitch: its link, title and the claim it supports.
        Every episode that uses the pitch shows it. Needs content:write. `accessed_on`
        defaults to today. Each call adds another source unless you pass an `idempotency_key`:
        a retry with the same key and source returns the first one."""
        call = IdempotentCall(
            operation=f"add_pitch_source/{slug}/{pitch_id}", arguments=source, key=idempotency_key
        )
        return pitch_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: pitches.add_source(services, access(context, slug), pitch_id, source),
                PitchSource,
            )
        )

    @server.tool(annotations=tool_annotations("Remove a pitch source", Effect.REMOVE))
    def remove_pitch_source(
        context: Context,
        slug: str,
        pitch_id: UUID,
        source_id: UUID,
        expected_revision: int | None = None,
    ) -> Removed:
        """Stop citing a source for a pitch, by the `id` get_pitch lists. Needs content:write."""
        change = PitchSourceChange(
            pitch_id=pitch_id, source_id=source_id, expected_revision=expected_revision
        )

        def remove() -> Removed:
            pitches.remove_source(services, access(context, slug), change)
            return Removed(kind="pitch_source", id=source_id)

        return pitch_reported(remove)
