"""Library operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Adding and editing characters, locations and styles is content work, open to contributors and
the content:write scope, and so is picking an episode's style; deleting an entry changes every
episode that refers to it, so it needs a role that may change the channel itself.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.library import (
    EntryInput,
    EpisodeStyle,
    LibraryEntry,
    LibraryKind,
    StyleChoice,
    name_key,
)
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor

CREATE = "create"
WRITE = "write"
DELETE = "delete"
STYLE_WRITE = "episode_style.write"


def operation(kind: LibraryKind, verb: str) -> str:
    """The stmt operation an agent's library write is gated on, such as character.create."""
    return f"{kind}.{verb}"


class EntryChange(BaseModel):
    """Which entry a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    kind: LibraryKind
    entry_id: UUID
    expected_revision: int | None = None


class CastMember(BaseModel):
    """A name an episode's screenplay uses, and the library entry it refers to, if any."""

    model_config = ConfigDict(frozen=True)

    name: str
    entry: LibraryEntry | None = None


class Cast(BaseModel):
    """Who speaks in an episode and where it happens, matched against the channel's library."""

    model_config = ConfigDict(frozen=True)

    characters: list[CastMember]
    locations: list[CastMember]


def list_entries(
    app: Services, access: ChannelAccess, kind: LibraryKind, cursor: str | None, limit: int
) -> Page[LibraryEntry]:
    """The channel's characters or locations in the order they were added."""
    access.require(Permission.READ)
    rows = app.library.entries(access.channel.slug, kind, after_number(cursor), limit + 1)
    items = rows[:limit]
    has_more = len(rows) > limit
    cursor_out = encode_cursor(str(app.library.position(items[-1].id))) if has_more else None
    return Page[LibraryEntry](items=items, next_cursor=cursor_out)


def get_entry(
    app: Services, access: ChannelAccess, kind: LibraryKind, entry_id: UUID
) -> LibraryEntry:
    """One character or location."""
    access.require(Permission.READ)
    return app.library.get(access.channel.slug, kind, entry_id)


def create_entry(
    app: Services, access: ChannelAccess, kind: LibraryKind, data: EntryInput
) -> LibraryEntry:
    """Add a character or location to the channel's library."""
    access.require(Permission.WRITE_CONTENT)
    slug = access.channel.slug
    return gated(app, access, operation(kind, CREATE), lambda: app.library.create(slug, kind, data))


def replace_entry(
    app: Services, access: ChannelAccess, change: EntryChange, data: EntryInput
) -> LibraryEntry:
    """Rename or describe a character or location."""
    access.require(Permission.WRITE_CONTENT)
    entry = app.library.get(access.channel.slug, change.kind, change.entry_id)
    return gated(
        app,
        access,
        operation(change.kind, WRITE),
        lambda: app.library.replace(entry, data, change.expected_revision),
    )


def delete_entry(app: Services, access: ChannelAccess, change: EntryChange) -> None:
    """Remove a character or location; episodes that name it no longer refer to anything."""
    access.require(Permission.WRITE_CHANNEL)
    entry = app.library.get(access.channel.slug, change.kind, change.entry_id)
    gated(
        app,
        access,
        operation(change.kind, DELETE),
        lambda: app.library.delete(entry, change.expected_revision),
    )


def matched(app: Services, slug: str, kind: LibraryKind, names: list[str]) -> list[CastMember]:
    """Each name with the library entry it refers to."""
    found = app.library.named(slug, kind, names)
    return [CastMember(name=name, entry=found.get(name_key(name))) for name in names]


def episode_cast(app: Services, access: ChannelAccess, number: int) -> Cast:
    """The characters and locations an episode's screenplay names, matched against the library."""
    slug = episode_access(app, access, number, Permission.READ)
    screenplay = app.screenplays.screenplay(slug, number)
    return Cast(
        characters=matched(app, slug, LibraryKind.CHARACTER, screenplay.characters),
        locations=matched(app, slug, LibraryKind.LOCATION, screenplay.locations),
    )


def get_episode_style(app: Services, access: ChannelAccess, number: int) -> EpisodeStyle:
    """The visual style an episode picked from the library, with its revision."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.library.episode_style(slug, number)


def choose_episode_style(
    app: Services,
    access: ChannelAccess,
    number: int,
    choice: StyleChoice,
    expected_revision: int | None,
) -> EpisodeStyle:
    """Pick the episode's visual style from the library, or none."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        STYLE_WRITE,
        lambda: app.library.choose_style(slug, number, choice, expected_revision),
    )
