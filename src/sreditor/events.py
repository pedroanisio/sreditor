"""The change feed: what changed in a channel and when, never what was written (rule agentic.events).

Every gated write and every render transition records one event. An event names the operation,
the episode or item it concerns and the actor, and nothing people wrote, so an agent that reacts
to "script revised" reads the script with the tool the event names. Events are kept for a
retention period and pruned as new ones arrive.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sreditor.database import Database, parse, stamp
from sreditor.field_types import ChannelSlug
from sreditor.pagination import encode_cursor

RENDER_STATUS = "render.status"
# Events of this area describe signed releases, which hold personal data (rule project.waivers).
WAIVER_AREA = "waiver"
EVENT_COLUMNS = (
    "id, channel_slug, occurred_at, operation, episode_number, item_id, status, actor_type,"
    " actor_id"
)

# The tool that reads what each kind of operation changed, by the part of the operation name
# before its dot; these are fixed strings, a test checks that each tool is registered.
READ_WITH: dict[str, str] = {
    "episode": "get_episode_overview",
    "episode_slug": "get_episode_overview",
    "episode_style": "get_episode_style",
    "episode_pitch": "get_episode_overview",
    "research": "get_research",
    "source": "get_research",
    "screenplay": "get_screenplay",
    "short_script": "get_short_script",
    "voice": "get_voice_over",
    "pitch": "get_pitch",
    "pitch_research": "get_pitch",
    "pitch_source": "get_pitch",
    "claim": "list_claims",
    "media": "list_media",
    "render": "get_render",
    "delivery": "get_delivery",
    "post_metrics": "list_post_snapshots",
    "external_media": "list_external_media",
    "external_media_copy": "list_external_media",
    "external_media_use": "get_episode_credits",
    "classification": "get_episode_classification",
    "channel_profile": "get_channel_profile",
    "waiver": "list_waivers",
    "member": "list_members",
    "invitation": "list_members",
    "channel": "get_channel",
    "editorial": "get_editorial",
    "_sheet": "get_library_sheet",
    "relationship": "list_relationships",
    "item_context": "get_context",
    "item_link": "get_context",
}


class ActorType(StrEnum):
    """Who made a change."""

    USER = "user"
    TOKEN = "token"
    WORKER = "worker"


class EventActor(BaseModel):
    """The user, API token or render worker behind a change, by id (rule agentic.audit)."""

    model_config = ConfigDict(frozen=True)

    type: ActorType
    id: str


class NewEvent(BaseModel):
    """What to record: which operation changed what in which channel, and who did it."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    operation: str
    episode_number: int | None = None
    item_id: str | None = None
    status: str | None = None
    actor: EventActor | None = None


class Event(BaseModel):
    """One entry of a channel's change feed; it holds no text written by people."""

    model_config = ConfigDict(frozen=True)

    cursor: str
    occurred_at: datetime
    channel: ChannelSlug
    operation: str
    episode_number: int | None
    item_id: str | None
    status: str | None
    actor: EventActor | None
    read_with: str | None


class EventPage(BaseModel):
    """Events oldest first, and where to continue; next_cursor is always set, even when empty."""

    model_config = ConfigDict(frozen=True)

    items: list[Event]
    next_cursor: str
    has_more: bool


class EventQuery(BaseModel):
    """Which events to read: after an event id (None: the latest), at most `limit`, with or
    without those about waivers."""

    model_config = ConfigDict(frozen=True)

    after: int | None
    limit: int
    has_waivers: bool


def area_of(operation: str) -> str:
    """The part of an operation name before its dot, such as waiver for waiver.create."""
    return operation.split(".", 1)[0]


def read_tool_for(operation: str) -> str | None:
    """The tool that reads what the operation changed, when there is one."""
    return READ_WITH.get(area_of(operation))


def to_event(row: sqlite3.Row) -> Event:
    """Build an event from its stored row."""
    actor = None
    if row["actor_type"] is not None:
        actor = EventActor(type=ActorType(row["actor_type"]), id=row["actor_id"])
    occurred_at = parse(row["occurred_at"])
    assert occurred_at is not None
    return Event(
        cursor=encode_cursor(str(row["id"])),
        occurred_at=occurred_at,
        channel=row["channel_slug"],
        operation=row["operation"],
        episode_number=row["episode_number"],
        item_id=row["item_id"],
        status=row["status"],
        actor=actor,
        read_with=read_tool_for(row["operation"]),
    )


def record_in(connection: sqlite3.Connection, new: NewEvent, occurred_at: str) -> None:
    """Record an event within the caller's transaction.

    Nothing is recorded when the channel is gone, as after deleting it, whose events went too.
    """
    actor = new.actor
    connection.execute(
        "INSERT INTO events (channel_slug, occurred_at, operation, episode_number, item_id,"
        " status, actor_type, actor_id) SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS"
        " (SELECT 1 FROM channels WHERE slug = ?)",
        (
            new.channel,
            occurred_at,
            new.operation,
            new.episode_number,
            new.item_id,
            new.status,
            actor.type if actor else None,
            actor.id if actor else None,
            new.channel,
        ),
    )


class CursorExpired(Exception):
    """The cursor is older than events retention removed, so the caller missed some."""

    def __init__(self, channel: str, retention: timedelta) -> None:
        super().__init__(f"Events of {channel} older than {retention.days} days were removed")
        self.retention = retention


class EventStore:
    """The change feed in the application database."""

    def __init__(self, database: Database, retention: timedelta) -> None:
        self.database = database
        self.retention = retention

    def record(self, new: NewEvent) -> None:
        """Record an event, and drop the ones past retention while at it."""
        self.prune()
        with self.database.connect() as connection:
            record_in(connection, new, self.database.now())

    def prune(self) -> None:
        """Delete events older than the retention period, remembering the newest one removed."""
        cutoff = stamp(self.database.clock() - self.retention)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO event_horizons (channel_slug, pruned_through)"
                " SELECT channel_slug, MAX(id) FROM events WHERE occurred_at < ?"
                " GROUP BY channel_slug ON CONFLICT (channel_slug)"
                " DO UPDATE SET pruned_through = excluded.pruned_through",
                (cutoff,),
            )
            connection.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff,))

    def horizon(self, channel: str) -> int:
        """The newest event id of the channel that retention removed, or 0."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT pruned_through FROM event_horizons WHERE channel_slug = ?", (channel,)
            ).fetchone()
        return int(row["pruned_through"]) if row else 0

    def page(self, channel: str, query: EventQuery) -> EventPage:
        """Events after the id, oldest first; without one, the latest ones.

        Raises CursorExpired when events after the id were removed by retention.
        """
        self.prune()
        horizon = self.horizon(channel)
        after = query.after
        if after is not None and after < horizon:
            raise CursorExpired(channel, self.retention)
        rows = self.fetch(channel, after, query)
        shown = rows[: query.limit]
        resume = shown[-1]["id"] if shown else (after if after is not None else horizon)
        return EventPage(
            items=[to_event(row) for row in shown],
            next_cursor=encode_cursor(str(resume)),
            has_more=len(rows) > query.limit,
        )

    def fetch(self, channel: str, after: int | None, query: EventQuery) -> list[sqlite3.Row]:
        """Up to limit + 1 rows: the oldest after the id, or the latest ones when there is none."""
        is_latest = after is None
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {EVENT_COLUMNS} FROM events WHERE channel_slug = ? AND id > ?"
                " AND (? OR operation NOT LIKE ?)"
                f" ORDER BY id {'DESC' if is_latest else 'ASC'} LIMIT ?",
                (
                    channel,
                    after or 0,
                    query.has_waivers,
                    f"{WAIVER_AREA}.%",
                    query.limit if is_latest else query.limit + 1,
                ),
            ).fetchall()
        return rows[::-1] if is_latest else rows
