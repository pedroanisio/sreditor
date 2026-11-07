"""An episode's claims: each figure or fact it states, tied to the sources that back it (rule
project.claims).

A claim keeps its quote, where it appears and its backing sources. A backing source points at a
source of the episode or of one of its pitches by id, so the title and link shown are always the
source's own, never a copy. Each backing source is primary or secondary and checked or not, with
who checked it and when. A claim without a checked primary source only warns; nothing here ever
blocks a status.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    computed_field,
    field_validator,
)

from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.store import Owner

QUOTE_LENGTH = 1000
PLACE_LENGTH = 300
MAX_BACKING_SOURCES = 20
FIRST_POSITION = 1

ClaimQuote = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=QUOTE_LENGTH)
]
PlaceReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PLACE_LENGTH)
]


class PlaceKind(StrEnum):
    """Where a claim appears: a screenplay scene, a short-script block, or somewhere else."""

    SCENE = "scene"
    BLOCK = "block"
    TEXT = "text"


class SourceKind(StrEnum):
    """Whose source backs a claim: the episode's own or one of its pitches'."""

    EPISODE = "episode"
    PITCH = "pitch"


class ClaimPlace(BaseModel):
    """Where a claim appears. `ref` is the scene heading for a scene, the block's number in the
    short script (counting from 1) for a block, and free text for anything else."""

    model_config = ConfigDict(frozen=True)

    kind: PlaceKind
    ref: PlaceReference


class Backing(BaseModel):
    """A source offered as backing for a claim, and whether it is the primary one."""

    model_config = ConfigDict(frozen=True)

    source_id: UUID
    is_primary: bool = False


class ClaimInput(BaseModel):
    """A claim as written by a person or an agent: quote, place and backing sources."""

    model_config = ConfigDict(frozen=True)

    quote: ClaimQuote
    place: ClaimPlace
    sources: list[Backing] = Field(default_factory=list, max_length=MAX_BACKING_SOURCES)

    @field_validator("sources")
    @classmethod
    def each_source_once(cls, sources: list[Backing]) -> list[Backing]:
        """A source backs a claim once; whether it is primary is one flag, not two entries."""
        if len({backing.source_id for backing in sources}) != len(sources):
            raise ValueError("Each source may back a claim only once")
        return sources


class CheckInput(BaseModel):
    """Whether a backing source was checked: true to record it, false to take it back."""

    model_config = ConfigDict(frozen=True)

    is_checked: bool = True


class SourceMark(BaseModel):
    """One backing source of a claim, and whether it was checked."""

    model_config = ConfigDict(frozen=True)

    source_id: UUID
    is_checked: bool = True


class BackingSource(BaseModel):
    """A backing source with its title and link as they are now, and what was checked."""

    model_config = ConfigDict(frozen=True)

    source_id: UUID
    source_kind: SourceKind
    pitch_id: UUID | None = None  # the pitch a pitch source belongs to
    title: str
    url: HttpUrl
    is_primary: bool
    is_checked: bool
    checked_by: Owner | None = None  # a user or an API token (rule agentic.audit)
    checked_at: datetime | None = None


class Claim(BaseModel):
    """A claim of an episode, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    number: int
    quote: str
    place: ClaimPlace
    sources: list[BackingSource]
    revision: int
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_checked_primary(self) -> bool:
        """Whether a primary source backs the claim and was checked; otherwise it warns."""
        return any(source.is_primary and source.is_checked for source in self.sources)


class UncheckedClaim(BaseModel):
    """A claim that still has no checked primary source."""

    model_config = ConfigDict(frozen=True)

    claim_id: UUID
    quote: str
    has_primary: bool  # false: no source is marked primary yet; true: it is, but not checked


class ClaimWarnings(BaseModel):
    """The claims of an episode that have no checked primary source; a warning, never a block."""

    model_config = ConfigDict(frozen=True)

    count: int
    claims: list[UncheckedClaim]
    message: str | None = None


class CheckRecord(BaseModel):
    """A check to record, with who made it; the store stamps the time."""

    model_config = ConfigDict(frozen=True)

    is_checked: bool
    checked_by: Owner | None = None


class ClaimNotFound(LookupError):
    """The episode has no claim with that id."""


class BackingNotFound(LookupError):
    """The claim has no backing source with that id."""


class SourceNotAvailable(LookupError):
    """The source is neither the episode's nor one of its pitches', so it cannot back a claim."""


CLAIM_COLUMNS = (
    "id, channel_slug, number, quote, place_kind, place_ref, revision, created_at, updated_at"
)
# A backing source counts only while its source exists in the episode or in a pitch the episode
# still uses, so dropping a pitch hides the claims' backing from it and re-adding it brings it back.
BACKING_QUERY = """
SELECT cs.claim_id, cs.source_id, cs.is_primary, cs.is_checked, cs.checked_by_type,
       cs.checked_by_id, cs.checked_at,
       COALESCE(es.title, ps.title) AS title, COALESCE(es.url, ps.url) AS url,
       ps.pitch_id AS pitch_id
FROM claim_sources cs
JOIN episode_claims c ON c.id = cs.claim_id
LEFT JOIN episode_sources es ON es.id = cs.source_id
    AND es.channel_slug = c.channel_slug AND es.number = c.number
LEFT JOIN pitch_sources ps ON ps.id = cs.source_id AND EXISTS (
    SELECT 1 FROM episode_pitches ep WHERE ep.pitch_id = ps.pitch_id
        AND ep.channel_slug = c.channel_slug AND ep.number = c.number)
WHERE cs.claim_id IN ({marks}) AND (es.id IS NOT NULL OR ps.id IS NOT NULL)
ORDER BY cs.position
"""


def to_backing(row: sqlite3.Row) -> BackingSource:
    """A backing source row as a model."""
    is_pitch = row["pitch_id"] is not None
    checked_by = (
        Owner(type=row["checked_by_type"], id=row["checked_by_id"])
        if row["checked_by_type"]
        else None
    )
    return BackingSource(
        source_id=UUID(row["source_id"]),
        source_kind=SourceKind.PITCH if is_pitch else SourceKind.EPISODE,
        pitch_id=UUID(row["pitch_id"]) if is_pitch else None,
        title=row["title"],
        url=row["url"],
        is_primary=bool(row["is_primary"]),
        is_checked=bool(row["is_checked"]),
        checked_by=checked_by,
        checked_at=datetime.fromisoformat(row["checked_at"]) if row["checked_at"] else None,
    )


def to_claim(row: sqlite3.Row, sources: list[BackingSource]) -> Claim:
    """A claim row and its backing sources as a model."""
    return Claim(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        quote=row["quote"],
        place=ClaimPlace(kind=PlaceKind(row["place_kind"]), ref=row["place_ref"]),
        sources=sources,
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def unchecked_of(claims: list[Claim]) -> ClaimWarnings:
    """The claims with no checked primary source, with the warning that says so."""
    found = [
        UncheckedClaim(
            claim_id=claim.id,
            quote=claim.quote,
            has_primary=any(source.is_primary for source in claim.sources),
        )
        for claim in claims
        if not claim.has_checked_primary
    ]
    return ClaimWarnings(count=len(found), claims=found, message=warning_text(len(found)))


def warning_text(count: int) -> str | None:
    """The warning for this many unchecked claims; None when there are none."""
    if count == 0:
        return None
    noun = "claim has" if count == 1 else "claims have"
    return f"{count} {noun} no checked primary source; see the episode's claims"


class ClaimStore:
    """Claims and their backing sources; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def with_sources(self, connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Claim]:
        """The claim rows as models, each with its backing sources, read in one query."""
        if not rows:
            return []
        marks = ", ".join("?" for _ in rows)
        backing = connection.execute(
            BACKING_QUERY.format(marks=marks), [row["id"] for row in rows]
        ).fetchall()
        by_claim: dict[str, list[BackingSource]] = {row["id"]: [] for row in rows}
        for found in backing:
            by_claim[found["claim_id"]].append(to_backing(found))
        return [to_claim(row, by_claim[row["id"]]) for row in rows]

    def page(self, channel: str, number: int, after: int | None, limit: int) -> list[Claim]:
        """The episode's claims in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {CLAIM_COLUMNS} FROM episode_claims"
                " WHERE channel_slug = ? AND number = ? AND position > ?"
                " ORDER BY position LIMIT ?",
                (channel, number, after or 0, limit),
            ).fetchall()
            return self.with_sources(connection, rows)

    def every(self, channel: str, number: int) -> list[Claim]:
        """All of the episode's claims, oldest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {CLAIM_COLUMNS} FROM episode_claims"
                " WHERE channel_slug = ? AND number = ? ORDER BY position",
                (channel, number),
            ).fetchall()
            return self.with_sources(connection, rows)

    def count(self, channel: str, number: int) -> int:
        """How many claims the episode has."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM episode_claims WHERE channel_slug = ? AND number = ?",
                (channel, number),
            ).fetchone()
        return int(row["total"])

    def unchecked(self, channel: str, number: int) -> ClaimWarnings:
        """The claims that have no checked primary source."""
        return unchecked_of(self.every(channel, number))

    def position(self, claim_id: UUID) -> int:
        """Where a claim sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_claims WHERE id = ?", (str(claim_id),)
            ).fetchone()
        if row is None:
            raise ClaimNotFound(str(claim_id))
        return int(row["position"])

    def number_of(self, channel: str, claim_id: UUID) -> int:
        """The number of the episode of this channel that holds the claim."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT number FROM episode_claims WHERE channel_slug = ? AND id = ?",
                (channel, str(claim_id)),
            ).fetchone()
        if row is None:
            raise ClaimNotFound(str(claim_id))
        return int(row["number"])

    def get(self, channel: str, number: int, claim_id: UUID) -> Claim:
        """One claim of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {CLAIM_COLUMNS} FROM episode_claims"
                " WHERE channel_slug = ? AND number = ? AND id = ?",
                (channel, number, str(claim_id)),
            ).fetchone()
            if row is None:
                raise ClaimNotFound(str(claim_id))
            return self.with_sources(connection, [row])[0]

    def add(self, channel: str, number: int, data: ClaimInput) -> Claim:
        """Add a claim with its backing sources, none checked."""
        now = self.database.now()
        claim_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO episode_claims (id, channel_slug, number, quote, place_kind,"
                " place_ref, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (claim_id, channel, number, data.quote, data.place.kind, data.place.ref, now, now),
            )
            self.write_backing(connection, claim_id, data.sources)
        return self.get(channel, number, UUID(claim_id))

    def write_backing(
        self, connection: sqlite3.Connection, claim_id: str, sources: list[Backing]
    ) -> None:
        """Make the claim's backing sources these ones; sources kept stay as checked as they were."""
        marks = ", ".join("?" for _ in sources)
        connection.execute(
            f"DELETE FROM claim_sources WHERE claim_id = ? AND source_id NOT IN ({marks})",
            [claim_id, *(str(backing.source_id) for backing in sources)],
        )
        connection.executemany(
            "INSERT INTO claim_sources (claim_id, source_id, position, is_primary)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (claim_id, source_id)"
            " DO UPDATE SET position = excluded.position, is_primary = excluded.is_primary",
            [
                (claim_id, str(backing.source_id), position, backing.is_primary)
                for position, backing in enumerate(sources, FIRST_POSITION)
            ],
        )

    def bump(
        self, connection: sqlite3.Connection, claim: Claim, expected_revision: int | None
    ) -> None:
        """Move the claim's revision on, or raise PartChanged when it is not at `expected_revision`."""
        row = connection.execute(
            "UPDATE episode_claims SET revision = revision + 1, updated_at = ?"
            " WHERE id = ? AND (? IS NULL OR revision = ?) RETURNING id",
            (self.database.now(), str(claim.id), expected_revision, expected_revision),
        ).fetchone()
        if row is None:
            current = self.get(claim.channel, claim.number, claim.id).revision
            raise PartChanged(f"Claim {claim.id}", current)

    def replace(self, claim: Claim, data: ClaimInput, expected_revision: int | None) -> Claim:
        """Replace quote, place and backing sources; with `expected_revision`, only if still at it."""
        with self.database.connect() as connection:
            self.bump(connection, claim, expected_revision)
            connection.execute(
                "UPDATE episode_claims SET quote = ?, place_kind = ?, place_ref = ? WHERE id = ?",
                (data.quote, data.place.kind, data.place.ref, str(claim.id)),
            )
            self.write_backing(connection, str(claim.id), data.sources)
        return self.get(claim.channel, claim.number, claim.id)

    def check(
        self, claim: Claim, source_id: UUID, record: CheckRecord, expected_revision: int | None
    ) -> Claim:
        """Record, or take back, the check of one backing source; the claim's revision moves on."""
        by = record.checked_by
        checked_at = self.database.now() if record.is_checked else None
        with self.database.connect() as connection:
            self.bump(connection, claim, expected_revision)
            cursor = connection.execute(
                "UPDATE claim_sources SET is_checked = ?, checked_by_type = ?, checked_by_id = ?,"
                " checked_at = ? WHERE claim_id = ? AND source_id = ?",
                (
                    record.is_checked,
                    by.type if by else None,
                    by.id if by else None,
                    checked_at,
                    str(claim.id),
                    str(source_id),
                ),
            )
            if cursor.rowcount == 0:
                raise BackingNotFound(str(source_id))
        return self.get(claim.channel, claim.number, claim.id)

    def delete(self, claim: Claim, expected_revision: int | None) -> None:
        """Delete a claim with its backing sources; with `expected_revision`, only if still at it."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episode_claims WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(claim.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.get(claim.channel, claim.number, claim.id).revision
            raise PartChanged(f"Claim {claim.id}", current)
