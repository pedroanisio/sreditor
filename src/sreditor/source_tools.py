"""MCP tool to edit a cited source in place, whether an episode or a pitch cites it.

One tool for both owners (agentic.mcp.tools.outcomes), addressed like add_source and
add_pitch_source: an episode by `number`, a pitch by `pitch_id`. Editing keeps the source's id,
so the claims that point at it keep their link; removing and citing again would drop it.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, HttpUrl

from sreditor import pitch_operations, research_operations
from sreditor.deps import ChannelAccess, Services
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pitch_operations import PitchSourceChange
from sreditor.pitch_tools import channel_access, pitch_reported
from sreditor.research import SourceInput
from sreditor.research_operations import SourceChange

SHARED_FIELDS = {"id", "url", "title", "claim", "accessed_on", "revision"}


class EpisodeSourceTarget(BaseModel):
    """A source of an episode: the episode's number and the source's id from get_research."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    source_id: UUID


class PitchSourceTarget(BaseModel):
    """A source of a pitch: the pitch's id and the source's id from get_pitch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pitch_id: UUID
    source_id: UUID


class SourceView(BaseModel):
    """An edited source: `number` is set for an episode's source, `pitch_id` for a pitch's."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    url: HttpUrl
    title: str
    claim: str
    accessed_on: date
    revision: int
    number: int | None = None
    pitch_id: UUID | None = None


def replace_episode_source(
    services: Services,
    access: ChannelAccess,
    target: EpisodeSourceTarget,
    source: SourceInput,
    expected_revision: int | None,
) -> SourceView:
    """Replace a source of an episode; its refusals name get_research."""
    change = SourceChange(
        number=target.number, source_id=target.source_id, expected_revision=expected_revision
    )
    done = reported(lambda: research_operations.replace_source(services, access, change, source))
    return SourceView(**done.model_dump(include=SHARED_FIELDS), number=done.number)


def replace_pitch_source(
    services: Services,
    access: ChannelAccess,
    target: PitchSourceTarget,
    source: SourceInput,
    expected_revision: int | None,
) -> SourceView:
    """Replace a source of a pitch; its refusals name get_pitch."""
    change = PitchSourceChange(
        pitch_id=target.pitch_id, source_id=target.source_id, expected_revision=expected_revision
    )
    done = pitch_reported(lambda: pitch_operations.replace_source(services, access, change, source))
    return SourceView(**done.model_dump(include=SHARED_FIELDS), pitch_id=done.pitch_id)


def add_source_tools(server: MCPServer, services: Services) -> None:
    """The tool that edits an episode's or a pitch's source."""

    @server.tool(annotations=tool_annotations("Update a source", Effect.REPLACE))
    def update_source(
        context: Context,
        slug: str,
        target: EpisodeSourceTarget | PitchSourceTarget,
        source: SourceInput,
        expected_revision: int | None = None,
    ) -> SourceView:
        """Replace a cited source's link, title, claim and access date in place, for example to
        fix a URL or the claim it supports. The source keeps its id, so claims that point at it
        keep their link; do not remove and cite it again. In `target` give the `source_id` and
        the owner, exactly one of `number` (an episode's source, ids from get_research) or
        `pitch_id` (a pitch's source, ids from get_pitch). `accessed_on` left out keeps the
        date. Needs content:write. Pass the source's `revision` from that read as
        `expected_revision` so you never overwrite a change you have not read."""
        access = channel_access(services, context, slug)
        if isinstance(target, EpisodeSourceTarget):
            return replace_episode_source(services, access, target, source, expected_revision)
        return replace_pitch_source(services, access, target, source, expected_revision)
