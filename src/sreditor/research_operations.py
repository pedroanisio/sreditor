"""Research operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Reading needs the read permission; writing notes and sources is content work, open to
contributors and the content:write scope. Every call first checks that the episode exists.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.research import NotesInput, ResearchNotes, Source, SourceInput


class ResearchOperation:
    """stmt operation names an agent's research writes are gated on."""

    NOTES_WRITE = "research.write"
    SOURCE_CREATE = "source.create"
    SOURCE_WRITE = "source.write"
    SOURCE_DELETE = "source.delete"


def get_notes(app: Services, access: ChannelAccess, number: int) -> ResearchNotes:
    """The episode's research notes, with the revision to send back when changing them."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.research.notes(slug, number)


def write_notes(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: NotesInput,
    expected_revision: int | None,
) -> ResearchNotes:
    """Replace the episode's research notes."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        ResearchOperation.NOTES_WRITE,
        lambda: app.research.write_notes(slug, number, data, expected_revision),
    )


def list_sources(
    app: Services, access: ChannelAccess, number: int, cursor: str | None, limit: int
) -> Page[Source]:
    """The episode's sources in the order they were added."""
    slug = episode_access(app, access, number, Permission.READ)
    rows = app.research.sources(slug, number, after_number(cursor), limit + 1)
    items = rows[:limit]
    has_more = len(rows) > limit
    cursor_out = encode_cursor(str(app.research.position(items[-1].id))) if has_more else None
    return Page[Source](items=items, next_cursor=cursor_out)


def get_source(app: Services, access: ChannelAccess, number: int, source_id: UUID) -> Source:
    """One source of the episode."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.research.source(slug, number, source_id)


def add_source(app: Services, access: ChannelAccess, number: int, data: SourceInput) -> Source:
    """Add a source to the episode."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        ResearchOperation.SOURCE_CREATE,
        lambda: app.research.add_source(slug, number, data),
    )


class SourceChange(BaseModel):
    """Which source a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    number: int
    source_id: UUID
    expected_revision: int | None = None


def replace_source(
    app: Services, access: ChannelAccess, change: SourceChange, data: SourceInput
) -> Source:
    """Replace a source's link, title, claim and access date."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    source = app.research.source(slug, change.number, change.source_id)
    return gated(
        app,
        access,
        ResearchOperation.SOURCE_WRITE,
        lambda: app.research.replace_source(source, data, change.expected_revision),
    )


def remove_source(app: Services, access: ChannelAccess, change: SourceChange) -> None:
    """Remove a source from the episode."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    source = app.research.source(slug, change.number, change.source_id)
    gated(
        app,
        access,
        ResearchOperation.SOURCE_DELETE,
        lambda: app.research.delete_source(source, change.expected_revision),
    )
