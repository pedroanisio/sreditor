"""An episode's research: free notes and the sources behind its claims (project.episodes).

Notes and each source are stored and revised on their own, so a person editing the notes never
conflicts with an agent adding a source.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, HttpUrl, StringConstraints

from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.field_types import ChannelSlug

NOTES_LENGTH = 20000
SOURCE_TITLE_LENGTH = 200
CLAIM_LENGTH = 1000

Notes = Annotated[str, StringConstraints(max_length=NOTES_LENGTH)]
SourceTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SOURCE_TITLE_LENGTH)
]
Claim = Annotated[str, StringConstraints(strip_whitespace=True, max_length=CLAIM_LENGTH)]


class NotesInput(BaseModel):
    """The whole text of an episode's research notes."""

    model_config = ConfigDict(frozen=True)

    text: Notes


class ResearchNotes(BaseModel):
    """An episode's research notes; revision 0 until someone writes them."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    number: int
    text: str
    revision: int
    updated_at: datetime | None = None


class SourceInput(BaseModel):
    """A source to cite: where it is, what it is, and which claim of the episode it supports."""

    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    title: SourceTitle
    claim: Claim = ""
    accessed_on: date | None = None  # today when left out


class Source(BaseModel):
    """A source of an episode, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: ChannelSlug
    number: int
    url: HttpUrl
    title: str
    claim: str
    accessed_on: date
    revision: int
    created_at: datetime
    updated_at: datetime


class SourceNotFound(LookupError):
    """The episode has no source with that id."""


SOURCE_COLUMNS = (
    "id, channel_slug, number, url, title, claim, accessed_on, revision, created_at, updated_at"
)


def to_source(row: sqlite3.Row) -> Source:
    """A source row as a model."""
    return Source(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        url=row["url"],
        title=row["title"],
        claim=row["claim"],
        accessed_on=date.fromisoformat(row["accessed_on"]),
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def notes_key(channel: str, number: int) -> TextKey:
    """Where an episode's notes are stored."""
    return TextKey(part=TextPart.RESEARCH, channel=channel, number=number)


def notes_label(channel: str, number: int) -> str:
    """How errors name an episode's notes."""
    return f"Research notes of {channel} #{number}"


class ResearchStore:
    """Notes and sources in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.texts = EpisodeTexts(database)

    def notes(self, channel: str, number: int) -> ResearchNotes:
        """The episode's notes, empty at revision 0 when nobody has written them."""
        stored = self.texts.read(notes_key(channel, number))
        return ResearchNotes(channel=channel, number=number, **stored.model_dump())

    def write_notes(
        self, channel: str, number: int, data: NotesInput, expected_revision: int | None
    ) -> ResearchNotes:
        """Replace the notes; with `expected_revision`, only if they are still at it."""
        key = notes_key(channel, number)
        if self.texts.write(key, data.text, expected_revision) is None:
            current = self.notes(channel, number).revision
            raise PartChanged(notes_label(channel, number), current)
        return self.notes(channel, number)

    def sources(self, channel: str, number: int, after: int | None, limit: int) -> list[Source]:
        """The episode's sources in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {SOURCE_COLUMNS} FROM episode_sources"
                " WHERE channel_slug = ? AND number = ? AND position > ?"
                " ORDER BY position LIMIT ?",
                (channel, number, after or 0, limit),
            ).fetchall()
        return [to_source(row) for row in rows]

    def position(self, source_id: UUID) -> int:
        """Where a source sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
        if row is None:
            raise SourceNotFound(str(source_id))
        return int(row["position"])

    def source(self, channel: str, number: int, source_id: UUID) -> Source:
        """One source of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {SOURCE_COLUMNS} FROM episode_sources"
                " WHERE channel_slug = ? AND number = ? AND id = ?",
                (channel, number, str(source_id)),
            ).fetchone()
        if row is None:
            raise SourceNotFound(str(source_id))
        return to_source(row)

    def add_source(self, channel: str, number: int, data: SourceInput) -> Source:
        """Add a source; it was accessed today unless the input says otherwise."""
        now = self.database.now()
        accessed = data.accessed_on or self.database.clock().date()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO episode_sources (id, channel_slug, number, url, title, claim,"
                " accessed_on, revision, created_at, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {SOURCE_COLUMNS}",
                (
                    str(uuid4()),
                    channel,
                    number,
                    str(data.url),
                    data.title,
                    data.claim,
                    accessed.isoformat(),
                    now,
                    now,
                ),
            ).fetchone()
        return to_source(row)

    def replace_source(
        self, source: Source, data: SourceInput, expected_revision: int | None
    ) -> Source:
        """Replace a source's fields; with `expected_revision`, only if it is still at it."""
        accessed = data.accessed_on or source.accessed_on
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE episode_sources SET url = ?, title = ?, claim = ?, accessed_on = ?,"
                " revision = revision + 1, updated_at = ?"
                " WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {SOURCE_COLUMNS}",
                (
                    str(data.url),
                    data.title,
                    data.claim,
                    accessed.isoformat(),
                    self.database.now(),
                    str(source.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            current = self.source(source.channel, source.number, source.id).revision
            raise PartChanged(f"Source {source.id}", current)
        return to_source(row)

    def delete_source(self, source: Source, expected_revision: int | None) -> None:
        """Delete a source; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episode_sources WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(source.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.source(source.channel, source.number, source.id).revision
            raise PartChanged(f"Source {source.id}", current)
