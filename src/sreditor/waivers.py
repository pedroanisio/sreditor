"""An episode's waivers: the signed releases behind it (rule project.waivers).

A waiver is a scanned or signed file (PDF, JPEG or PNG) with who signed it, when and what it
covers. It holds personal data, so the operations let only owners and editors near it; the file
is stored by hash like any other asset and never changes, while its details have a revision.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Annotated, BinaryIO
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreditor.assets import JPEG, PDF, PNG, AssetStore, StoredFile, names_of
from sreditor.database import Database
from sreditor.episode_texts import PartChanged

SIGNER_LENGTH = 120
COVERS_LENGTH = 2000
WAIVER_FORMATS = (PDF, JPEG, PNG)
WAIVER_EXTENSIONS = {item.media_type: item.extension for item in WAIVER_FORMATS}
WAIVER_ACCEPT = ",".join(item.media_type for item in WAIVER_FORMATS)
WAIVER_FORMAT_NAMES = names_of(WAIVER_FORMATS)

Signer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SIGNER_LENGTH)
]
Covers = Annotated[str, StringConstraints(strip_whitespace=True, max_length=COVERS_LENGTH)]


class WaiverInput(BaseModel):
    """What a waiver says: who signed it, when, and what it lets the channel use."""

    model_config = ConfigDict(frozen=True)

    signer: Signer
    covers: Covers = ""
    signed_on: date | None = None


class Waiver(BaseModel):
    """A waiver of an episode, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    number: int
    signer: str
    covers: str
    signed_on: date | None
    media_type: str
    size_bytes: int
    sha256: str
    revision: int
    created_at: datetime
    updated_at: datetime

    def stored(self) -> StoredFile:
        """Where the signed file sits in the assets directory."""
        path = f"{self.channel}/{self.sha256}{WAIVER_EXTENSIONS[self.media_type]}"
        return StoredFile(
            path=path, media_type=self.media_type, sha256=self.sha256, size_bytes=self.size_bytes
        )

    def filename(self) -> str:
        """The name a download is saved under: the signer and the file's extension."""
        return self.signer + WAIVER_EXTENSIONS[self.media_type]


class NewWaiver(BaseModel):
    """A waiver to add: which episode and what it says."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    data: WaiverInput


class WaiverNotFound(LookupError):
    """The episode has no such waiver."""


WAIVER_COLUMNS = (
    "id, channel_slug, number, signer, covers, signed_on, media_type, size_bytes, sha256,"
    " revision, created_at, updated_at"
)


def to_waiver(row: sqlite3.Row) -> Waiver:
    """A waiver row as a model."""
    signed_on = row["signed_on"]
    return Waiver(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        signer=row["signer"],
        covers=row["covers"],
        signed_on=date.fromisoformat(signed_on) if signed_on else None,
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def waiver_label(waiver: Waiver) -> str:
    """How a waiver is named in a conflict message."""
    return f"Waiver {waiver.id} of {waiver.channel} #{waiver.number}"


def signed_text(data: WaiverInput) -> str | None:
    """The signing date as stored."""
    return data.signed_on.isoformat() if data.signed_on else None


class WaiverStore:
    """Waiver rows in the application database and their files in the assets directory."""

    def __init__(self, database: Database, assets: AssetStore, max_bytes: int) -> None:
        self.database = database
        self.assets = assets
        self.max_bytes = max_bytes

    def waivers(self, channel: str, number: int, after: int | None, limit: int) -> list[Waiver]:
        """The episode's waivers in the order they were added, after a position."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {WAIVER_COLUMNS} FROM episode_waivers"
                " WHERE channel_slug = ? AND number = ? AND position > ?"
                " ORDER BY position LIMIT ?",
                (channel, number, after or 0, limit),
            ).fetchall()
        return [to_waiver(row) for row in rows]

    def position(self, waiver_id: UUID) -> int:
        """Where a waiver sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_waivers WHERE id = ?", (str(waiver_id),)
            ).fetchone()
        if row is None:
            raise WaiverNotFound(str(waiver_id))
        return int(row["position"])

    def get(self, channel: str, number: int, waiver_id: UUID) -> Waiver:
        """One waiver of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {WAIVER_COLUMNS} FROM episode_waivers"
                " WHERE channel_slug = ? AND number = ? AND id = ?",
                (channel, number, str(waiver_id)),
            ).fetchone()
        if row is None:
            raise WaiverNotFound(str(waiver_id))
        return to_waiver(row)

    def add(self, new: NewWaiver, source: BinaryIO) -> Waiver:
        """Check and store the signed file, then add the waiver; AssetRejected when unfit."""
        stored = self.assets.save_stream(new.channel, WAIVER_FORMATS, source, self.max_bytes)
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO episode_waivers (id, channel_slug, number, signer, covers, signed_on,"
                " media_type, size_bytes, sha256, revision, created_at, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {WAIVER_COLUMNS}",
                (
                    str(uuid4()),
                    new.channel,
                    new.number,
                    new.data.signer,
                    new.data.covers,
                    signed_text(new.data),
                    stored.media_type,
                    stored.size_bytes,
                    stored.sha256,
                    now,
                    now,
                ),
            ).fetchone()
        return to_waiver(row)

    def replace(self, waiver: Waiver, data: WaiverInput, expected_revision: int | None) -> Waiver:
        """Change who signed, what it covers or when; with `expected_revision`, only if at it."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE episode_waivers SET signer = ?, covers = ?, signed_on = ?,"
                " revision = revision + 1, updated_at = ?"
                " WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {WAIVER_COLUMNS}",
                (
                    data.signer,
                    data.covers,
                    signed_text(data),
                    self.database.now(),
                    str(waiver.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            current = self.get(waiver.channel, waiver.number, waiver.id).revision
            raise PartChanged(waiver_label(waiver), current)
        return to_waiver(row)

    def delete(self, waiver: Waiver, expected_revision: int | None) -> None:
        """Take a waiver out of the episode; the stored file stays, as others may share it."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episode_waivers WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(waiver.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.get(waiver.channel, waiver.number, waiver.id).revision
            raise PartChanged(waiver_label(waiver), current)
