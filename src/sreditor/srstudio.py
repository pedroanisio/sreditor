"""What srstudio needs beyond a library entry's name and description (rules project.srstudio.*).

Each library entry may have a sheet: its srstudio slug and the fields of its kind (a character's
bible entry, a place's world and citations, a style's srstudio style and palette, a prop's rig).
An episode has a slug and a seed, and the library records relationships between characters.
Each is stored and revised on its own, so editing a sheet never conflicts with renaming the entry.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from sreditor.database import Database, parse
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_
from sreditor.library import EpisodeStyle, LibraryEntry, LibraryKind
from sreditor.srstudio_vocabulary import (
    MAX_SPEED,
    MIN_SPEED,
    SLUG_PATTERN,
    CharacterKind,
    CharacterStatus,
    ComicRole,
    FactField,
    FactKind,
    PairStatus,
    Style,
    World,
)

TEXT_LENGTH = 2000
SHORT_LENGTH = 200
SLUG_LENGTH = 80
MAX_FACTS = 100
MAX_ITEMS = 30
MAX_SEED = 2**31 - 1
COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"
VOICE_ID_PATTERN = r"^[A-Za-z0-9]{0,40}$"
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY = 0.75
FALLBACK_SLUG = "untitled"
FONT_FAMILY_LENGTH = 100
MIN_FONT_WEIGHT = 100
MAX_FONT_WEIGHT = 900
FONT_WEIGHT_STEP = 100
MAX_TEXT_PX = 2160
MAX_SAFE_ZONE_PERCENT = 50
MAX_SHOT_SECONDS = 600
FULL_FRAME_PERCENT = 100

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=TEXT_LENGTH)]
Said = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TEXT_LENGTH)
]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SHORT_LENGTH)]
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, max_length=SLUG_LENGTH)]
Rig = Annotated[str, StringConstraints(pattern=f"({SLUG_PATTERN})|^$", max_length=SLUG_LENGTH)]
Color = Annotated[str, StringConstraints(pattern=COLOR_PATTERN)]
Ratio = Annotated[float, Field(ge=0, le=1)]


class Fact(BaseModel):
    """Something a published episode of the channel established, and how it shows there."""

    model_config = ConfigDict(frozen=True)

    fact: Said
    field: FactField
    episode: PositiveInt
    kind: FactKind
    quote: Said


class OpenItem(BaseModel):
    """A question about a character nobody has settled, with what someone proposes."""

    model_config = ConfigDict(frozen=True)

    question: Said
    proposal: Said


class Look(BaseModel):
    """How a character looks: colors, clothes and what is always the same."""

    model_config = ConfigDict(frozen=True)

    palette: Text = ""
    outfits: Text = ""
    never_changes: Text = ""


class VoiceProfile(BaseModel):
    """The ElevenLabs voice a character speaks with, its settings and how they talk (rule project.srstudio.voice)."""

    model_config = ConfigDict(frozen=True)

    voice_id: Annotated[str, StringConstraints(pattern=VOICE_ID_PATTERN)] = ""
    voice_name: Short = ""
    stability: Ratio = DEFAULT_STABILITY
    similarity_boost: Ratio = DEFAULT_SIMILARITY
    style: Ratio = 0.0
    speed: Annotated[float, Field(ge=MIN_SPEED, le=MAX_SPEED)] = 1.0
    use_speaker_boost: bool = True
    pace: Text = ""
    tics: Text = ""
    uses: Text = ""
    never_uses: Text = ""


class Who(BaseModel):
    """Who a character is."""

    model_config = ConfigDict(frozen=True)

    age_range: Text = ""
    job: Text = ""
    home: Text = ""
    normal_day: Text = ""
    wants: Text = ""
    flaw: Text = ""
    habit: Text = ""
    pov: Text = ""


class Comic(BaseModel):
    """What a character does in a joke."""

    model_config = ConfigDict(frozen=True)

    role: ComicRole | None = None
    under_pressure: Text = ""
    always_gets_them: Text = ""


class CharacterSheet(BaseModel):
    """A character's srstudio bible entry (rule project.srstudio.bible); empty texts export as open questions."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[LibraryKind.CHARACTER] = LibraryKind.CHARACTER
    world: World | None = None
    status: CharacterStatus = CharacterStatus.PLACEHOLDER
    character_kind: CharacterKind | None = None
    rig: Rig = ""
    rig_version: PositiveInt = 1
    look: Look = Look()
    voice: VoiceProfile = VoiceProfile()
    who: Who = Who()
    comic: Comic = Comic()
    never: list[Said] = Field(default_factory=list, max_length=MAX_ITEMS)
    open: list[OpenItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    canon: list[Fact] = Field(default_factory=list, max_length=MAX_FACTS)


class PlaceSheet(BaseModel):
    """A place's srstudio world and the facts episodes established about it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[LibraryKind.LOCATION] = LibraryKind.LOCATION
    world: World | None = None
    rig: Rig = ""
    cites: list[Fact] = Field(default_factory=list, max_length=MAX_FACTS)


class Palette(BaseModel):
    """A srstudio palette: line, light, shade and background colors."""

    model_config = ConfigDict(frozen=True)

    ink: Color
    white: Color
    shade: Color
    background: Color


class FontSpec(BaseModel):
    """One font role: the family and the weight (100 to 900) its text is set in."""

    model_config = ConfigDict(frozen=True)

    family: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FONT_FAMILY_LENGTH)
    ]
    weight: Annotated[
        int,
        Field(ge=MIN_FONT_WEIGHT, le=MAX_FONT_WEIGHT, multiple_of=FONT_WEIGHT_STEP),
    ]


class Fonts(BaseModel):
    """A style's fonts by role; a role left out uses srstudio's own font."""

    model_config = ConfigDict(frozen=True)

    heading: FontSpec | None = None
    body: FontSpec | None = None
    caption: FontSpec | None = None


class CutRhythm(BaseModel):
    """How fast a style cuts: the shortest shot and the average shot, in seconds."""

    model_config = ConfigDict(frozen=True)

    shortest_shot_s: Annotated[float, Field(gt=0, le=MAX_SHOT_SECONDS)]
    average_shot_s: Annotated[float, Field(gt=0, le=MAX_SHOT_SECONDS)]

    @model_validator(mode="after")
    def shortest_is_not_longer_than_average(self) -> CutRhythm:
        """The shortest shot cannot outlast the average one."""
        if self.shortest_shot_s > self.average_shot_s:
            raise ValueError(N_("The shortest shot cannot be longer than the average shot."))
        return self


Percent = Annotated[float, Field(ge=0, le=MAX_SAFE_ZONE_PERCENT)]


class SafeZone(BaseModel):
    """The margin kept clear of text on each edge, as a percent of the frame (0 to 50)."""

    model_config = ConfigDict(frozen=True)

    top: Percent
    right: Percent
    bottom: Percent
    left: Percent

    @model_validator(mode="after")
    def margins_leave_room(self) -> SafeZone:
        """Opposite margins must leave part of the frame for text."""
        if (
            self.top + self.bottom >= FULL_FRAME_PERCENT
            or self.left + self.right >= FULL_FRAME_PERCENT
        ):
            raise ValueError(N_("Opposite margins must leave part of the frame free."))
        return self


class StyleSheet(BaseModel):
    """A visual style as data srstudio reuses, so every episode looks alike (rule project.visual-style)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[LibraryKind.STYLE] = LibraryKind.STYLE
    style: Style | None = None
    palette: Palette | None = None
    fonts: Fonts | None = None
    cut_rhythm: CutRhythm | None = None
    min_text_px: Annotated[int, Field(gt=0, le=MAX_TEXT_PX)] | None = None
    safe_zone: SafeZone | None = None


class PropSheet(BaseModel):
    """The srstudio rig that draws a prop (rule project.srstudio.props)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[LibraryKind.PROP] = LibraryKind.PROP
    rig: Rig = ""


Sheet = Annotated[CharacterSheet | PlaceSheet | StyleSheet | PropSheet, Field(discriminator="kind")]
SHEET = TypeAdapter[CharacterSheet | PlaceSheet | StyleSheet | PropSheet](Sheet)
EMPTY_SHEETS: dict[LibraryKind, CharacterSheet | PlaceSheet | StyleSheet | PropSheet] = {
    LibraryKind.CHARACTER: CharacterSheet(),
    LibraryKind.LOCATION: PlaceSheet(),
    LibraryKind.STYLE: StyleSheet(),
    LibraryKind.PROP: PropSheet(),
}


class SheetInput(BaseModel):
    """A library entry's srstudio slug and sheet, as a caller sends them."""

    model_config = ConfigDict(frozen=True)

    slug: Slug
    sheet: Sheet


class EntrySheet(SheetInput):
    """A library entry's sheet; revision 0 and a proposed slug until someone saves it.

    `exported_at` is when an srstudio zip first carried the slug; from then on it is fixed.
    """

    entry_id: UUID
    revision: int
    updated_at: datetime | None = None
    exported_at: datetime | None = None


class EpisodeStyleDetail(EpisodeStyle):
    """An episode's visual style with its structured sheet; the sheet is null when it picked none."""

    sheet: EntrySheet | None = None


class EpisodeSlugInput(BaseModel):
    """An episode's srstudio slug and seed, as a caller sends them."""

    model_config = ConfigDict(frozen=True)

    slug: Slug
    seed: Annotated[int, Field(ge=0, le=MAX_SEED)]


class EpisodeSlug(EpisodeSlugInput):
    """An episode's srstudio slug and seed; revision 0 and proposed values until someone saves them.

    `exported_at` is when an srstudio zip first carried the slug; from then on it is fixed.
    """

    channel: str
    number: int
    revision: int
    updated_at: datetime | None = None
    exported_at: datetime | None = None


class RelationshipInput(BaseModel):
    """How two characters of the channel's library relate (srstudio's relationships.json)."""

    model_config = ConfigDict(frozen=True)

    first: UUID
    second: UUID
    relation: Text = ""
    how_they_know: Text = ""
    dynamic: Text = ""
    tension: Text = ""
    meet: Text = ""
    status: PairStatus = PairStatus.PROPOSAL
    history: list[Fact] = Field(default_factory=list, max_length=MAX_FACTS)


class Relationship(RelationshipInput):
    """A stored relationship, with the revision to send back when changing it."""

    id: UUID
    channel: str
    revision: int


class SlugTaken(ValueError):
    """Another entry of the channel (or another episode) already has this slug."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"The slug {slug} is already taken in this channel")
        self.slug = slug


class SlugLocked(ValueError):
    """The slug has gone out in an srstudio zip, so srstudio already knows the item by it."""

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"The slug {slug} is fixed: an srstudio export already used it, and srstudio"
            " finds the item by it. Send the same slug to change the other fields"
        )
        self.slug = slug


class SheetMismatch(ValueError):
    """The sheet sent is for another kind of entry."""

    def __init__(self, expected: LibraryKind, sent: LibraryKind) -> None:
        super().__init__(f"A {expected} takes a {expected} sheet, not a {sent} one")
        self.expected = expected
        self.sent = sent


class RelationshipNotFound(LookupError):
    """The channel's library has no such relationship."""


class SameCharacter(ValueError):
    """A relationship pairs a character with themself."""


def proposed_slug(name: str) -> str:
    """A slug made from a name: accents dropped, lowercase, words joined by hyphens."""
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).lower()
    words = "".join(char if char.isascii() and char.isalnum() else " " for char in plain).split()
    slug = "-".join(words)[:SLUG_LENGTH].strip("-")
    return slug if slug and slug[0].isalpha() else f"{FALLBACK_SLUG}-{slug}".strip("-")


def to_sheet(entry: LibraryEntry, row: sqlite3.Row | None) -> EntrySheet:
    """A stored sheet, or the proposal for an entry nobody has filled in yet."""
    if row is None:
        return EntrySheet(
            entry_id=entry.id,
            slug=proposed_slug(entry.name),
            sheet=EMPTY_SHEETS[entry.kind],
            revision=0,
        )
    return EntrySheet(
        entry_id=entry.id,
        slug=row["slug"],
        sheet=SHEET.validate_json(row["sheet"]),
        revision=row["revision"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        exported_at=parse(row["exported_at"]),
    )


def to_relationship(row: sqlite3.Row) -> Relationship:
    """A relationship row as a model."""
    data = json.loads(row["data"])
    return Relationship(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        first=UUID(row["first_id"]),
        second=UUID(row["second_id"]),
        revision=row["revision"],
        **data,
    )


RELATIONSHIP_COLUMNS = "id, channel_slug, first_id, second_id, data, revision"


def relationship_data(data: RelationshipInput) -> str:
    """The texts, status and history of a relationship, stored as one JSON document."""
    # A Relationship passed back in also carries its id and revision, which live in their own columns.
    stored = set(RelationshipInput.model_fields) - {"first", "second"}
    return data.model_dump_json(include=stored)


class ExportedSheet(BaseModel):
    """A library entry and the sheet an srstudio zip carried for it."""

    model_config = ConfigDict(frozen=True)

    entry: LibraryEntry
    sheet: EntrySheet


def exported_row(item: ExportedSheet, now: str) -> tuple[str, str, str, str, str, str, str]:
    """The library_sheets row that saves an exported proposal as the entry's first revision."""
    entry, sheet = item.entry, item.sheet
    return (
        str(entry.id),
        entry.channel,
        entry.kind,
        sheet.slug,
        sheet.sheet.model_dump_json(),
        now,
        now,
    )


class SrstudioStore:
    """Sheets, episode slugs and relationships in the application database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def sheet(self, entry: LibraryEntry) -> EntrySheet:
        """An entry's sheet, or the proposal for one."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT slug, sheet, revision, updated_at, exported_at FROM library_sheets"
                " WHERE entry_id = ?",
                (str(entry.id),),
            ).fetchone()
        return to_sheet(entry, row)

    def sheets(self, entries: list[LibraryEntry]) -> dict[UUID, EntrySheet]:
        """The sheets of these entries, proposals for those without one."""
        if not entries:
            return {}
        marks = ", ".join("?" * len(entries))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT entry_id, slug, sheet, revision, updated_at, exported_at FROM library_sheets"
                f" WHERE entry_id IN ({marks})",
                tuple(str(entry.id) for entry in entries),
            ).fetchall()
        stored = {row["entry_id"]: row for row in rows}
        return {entry.id: to_sheet(entry, stored.get(str(entry.id))) for entry in entries}

    def save_sheet(
        self, entry: LibraryEntry, data: SheetInput, expected_revision: int | None
    ) -> EntrySheet:
        """Save an entry's sheet; with `expected_revision`, only if it is still at it (0: none yet)."""
        if data.sheet.kind != entry.kind:
            raise SheetMismatch(entry.kind, LibraryKind(data.sheet.kind))
        current = self.sheet(entry)
        if current.exported_at and data.slug != current.slug:
            raise SlugLocked(current.slug)
        try:
            with self.database.connect() as connection:
                row = self.write_sheet(connection, entry, data, expected_revision)
        except sqlite3.IntegrityError as taken:
            raise SlugTaken(data.slug) from taken
        if row is None:
            raise PartChanged(f"srstudio sheet of {entry.name}", self.sheet(entry).revision)
        return to_sheet(entry, row)

    def write_sheet(
        self,
        connection: sqlite3.Connection,
        entry: LibraryEntry,
        data: SheetInput,
        expected_revision: int | None,
    ) -> sqlite3.Row | None:
        """Insert the first revision or update the one the caller saw; None when it changed."""
        values = (data.slug, data.sheet.model_dump_json(), self.database.now())
        if expected_revision == 0 or (
            expected_revision is None and self.sheet(entry).revision == 0
        ):
            row: sqlite3.Row | None = connection.execute(
                "INSERT INTO library_sheets (entry_id, channel_slug, kind, slug, sheet, revision,"
                " updated_at) VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT (entry_id) DO NOTHING"
                " RETURNING slug, sheet, revision, updated_at, exported_at",
                (str(entry.id), entry.channel, entry.kind, *values),
            ).fetchone()
            return row
        found: sqlite3.Row | None = connection.execute(
            "UPDATE library_sheets SET slug = ?, sheet = ?, updated_at = ?, revision = revision + 1"
            " WHERE entry_id = ? AND (? IS NULL OR revision = ?)"
            " RETURNING slug, sheet, revision, updated_at, exported_at",
            (*values, str(entry.id), expected_revision, expected_revision),
        ).fetchone()
        return found

    def episode_slug(self, channel: str, number: int, title: str) -> EpisodeSlug:
        """An episode's slug and seed, or the proposal made from its title and number."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT slug, seed, revision, updated_at, exported_at FROM episode_slugs"
                " WHERE channel_slug = ? AND number = ?",
                (channel, number),
            ).fetchone()
        if row is None:
            return EpisodeSlug(
                channel=channel, number=number, slug=proposed_slug(title), seed=number, revision=0
            )
        return EpisodeSlug(
            channel=channel,
            number=number,
            slug=row["slug"],
            seed=row["seed"],
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            exported_at=parse(row["exported_at"]),
        )

    def save_episode_slug(
        self, current: EpisodeSlug, data: EpisodeSlugInput, expected_revision: int | None
    ) -> EpisodeSlug:
        """Save an episode's slug and seed; with `expected_revision`, only if still at it."""
        if current.exported_at and data.slug != current.slug:
            raise SlugLocked(current.slug)
        revision = current.revision if expected_revision is None else expected_revision
        now = self.database.now()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "INSERT INTO episode_slugs (channel_slug, number, slug, seed, revision, updated_at)"
                    " VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT (channel_slug, number) DO UPDATE SET"
                    " slug = excluded.slug, seed = excluded.seed, updated_at = excluded.updated_at,"
                    " revision = episode_slugs.revision + 1 WHERE episode_slugs.revision = ?"
                    " RETURNING revision",
                    (current.channel, current.number, data.slug, data.seed, now, revision),
                ).fetchone()
        except sqlite3.IntegrityError as taken:
            raise SlugTaken(data.slug) from taken
        if row is None:
            latest = self.episode_slug(current.channel, current.number, current.slug).revision
            raise PartChanged(f"srstudio slug of {current.channel} #{current.number}", latest)
        return current.model_copy(
            update={**data.model_dump(), "revision": row["revision"], "updated_at": parse(now)}
        )

    def mark_exported(self, episodes: list[EpisodeSlug], sheets: list[ExportedSheet]) -> None:
        """Fix the slugs an srstudio zip carried, saving any that were still proposals.

        A proposal whose slug another item already took stays a proposal: the export's own
        checks report the clash, and fixing it here would hide it.
        """
        now = self.database.now()
        with self.database.connect() as connection:
            connection.executemany(
                "UPDATE episode_slugs SET exported_at = ?"
                " WHERE channel_slug = ? AND number = ? AND exported_at IS NULL",
                [(now, slug.channel, slug.number) for slug in episodes],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO episode_slugs (channel_slug, number, slug, seed, revision,"
                " updated_at, exported_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                [(slug.channel, slug.number, slug.slug, slug.seed, now, now) for slug in episodes],
            )
            connection.executemany(
                "UPDATE library_sheets SET exported_at = ? WHERE entry_id = ? AND exported_at IS NULL",
                [(now, str(item.entry.id)) for item in sheets],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO library_sheets (entry_id, channel_slug, kind, slug, sheet,"
                " revision, updated_at, exported_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                [exported_row(item, now) for item in sheets],
            )

    def relationships(self, channel: str) -> list[Relationship]:
        """The channel's relationships in the order they were recorded."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {RELATIONSHIP_COLUMNS} FROM library_relationships"
                " WHERE channel_slug = ? ORDER BY position",
                (channel,),
            ).fetchall()
        return [to_relationship(row) for row in rows]

    def relationship(self, channel: str, relationship_id: UUID) -> Relationship:
        """One relationship of the channel's library."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {RELATIONSHIP_COLUMNS} FROM library_relationships"
                " WHERE channel_slug = ? AND id = ?",
                (channel, str(relationship_id)),
            ).fetchone()
        if row is None:
            raise RelationshipNotFound(str(relationship_id))
        return to_relationship(row)

    def add_relationship(self, channel: str, data: RelationshipInput) -> Relationship:
        """Record how two characters relate."""
        if data.first == data.second:
            raise SameCharacter(str(data.first))
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO library_relationships (id, channel_slug, first_id, second_id, data,"
                f" revision) VALUES (?, ?, ?, ?, ?, 1) RETURNING {RELATIONSHIP_COLUMNS}",
                (str(uuid4()), channel, str(data.first), str(data.second), relationship_data(data)),
            ).fetchone()
        return to_relationship(row)

    def replace_relationship(
        self, current: Relationship, data: RelationshipInput, expected_revision: int | None
    ) -> Relationship:
        """Change a relationship; with `expected_revision`, only if still at it."""
        if data.first == data.second:
            raise SameCharacter(str(data.first))
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE library_relationships SET first_id = ?, second_id = ?, data = ?,"
                " revision = revision + 1 WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {RELATIONSHIP_COLUMNS}",
                (
                    str(data.first),
                    str(data.second),
                    relationship_data(data),
                    str(current.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            latest = self.relationship(current.channel, current.id).revision
            raise PartChanged(f"Relationship {current.id}", latest)
        return to_relationship(row)

    def remove_relationship(self, current: Relationship, expected_revision: int | None) -> None:
        """Delete a relationship; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM library_relationships WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(current.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            latest = self.relationship(current.channel, current.id).revision
            raise PartChanged(f"Relationship {current.id}", latest)
