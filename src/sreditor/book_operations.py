"""Books: ready-made exports people ask for by name (a pitch, an episode, the library, the
channel or its rights and credits), each built from parts, as one model that the JSON, Excel and
PDF files are all written from (rule project.export).

A book only reads, with the same permission as reading each part. Waivers and media files are
never part of one; the srstudio zip is a separate export. The PDF may show the channel's own
logo and the library's pictures, which `BookImage` and `BookBrand` point to without putting
their paths in the JSON.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from sreditor import classification_operations as classifying
from sreditor import context_operations, episode_operations, library_operations, pitch_operations
from sreditor import external_media_operations as third_party
from sreditor import srstudio_operations as srstudio
from sreditor.assets import StoredFile
from sreditor.channels import Asset, AssetKind, Color, primary_of
from sreditor.classification import ChannelProfile, ObservedProfile
from sreditor.context import ItemContext, ItemKind, ItemRef, Link, LinkDirection
from sreditor.deps import ChannelAccess, Services
from sreditor.editorial import Editorial
from sreditor.episodes import Episode, EpisodeStatus
from sreditor.export_operations import (
    FORMAT_VERSION,
    ChannelSummary,
    EpisodeExport,
    channel_summary,
    every_item,
    export_episode,
)
from sreditor.external_media import EpisodeCredits, ExternalKind, ExternalMedia
from sreditor.field_types import MediaType
from sreditor.library import LibraryEntry, LibraryKind
from sreditor.library_operations import Cast
from sreditor.members import Permission
from sreditor.operations import get_editorial
from sreditor.pagination import MAX_LIMIT, Page
from sreditor.pitch_operations import PitchQuery
from sreditor.pitches import Pitch, PitchSource
from sreditor.srstudio import EntrySheet, Relationship

# Where the API serves books; the workspace pages link to them for download.
PITCH_BOOK = "/channels/{slug}/pitches/{pitch_id}/book"
EPISODE_BOOK = "/channels/{slug}/episodes/{number}/book"
LIBRARY_BOOK = "/channels/{slug}/books/library"
CHANNEL_BOOK = "/channels/{slug}/books/channel"
RIGHTS_BOOK = "/channels/{slug}/books/rights"
# Pictures the PDF renderer draws; anything else (3D, EXR, KTX2…) stays out of the book.
PRINTABLE_IMAGES = frozenset({"image/png", "image/jpeg", "image/webp"})
PRINTABLE_LOGOS = PRINTABLE_IMAGES | {"image/svg+xml"}
# Enough to recognise a character or a place without turning the book into an album.
IMAGES_PER_ENTRY = 3


class BookKind(StrEnum):
    """The books a channel offers."""

    PITCH = "pitch"
    EPISODE = "episode"
    LIBRARY = "library"
    CHANNEL = "channel"
    RIGHTS = "rights"


class BookSection(StrEnum):
    """A part of a book that people may leave out."""

    EDITORIAL = "editorial"
    PROFILE = "profile"
    PITCHES = "pitches"
    EPISODES = "episodes"
    CHARACTERS = "characters"
    PLACES = "places"
    STYLES = "styles"
    PROPS = "props"
    RELATIONSHIPS = "relationships"
    THIRD_PARTY = "third_party"
    CREDITS = "credits"


LIBRARY_SECTIONS = (
    BookSection.CHARACTERS,
    BookSection.PLACES,
    BookSection.STYLES,
    BookSection.PROPS,
    BookSection.RELATIONSHIPS,
)
# The sections each book may hold, in the order the book prints them.
KIND_SECTIONS: dict[BookKind, tuple[BookSection, ...]] = {
    BookKind.PITCH: (),
    BookKind.EPISODE: (),
    BookKind.LIBRARY: LIBRARY_SECTIONS,
    BookKind.CHANNEL: (
        BookSection.EDITORIAL,
        BookSection.PROFILE,
        BookSection.PITCHES,
        BookSection.EPISODES,
        *LIBRARY_SECTIONS,
        BookSection.THIRD_PARTY,
    ),
    BookKind.RIGHTS: (BookSection.THIRD_PARTY, BookSection.CREDITS),
}
SECTION_KINDS = {
    BookSection.CHARACTERS: LibraryKind.CHARACTER,
    BookSection.PLACES: LibraryKind.LOCATION,
    BookSection.STYLES: LibraryKind.STYLE,
    BookSection.PROPS: LibraryKind.PROP,
}
ENTRY_ITEM_KINDS = {
    LibraryKind.CHARACTER: ItemKind.CHARACTER,
    LibraryKind.LOCATION: ItemKind.LOCATION,
    LibraryKind.STYLE: ItemKind.STYLE,
    LibraryKind.PROP: ItemKind.PROP,
}


class SectionNotOffered(ValueError):
    """A book was asked for a section it does not hold."""

    def __init__(self, kind: BookKind, section: BookSection) -> None:
        offered = ", ".join(KIND_SECTIONS[kind]) or "none"
        super().__init__(f"The {kind} book has no {section} section; it offers: {offered}")
        self.section = section


class EpisodeFilter(BaseModel):
    """Which episodes a book holds: of some statuses and within a range of numbers."""

    model_config = ConfigDict(frozen=True)

    statuses: frozenset[EpisodeStatus] = frozenset()
    first: PositiveInt | None = None
    last: PositiveInt | None = None

    @model_validator(mode="after")
    def range_is_ordered(self) -> EpisodeFilter:
        """Refuse a range that ends before it starts, which would quietly hold nothing."""
        if self.first is not None and self.last is not None and self.first > self.last:
            raise ValueError("The first episode must not come after the last")
        return self

    def accepts(self, episode: Episode) -> bool:
        """Whether the episode belongs in the book."""
        if self.statuses and episode.status not in self.statuses:
            return False
        if self.first is not None and episode.number < self.first:
            return False
        return self.last is None or episode.number <= self.last


class BookOptions(BaseModel):
    """What a book holds; no sections means every section the book offers."""

    model_config = ConfigDict(frozen=True)

    sections: tuple[BookSection, ...] = ()
    episodes: EpisodeFilter = EpisodeFilter()


class BookImage(BaseModel):
    """A picture of the channel's own library shown in the PDF; its file stays out of JSON."""

    model_config = ConfigDict(frozen=True)

    media_id: UUID
    title: str
    attribution: str
    media_type: MediaType
    stored: StoredFile = Field(exclude=True)


class BookBrand(BaseModel):
    """The channel kit's colors, and its logo for the PDF's cover and header."""

    model_config = ConfigDict(frozen=True)

    colors: list[Color]
    logo: Asset | None = Field(default=None, exclude=True)

    @property
    def cover_color(self) -> Color | None:
        """The primary-role color the cover is drawn in; the first color when none is primary."""
        return primary_of(self.colors)


class PitchBookPart(BaseModel):
    """A pitch with its research, context and the episodes it feeds."""

    model_config = ConfigDict(frozen=True)

    type: Literal["pitch"] = "pitch"
    pitch: Pitch
    notes: str
    sources: list[PitchSource]
    context: ItemContext
    episodes: list[int]


class EpisodeBookPart(BaseModel):
    """Everything an episode holds, with its context and the cast its screenplay names."""

    model_config = ConfigDict(frozen=True)

    type: Literal["episode"] = "episode"
    export: EpisodeExport
    context: ItemContext
    cast: Cast


class EntryBookPart(BaseModel):
    """A library entry with its srstudio sheet, its context and pictures linked to it."""

    model_config = ConfigDict(frozen=True)

    type: Literal["library_entry"] = "library_entry"
    entry: LibraryEntry
    sheet: EntrySheet
    context: ItemContext
    images: list[BookImage]


class RelationshipRow(BaseModel):
    """How two characters relate, with their names."""

    model_config = ConfigDict(frozen=True)

    first: str
    second: str
    relationship: Relationship


class RelationshipsBookPart(BaseModel):
    """How the library's characters relate."""

    model_config = ConfigDict(frozen=True)

    type: Literal["relationships"] = "relationships"
    relationships: list[RelationshipRow]


class ThirdPartyBookPart(BaseModel):
    """A third-party item, its rights and the episodes that use it."""

    model_config = ConfigDict(frozen=True)

    type: Literal["third_party"] = "third_party"
    item: ExternalMedia
    episodes: list[int]
    context: ItemContext


class CreditsBookPart(BaseModel):
    """What one episode owes for the third-party media it uses."""

    model_config = ConfigDict(frozen=True)

    type: Literal["credits"] = "credits"
    episode: Episode
    credits: EpisodeCredits


class EditorialBookPart(BaseModel):
    """The channel's editorial guidelines."""

    model_config = ConfigDict(frozen=True)

    type: Literal["editorial"] = "editorial"
    editorial: Editorial


class ProfileBookPart(BaseModel):
    """The profile the channel declares beside the one its episodes show."""

    model_config = ConfigDict(frozen=True)

    type: Literal["profile"] = "profile"
    declared: ChannelProfile
    observed: ObservedProfile


BookPart = Annotated[
    PitchBookPart
    | EpisodeBookPart
    | EntryBookPart
    | RelationshipsBookPart
    | ThirdPartyBookPart
    | CreditsBookPart
    | EditorialBookPart
    | ProfileBookPart,
    Field(discriminator="type"),
]


class Book(BaseModel):
    """A book: what it is, the channel and brand it comes from, and its parts in order."""

    model_config = ConfigDict(frozen=True)

    format_version: str = FORMAT_VERSION
    kind: BookKind
    title: str
    exported_at: datetime
    channel: ChannelSummary
    brand: BookBrand
    options: BookOptions
    parts: list[BookPart]

    def of_type(self, part_type: str) -> list[BookPart]:
        """The parts of one type, in book order."""
        return [part for part in self.parts if part.type == part_type]


@dataclass(frozen=True)
class Reader:
    """What every part builder needs: the services, the access and the options."""

    app: Services
    access: ChannelAccess
    options: BookOptions


def chosen_sections(kind: BookKind, options: BookOptions) -> list[BookSection]:
    """The sections to print, in the book's order; SectionNotOffered for a foreign one."""
    offered = KIND_SECTIONS[kind]
    for section in options.sections:
        if section not in offered:
            raise SectionNotOffered(kind, section)
    return [section for section in offered if not options.sections or section in options.sections]


def book_brand(access: ChannelAccess) -> BookBrand:
    """The channel kit's colors and a printable logo (or the avatar, when there is no logo)."""
    kit = access.channel.brand
    logo = kit.assets.get(AssetKind.LOGO) or kit.avatar
    if logo is not None and logo.media_type not in PRINTABLE_LOGOS:
        logo = None
    return BookBrand(colors=kit.colors, logo=logo)


def item_context(reader: Reader, kind: ItemKind, item_id: str) -> ItemContext:
    """An item's answers to the six questions."""
    return context_operations.get_context(reader.app, reader.access, ItemRef(kind=kind, id=item_id))


def pitch_part(reader: Reader, pitch_id: UUID) -> PitchBookPart:
    """A pitch with its notes, sources, context and episodes."""
    app, access = reader.app, reader.access
    sources = every_item(
        lambda cursor: pitch_operations.list_sources(app, access, pitch_id, cursor, MAX_LIMIT)
    )
    return PitchBookPart(
        pitch=pitch_operations.get_pitch(app, access, pitch_id),
        notes=pitch_operations.get_notes(app, access, pitch_id).text,
        sources=sources,
        context=item_context(reader, ItemKind.PITCH, str(pitch_id)),
        episodes=pitch_operations.pitch_episodes(app, access, pitch_id),
    )


def episode_part(reader: Reader, number: int) -> EpisodeBookPart:
    """Everything an episode holds, with its context and cast."""
    return EpisodeBookPart(
        export=export_episode(reader.app, reader.access, number),
        context=item_context(reader, ItemKind.EPISODE, str(number)),
        cast=library_operations.episode_cast(reader.app, reader.access, number),
    )


def chosen_episodes(reader: Reader) -> list[Episode]:
    """The channel's episodes the options keep, by number."""
    episodes = every_item(
        lambda cursor: episode_operations.list_episodes(
            reader.app, reader.access, cursor, MAX_LIMIT
        )
    )
    return [episode for episode in episodes if reader.options.episodes.accepts(episode)]


def links_of(reader: Reader, item: ItemRef, direction: LinkDirection) -> list[Link]:
    """Every link an item makes, or every link that mentions it."""

    def fetch(cursor: str | None) -> Page[Link]:
        query = context_operations.LinkQuery(
            item=item, direction=direction, cursor=cursor, limit=MAX_LIMIT
        )
        return context_operations.list_links(reader.app, reader.access, query)

    return every_item(fetch)


def linked_media_ids(reader: Reader, item: ItemRef) -> list[UUID]:
    """The third-party items an item links to or is mentioned by, oldest link first."""
    found: list[UUID] = []
    for direction in LinkDirection:
        for link in links_of(reader, item, direction):
            other = link.target if direction is LinkDirection.OUT else link.source
            if other.kind is ItemKind.EXTERNAL_MEDIA and UUID(other.id) not in found:
                found.append(UUID(other.id))
    return found


def printable_image(item: ExternalMedia) -> BookImage | None:
    """The item as a picture for the book, when it is one with a copy the PDF can draw."""
    stored = item.stored()
    if item.kind is not ExternalKind.IMAGE or stored is None:
        return None
    if stored.media_type not in PRINTABLE_IMAGES:
        return None
    return BookImage(
        media_id=item.id,
        title=item.title,
        attribution=item.attribution,
        media_type=stored.media_type,
        stored=stored,
    )


def entry_images(reader: Reader, item: ItemRef) -> list[BookImage]:
    """Up to IMAGES_PER_ENTRY pictures linked to a library entry."""
    images: list[BookImage] = []
    for media_id in linked_media_ids(reader, item):
        image = printable_image(third_party.get_item(reader.app, reader.access, media_id))
        if image is not None:
            images.append(image)
        if len(images) == IMAGES_PER_ENTRY:
            break
    return images


def entry_parts(reader: Reader, kind: LibraryKind) -> list[EntryBookPart]:
    """Every library entry of a kind with its sheet, context and pictures."""
    parts = []
    for found in srstudio.library_sheets(reader.app, reader.access, kind):
        item = ItemRef(kind=ENTRY_ITEM_KINDS[kind], id=str(found.entry.id))
        parts.append(
            EntryBookPart(
                entry=found.entry,
                sheet=found.sheet,
                context=item_context(reader, item.kind, item.id),
                images=entry_images(reader, item),
            )
        )
    return parts


def relationships_part(reader: Reader) -> list[BookPart]:
    """The relationships between characters, named, or nothing when there are none."""
    found = srstudio.list_relationships(reader.app, reader.access)
    if not found:
        return []
    names = {
        entry.id: entry.name
        for entry in srstudio.all_entries(
            reader.app, reader.access.channel.slug, LibraryKind.CHARACTER
        )
    }
    rows = [
        RelationshipRow(
            first=names.get(pair.first, ""), second=names.get(pair.second, ""), relationship=pair
        )
        for pair in found
    ]
    return [RelationshipsBookPart(relationships=rows)]


def third_party_parts(reader: Reader) -> list[BookPart]:
    """Every third-party item with its rights, context and the episodes that use it."""
    app, access = reader.app, reader.access
    items = every_item(
        lambda cursor: third_party.list_items(
            app, access, third_party.ItemQuery(cursor=cursor, limit=MAX_LIMIT)
        )
    )
    return [
        ThirdPartyBookPart(
            item=item,
            episodes=third_party.item_episodes(app, access, item.id),
            context=item_context(reader, ItemKind.EXTERNAL_MEDIA, str(item.id)),
        )
        for item in items
    ]


def credits_parts(reader: Reader) -> list[BookPart]:
    """What each chosen episode owes for its third-party media."""
    return [
        CreditsBookPart(
            episode=episode,
            credits=third_party.get_credits(reader.app, reader.access, episode.number),
        )
        for episode in chosen_episodes(reader)
    ]


def pitches_parts(reader: Reader) -> list[BookPart]:
    """Every pitch of the channel."""
    pitches = every_item(
        lambda cursor: pitch_operations.list_pitches(
            reader.app, reader.access, PitchQuery(cursor=cursor, limit=MAX_LIMIT)
        )
    )
    return [pitch_part(reader, pitch.id) for pitch in pitches]


def episodes_parts(reader: Reader) -> list[BookPart]:
    """Every chosen episode in full."""
    return [episode_part(reader, episode.number) for episode in chosen_episodes(reader)]


def editorial_part(reader: Reader) -> list[BookPart]:
    """The channel-wide editorial guidelines."""
    return [EditorialBookPart(editorial=get_editorial(reader.app, reader.access, None))]


def profile_part(reader: Reader) -> list[BookPart]:
    """The declared and observed profiles."""
    return [
        ProfileBookPart(
            declared=classifying.get_profile(reader.app, reader.access),
            observed=classifying.get_observed_profile(reader.app, reader.access),
        )
    ]


def library_builder(kind: LibraryKind) -> Callable[[Reader], list[BookPart]]:
    """The builder of one library section."""

    def build(reader: Reader) -> list[BookPart]:
        return list(entry_parts(reader, kind))

    return build


SECTION_BUILDERS: dict[BookSection, Callable[[Reader], list[BookPart]]] = {
    BookSection.EDITORIAL: editorial_part,
    BookSection.PROFILE: profile_part,
    BookSection.PITCHES: pitches_parts,
    BookSection.EPISODES: episodes_parts,
    **{section: library_builder(kind) for section, kind in SECTION_KINDS.items()},
    BookSection.RELATIONSHIPS: relationships_part,
    BookSection.THIRD_PARTY: third_party_parts,
    BookSection.CREDITS: credits_parts,
}


class BookRequest(BaseModel):
    """Which sectioned book to build, and with what options."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[BookKind.LIBRARY, BookKind.CHANNEL, BookKind.RIGHTS]
    options: BookOptions = BookOptions()


def assembled(reader: Reader, kind: BookKind, title: str, parts: list[BookPart]) -> Book:
    """A book around its parts."""
    return Book(
        kind=kind,
        title=title,
        exported_at=datetime.now(UTC),
        channel=channel_summary(reader.access),
        brand=book_brand(reader.access),
        options=reader.options,
        parts=parts,
    )


def sectioned_book(app: Services, access: ChannelAccess, request: BookRequest) -> Book:
    """The library, channel or rights book, holding the sections the options choose."""
    access.require(Permission.READ)
    reader = Reader(app=app, access=access, options=request.options)
    parts: list[BookPart] = []
    for section in chosen_sections(request.kind, request.options):
        parts += SECTION_BUILDERS[section](reader)
    return assembled(reader, request.kind, access.channel.name, parts)


def pitch_book(app: Services, access: ChannelAccess, pitch_id: UUID) -> Book:
    """One pitch's book: its summary, research, sources, context and episodes."""
    access.require(Permission.READ)
    reader = Reader(app=app, access=access, options=BookOptions())
    part = pitch_part(reader, pitch_id)
    return assembled(reader, BookKind.PITCH, part.pitch.title, [part])


def episode_book(app: Services, access: ChannelAccess, number: int) -> Book:
    """One episode's book: everything it holds, its context and cast."""
    access.require(Permission.READ)
    reader = Reader(app=app, access=access, options=BookOptions())
    part = episode_part(reader, number)
    title = f"#{number} · {part.export.episode.title}"
    return assembled(reader, BookKind.EPISODE, title, [part])
