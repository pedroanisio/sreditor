"""How a published post performed: dated snapshots of its numbers (rule project.metrics).

Numbers are entered by hand, through the API or MCP, or imported from a platform's retention CSV;
sreditor never fetches them. A post is an episode's post on one platform, so its snapshots are
keyed by episode, platform and day, and a second entry for the same day merges into the first.
This is unrelated to metrics.py, which serves Prometheus request metrics.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sreditor.channels import Platform
from sreditor.database import Database
from sreditor.delivery import DeliveryInput, Post
from sreditor.field_types import ChannelSlug

MAX_COUNT = 10**12
MAX_PERCENT = 1000.0  # a platform may report above 100 when viewers rewatch
MAX_SECOND = 24 * 60 * 60.0
MAX_RETENTION_POINTS = 3600

Count = Annotated[int, Field(ge=0, le=MAX_COUNT)]
Seconds = Annotated[float, Field(ge=0, le=MAX_SECOND)]


class Metric(StrEnum):
    """A number a post is compared by."""

    VIEWS = "views"
    REACH = "reach"
    LIKES = "likes"
    COMMENTS = "comments"
    SHARES = "shares"
    SAVES = "saves"
    AVG_WATCH_S = "avg_watch_s"


class RetentionPoint(BaseModel):
    """The share of viewers still watching at one second of the video."""

    model_config = ConfigDict(frozen=True)

    second: Seconds
    percent: float = Field(ge=0, le=MAX_PERCENT)


class Numbers(BaseModel):
    """A post's numbers on one day; each is optional, and zero is a real value."""

    model_config = ConfigDict(frozen=True)

    views: Count | None = None
    reach: Count | None = None
    likes: Count | None = None
    comments: Count | None = None
    shares: Count | None = None
    saves: Count | None = None
    avg_watch_s: Annotated[float, Field(ge=0, le=MAX_SECOND)] | None = None


def in_order(points: list[RetentionPoint]) -> list[RetentionPoint]:
    """Points by second; ValueError when two share a second."""
    ordered = sorted(points, key=lambda point: point.second)
    seconds = [point.second for point in ordered]
    if len(set(seconds)) != len(seconds):
        raise ValueError("Each second may appear once in the retention")
    return ordered


class SnapshotInput(Numbers):
    """What people enter for one day; a field left out keeps the day's earlier value."""

    captured_on: date | None = Field(default=None, description="The day; today when left out.")
    retention: list[RetentionPoint] | None = Field(
        default=None, max_length=MAX_RETENTION_POINTS, description="Percent still watching."
    )

    @field_validator("retention")
    @classmethod
    def has_ordered_retention(
        cls, points: list[RetentionPoint] | None
    ) -> list[RetentionPoint] | None:
        """Retention comes by second without repeats."""
        return None if points is None else in_order(points)


class Snapshot(Numbers):
    """A stored snapshot: one post's numbers on one day."""

    id: UUID
    channel: ChannelSlug
    number: int
    platform: Platform
    captured_on: date
    retention: list[RetentionPoint]
    created_at: datetime
    updated_at: datetime


class PostRef(BaseModel):
    """One post: an episode's post on a platform account."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    number: int
    platform: Platform


class PostMetric(BaseModel):
    """A post of the channel with its latest snapshot, for comparing posts."""

    model_config = ConfigDict(frozen=True)

    number: int
    episode_title: str
    platform: Platform
    cut: str
    url: str | None
    published_at: datetime | None
    captured_on: date | None = Field(description="The day of the latest snapshot, if any.")
    snapshots: int
    retention_points: int
    latest: Numbers | None = None


class ChannelPost(BaseModel):
    """A post of a channel's episode, with the episode's title."""

    model_config = ConfigDict(frozen=True)

    number: int
    title: str
    post: Post


SNAPSHOT_COLUMNS = (
    "id, channel_slug, number, platform, captured_on, views, reach, likes, comments, shares,"
    " saves, avg_watch_s, retention, created_at, updated_at"
)
NUMBER_COLUMNS = tuple(metric.value for metric in Metric)


def to_snapshot(row: sqlite3.Row) -> Snapshot:
    """A snapshot row as a model."""
    return Snapshot(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        platform=Platform(row["platform"]),
        captured_on=date.fromisoformat(row["captured_on"]),
        retention=[RetentionPoint(**point) for point in json.loads(row["retention"] or "[]")],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        **{name: row[name] for name in NUMBER_COLUMNS},
    )


def upsert_sql() -> str:
    """The statement that adds a day's snapshot or merges into it, keeping what was left out."""
    names = ", ".join(NUMBER_COLUMNS)
    marks = ", ".join("?" for _ in NUMBER_COLUMNS)
    merged = ", ".join(
        f"{name} = COALESCE(excluded.{name}, {name})" for name in (*NUMBER_COLUMNS, "retention")
    )
    return (
        "INSERT INTO post_snapshots (id, channel_slug, number, platform, captured_on,"
        f" {names}, retention, created_at, updated_at) VALUES (?, ?, ?, ?, ?, {marks}, ?, ?, ?)"
        " ON CONFLICT (channel_slug, number, platform, captured_on) DO UPDATE SET"
        f" {merged}, updated_at = excluded.updated_at RETURNING {SNAPSHOT_COLUMNS}"
    )


UPSERT = upsert_sql()


class PostMetricsStore:
    """Snapshots in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def today(self) -> date:
        """The current day by the database's clock, in UTC."""
        return self.database.clock().astimezone(UTC).date()

    def record(self, post: PostRef, day: date, data: SnapshotInput) -> Snapshot:
        """Add the day's snapshot, or merge into it: fields left out keep their value."""
        retention = (
            None
            if data.retention is None
            else json.dumps([point.model_dump() for point in data.retention])
        )
        now = self.database.now()
        values = (
            str(uuid4()),
            post.channel,
            post.number,
            post.platform,
            day.isoformat(),
            *(getattr(data, name) for name in NUMBER_COLUMNS),
            retention,
            now,
            now,
        )
        with self.database.connect() as connection:
            row = connection.execute(UPSERT, values).fetchone()
        return to_snapshot(row)

    def snapshots(self, post: PostRef, before: date | None, limit: int) -> list[Snapshot]:
        """The post's snapshots, newest day first, from the day before `before`."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {SNAPSHOT_COLUMNS} FROM post_snapshots"
                " WHERE channel_slug = ? AND number = ? AND platform = ?"
                " AND (? IS NULL OR captured_on < ?) ORDER BY captured_on DESC LIMIT ?",
                (
                    post.channel,
                    post.number,
                    post.platform,
                    before and before.isoformat(),
                    before and before.isoformat(),
                    limit,
                ),
            ).fetchall()
        return [to_snapshot(row) for row in rows]

    def latest(self, channel: str) -> dict[tuple[int, Platform], Snapshot]:
        """The newest snapshot of each post of the channel."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {SNAPSHOT_COLUMNS} FROM post_snapshots AS found"
                " WHERE channel_slug = ? AND captured_on = (SELECT MAX(captured_on)"
                " FROM post_snapshots WHERE channel_slug = found.channel_slug"
                " AND number = found.number AND platform = found.platform)",
                (channel,),
            ).fetchall()
        snapshots = [to_snapshot(row) for row in rows]
        return {(snapshot.number, snapshot.platform): snapshot for snapshot in snapshots}

    def counts(self, channel: str) -> dict[tuple[int, Platform], int]:
        """How many snapshots each post of the channel has."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT number, platform, COUNT(*) AS total FROM post_snapshots"
                " WHERE channel_slug = ? GROUP BY number, platform",
                (channel,),
            ).fetchall()
        return {(row["number"], Platform(row["platform"])): row["total"] for row in rows}

    def channel_posts(self, channel: str) -> list[ChannelPost]:
        """Every post in the channel's delivery packages, by episode number."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT delivery.number, delivery.text, episode.title"
                " FROM episode_deliveries AS delivery JOIN episodes AS episode"
                " ON episode.channel_slug = delivery.channel_slug"
                " AND episode.number = delivery.number"
                " WHERE delivery.channel_slug = ? ORDER BY delivery.number",
                (channel,),
            ).fetchall()
        return [
            ChannelPost(number=row["number"], title=row["title"], post=post)
            for row in rows
            for post in DeliveryInput.model_validate_json(row["text"]).posts
        ]
