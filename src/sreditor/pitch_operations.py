"""Pitch operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Reading needs the read permission. Writing a pitch, its notes and its sources is content work,
open to contributors and the content:write scope, and so is pointing an episode at a pitch.
Deleting a whole pitch throws its research away, so it needs a role that may change the channel.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.pitches import (
    EpisodePitch,
    Pitch,
    PitchChoice,
    PitchInput,
    PitchNotes,
    PitchNotesInput,
    PitchSource,
    PitchStatus,
    PositionTable,
)
from sreditor.research import SourceInput

# How many of a pitch's sources an episode shows next to its own research.
SHOWN_SOURCES = 50


class PitchOperation:
    """stmt operation names an agent's pitch writes are gated on."""

    CREATE = "pitch.create"
    WRITE = "pitch.write"
    DELETE = "pitch.delete"
    NOTES_WRITE = "pitch_research.write"
    SOURCE_CREATE = "pitch_source.create"
    SOURCE_WRITE = "pitch_source.write"
    SOURCE_DELETE = "pitch_source.delete"
    EPISODE_WRITE = "episode_pitch.write"


class PitchChange(BaseModel):
    """Which pitch a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    pitch_id: UUID
    expected_revision: int | None = None


class PitchSourceChange(BaseModel):
    """Which source of which pitch a change targets, and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    pitch_id: UUID
    source_id: UUID
    expected_revision: int | None = None


class PitchQuery(BaseModel):
    """Which page of a channel's pitches to list, optionally of one status."""

    model_config = ConfigDict(frozen=True)

    status: PitchStatus | None = None
    cursor: str | None = None
    limit: int


class PitchResearch(BaseModel):
    """A pitch with its notes and first sources, as an episode that refers to it shows them."""

    model_config = ConfigDict(frozen=True)

    pitch: Pitch
    notes: PitchNotes
    sources: list[PitchSource]
    has_more_sources: bool


def pitch_access(
    app: Services, access: ChannelAccess, pitch_id: UUID, permission: Permission
) -> Pitch:
    """The channel's pitch once the permission is checked; PitchNotFound when there is none."""
    access.require(permission)
    return app.pitches.get(access.channel.slug, pitch_id)


def next_cursor[T](rows: list[T], limit: int, position: int | None) -> str | None:
    """The cursor after the last row shown, when there are more rows."""
    return encode_cursor(str(position)) if len(rows) > limit and position else None


def list_pitches(app: Services, access: ChannelAccess, query: PitchQuery) -> Page[Pitch]:
    """The channel's pitches in the order they were added."""
    access.require(Permission.READ)
    slug = access.channel.slug
    rows = app.pitches.list(slug, after_number(query.cursor), query.limit + 1, query.status)
    items = rows[: query.limit]
    last = app.pitches.position(PositionTable.PITCHES, items[-1].id) if items else None
    return Page[Pitch](items=items, next_cursor=next_cursor(rows, query.limit, last))


def get_pitch(app: Services, access: ChannelAccess, pitch_id: UUID) -> Pitch:
    """One pitch, with the revision to send back when changing it."""
    return pitch_access(app, access, pitch_id, Permission.READ)


def pitch_episodes(app: Services, access: ChannelAccess, pitch_id: UUID) -> list[int]:
    """The numbers of the episodes that refer to the pitch."""
    pitch = pitch_access(app, access, pitch_id, Permission.READ)
    return app.pitches.episodes_of(pitch.id)


def create_pitch(app: Services, access: ChannelAccess, data: PitchInput) -> Pitch:
    """Add a pitch to the channel."""
    access.require(Permission.WRITE_CONTENT)
    slug = access.channel.slug
    return gated(app, access, PitchOperation.CREATE, lambda: app.pitches.create(slug, data))


def replace_pitch(
    app: Services, access: ChannelAccess, change: PitchChange, data: PitchInput
) -> Pitch:
    """Replace a pitch's title, summary and status."""
    pitch = pitch_access(app, access, change.pitch_id, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        PitchOperation.WRITE,
        lambda: app.pitches.replace(pitch, data, change.expected_revision),
    )


def delete_pitch(app: Services, access: ChannelAccess, change: PitchChange) -> None:
    """Delete a pitch with its notes and sources; refused while episodes refer to it."""
    pitch = pitch_access(app, access, change.pitch_id, Permission.WRITE_CHANNEL)
    gated(
        app,
        access,
        PitchOperation.DELETE,
        lambda: app.pitches.delete(pitch, change.expected_revision),
    )


def get_notes(app: Services, access: ChannelAccess, pitch_id: UUID) -> PitchNotes:
    """The pitch's research notes, with the revision to send back when changing them."""
    pitch = pitch_access(app, access, pitch_id, Permission.READ)
    return app.pitches.notes(pitch.id)


def write_notes(
    app: Services, access: ChannelAccess, change: PitchChange, data: PitchNotesInput
) -> PitchNotes:
    """Replace the pitch's research notes."""
    pitch = pitch_access(app, access, change.pitch_id, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        PitchOperation.NOTES_WRITE,
        lambda: app.pitches.write_notes(pitch.id, data, change.expected_revision),
    )


def list_sources(
    app: Services, access: ChannelAccess, pitch_id: UUID, cursor: str | None, limit: int
) -> Page[PitchSource]:
    """The pitch's sources in the order they were added."""
    pitch = pitch_access(app, access, pitch_id, Permission.READ)
    rows = app.pitches.sources(pitch.id, after_number(cursor), limit + 1)
    items = rows[:limit]
    last = app.pitches.position(PositionTable.SOURCES, items[-1].id) if items else None
    return Page[PitchSource](items=items, next_cursor=next_cursor(rows, limit, last))


def get_source(
    app: Services, access: ChannelAccess, pitch_id: UUID, source_id: UUID
) -> PitchSource:
    """One source of the pitch."""
    pitch = pitch_access(app, access, pitch_id, Permission.READ)
    return app.pitches.source(pitch.id, source_id)


def add_source(
    app: Services, access: ChannelAccess, pitch_id: UUID, data: SourceInput
) -> PitchSource:
    """Add a source to the pitch."""
    pitch = pitch_access(app, access, pitch_id, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        PitchOperation.SOURCE_CREATE,
        lambda: app.pitches.add_source(pitch.id, data),
    )


def replace_source(
    app: Services, access: ChannelAccess, change: PitchSourceChange, data: SourceInput
) -> PitchSource:
    """Replace a source's link, title, claim and access date."""
    pitch = pitch_access(app, access, change.pitch_id, Permission.WRITE_CONTENT)
    source = app.pitches.source(pitch.id, change.source_id)
    return gated(
        app,
        access,
        PitchOperation.SOURCE_WRITE,
        lambda: app.pitches.replace_source(source, data, change.expected_revision),
    )


def remove_source(app: Services, access: ChannelAccess, change: PitchSourceChange) -> None:
    """Remove a source from the pitch."""
    pitch = pitch_access(app, access, change.pitch_id, Permission.WRITE_CONTENT)
    source = app.pitches.source(pitch.id, change.source_id)
    gated(
        app,
        access,
        PitchOperation.SOURCE_DELETE,
        lambda: app.pitches.delete_source(source, change.expected_revision),
    )


def get_episode_pitch(app: Services, access: ChannelAccess, number: int) -> EpisodePitch:
    """The pitch an episode refers to, with the revision to send back when changing it."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.pitches.episode_pitch(slug, number)


def choose_episode_pitch(
    app: Services,
    access: ChannelAccess,
    number: int,
    choice: PitchChoice,
    expected_revision: int | None,
) -> EpisodePitch:
    """Point the episode at one of the channel's pitches, or at none."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    if choice.pitch_id is not None:
        app.pitches.get(slug, choice.pitch_id)  # only a pitch of the same channel
    return gated(
        app,
        access,
        PitchOperation.EPISODE_WRITE,
        lambda: app.pitches.choose(slug, number, choice, expected_revision),
    )


def episode_pitch_research(
    app: Services, access: ChannelAccess, number: int
) -> PitchResearch | None:
    """The research of the pitch an episode refers to, as it is now; None without a pitch."""
    chosen = get_episode_pitch(app, access, number).pitch_id
    if chosen is None:
        return None
    pitch = app.pitches.get(access.channel.slug, chosen)
    rows = app.pitches.sources(pitch.id, None, SHOWN_SOURCES + 1)
    return PitchResearch(
        pitch=pitch,
        notes=app.pitches.notes(pitch.id),
        sources=rows[:SHOWN_SOURCES],
        has_more_sources=len(rows) > SHOWN_SOURCES,
    )
