"""Episodes: a channel's numbered productions, each moving from idea to published (project.episodes).

This is the episode's skeleton. Its parts (research, screenplay, voice-over, sound, visual) come
in later steps and are stored on their own, so editing one never conflicts with another.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

from sreditor.database import Database
from sreditor.delivery import DeliveryWarning

TITLE_LENGTH = 200
SUMMARY_LENGTH = 2000
FIRST_NUMBER = 1

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_LENGTH)
]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SUMMARY_LENGTH)]


class EpisodeStatus(StrEnum):
    """Where an episode is in production, in the order it usually moves."""

    IDEA = "idea"
    RESEARCH = "research"
    SCRIPT = "script"
    PRODUCTION = "production"
    PUBLISHED = "published"


class EpisodeInput(BaseModel):
    """What a person or an agent sets on an episode."""

    model_config = ConfigDict(frozen=True)

    title: Title
    summary: Summary = ""
    status: EpisodeStatus = EpisodeStatus.IDEA


class Episode(BaseModel):
    """An episode of a channel, numbered from 1 in the order it was created."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: PositiveInt
    title: str
    summary: str
    status: EpisodeStatus
    created_at: datetime
    updated_at: datetime


class WrittenEpisode(Episode):
    """An episode just written; once published, it names what its delivery and claims lack."""

    warnings: list[DeliveryWarning] = Field(default_factory=list)
    claims_warning: str | None = None


class RevisedEpisode(BaseModel):
    """An episode with the revision its ETag is built from.

    Only a write that moves the episode to published fills `warnings`, with the gaps of its
    delivery package (rule project.delivery); they never block the change.
    """

    model_config = ConfigDict(frozen=True)

    episode: Episode
    revision: int
    warnings: list[DeliveryWarning] = Field(default_factory=list)
    # Set when a write moves the episode to published and claims still lack a checked primary
    # source: a heads-up that never blocks the change (rule project.claims).
    claims_warning: str | None = None


class EpisodeNotFound(LookupError):
    """The channel has no episode with that number."""

    def __init__(self, channel: str, number: int) -> None:
        super().__init__(f"{channel} #{number}")
        self.channel = channel
        self.number = number


class EpisodeChanged(ValueError):
    """The episode moved on from the revision the caller expected."""

    def __init__(self, channel: str, number: int, current: int) -> None:
        super().__init__(f"{channel} #{number}")
        self.current = current


COLUMNS = "channel_slug, number, title, summary, status, revision, created_at, updated_at"


def to_revised(row: sqlite3.Row) -> RevisedEpisode:
    """An episode row as a model with its revision."""
    episode = Episode(
        channel=row["channel_slug"],
        number=row["number"],
        title=row["title"],
        summary=row["summary"],
        status=EpisodeStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
    return RevisedEpisode(episode=episode, revision=int(row["revision"]))


class PartCounts(BaseModel):
    """How many of the episode's listed parts it has, for the counts on its tabs."""

    model_config = ConfigDict(frozen=True)

    sources: int = 0
    media: int = 0
    waivers: int = 0
    uses: int = 0


# One count per listed part, in PartCounts' order; each table keys rows by channel and number.
PART_COUNT_QUERY = (
    "SELECT"
    " (SELECT COUNT(*) FROM episode_sources WHERE channel_slug = :channel AND number = :number)"
    " AS sources,"
    " (SELECT COUNT(*) FROM episode_media WHERE channel_slug = :channel AND number = :number)"
    " AS media,"
    " (SELECT COUNT(*) FROM episode_waivers WHERE channel_slug = :channel AND number = :number)"
    " AS waivers,"
    " (SELECT COUNT(*) FROM episode_media_uses WHERE channel_slug = :channel AND number = :number)"
    " AS uses"
)


class EpisodeStore:
    """Episodes in the application database; the channel's row owns them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self, channel: str, after: int | None, limit: int) -> list[Episode]:
        """The channel's episodes by number, after a number, up to `limit`."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {COLUMNS} FROM episodes WHERE channel_slug = ? AND number > ?"
                " ORDER BY number LIMIT ?",
                (channel, after or 0, limit),
            ).fetchall()
        return [to_revised(row).episode for row in rows]

    def count(self, channel: str) -> int:
        """How many episodes the channel has."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM episodes WHERE channel_slug = ?", (channel,)
            ).fetchone()
        return int(row["total"])

    def part_counts(self, channel: str, number: int) -> PartCounts:
        """How many sources, media files, waivers and third-party uses the episode has."""
        with self.database.connect() as connection:
            row = connection.execute(
                PART_COUNT_QUERY, {"channel": channel, "number": number}
            ).fetchone()
        return PartCounts(
            sources=row["sources"], media=row["media"], waivers=row["waivers"], uses=row["uses"]
        )

    def get(self, channel: str, number: int) -> RevisedEpisode:
        """One episode with its revision."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {COLUMNS} FROM episodes WHERE channel_slug = ? AND number = ?",
                (channel, number),
            ).fetchone()
        if row is None:
            raise EpisodeNotFound(channel, number)
        return to_revised(row)

    def create(self, channel: str, data: EpisodeInput) -> RevisedEpisode:
        """Add an episode with the channel's next number.

        Numbers come from a counter rather than the highest episode, so a deleted episode's
        number is never given again and an agent's old ETag can never match a new episode.
        """
        now = self.database.now()
        with self.database.connect() as connection:
            counter = connection.execute(
                "INSERT INTO episode_counters (channel_slug, last_number) VALUES (?, ?)"
                " ON CONFLICT (channel_slug) DO UPDATE SET last_number = last_number + 1"
                " RETURNING last_number",
                (channel, FIRST_NUMBER),
            ).fetchone()
            row = connection.execute(
                "INSERT INTO episodes (channel_slug, number, title, summary, status, revision,"
                f" created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) RETURNING {COLUMNS}",
                (channel, counter["last_number"], data.title, data.summary, data.status, now, now),
            ).fetchone()
        return to_revised(row)

    def replace(
        self, channel: str, number: int, data: EpisodeInput, expected_revision: int | None
    ) -> RevisedEpisode:
        """Replace what is set on an episode; with `expected_revision`, only if still there."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE episodes SET title = ?, summary = ?, status = ?, updated_at = ?,"
                " revision = revision + 1"
                " WHERE channel_slug = ? AND number = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {COLUMNS}",
                (
                    data.title,
                    data.summary,
                    data.status,
                    self.database.now(),
                    channel,
                    number,
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            raise EpisodeChanged(channel, number, self.get(channel, number).revision)
        return to_revised(row)

    def delete(self, channel: str, number: int, expected_revision: int | None) -> None:
        """Delete an episode; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episodes WHERE channel_slug = ? AND number = ?"
                " AND (? IS NULL OR revision = ?)",
                (channel, number, expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            raise EpisodeChanged(channel, number, self.get(channel, number).revision)
