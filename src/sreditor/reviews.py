"""An episode's reviews: who looked at what, which parts, and the verdict (rule project.reviews).

A review is an append-only record: it is added and may be deleted, never edited, so the history
of an episode's approvals stays as it was said. Each review is its own row, naming the channel in
every foreign key, so the database refuses a review of another episode's file. The episode's
latest verdict is the one of the review with the latest `reviewed_at`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from sreditor.database import Database, stamp
from sreditor.delivery import in_utc
from sreditor.field_types import ChannelSlug, Sha256
from sreditor.i18n import N_

NOTES_LENGTH = 10000
MAX_SEEN_RANGES = 50
MAX_SECOND = 24 * 60 * 60.0
FIRST_POSITION = 1

ReviewNotes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=NOTES_LENGTH)]
Second = Annotated[float, Field(ge=0, le=MAX_SECOND)]


class Verdict(StrEnum):
    """What the reviewer decided."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class SeenRange(BaseModel):
    """A stretch of the reviewed file the reviewer watched, in seconds from its start."""

    model_config = ConfigDict(frozen=True)

    start_s: Second
    end_s: Second

    @model_validator(mode="after")
    def ends_after_it_starts(self) -> Self:
        """A range runs forward."""
        if self.end_s <= self.start_s:
            raise ValueError(N_("A range must end after it starts"))
        return self


class ReviewInput(BaseModel):
    """What a reviewer says; who they are and, unless given, when come from the call."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    notes: ReviewNotes = Field(default="", description="What the reviewer found, in Markdown.")
    media_id: UUID | None = Field(
        default=None,
        description="The id of the file reviewed, a media file of this episode (list_media), "
        "usually a render or a cut's final video.",
    )
    seen: list[SeenRange] = Field(
        default_factory=list,
        max_length=MAX_SEEN_RANGES,
        description="The stretches of that file the reviewer watched, as start_s and end_s in "
        "seconds; a file's duration, when measured, bounds them.",
    )
    reviewed_at: Annotated[datetime, AfterValidator(in_utc)] | None = Field(
        default=None, description="When the review was made, not in the future; now when left out."
    )

    @field_validator("seen")
    @classmethod
    def sees_a_file(cls, seen: list[SeenRange], info: ValidationInfo) -> list[SeenRange]:
        """Ranges are stretches of a file, so they need one."""
        if seen and info.data.get("media_id") is None:
            raise ValueError(N_("Time ranges need the file they belong to"))
        return seen


class Review(BaseModel):
    """A stored review."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: ChannelSlug
    number: int
    reviewer_id: UUID = Field(description="The user who reviewed; for an agent, the token's user.")
    token_id: UUID | None = Field(
        default=None, description="The API token the review was written through, if any."
    )
    reviewed_at: datetime
    verdict: Verdict
    notes: str
    media_id: UUID | None = Field(
        default=None, description="The file reviewed; empty when none was named or it was removed."
    )
    media_sha256: Sha256 | None = Field(
        default=None, description="The SHA-256 of the file reviewed."
    )
    seen: list[SeenRange]


class ReviewAuthor(BaseModel):
    """Who wrote a review: the user, and the token an agent acted through."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    token_id: UUID | None = None


class NewReview(BaseModel):
    """A review ready to store: the episode, the author, the time and what was said."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    number: int
    author: ReviewAuthor
    reviewed_at: datetime
    data: ReviewInput


class ReviewNotFound(LookupError):
    """The episode has no review with that id."""


class ReviewSummary(BaseModel):
    """What an episode shows of its reviews: the latest verdict and how many there are."""

    model_config = ConfigDict(frozen=True)

    count: int = 0
    latest: Review | None = None


def warning_text(latest: Review | None) -> str | None:
    """The warning for publishing under this latest review; None when it approves."""
    if latest is None:
        return "The episode has no review; add one with add_review"
    if latest.verdict is Verdict.APPROVED:
        return None
    verdict = latest.verdict.value
    return f"The latest review of the episode is {verdict}, not approved; see its reviews"


REVIEW_QUERY = """
SELECT r.position, r.id, r.channel_slug, r.number, r.reviewer_id, r.token_id, r.reviewed_at,
       r.verdict, r.notes, r.media_id, m.sha256 AS media_sha256
FROM episode_reviews AS r
LEFT JOIN episode_media AS m
    ON m.channel_slug = r.channel_slug AND m.number = r.number AND m.id = r.media_id
"""


def to_review(row: sqlite3.Row, seen: list[SeenRange]) -> Review:
    """A review row, with the ranges read for it, as a model."""
    return Review(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        reviewer_id=UUID(row["reviewer_id"]),
        token_id=UUID(row["token_id"]) if row["token_id"] else None,
        reviewed_at=datetime.fromisoformat(row["reviewed_at"]),
        verdict=Verdict(row["verdict"]),
        notes=row["notes"],
        media_id=UUID(row["media_id"]) if row["media_id"] else None,
        media_sha256=row["media_sha256"],
        seen=seen,
    )


class ReviewStore:
    """Reviews and their ranges in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def moment(self) -> datetime:
        """The current time from the database's clock."""
        return self.database.clock()

    def with_ranges(self, connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Review]:
        """The review rows as models, each with its ranges, read in one query."""
        if not rows:
            return []
        marks = ", ".join("?" for _ in rows)
        found = connection.execute(
            "SELECT review_id, start_s, end_s FROM review_ranges"
            f" WHERE review_id IN ({marks}) ORDER BY review_id, position",
            [row["id"] for row in rows],
        ).fetchall()
        by_review: dict[str, list[SeenRange]] = {row["id"]: [] for row in rows}
        for item in found:
            by_review[item["review_id"]].append(
                SeenRange(start_s=item["start_s"], end_s=item["end_s"])
            )
        return [to_review(row, by_review[row["id"]]) for row in rows]

    def page(self, channel: str, number: int, after: int | None, limit: int) -> list[Review]:
        """The episode's reviews in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"{REVIEW_QUERY} WHERE r.channel_slug = ? AND r.number = ? AND r.position > ?"
                " ORDER BY r.position LIMIT ?",
                (channel, number, after or 0, limit),
            ).fetchall()
            return self.with_ranges(connection, rows)

    def position(self, review_id: UUID) -> int:
        """Where a review sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_reviews WHERE id = ?", (str(review_id),)
            ).fetchone()
        if row is None:
            raise ReviewNotFound(str(review_id))
        return int(row["position"])

    def get(self, channel: str, number: int, review_id: UUID) -> Review:
        """One review of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"{REVIEW_QUERY} WHERE r.channel_slug = ? AND r.number = ? AND r.id = ?",
                (channel, number, str(review_id)),
            ).fetchone()
            if row is None:
                raise ReviewNotFound(str(review_id))
            return self.with_ranges(connection, [row])[0]

    def summary(self, channel: str, number: int) -> ReviewSummary:
        """How many reviews the episode has and the latest one, by when it was made."""
        with self.database.connect() as connection:
            latest = connection.execute(
                f"{REVIEW_QUERY} WHERE r.channel_slug = ? AND r.number = ?"
                " ORDER BY r.reviewed_at DESC, r.position DESC LIMIT 1",
                (channel, number),
            ).fetchone()
            total = connection.execute(
                "SELECT COUNT(*) AS total FROM episode_reviews"
                " WHERE channel_slug = ? AND number = ?",
                (channel, number),
            ).fetchone()
            found = self.with_ranges(connection, [latest] if latest else [])
        return ReviewSummary(count=int(total["total"]), latest=found[0] if found else None)

    def add(self, new: NewReview) -> Review:
        """Append a review with its ranges."""
        review_id = str(uuid4())
        data = new.data
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO episode_reviews (id, channel_slug, number, reviewer_id, token_id,"
                " reviewed_at, verdict, notes, media_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    new.channel,
                    new.number,
                    str(new.author.user_id),
                    str(new.author.token_id) if new.author.token_id else None,
                    stamp(new.reviewed_at),
                    data.verdict,
                    data.notes,
                    str(data.media_id) if data.media_id else None,
                ),
            )
            connection.executemany(
                "INSERT INTO review_ranges (review_id, position, start_s, end_s)"
                " VALUES (?, ?, ?, ?)",
                [
                    (review_id, position, seen.start_s, seen.end_s)
                    for position, seen in enumerate(data.seen, FIRST_POSITION)
                ],
            )
        return self.get(new.channel, new.number, UUID(review_id))

    def delete(self, review: Review) -> None:
        """Remove a review and its ranges."""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM episode_reviews WHERE id = ?", (str(review.id),))
