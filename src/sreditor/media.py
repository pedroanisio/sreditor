"""An episode's media: voice-over, sound effects, score and visual effects (rule project.media).

Each file is checked by its content and stored by hash in the assets directory; the episode
keeps a row per file with its kind, a name people chose and a note. Renaming or removing one
file never touches another, so each has its own revision.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import StrEnum
from pathlib import PurePath
from typing import Annotated, BinaryIO
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreditor.assets import M4A, MP3, MP4, WAV, WEBM, AssetStore, FileFormat, StoredFile, names_of
from sreditor.database import Database
from sreditor.episode_texts import PartChanged

NAME_LENGTH = 120
NOTE_LENGTH = 2000
AUDIO_FORMATS = (MP3, WAV, M4A)
VIDEO_FORMATS = (MP4, WEBM)

MediaName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_LENGTH)
]
MediaNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=NOTE_LENGTH)]


class MediaKind(StrEnum):
    """What a media file is for in the episode."""

    VOICE_OVER = "voice_over"
    SOUND_EFFECT = "sound_effect"
    SCORE = "score"
    VISUAL_EFFECT = "visual_effect"
    RENDER = "render"  # a finished video from a render worker, never uploaded by hand


KIND_FORMATS: dict[MediaKind, tuple[FileFormat, ...]] = {
    MediaKind.VOICE_OVER: AUDIO_FORMATS,
    MediaKind.SOUND_EFFECT: AUDIO_FORMATS,
    MediaKind.SCORE: AUDIO_FORMATS,
    MediaKind.VISUAL_EFFECT: VIDEO_FORMATS,
    MediaKind.RENDER: VIDEO_FORMATS,
}
# Kinds a person or agent may upload; a render arrives only from a worker (rule project.render).
UPLOADABLE_KINDS = tuple(kind for kind in MediaKind if kind is not MediaKind.RENDER)
EXTENSIONS = {item.media_type: item.extension for item in AUDIO_FORMATS + VIDEO_FORMATS}


def accept_of(kind: MediaKind) -> str:
    """The file picker's accept= value for a kind."""
    return ",".join(item.media_type for item in KIND_FORMATS[kind])


def default_name(filename: str | None, kind: MediaKind) -> str:
    """A name for a file nobody named: its file name without the extension, else its kind."""
    plain = (filename or "").strip()
    return (PurePath(plain).stem.strip() or plain or kind.value)[:NAME_LENGTH]


def formats_of(kind: MediaKind) -> str:
    """The extensions a kind takes, for a person to read."""
    return names_of(KIND_FORMATS[kind])


class MediaInput(BaseModel):
    """What people say about a media file: a name and an optional note."""

    model_config = ConfigDict(frozen=True)

    name: MediaName
    note: MediaNote = ""


class MediaFile(BaseModel):
    """A media file of an episode, with the revision to send back when changing it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    number: int
    kind: MediaKind
    name: str
    note: str
    media_type: str
    size_bytes: int
    sha256: str
    revision: int
    created_at: datetime
    updated_at: datetime

    def stored(self) -> StoredFile:
        """Where the file sits in the assets directory."""
        path = f"{self.channel}/{self.sha256}{EXTENSIONS[self.media_type]}"
        return StoredFile(
            path=path, media_type=self.media_type, sha256=self.sha256, size_bytes=self.size_bytes
        )


class NewMedia(BaseModel):
    """A file to add: which episode, what it is for and what people call it."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    kind: MediaKind
    data: MediaInput


class MediaNotFound(LookupError):
    """The episode has no such media file."""


MEDIA_COLUMNS = (
    "id, channel_slug, number, kind, name, note, media_type, size_bytes, sha256, revision,"
    " created_at, updated_at"
)


def to_media(row: sqlite3.Row) -> MediaFile:
    """A media row as a model."""
    return MediaFile(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        kind=MediaKind(row["kind"]),
        name=row["name"],
        note=row["note"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def media_label(media: MediaFile) -> str:
    """How a file is named in a conflict message."""
    return f"Media {media.id} of {media.channel} #{media.number}"


class MediaStore:
    """Media rows in the application database and their files in the assets directory."""

    def __init__(self, database: Database, assets: AssetStore, max_bytes: int) -> None:
        self.database = database
        self.assets = assets
        self.max_bytes = max_bytes

    def files(
        self, channel: str, number: int, kind: MediaKind | None, after: int | None, limit: int
    ) -> list[MediaFile]:
        """The episode's files, of one kind or all, in the order they were added."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {MEDIA_COLUMNS} FROM episode_media"
                " WHERE channel_slug = ? AND number = ? AND (? IS NULL OR kind = ?)"
                " AND position > ? ORDER BY position LIMIT ?",
                (channel, number, kind, kind, after or 0, limit),
            ).fetchall()
        return [to_media(row) for row in rows]

    def position(self, media_id: UUID) -> int:
        """Where a file sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_media WHERE id = ?", (str(media_id),)
            ).fetchone()
        if row is None:
            raise MediaNotFound(str(media_id))
        return int(row["position"])

    def get(self, channel: str, number: int, media_id: UUID) -> MediaFile:
        """One media file of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {MEDIA_COLUMNS} FROM episode_media"
                " WHERE channel_slug = ? AND number = ? AND id = ?",
                (channel, number, str(media_id)),
            ).fetchone()
        if row is None:
            raise MediaNotFound(str(media_id))
        return to_media(row)

    def add(self, new: NewMedia, source: BinaryIO) -> MediaFile:
        """Check and store the file, then add it to the episode; AssetRejected when unfit."""
        stored = self.assets.save_stream(
            new.channel, KIND_FORMATS[new.kind], source, self.max_bytes
        )
        return self.insert(new, stored)

    def insert(self, new: NewMedia, stored: StoredFile) -> MediaFile:
        """Add a file already checked and stored, such as a render assembled from its parts."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO episode_media (id, channel_slug, number, kind, name, note, media_type,"
                " size_bytes, sha256, revision, created_at, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {MEDIA_COLUMNS}",
                (
                    str(uuid4()),
                    new.channel,
                    new.number,
                    new.kind,
                    new.data.name,
                    new.data.note,
                    stored.media_type,
                    stored.size_bytes,
                    stored.sha256,
                    now,
                    now,
                ),
            ).fetchone()
        return to_media(row)

    def replace(
        self, media: MediaFile, data: MediaInput, expected_revision: int | None
    ) -> MediaFile:
        """Rename a file or change its note; with `expected_revision`, only if still at it."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE episode_media SET name = ?, note = ?, revision = revision + 1,"
                " updated_at = ? WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {MEDIA_COLUMNS}",
                (
                    data.name,
                    data.note,
                    self.database.now(),
                    str(media.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            current = self.get(media.channel, media.number, media.id).revision
            raise PartChanged(media_label(media), current)
        return to_media(row)

    def delete(self, media: MediaFile, expected_revision: int | None) -> None:
        """Take a file out of the episode; the stored file stays, as others may share it."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episode_media WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(media.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.get(media.channel, media.number, media.id).revision
            raise PartChanged(media_label(media), current)
