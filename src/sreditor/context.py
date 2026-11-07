"""The context of a channel's items: what, who, where, when, why and how (rule project.context).

Episodes, pitches, sources, media files, third-party media and library entries each keep short
answers to the six questions, stored and revised on their own. Any of them may also link to
another item of the same channel under one of the questions, such as a pitch whose "who" is a
character of the library, so each item lists what it links to and where it is mentioned.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from sreditor.database import Database
from sreditor.episode_texts import UNWRITTEN, PartChanged

ANSWER_LENGTH = 2000
NOTE_LENGTH = 500
ITEM_ID_LENGTH = 64
# How many items the page's link picker offers at once.
CATALOG_LIMIT = 500

Answer = Annotated[str, StringConstraints(strip_whitespace=True, max_length=ANSWER_LENGTH)]
LinkNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=NOTE_LENGTH)]
ItemId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ITEM_ID_LENGTH)
]


class Question(StrEnum):
    """The six questions an item's context answers, and a link answers one of."""

    WHAT = "what"
    WHO = "who"
    WHERE = "where"
    WHEN = "when"
    WHY = "why"
    HOW = "how"


class ItemKind(StrEnum):
    """The kinds of channel item that keep a context and can be linked. Waivers never are."""

    EPISODE = "episode"
    PITCH = "pitch"
    PITCH_SOURCE = "pitch_source"
    SOURCE = "source"
    MEDIA = "media"
    EXTERNAL_MEDIA = "external_media"
    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"


class ItemRef(BaseModel):
    """One item of the channel: its kind and id (an episode's id is its number)."""

    model_config = ConfigDict(frozen=True)

    kind: ItemKind
    id: ItemId

    @model_validator(mode="after")
    def canonical(self) -> Self:
        """Refuse an id of the wrong shape, so 01 and 1, or upper-case UUIDs, are one item."""
        if self.kind is ItemKind.EPISODE:
            if not self.id.isdecimal() or int(self.id) < 1:
                raise ValueError("An episode's id is its number, such as 1")
            canonical = str(int(self.id))
        else:
            canonical = str(UUID(self.id))
        object.__setattr__(self, "id", canonical)
        return self


class ContextInput(BaseModel):
    """Short answers to the six questions; leave a question empty when it does not apply."""

    model_config = ConfigDict(frozen=True)

    what: Answer = ""
    who: Answer = ""
    where: Answer = ""
    when: Answer = ""
    why: Answer = ""
    how: Answer = ""


class ItemContext(ContextInput):
    """An item's answers, with the revision to send back when changing them; 0 when unwritten."""

    item: ItemRef
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class LinkedItem(ItemRef):
    """An item as a link shows it: its name and, for episode parts, the episode's number."""

    label: str
    episode: int | None = None
    parent: str | None = None  # the pitch a pitch source belongs to


class LinkInput(BaseModel):
    """A link from one item to another of the channel, answering one question about it."""

    model_config = ConfigDict(frozen=True)

    to: ItemRef
    question: Question
    note: LinkNote = ""


class Link(BaseModel):
    """A link between two items of the channel: `source`'s `question` is answered by `target`."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    source: LinkedItem
    target: LinkedItem
    question: Question
    note: str
    created_at: datetime


class LinkDirection(StrEnum):
    """Links an item makes, or links other items make to it (where it is mentioned)."""

    OUT = "out"
    IN = "in"


class ItemNotFound(LookupError):
    """The channel has no item of that kind with that id."""

    def __init__(self, item: ItemRef) -> None:
        super().__init__(f"{item.kind} {item.id}")
        self.item = item


class TargetNotFound(ItemNotFound):
    """The item a new link points to is not in the channel."""


class LinkNotFound(LookupError):
    """The channel has no link with that id."""


class LinkExists(ValueError):
    """The same link, with the same question, is already there."""


class SelfLink(ValueError):
    """An item cannot link to itself."""


# How to find each kind of item and name it; fixed here, never built from input.
ITEM_QUERIES: dict[ItemKind, str] = {
    ItemKind.EPISODE: "SELECT CAST(number AS TEXT) AS id, title AS label, number AS episode,"
    " NULL AS parent, 0 AS position FROM episodes WHERE channel_slug = :channel",
    ItemKind.PITCH: "SELECT id, title AS label, NULL AS episode, NULL AS parent, position"
    " FROM pitches WHERE channel_slug = :channel",
    ItemKind.PITCH_SOURCE: "SELECT s.id, s.title AS label, NULL AS episode, s.pitch_id AS parent,"
    " s.position FROM pitch_sources s JOIN pitches p ON p.id = s.pitch_id"
    " WHERE p.channel_slug = :channel",
    ItemKind.SOURCE: "SELECT id, title AS label, number AS episode, NULL AS parent, position"
    " FROM episode_sources WHERE channel_slug = :channel",
    ItemKind.MEDIA: "SELECT id, name AS label, number AS episode, NULL AS parent, position"
    " FROM episode_media WHERE channel_slug = :channel",
    ItemKind.EXTERNAL_MEDIA: "SELECT id, title AS label, NULL AS episode, NULL AS parent, position"
    " FROM external_media WHERE channel_slug = :channel",
    **{
        kind: "SELECT id, name AS label, NULL AS episode, NULL AS parent, position"
        f" FROM channel_library WHERE channel_slug = :channel AND kind = '{kind.value}'"
        for kind in (ItemKind.CHARACTER, ItemKind.LOCATION, ItemKind.PROP, ItemKind.STYLE)
    },
}
# Each query keeps its own id column name, so one filter fits all of them.
ITEM_BY_ID = "SELECT * FROM ({query}) WHERE id = :id"
# Every kind in one list, in the order ItemKind names them.
CATALOG = " UNION ALL ".join(
    f"SELECT '{kind.value}' AS kind, {rank} AS rank, * FROM ({query})"
    for rank, (kind, query) in enumerate(ITEM_QUERIES.items())
)

LINK_COLUMNS = (
    "id, position, source_kind, source_id, target_kind, target_id, question, note, created_at"
)


def to_item(kind: ItemKind, row: sqlite3.Row) -> LinkedItem:
    """A found item as a link shows it."""
    return LinkedItem(
        kind=kind, id=row["id"], label=row["label"], episode=row["episode"], parent=row["parent"]
    )


class ContextStore:
    """Items' contexts and the links between them; deleting an item deletes both (triggers)."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def find(self, channel: str, item: ItemRef) -> LinkedItem:
        """The channel's item, named; ItemNotFound when there is none."""
        with self.database.connect() as connection:
            row = connection.execute(
                ITEM_BY_ID.format(query=ITEM_QUERIES[item.kind]),
                {"channel": channel, "id": item.id},
            ).fetchone()
        if row is None:
            raise ItemNotFound(item)
        return to_item(item.kind, row)

    def catalog(
        self, channel: str, kind: ItemKind | None, after: int, limit: int
    ) -> list[LinkedItem]:
        """The channel's linkable items, kind by kind and oldest first, skipping `after` of them."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ({CATALOG}) WHERE :kind IS NULL OR kind = :kind"
                " ORDER BY rank, position, episode LIMIT :limit OFFSET :after",
                {"channel": channel, "kind": kind, "limit": limit, "after": after},
            ).fetchall()
        return [to_item(ItemKind(row["kind"]), row) for row in rows]

    def context(self, channel: str, item: ItemRef) -> ItemContext:
        """The item's answers, empty at revision 0 when nobody has written them."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT answers, revision, updated_at FROM item_contexts"
                " WHERE channel_slug = ? AND item_kind = ? AND item_id = ?",
                (channel, item.kind, item.id),
            ).fetchone()
        if row is None:
            return ItemContext(item=item)
        return ItemContext(
            item=item,
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            **json.loads(row["answers"]),
        )

    def write_context(
        self, channel: str, item: ItemRef, data: ContextInput, expected_revision: int | None
    ) -> ItemContext:
        """Replace the answers; with `expected_revision`, only if they are still at it."""
        answers = data.model_dump_json()
        now = self.database.now()
        key = (channel, item.kind.value, item.id)
        if expected_revision is None:
            sql = (
                "INSERT INTO item_contexts (channel_slug, item_kind, item_id, answers, revision,"
                " updated_at) VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT (channel_slug, item_kind,"
                " item_id) DO UPDATE SET answers = excluded.answers, revision = revision + 1,"
                " updated_at = excluded.updated_at RETURNING revision"
            )
            parameters: tuple[object, ...] = (*key, answers, now)
        elif expected_revision == UNWRITTEN:
            sql = (
                "INSERT OR IGNORE INTO item_contexts (channel_slug, item_kind, item_id, answers,"
                " revision, updated_at) VALUES (?, ?, ?, ?, 1, ?) RETURNING revision"
            )
            parameters = (*key, answers, now)
        else:
            sql = (
                "UPDATE item_contexts SET answers = ?, revision = revision + 1, updated_at = ?"
                " WHERE channel_slug = ? AND item_kind = ? AND item_id = ? AND revision = ?"
                " RETURNING revision"
            )
            parameters = (answers, now, *key, expected_revision)
        with self.database.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            current = self.context(channel, item).revision
            raise PartChanged(f"Context of {item.kind} {item.id}", current)
        return self.context(channel, item)

    def add_link(self, channel: str, source: LinkedItem, data: LinkInput) -> Link:
        """Link `source` to `data.to` under a question; the caller has checked both exist."""
        if (source.kind, source.id) == (data.to.kind, data.to.id):
            raise SelfLink(f"{source.kind} {source.id} cannot link to itself")
        try:
            target = self.find(channel, data.to)
        except ItemNotFound as missing:
            raise TargetNotFound(missing.item) from missing
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "INSERT INTO item_links (id, channel_slug, source_kind, source_id,"
                    " target_kind, target_id, question, note, created_at)"
                    f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {LINK_COLUMNS}",
                    (
                        str(uuid4()),
                        channel,
                        source.kind,
                        source.id,
                        target.kind,
                        target.id,
                        data.question,
                        data.note,
                        self.database.now(),
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as taken:
            raise LinkExists(
                f"{source.kind} {source.id} already links to {target.kind} {target.id}"
                f" as {data.question}"
            ) from taken
        return self.to_link(channel, row)

    def links(
        self, channel: str, item: ItemRef, direction: LinkDirection, after: int | None, limit: int
    ) -> list[tuple[int, Link]]:
        """The item's links (or mentions), oldest first after a position, with positions."""
        side = "source" if direction is LinkDirection.OUT else "target"
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {LINK_COLUMNS} FROM item_links WHERE channel_slug = ?"
                f" AND {side}_kind = ? AND {side}_id = ? AND position > ?"
                " ORDER BY position LIMIT ?",
                (channel, item.kind, item.id, after or 0, limit),
            ).fetchall()
        return [(row["position"], self.to_link(channel, row)) for row in rows]

    def link(self, channel: str, link_id: UUID) -> Link:
        """One link of the channel; LinkNotFound when there is none."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {LINK_COLUMNS} FROM item_links WHERE channel_slug = ? AND id = ?",
                (channel, str(link_id)),
            ).fetchone()
        if row is None:
            raise LinkNotFound(str(link_id))
        return self.to_link(channel, row)

    def remove_link(self, channel: str, link_id: UUID) -> None:
        """Remove a link; LinkNotFound when the channel has none with that id."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM item_links WHERE channel_slug = ? AND id = ?",
                (channel, str(link_id)),
            )
        if not cursor.rowcount:
            raise LinkNotFound(str(link_id))

    def to_link(self, channel: str, row: sqlite3.Row) -> Link:
        """A stored link, with both ends named as they are now."""
        return Link(
            id=UUID(row["id"]),
            source=self.find(channel, stored_ref(row["source_kind"], row["source_id"])),
            target=self.find(channel, stored_ref(row["target_kind"], row["target_id"])),
            question=Question(row["question"]),
            note=row["note"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


def stored_ref(kind: str, item_id: str) -> ItemRef:
    """A reference read back from the database, already in canonical form."""
    return ItemRef(kind=ItemKind(kind), id=item_id)
