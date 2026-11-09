"""srstudio sheet, slug and relationship operations shared by the routes, tools and pages (rule agentic.mcp).

Filling in what srstudio needs is content work, like the library itself: open to contributors and
the content:write scope. Removing a relationship, like removing an entry, needs a role that may
change the channel. A fact may only cite an episode the channel has published (rule project.srstudio.bible).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.episodes import EpisodeStatus
from sreditor.library import EntryNotFound, EpisodeStyle, LibraryEntry, LibraryKind, StyleChoice
from sreditor.library_operations import EntryChange, choose_episode_style, get_episode_style
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import MAX_LIMIT
from sreditor.short_script import Block
from sreditor.srstudio import (
    CharacterSheet,
    EntrySheet,
    EpisodeSlug,
    EpisodeSlugInput,
    EpisodeStyleDetail,
    Fact,
    PlaceSheet,
    Relationship,
    RelationshipInput,
    SheetInput,
)
from sreditor.srstudio_vocabulary import is_unknown_toon_rig, toon_rigs

SHEET_WRITE = "_sheet.write"  # character_sheet.write: stmt operations are resource.verb
SLUG_WRITE = "episode_slug.write"
RELATIONSHIP_CREATE = "relationship.create"
RELATIONSHIP_WRITE = "relationship.write"
RELATIONSHIP_DELETE = "relationship.delete"


class FactNotPublished(ValueError):
    """A fact cites an episode the channel has not published."""

    def __init__(self, number: int) -> None:
        super().__init__(f"Episode {number} is not published, so a fact cannot cite it yet")
        self.number = number


class NotInLibrary(ValueError):
    """A short script names a speaker, cast character or prop the channel's library lacks."""

    def __init__(self, location: tuple[str | int, ...], detail: str) -> None:
        super().__init__(detail)
        self.location = location


class UnknownToonRig(ValueError):
    """A character's rig claims srstudio's toon cast (toon-...) but names no one in it."""

    def __init__(self, rig: str) -> None:
        super().__init__(
            f"The rig {rig} is not in srstudio's toon cast, so nothing would draw it; use one of "
            f"{', '.join(toon_rigs())}, a rig outside the toon cast, or none"
        )
        self.rig = rig


# Where an unknown rig sits in the body save_sheet takes: SheetInput.sheet.rig.
RIG_LOCATION = ("sheet", "sheet", "rig")

NO_CHARACTER = (
    "No character with id %(id)s in this channel's library; list_library with kind character"
    " gives the valid entry ids."
)
NO_PLACE = (
    "No place with id %(id)s in this channel's library; list_library with kind location"
    " gives the valid entry ids."
)
NO_PROP = (
    "No prop with slug %(slug)s in this channel's library; list_library with kind prop"
    " lists the props, and each one's srstudio sheet holds its slug."
)


class RelationshipChange(BaseModel):
    """Which relationship a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    relationship_id: UUID
    expected_revision: int | None = None


class LibrarySheet(BaseModel):
    """A library entry beside its srstudio sheet."""

    model_config = ConfigDict(frozen=True)

    entry: LibraryEntry
    sheet: EntrySheet


def published_numbers(app: Services, channel: str) -> set[int]:
    """The numbers of the channel's published episodes."""
    episodes = app.episodes.list(channel, None, app.episodes.count(channel) or 1)
    return {episode.number for episode in episodes if episode.status is EpisodeStatus.PUBLISHED}


def sheet_facts(data: SheetInput) -> list[Fact]:
    """The facts a sheet cites: a character's canon or a place's citations."""
    if isinstance(data.sheet, CharacterSheet):
        return data.sheet.canon
    return data.sheet.cites if isinstance(data.sheet, PlaceSheet) else []


def check_facts(app: Services, channel: str, facts: list[Fact]) -> None:
    """Refuse a fact that cites an episode the channel has not published."""
    if not facts:
        return
    published = published_numbers(app, channel)
    for fact in facts:
        if fact.episode not in published:
            raise FactNotPublished(fact.episode)


def check_rig(data: SheetInput) -> None:
    """Refuse a character whose toon rig srstudio cannot draw (rule project.srstudio.bible)."""
    if isinstance(data.sheet, CharacterSheet) and is_unknown_toon_rig(data.sheet.rig):
        raise UnknownToonRig(data.sheet.rig)


def all_entries(app: Services, channel: str, kind: LibraryKind) -> list[LibraryEntry]:
    """Every entry of a kind, page by page, so a large library is checked whole."""
    found: list[LibraryEntry] = []
    while page := app.library.entries(
        channel, kind, app.library.position(found[-1].id) if found else None, MAX_LIMIT
    ):
        found.extend(page)
    return found


def prop_slugs(app: Services, channel: str) -> set[str]:
    """The slugs the channel's props go by, proposed ones included."""
    sheets = app.srstudio.sheets(all_entries(app, channel, LibraryKind.PROP))
    return {sheet.slug for sheet in sheets.values()}


def has_entry(app: Services, channel: str, kind: LibraryKind, entry_id: UUID) -> bool:
    """Whether the channel's library holds this entry of this kind."""
    try:
        app.library.get(channel, kind, entry_id)
    except EntryNotFound:
        return False
    return True


def has_character(app: Services, channel: str, entry_id: UUID) -> bool:
    """Whether the channel's library holds this character."""
    return has_entry(app, channel, LibraryKind.CHARACTER, entry_id)


def place_references(blocks: list[Block]) -> list[tuple[tuple[str | int, ...], UUID]]:
    """Every place a script's blocks play in, with where it is named."""
    return [
        (("blocks", index, "place"), block.place)
        for index, block in enumerate(blocks)
        if block.place is not None
    ]


def cast_references(blocks: list[Block]) -> list[tuple[tuple[str | int, ...], UUID]]:
    """Every character a script names, with where: block speakers and beat characters."""
    found: list[tuple[tuple[str | int, ...], UUID]] = []
    for index, block in enumerate(blocks):
        if block.speaker is not None:
            found.append((("blocks", index, "speaker"), block.speaker))
        for beat_index, beat in enumerate(block.beats):
            found.append((("blocks", index, "beats", beat_index, "character"), beat.character))
    return found


def check_cast(app: Services, channel: str, blocks: list[Block]) -> None:
    """Refuse a speaker, beat character, place or prop the channel's library does not hold."""
    known: dict[UUID, bool] = {}
    for location, entry_id in cast_references(blocks):
        if not known.setdefault(entry_id, has_character(app, channel, entry_id)):
            raise NotInLibrary(location, NO_CHARACTER % {"id": entry_id})
    for location, entry_id in place_references(blocks):
        if not has_entry(app, channel, LibraryKind.LOCATION, entry_id):
            raise NotInLibrary(location, NO_PLACE % {"id": entry_id})
    props = [
        (("blocks", index, "beats", beat_index, "prop"), beat.prop)
        for index, block in enumerate(blocks)
        for beat_index, beat in enumerate(block.beats)
        if beat.prop is not None
    ]
    slugs = prop_slugs(app, channel) if props else set()
    for location, slug in props:
        if slug not in slugs:
            raise NotInLibrary(location, NO_PROP % {"slug": slug})


def get_sheet(
    app: Services, access: ChannelAccess, kind: LibraryKind, entry_id: UUID
) -> EntrySheet:
    """A library entry's srstudio sheet, or the proposal for one."""
    access.require(Permission.READ)
    return app.srstudio.sheet(app.library.get(access.channel.slug, kind, entry_id))


def library_sheets(app: Services, access: ChannelAccess, kind: LibraryKind) -> list[LibrarySheet]:
    """Every entry of a kind with its sheet, in the order they were added."""
    access.require(Permission.READ)
    entries = app.library.entries(access.channel.slug, kind, None, MAX_LIMIT)
    sheets = app.srstudio.sheets(entries)
    return [LibrarySheet(entry=entry, sheet=sheets[entry.id]) for entry in entries]


def save_sheet(
    app: Services, access: ChannelAccess, change: EntryChange, data: SheetInput
) -> EntrySheet:
    """Save a library entry's srstudio sheet."""
    access.require(Permission.WRITE_CONTENT)
    entry = app.library.get(access.channel.slug, change.kind, change.entry_id)
    check_facts(app, entry.channel, sheet_facts(data))
    check_rig(data)
    return gated(
        app,
        access,
        f"{change.kind}{SHEET_WRITE}",
        lambda: app.srstudio.save_sheet(entry, data, change.expected_revision),
    )


def with_sheet(app: Services, chosen: EpisodeStyle) -> EpisodeStyleDetail:
    """The episode's picked style together with its structured sheet (rule project.visual-style)."""
    sheet = app.srstudio.sheet(chosen.style) if chosen.style is not None else None
    return EpisodeStyleDetail(**dict(chosen), sheet=sheet)


def get_episode_style_detail(
    app: Services, access: ChannelAccess, number: int
) -> EpisodeStyleDetail:
    """The visual style an episode picked, with its palette, fonts, cut rhythm and safe zone."""
    return with_sheet(app, get_episode_style(app, access, number))


def choose_episode_style_detail(
    app: Services,
    access: ChannelAccess,
    number: int,
    choice: StyleChoice,
    expected_revision: int | None,
) -> EpisodeStyleDetail:
    """Pick the episode's visual style, or none, and answer with its structured sheet."""
    chosen = choose_episode_style(app, access, number, choice, expected_revision)
    return with_sheet(app, chosen)


def get_episode_slug(app: Services, access: ChannelAccess, number: int) -> EpisodeSlug:
    """An episode's srstudio slug and seed, or the proposal made from its title."""
    slug = episode_access(app, access, number, Permission.READ)
    title = app.episodes.get(slug, number).episode.title
    return app.srstudio.episode_slug(slug, number, title)


def save_episode_slug(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: EpisodeSlugInput,
    expected_revision: int | None,
) -> EpisodeSlug:
    """Save an episode's srstudio slug and seed."""
    current = get_episode_slug(app, access, number)
    access.require(Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        SLUG_WRITE,
        lambda: app.srstudio.save_episode_slug(current, data, expected_revision),
    )


def check_pair(app: Services, channel: str, data: RelationshipInput) -> None:
    """Both sides must be characters of the channel's library, and facts must cite published episodes."""
    app.library.get(channel, LibraryKind.CHARACTER, data.first)
    app.library.get(channel, LibraryKind.CHARACTER, data.second)
    check_facts(app, channel, data.history)


def list_relationships(app: Services, access: ChannelAccess) -> list[Relationship]:
    """How the channel's characters relate, in the order the relationships were recorded."""
    access.require(Permission.READ)
    return app.srstudio.relationships(access.channel.slug)


def add_relationship(app: Services, access: ChannelAccess, data: RelationshipInput) -> Relationship:
    """Record how two characters of the library relate."""
    access.require(Permission.WRITE_CONTENT)
    channel = access.channel.slug
    check_pair(app, channel, data)
    return gated(
        app, access, RELATIONSHIP_CREATE, lambda: app.srstudio.add_relationship(channel, data)
    )


def replace_relationship(
    app: Services, access: ChannelAccess, change: RelationshipChange, data: RelationshipInput
) -> Relationship:
    """Change how two characters relate."""
    access.require(Permission.WRITE_CONTENT)
    channel = access.channel.slug
    current = app.srstudio.relationship(channel, change.relationship_id)
    check_pair(app, channel, data)
    return gated(
        app,
        access,
        RELATIONSHIP_WRITE,
        lambda: app.srstudio.replace_relationship(current, data, change.expected_revision),
    )


def delete_relationship(app: Services, access: ChannelAccess, change: RelationshipChange) -> None:
    """Remove a relationship from the library."""
    access.require(Permission.WRITE_CHANNEL)
    current = app.srstudio.relationship(access.channel.slug, change.relationship_id)
    gated(
        app,
        access,
        RELATIONSHIP_DELETE,
        lambda: app.srstudio.remove_relationship(current, change.expected_revision),
    )
