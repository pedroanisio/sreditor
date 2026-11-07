"""A channel's library: the characters, locations, visual styles and props its episodes share.

An episode refers to a character or location by name: one who speaks in its screenplay, or a
place a scene heading names, matches the entry whose name is the same ignoring case and spacing
(rule project.library.by-name). Each episode picks one visual style (rule project.visual-style).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart

NAME_LENGTH = 120
DESCRIPTION_LENGTH = 2000

EntryName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_LENGTH)
]
EntryDescription = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_LENGTH)
]


class LibraryKind(StrEnum):
    """What a library entry is."""

    CHARACTER = "character"
    LOCATION = "location"
    STYLE = "style"
    PROP = "prop"


class EntryInput(BaseModel):
    """A character, location or style: its name (as a screenplay writes it) and what it is."""

    model_config = ConfigDict(frozen=True)

    name: EntryName
    description: EntryDescription = ""


class LibraryEntry(BaseModel):
    """A library entry of a channel, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    kind: LibraryKind
    name: str
    description: str
    revision: int
    created_at: datetime
    updated_at: datetime


class StyleChoice(BaseModel):
    """The visual style an episode picks from its channel's library; null for none."""

    model_config = ConfigDict(frozen=True)

    style_id: UUID | None = None


class EpisodeStyle(BaseModel):
    """An episode's visual style; revision 0 until someone picks one."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    style: LibraryEntry | None
    revision: int
    updated_at: datetime | None = None


class StyleNotInLibrary(ValueError):
    """The chosen style is not one of the channel's library styles."""

    def __init__(self, style_id: UUID) -> None:
        super().__init__(f"Style {style_id} is not in the channel's library")
        self.style_id = style_id


class EntryNotFound(LookupError):
    """The channel's library has no such entry."""


class EntryExists(ValueError):
    """The channel's library already has an entry of this kind with this name."""

    def __init__(self, kind: LibraryKind, name: str) -> None:
        super().__init__(f"A {kind} named {name} is already in the library")
        self.kind = kind
        self.name = name


def name_key(name: str) -> str:
    """How names are matched: ignoring case and runs of spaces, so ANA matches Ana."""
    return " ".join(name.split()).casefold()


ENTRY_COLUMNS = "id, channel_slug, kind, name, description, revision, created_at, updated_at"


def to_entry(row: sqlite3.Row) -> LibraryEntry:
    """A library row as a model."""
    return LibraryEntry(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        kind=LibraryKind(row["kind"]),
        name=row["name"],
        description=row["description"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def style_key(channel: str, number: int) -> TextKey:
    """Where an episode's chosen style is stored."""
    return TextKey(part=TextPart.STYLE, channel=channel, number=number)


class LibraryStore:
    """Library entries in the application database; deleting the channel deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.texts = EpisodeTexts(database)

    def entries(
        self, channel: str, kind: LibraryKind, after: int | None, limit: int
    ) -> list[LibraryEntry]:
        """The channel's entries of a kind in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ENTRY_COLUMNS} FROM channel_library"
                " WHERE channel_slug = ? AND kind = ? AND position > ? ORDER BY position LIMIT ?",
                (channel, kind, after or 0, limit),
            ).fetchall()
        return [to_entry(row) for row in rows]

    def position(self, entry_id: UUID) -> int:
        """Where an entry sits in its list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM channel_library WHERE id = ?", (str(entry_id),)
            ).fetchone()
        if row is None:
            raise EntryNotFound(str(entry_id))
        return int(row["position"])

    def named(self, channel: str, kind: LibraryKind, names: list[str]) -> dict[str, LibraryEntry]:
        """The entries matching these names, by their name key."""
        keys = sorted({name_key(name) for name in names})
        if not keys:
            return {}
        marks = ", ".join("?" * len(keys))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ENTRY_COLUMNS}, name_key FROM channel_library"
                f" WHERE channel_slug = ? AND kind = ? AND name_key IN ({marks})",
                (channel, kind, *keys),
            ).fetchall()
        return {row["name_key"]: to_entry(row) for row in rows}

    def get(self, channel: str, kind: LibraryKind, entry_id: UUID) -> LibraryEntry:
        """One entry of the channel's library."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {ENTRY_COLUMNS} FROM channel_library"
                " WHERE channel_slug = ? AND kind = ? AND id = ?",
                (channel, kind, str(entry_id)),
            ).fetchone()
        if row is None:
            raise EntryNotFound(str(entry_id))
        return to_entry(row)

    def create(self, channel: str, kind: LibraryKind, data: EntryInput) -> LibraryEntry:
        """Add an entry; its name must be new among the channel's entries of that kind."""
        now = self.database.now()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "INSERT INTO channel_library (id, channel_slug, kind, name, name_key,"
                    " description, revision, created_at, updated_at)"
                    f" VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {ENTRY_COLUMNS}",
                    (
                        str(uuid4()),
                        channel,
                        kind,
                        data.name,
                        name_key(data.name),
                        data.description,
                        now,
                        now,
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as taken:
            raise EntryExists(kind, data.name) from taken
        return to_entry(row)

    def replace(
        self, entry: LibraryEntry, data: EntryInput, expected_revision: int | None
    ) -> LibraryEntry:
        """Rename or describe an entry; with `expected_revision`, only if it is still at it."""
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "UPDATE channel_library SET name = ?, name_key = ?, description = ?,"
                    " revision = revision + 1, updated_at = ?"
                    " WHERE id = ? AND (? IS NULL OR revision = ?)"
                    f" RETURNING {ENTRY_COLUMNS}",
                    (
                        data.name,
                        name_key(data.name),
                        data.description,
                        self.database.now(),
                        str(entry.id),
                        expected_revision,
                        expected_revision,
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as taken:
            raise EntryExists(entry.kind, data.name) from taken
        if row is None:
            current = self.get(entry.channel, entry.kind, entry.id).revision
            raise PartChanged(f"{entry.kind.capitalize()} {entry.id}", current)
        return to_entry(row)

    def delete(self, entry: LibraryEntry, expected_revision: int | None) -> None:
        """Delete an entry; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM channel_library WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(entry.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.get(entry.channel, entry.kind, entry.id).revision
            raise PartChanged(f"{entry.kind.capitalize()} {entry.id}", current)

    def episode_style(self, channel: str, number: int) -> EpisodeStyle:
        """The style an episode picked; none when it picked none or the style was removed."""
        stored = self.texts.read(style_key(channel, number))
        style = None
        if stored.text:
            found = self.styles_among(channel, [UUID(stored.text)])
            style = found[0] if found else None
        return EpisodeStyle(
            channel=channel,
            number=number,
            style=style,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )

    def styles_among(self, channel: str, style_ids: list[UUID]) -> list[LibraryEntry]:
        """The channel's styles among these ids."""
        marks = ", ".join("?" * len(style_ids))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ENTRY_COLUMNS} FROM channel_library"
                f" WHERE channel_slug = ? AND kind = ? AND id IN ({marks})",
                (channel, LibraryKind.STYLE, *(str(item) for item in style_ids)),
            ).fetchall()
        return [to_entry(row) for row in rows]

    def choose_style(
        self, channel: str, number: int, choice: StyleChoice, expected_revision: int | None
    ) -> EpisodeStyle:
        """Pick the episode's style, or none; with `expected_revision`, only if still at it."""
        if choice.style_id is not None and not self.styles_among(channel, [choice.style_id]):
            raise StyleNotInLibrary(choice.style_id)
        text = str(choice.style_id) if choice.style_id else ""
        key = style_key(channel, number)
        if self.texts.write(key, text, expected_revision) is None:
            current = self.episode_style(channel, number).revision
            raise PartChanged(f"Visual style of {channel} #{number}", current)
        return self.episode_style(channel, number)
