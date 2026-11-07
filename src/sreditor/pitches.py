"""A channel's pitches (pautas): story ideas with their own research (rule project.pitches).

A pitch, its notes and each of its sources are stored and revised on their own, so editing the
notes never conflicts with an agent adding a source. An episode refers to at most one pitch,
in a row of its own, so choosing a pitch never moves the episode's revision.
"""

from __future__ import annotations

import builtins
import sqlite3
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, HttpUrl, StringConstraints

from sreditor.database import Database
from sreditor.episode_texts import UNWRITTEN, PartChanged
from sreditor.research import Notes, SourceInput, SourceNotFound

TITLE_LENGTH = 200
SUMMARY_LENGTH = 2000

PitchTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_LENGTH)
]
PitchSummary = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SUMMARY_LENGTH)]


class PitchStatus(StrEnum):
    """Where a pitch stands: a rough idea, being researched, approved, or set aside."""

    IDEA = "idea"
    RESEARCH = "research"
    APPROVED = "approved"
    DISCARDED = "discarded"


class PitchInput(BaseModel):
    """A pitch's title, summary and status, as written by a person or an agent."""

    model_config = ConfigDict(frozen=True)

    title: PitchTitle
    summary: PitchSummary = ""
    status: PitchStatus = PitchStatus.IDEA


class Pitch(BaseModel):
    """A pitch of a channel, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    title: str
    summary: str
    status: PitchStatus
    revision: int
    created_at: datetime
    updated_at: datetime


class PitchNotesInput(BaseModel):
    """The whole text of a pitch's research notes."""

    model_config = ConfigDict(frozen=True)

    text: Notes


class PitchNotes(BaseModel):
    """A pitch's research notes; revision 0 until someone writes them."""

    model_config = ConfigDict(frozen=True)

    pitch_id: UUID
    text: str = ""
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class PitchSource(BaseModel):
    """A source of a pitch, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    pitch_id: UUID
    url: HttpUrl
    title: str
    claim: str
    accessed_on: date
    revision: int
    created_at: datetime
    updated_at: datetime


class PitchChoice(BaseModel):
    """The pitch an episode refers to, or none."""

    model_config = ConfigDict(frozen=True)

    pitch_id: UUID | None = None


class EpisodePitch(BaseModel):
    """The pitch an episode refers to; revision 0 until someone chooses one."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    pitch_id: UUID | None = None
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class PitchNotFound(LookupError):
    """The channel has no pitch with that id."""


class PitchInUse(ValueError):
    """Episodes still refer to the pitch, so it cannot be deleted."""

    def __init__(self, pitch_id: UUID, episodes: list[int]) -> None:
        super().__init__(str(pitch_id))
        self.episodes = episodes


PITCH_COLUMNS = "id, channel_slug, title, summary, status, revision, created_at, updated_at"
SOURCE_COLUMNS = "id, pitch_id, url, title, claim, accessed_on, revision, created_at, updated_at"


def to_pitch(row: sqlite3.Row) -> Pitch:
    """A pitch row as a model."""
    return Pitch(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        title=row["title"],
        summary=row["summary"],
        status=PitchStatus(row["status"]),
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def to_source(row: sqlite3.Row) -> PitchSource:
    """A pitch source row as a model."""
    return PitchSource(
        id=UUID(row["id"]),
        pitch_id=UUID(row["pitch_id"]),
        url=row["url"],
        title=row["title"],
        claim=row["claim"],
        accessed_on=date.fromisoformat(row["accessed_on"]),
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def pitch_label(pitch_id: UUID) -> str:
    """How errors name a pitch."""
    return f"Pitch {pitch_id}"


class PositionTable(StrEnum):
    """The tables whose rows are paged by position; never taken from input."""

    PITCHES = "pitches"
    SOURCES = "pitch_sources"


NOT_FOUND: dict[PositionTable, type[LookupError]] = {
    PositionTable.PITCHES: PitchNotFound,
    PositionTable.SOURCES: SourceNotFound,
}


class PitchStore:
    """Pitches, their notes and sources; deleting the channel deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self, channel: str, after: int | None, limit: int, status: PitchStatus | None = None
    ) -> builtins.list[Pitch]:
        """The channel's pitches in the order they were added, optionally of one status."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {PITCH_COLUMNS} FROM pitches WHERE channel_slug = ? AND position > ?"
                " AND (? IS NULL OR status = ?) ORDER BY position LIMIT ?",
                (channel, after or 0, status, status, limit),
            ).fetchall()
        return [to_pitch(row) for row in rows]

    def position(self, table: PositionTable, item_id: UUID) -> int:
        """Where a pitch or source sits in its list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT position FROM {table} WHERE id = ?", (str(item_id),)
            ).fetchone()
        if row is None:
            raise NOT_FOUND[table](str(item_id))
        return int(row["position"])

    def get(self, channel: str, pitch_id: UUID) -> Pitch:
        """One pitch of the channel."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {PITCH_COLUMNS} FROM pitches WHERE channel_slug = ? AND id = ?",
                (channel, str(pitch_id)),
            ).fetchone()
        if row is None:
            raise PitchNotFound(str(pitch_id))
        return to_pitch(row)

    def create(self, channel: str, data: PitchInput) -> Pitch:
        """Add a pitch to the channel."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO pitches (id, channel_slug, title, summary, status, revision,"
                f" created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) RETURNING {PITCH_COLUMNS}",
                (str(uuid4()), channel, data.title, data.summary, data.status, now, now),
            ).fetchone()
        return to_pitch(row)

    def replace(self, pitch: Pitch, data: PitchInput, expected_revision: int | None) -> Pitch:
        """Replace title, summary and status; with `expected_revision`, only if still at it."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE pitches SET title = ?, summary = ?, status = ?, revision = revision + 1,"
                " updated_at = ? WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {PITCH_COLUMNS}",
                (
                    data.title,
                    data.summary,
                    data.status,
                    self.database.now(),
                    str(pitch.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            raise PartChanged(pitch_label(pitch.id), self.get(pitch.channel, pitch.id).revision)
        return to_pitch(row)

    def delete(self, pitch: Pitch, expected_revision: int | None) -> None:
        """Delete a pitch with its notes and sources, unless an episode still refers to it."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pitches WHERE id = ? AND (? IS NULL OR revision = ?)"
                " AND NOT EXISTS (SELECT 1 FROM episode_pitches WHERE pitch_id = pitches.id)",
                (str(pitch.id), expected_revision, expected_revision),
            )
        if cursor.rowcount:
            return
        episodes = self.episodes_of(pitch.id)
        if episodes:
            raise PitchInUse(pitch.id, episodes)
        raise PartChanged(pitch_label(pitch.id), self.get(pitch.channel, pitch.id).revision)

    def notes(self, pitch_id: UUID) -> PitchNotes:
        """The pitch's notes, empty at revision 0 when nobody has written them."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT text, revision, updated_at FROM pitch_notes WHERE pitch_id = ?",
                (str(pitch_id),),
            ).fetchone()
        if row is None:
            return PitchNotes(pitch_id=pitch_id)
        return PitchNotes(
            pitch_id=pitch_id,
            text=row["text"],
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def write_notes(
        self, pitch_id: UUID, data: PitchNotesInput, expected_revision: int | None
    ) -> PitchNotes:
        """Replace the notes; with `expected_revision`, only if they are still at it."""
        values = (str(pitch_id), data.text, self.database.now())
        if expected_revision is None:
            sql = (
                "INSERT INTO pitch_notes (pitch_id, text, revision, updated_at)"
                " VALUES (?, ?, 1, ?) ON CONFLICT (pitch_id) DO UPDATE SET text = excluded.text,"
                " revision = revision + 1, updated_at = excluded.updated_at RETURNING revision"
            )
            parameters: tuple[object, ...] = values
        elif expected_revision == UNWRITTEN:
            sql = (
                "INSERT OR IGNORE INTO pitch_notes (pitch_id, text, revision, updated_at)"
                " VALUES (?, ?, 1, ?) RETURNING revision"
            )
            parameters = values
        else:
            sql = (
                "UPDATE pitch_notes SET text = ?, revision = revision + 1, updated_at = ?"
                " WHERE pitch_id = ? AND revision = ? RETURNING revision"
            )
            parameters = (data.text, values[2], values[0], expected_revision)
        with self.database.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise PartChanged(f"Notes of {pitch_label(pitch_id)}", self.notes(pitch_id).revision)
        return self.notes(pitch_id)

    def sources(self, pitch_id: UUID, after: int | None, limit: int) -> builtins.list[PitchSource]:
        """The pitch's sources in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {SOURCE_COLUMNS} FROM pitch_sources WHERE pitch_id = ? AND position > ?"
                " ORDER BY position LIMIT ?",
                (str(pitch_id), after or 0, limit),
            ).fetchall()
        return [to_source(row) for row in rows]

    def source(self, pitch_id: UUID, source_id: UUID) -> PitchSource:
        """One source of the pitch."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {SOURCE_COLUMNS} FROM pitch_sources WHERE pitch_id = ? AND id = ?",
                (str(pitch_id), str(source_id)),
            ).fetchone()
        if row is None:
            raise SourceNotFound(str(source_id))
        return to_source(row)

    def add_source(self, pitch_id: UUID, data: SourceInput) -> PitchSource:
        """Add a source; it was accessed today unless the input says otherwise."""
        now = self.database.now()
        accessed = data.accessed_on or self.database.clock().date()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO pitch_sources (id, pitch_id, url, title, claim, accessed_on,"
                " revision, created_at, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {SOURCE_COLUMNS}",
                (
                    str(uuid4()),
                    str(pitch_id),
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
        self, source: PitchSource, data: SourceInput, expected_revision: int | None
    ) -> PitchSource:
        """Replace a source's fields; with `expected_revision`, only if it is still at it."""
        accessed = data.accessed_on or source.accessed_on
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE pitch_sources SET url = ?, title = ?, claim = ?, accessed_on = ?,"
                " revision = revision + 1, updated_at = ?"
                f" WHERE id = ? AND (? IS NULL OR revision = ?) RETURNING {SOURCE_COLUMNS}",
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
            current = self.source(source.pitch_id, source.id).revision
            raise PartChanged(f"Source {source.id}", current)
        return to_source(row)

    def delete_source(self, source: PitchSource, expected_revision: int | None) -> None:
        """Delete a source; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pitch_sources WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(source.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.source(source.pitch_id, source.id).revision
            raise PartChanged(f"Source {source.id}", current)

    def episode_pitch(self, channel: str, number: int) -> EpisodePitch:
        """The pitch an episode refers to, at revision 0 when none was ever chosen."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT pitch_id, revision, updated_at FROM episode_pitches"
                " WHERE channel_slug = ? AND number = ?",
                (channel, number),
            ).fetchone()
        if row is None:
            return EpisodePitch(channel=channel, number=number)
        return EpisodePitch(
            channel=channel,
            number=number,
            pitch_id=UUID(row["pitch_id"]) if row["pitch_id"] else None,
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def choose(
        self, channel: str, number: int, choice: PitchChoice, expected_revision: int | None
    ) -> EpisodePitch:
        """Point the episode at a pitch, or none; with `expected_revision`, only if still at it."""
        chosen = str(choice.pitch_id) if choice.pitch_id else None
        values = (channel, number, chosen, self.database.now())
        if expected_revision is None:
            sql = (
                "INSERT INTO episode_pitches (channel_slug, number, pitch_id, revision,"
                " updated_at) VALUES (?, ?, ?, 1, ?) ON CONFLICT (channel_slug, number)"
                " DO UPDATE SET pitch_id = excluded.pitch_id, revision = revision + 1,"
                " updated_at = excluded.updated_at RETURNING revision"
            )
            parameters: tuple[object, ...] = values
        elif expected_revision == UNWRITTEN:
            sql = (
                "INSERT OR IGNORE INTO episode_pitches (channel_slug, number, pitch_id,"
                " revision, updated_at) VALUES (?, ?, ?, 1, ?) RETURNING revision"
            )
            parameters = values
        else:
            sql = (
                "UPDATE episode_pitches SET pitch_id = ?, revision = revision + 1,"
                " updated_at = ? WHERE channel_slug = ? AND number = ? AND revision = ?"
                " RETURNING revision"
            )
            parameters = (chosen, values[3], channel, number, expected_revision)
        with self.database.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            current = self.episode_pitch(channel, number).revision
            raise PartChanged(f"Pitch of {channel} #{number}", current)
        return self.episode_pitch(channel, number)

    def episodes_of(self, pitch_id: UUID) -> builtins.list[int]:
        """The numbers of the episodes that refer to the pitch."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT number FROM episode_pitches WHERE pitch_id = ? ORDER BY number",
                (str(pitch_id),),
            ).fetchall()
        return [int(row["number"]) for row in rows]
