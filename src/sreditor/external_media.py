"""Third-party media in a channel's library and each episode's use of it.

Rules project.external-media and project.external-media.use. An item is something made by
others that episodes show or build on: a clip, an image, audio, a document, a web page, a 3D
model, a texture or a shader. It keeps its link,
creator, rights and credit line, and may keep a copy uploaded by a person; sreditor never
downloads from the link. An episode records each use in a row of its own, so adding a use never
moves the item's revision, and an item cannot be removed while a use refers to it.
"""

from __future__ import annotations

import builtins
import sqlite3
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, BinaryIO, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

from sreditor.assets import (
    BLEND,
    DDS,
    EXR,
    FBX,
    GLB,
    JPEG,
    KTX2,
    M4A,
    MP3,
    MP4,
    PDF,
    PNG,
    TEXT,
    WAV,
    WEBM,
    WEBP,
    ZIP,
    AssetStore,
    FileFamily,
    FileFormat,
    StoredFile,
    names_of,
)
from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.field_types import ChannelSlug, MediaType, Sha256, Title

CREATOR_LENGTH = 200
LICENSE_LENGTH = 120
CREDIT_LENGTH = 500
NOTE_LENGTH = 2000
PAGES_LENGTH = 40
SCENE_LENGTH = 200

Creator = Annotated[str, StringConstraints(strip_whitespace=True, max_length=CREATOR_LENGTH)]
LicenseName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=LICENSE_LENGTH)]
Credit = Annotated[str, StringConstraints(strip_whitespace=True, max_length=CREDIT_LENGTH)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=NOTE_LENGTH)]
Pages = Annotated[str, StringConstraints(strip_whitespace=True, max_length=PAGES_LENGTH)]
Scene = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SCENE_LENGTH)]


class ExternalKind(StrEnum):
    """What a third-party item is."""

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    WEB_PAGE = "web_page"
    MODEL_3D = "model_3d"
    TEXTURE = "texture"
    SHADER = "shader"


class Rights(StrEnum):
    """Why the channel may show an item."""

    PUBLIC_DOMAIN = "public_domain"
    OPEN_LICENSE = "open_license"
    LICENSED = "licensed"
    CITATION = "citation"
    OWN = "own"
    UNKNOWN = "unknown"


class UsePurpose(StrEnum):
    """What an episode uses an item for."""

    ILLUSTRATION = "illustration"
    EVIDENCE = "evidence"
    REACTION = "reaction"
    QUOTE = "quote"
    PRODUCTION = "production"


class Problem(StrEnum):
    """A rights gap an episode warns about; none of them blocks a status."""

    RIGHTS_UNKNOWN = "rights_unknown"
    NO_REASON = "no_reason"
    NO_RANGE = "no_range"
    NO_CREDIT = "no_credit"


# The file types a copy of each kind may be; a web page is kept as a PDF or a screenshot, and a
# 3D model, texture or shader may come as a ZIP package of its files.
COPY_FORMATS: dict[ExternalKind, tuple[FileFormat, ...]] = {
    ExternalKind.VIDEO: (MP4, WEBM),
    ExternalKind.IMAGE: (PNG, JPEG, WEBP),
    ExternalKind.AUDIO: (MP3, WAV, M4A),
    ExternalKind.DOCUMENT: (PDF,),
    ExternalKind.WEB_PAGE: (PDF, PNG, JPEG),
    ExternalKind.MODEL_3D: (GLB, FBX, BLEND, ZIP),
    ExternalKind.TEXTURE: (PNG, JPEG, WEBP, EXR, KTX2, DDS, ZIP),
    ExternalKind.SHADER: (TEXT, ZIP),
}
EXTENSIONS = {
    item.media_type: item.extension for formats in COPY_FORMATS.values() for item in formats
}
# Pictures, video and sound play in the page (web.app.assets); anything else downloads.
INLINE_FAMILIES = frozenset({FileFamily.IMAGE, FileFamily.VIDEO, FileFamily.AUDIO})
INLINE_TYPES = frozenset(
    item.media_type
    for formats in COPY_FORMATS.values()
    for item in formats
    if item.family in INLINE_FAMILIES
)
TIMED_KINDS = frozenset({ExternalKind.VIDEO, ExternalKind.AUDIO})
# What a shader's file picker offers; any UTF-8 text is accepted whatever its name.
SHADER_EXTENSIONS = [".glsl", ".vert", ".frag", ".hlsl", ".wgsl", ".shader", ".txt"]
# Models the page shows in its 3D viewer; any other model file downloads.
VIEWABLE_MODELS = frozenset({GLB.media_type, FBX.media_type})
# A citation names its author and source (Lei 9.610/98, art. 46, III); an open license asks
# for attribution, so both need a credit line.
CREDITED_RIGHTS = frozenset({Rights.OPEN_LICENSE, Rights.CITATION})


def copy_formats(kind: ExternalKind) -> str:
    """The extensions a copy of this kind may have, for a person to read."""
    return names_of(COPY_FORMATS[kind])


def copy_accept(kind: ExternalKind) -> str:
    """The file picker's accept= value for a copy of this kind.

    Extensions come too, since browsers know no media type for FBX, BLEND or shader files; the
    upload is still checked by its content.
    """
    formats = COPY_FORMATS[kind]
    hints = [item.extension for item in formats if item is not TEXT]
    hints += SHADER_EXTENSIONS if TEXT in formats else []
    return ",".join([item.media_type for item in formats] + hints)


class ExternalMediaInput(BaseModel):
    """A third-party item to add or replace: what it is, where it lives and its rights."""

    model_config = ConfigDict(frozen=True)

    kind: ExternalKind
    title: Title
    url: HttpUrl = Field(description="The page where the item lives, never a download link.")
    creator: Creator = ""
    published_on: date | None = None
    accessed_on: date | None = None  # today when left out
    rights: Rights = Rights.UNKNOWN
    license_name: LicenseName = ""
    license_url: HttpUrl | None = None
    attribution: Credit = Field(default="", description="The credit line to show with it.")
    rights_note: Note = Field(default="", description="Why the use is allowed, and any limits.")


class MediaCopy(BaseModel):
    """The copy of an item a person uploaded."""

    model_config = ConfigDict(frozen=True)

    media_type: MediaType
    size_bytes: int
    sha256: Sha256

    def is_inline(self) -> bool:
        """Whether the copy plays in the page rather than downloads."""
        return self.media_type in INLINE_TYPES

    def is_viewable_model(self) -> bool:
        """Whether the page shows the copy in its 3D viewer; it still downloads when opened."""
        return self.media_type in VIEWABLE_MODELS


class ExternalMedia(BaseModel):
    """A third-party item in the channel's library."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: ChannelSlug
    kind: ExternalKind
    title: str
    url: HttpUrl
    creator: str
    published_on: date | None
    accessed_on: date
    rights: Rights
    license_name: str
    license_url: HttpUrl | None
    attribution: str
    rights_note: str
    copy_file: MediaCopy | None = None
    revision: int
    created_at: datetime
    updated_at: datetime

    def stored(self) -> StoredFile | None:
        """Where the copy sits in the assets directory, if there is one."""
        if self.copy_file is None:
            return None
        found = self.copy_file
        path = f"{self.channel}/{found.sha256}{EXTENSIONS[found.media_type]}"
        return StoredFile(
            path=path, media_type=found.media_type, sha256=found.sha256, size_bytes=found.size_bytes
        )


class MediaUseInput(BaseModel):
    """How an episode uses an item: where in it, what for and in which scene."""

    model_config = ConfigDict(frozen=True)

    media_id: UUID
    purpose: UsePurpose = UsePurpose.ILLUSTRATION
    start_seconds: int | None = Field(default=None, ge=0)
    end_seconds: int | None = Field(default=None, ge=0)
    pages: Pages = Field(default="", description="For a document, such as 3-5.")
    scene: Scene = Field(default="", description="The screenplay scene it appears in.")
    credit: Credit = Field(default="", description="Replaces the item's credit line here.")
    note: Note = ""

    @model_validator(mode="after")
    def end_after_start(self) -> Self:
        """A range ends after it starts."""
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("end_seconds must be after start_seconds")
        return self


class MediaUse(BaseModel):
    """One use of a third-party item in an episode."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: ChannelSlug
    number: int
    media_id: UUID
    purpose: UsePurpose
    start_seconds: int | None
    end_seconds: int | None
    pages: str
    scene: str
    credit: str
    note: str
    revision: int
    created_at: datetime
    updated_at: datetime


class CreditLine(BaseModel):
    """One credit an episode owes, ready for its description."""

    model_config = ConfigDict(frozen=True)

    media_id: UUID
    title: str
    text: str
    url: HttpUrl


class RightsWarning(BaseModel):
    """A rights gap in one use of an item."""

    model_config = ConfigDict(frozen=True)

    use_id: UUID
    media_id: UUID
    title: str
    problem: Problem


class EpisodeCredits(BaseModel):
    """What an episode owes for the third-party media it uses, and its rights gaps."""

    model_config = ConfigDict(frozen=True)

    credits: list[CreditLine]
    warnings: list[RightsWarning]


class NewCopy(BaseModel):
    """A copy to store for an item, and the item revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    item: ExternalMedia
    expected_revision: int | None = None


class ExternalMediaNotFound(LookupError):
    """The channel has no such third-party item."""


class MediaUseNotFound(LookupError):
    """The episode has no such use."""


class ExternalMediaInUse(ValueError):
    """An item episodes still use cannot be removed."""

    def __init__(self, media_id: UUID, episodes: builtins.list[int]) -> None:
        super().__init__(str(media_id))
        self.media_id = media_id
        self.episodes = episodes


class UseRefused(ValueError):
    """A use that does not fit its item, such as a time range on an image."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


ITEM_COLUMNS = (
    "id, channel_slug, kind, title, url, creator, published_on, accessed_on, rights,"
    " license_name, license_url, attribution, rights_note, copy_media_type, copy_size_bytes,"
    " copy_sha256, revision, created_at, updated_at"
)
USE_COLUMNS = (
    "id, channel_slug, number, media_id, purpose, start_seconds, end_seconds, pages, scene,"
    " credit, note, revision, created_at, updated_at"
)


def optional_day(value: str | None) -> date | None:
    """A stored day, or None."""
    return date.fromisoformat(value) if value else None


def to_copy(row: sqlite3.Row) -> MediaCopy | None:
    """The copy columns of an item row, or None when nobody uploaded one."""
    if row["copy_sha256"] is None:
        return None
    return MediaCopy(
        media_type=row["copy_media_type"],
        size_bytes=row["copy_size_bytes"],
        sha256=row["copy_sha256"],
    )


def to_item(row: sqlite3.Row) -> ExternalMedia:
    """An item row as a model."""
    return ExternalMedia(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        kind=ExternalKind(row["kind"]),
        title=row["title"],
        url=HttpUrl(row["url"]),
        creator=row["creator"],
        published_on=optional_day(row["published_on"]),
        accessed_on=date.fromisoformat(row["accessed_on"]),
        rights=Rights(row["rights"]),
        license_name=row["license_name"],
        license_url=HttpUrl(row["license_url"]) if row["license_url"] else None,
        attribution=row["attribution"],
        rights_note=row["rights_note"],
        copy_file=to_copy(row),
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def to_use(row: sqlite3.Row) -> MediaUse:
    """A use row as a model."""
    return MediaUse(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        media_id=UUID(row["media_id"]),
        purpose=UsePurpose(row["purpose"]),
        start_seconds=row["start_seconds"],
        end_seconds=row["end_seconds"],
        pages=row["pages"],
        scene=row["scene"],
        credit=row["credit"],
        note=row["note"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def item_values(data: ExternalMediaInput, today: date) -> tuple[object, ...]:
    """The columns an item's input fills, in ITEM_WRITE order."""
    return (
        data.kind,
        data.title,
        str(data.url),
        data.creator,
        data.published_on.isoformat() if data.published_on else None,
        (data.accessed_on or today).isoformat(),
        data.rights,
        data.license_name,
        str(data.license_url) if data.license_url else None,
        data.attribution,
        data.rights_note,
    )


def use_values(data: MediaUseInput) -> tuple[object, ...]:
    """The columns a use's input fills, in USE_WRITE order."""
    return (
        str(data.media_id),
        data.purpose,
        data.start_seconds,
        data.end_seconds,
        data.pages,
        data.scene,
        data.credit,
        data.note,
    )


ITEM_WRITE = (
    "kind, title, url, creator, published_on, accessed_on, rights, license_name, license_url,"
    " attribution, rights_note"
)
USE_WRITE = "media_id, purpose, start_seconds, end_seconds, pages, scene, credit, note"


def item_label(media_id: UUID) -> str:
    """How an item is named in a conflict message."""
    return f"Third-party item {media_id}"


def use_label(use_id: UUID) -> str:
    """How a use is named in a conflict message."""
    return f"Use {use_id}"


def credit_text(item: ExternalMedia, use: MediaUse) -> str:
    """The credit a use shows: its own, else the item's, else its title and creator."""
    plain = " — ".join(part for part in (item.title, item.creator) if part)
    return use.credit or item.attribution or plain


def problems_of(item: ExternalMedia, use: MediaUse) -> builtins.list[Problem]:
    """The rights gaps of one use (project.external-media.use); warnings, never refusals."""
    found = []
    if item.rights is Rights.UNKNOWN:
        found.append(Problem.RIGHTS_UNKNOWN)
    if item.rights is Rights.CITATION and not item.rights_note:
        found.append(Problem.NO_REASON)
    if item.rights is Rights.CITATION and item.kind in TIMED_KINDS and use.end_seconds is None:
        found.append(Problem.NO_RANGE)
    if item.rights in CREDITED_RIGHTS and not (use.credit or item.attribution):
        found.append(Problem.NO_CREDIT)
    return found


def episode_credits(
    uses: builtins.list[MediaUse], items: dict[UUID, ExternalMedia]
) -> EpisodeCredits:
    """The credits an episode owes, one per item in the order first used, and its gaps."""
    credits: dict[UUID, CreditLine] = {}
    warnings: builtins.list[RightsWarning] = []
    for use in uses:
        item = items[use.media_id]
        credits.setdefault(
            item.id,
            CreditLine(
                media_id=item.id, title=item.title, text=credit_text(item, use), url=item.url
            ),
        )
        warnings.extend(
            RightsWarning(use_id=use.id, media_id=item.id, title=item.title, problem=problem)
            for problem in problems_of(item, use)
        )
    return EpisodeCredits(credits=list(credits.values()), warnings=warnings)


def check_fits(item: ExternalMedia, data: MediaUseInput) -> None:
    """Refuse a use that does not fit its item: a time range only on video or audio."""
    is_timed = data.start_seconds is not None or data.end_seconds is not None
    if is_timed and item.kind not in TIMED_KINDS:
        raise UseRefused("start_seconds", "Only video and audio take a time range")
    if data.pages and item.kind not in {ExternalKind.DOCUMENT, ExternalKind.WEB_PAGE}:
        raise UseRefused("pages", "Only documents and web pages take pages")


class ExternalMediaStore:
    """Third-party items and their uses in the application database; copies in the assets."""

    def __init__(self, database: Database, assets: AssetStore, max_bytes: int) -> None:
        self.database = database
        self.assets = assets
        self.max_bytes = max_bytes

    def list(
        self, channel: str, after: int | None, limit: int, kind: ExternalKind | None = None
    ) -> builtins.list[ExternalMedia]:
        """The channel's items, of one kind or all, in the order they were added."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM external_media WHERE channel_slug = ?"
                " AND (? IS NULL OR kind = ?) AND position > ? ORDER BY position LIMIT ?",
                (channel, kind, kind, after or 0, limit),
            ).fetchall()
        return [to_item(row) for row in rows]

    def position(self, media_id: UUID) -> int:
        """Where an item sits in the channel's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM external_media WHERE id = ?", (str(media_id),)
            ).fetchone()
        if row is None:
            raise ExternalMediaNotFound(str(media_id))
        return int(row["position"])

    def get(self, channel: str, media_id: UUID) -> ExternalMedia:
        """One item of the channel."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM external_media WHERE channel_slug = ? AND id = ?",
                (channel, str(media_id)),
            ).fetchone()
        if row is None:
            raise ExternalMediaNotFound(str(media_id))
        return to_item(row)

    def create(self, channel: str, data: ExternalMediaInput) -> ExternalMedia:
        """Add an item to the channel's library, without a copy."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                f"INSERT INTO external_media (id, channel_slug, {ITEM_WRITE}, revision,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                f" RETURNING {ITEM_COLUMNS}",
                (str(uuid4()), channel, *item_values(data, self.database.clock().date()), now, now),
            ).fetchone()
        return to_item(row)

    def replace(
        self, item: ExternalMedia, data: ExternalMediaInput, expected_revision: int | None
    ) -> ExternalMedia:
        """Replace an item's details; its copy stays. With `expected_revision`, only if still at it."""
        assignments = ", ".join(f"{column.strip()} = ?" for column in ITEM_WRITE.split(","))
        with self.database.connect() as connection:
            row = connection.execute(
                f"UPDATE external_media SET {assignments}, revision = revision + 1,"
                " updated_at = ? WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {ITEM_COLUMNS}",
                (
                    *item_values(data, item.accessed_on),
                    self.database.now(),
                    str(item.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            raise PartChanged(item_label(item.id), self.get(item.channel, item.id).revision)
        return to_item(row)

    def attach(self, new: NewCopy, source: BinaryIO) -> ExternalMedia:
        """Check and store a copy, then put it on the item in place of any earlier one."""
        item = new.item
        stored = self.assets.save_stream(
            item.channel, COPY_FORMATS[item.kind], source, self.max_bytes
        )
        return self.set_copy(item, stored, new.expected_revision)

    def detach(self, item: ExternalMedia, expected_revision: int | None) -> ExternalMedia:
        """Take the copy off an item; the stored file stays, as others may share it."""
        return self.set_copy(item, None, expected_revision)

    def set_copy(
        self, item: ExternalMedia, stored: StoredFile | None, expected_revision: int | None
    ) -> ExternalMedia:
        """Point the item at a stored copy, or at none."""
        values = (
            (stored.media_type, stored.size_bytes, stored.sha256) if stored else (None, None, None)
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE external_media SET copy_media_type = ?, copy_size_bytes = ?,"
                " copy_sha256 = ?, revision = revision + 1, updated_at = ?"
                " WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {ITEM_COLUMNS}",
                (*values, self.database.now(), str(item.id), expected_revision, expected_revision),
            ).fetchone()
        if row is None:
            raise PartChanged(item_label(item.id), self.get(item.channel, item.id).revision)
        return to_item(row)

    def delete(self, item: ExternalMedia, expected_revision: int | None) -> None:
        """Remove an item from the library, unless an episode still uses it."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM external_media WHERE id = ? AND (? IS NULL OR revision = ?)"
                " AND NOT EXISTS (SELECT 1 FROM episode_media_uses"
                " WHERE media_id = external_media.id)",
                (str(item.id), expected_revision, expected_revision),
            )
        if cursor.rowcount:
            return
        episodes = self.episodes_of(item.id)
        if episodes:
            raise ExternalMediaInUse(item.id, episodes)
        raise PartChanged(item_label(item.id), self.get(item.channel, item.id).revision)

    def episodes_of(self, media_id: UUID) -> builtins.list[int]:
        """The numbers of the episodes that use an item."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT number FROM episode_media_uses WHERE media_id = ? ORDER BY number",
                (str(media_id),),
            ).fetchall()
        return [int(row["number"]) for row in rows]

    def uses(
        self, channel: str, number: int, after: int | None, limit: int
    ) -> builtins.list[MediaUse]:
        """An episode's uses, in the order they were added."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {USE_COLUMNS} FROM episode_media_uses WHERE channel_slug = ?"
                " AND number = ? AND position > ? ORDER BY position LIMIT ?",
                (channel, number, after or 0, limit),
            ).fetchall()
        return [to_use(row) for row in rows]

    def use_position(self, use_id: UUID) -> int:
        """Where a use sits in its episode's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM episode_media_uses WHERE id = ?", (str(use_id),)
            ).fetchone()
        if row is None:
            raise MediaUseNotFound(str(use_id))
        return int(row["position"])

    def use(self, channel: str, number: int, use_id: UUID) -> MediaUse:
        """One use of the episode."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {USE_COLUMNS} FROM episode_media_uses"
                " WHERE channel_slug = ? AND number = ? AND id = ?",
                (channel, number, str(use_id)),
            ).fetchone()
        if row is None:
            raise MediaUseNotFound(str(use_id))
        return to_use(row)

    def add_use(self, channel: str, number: int, data: MediaUseInput) -> MediaUse:
        """Record a use of one of the channel's items in the episode."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                f"INSERT INTO episode_media_uses (id, channel_slug, number, {USE_WRITE},"
                " revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                f" 1, ?, ?) RETURNING {USE_COLUMNS}",
                (str(uuid4()), channel, number, *use_values(data), now, now),
            ).fetchone()
        return to_use(row)

    def replace_use(
        self, use: MediaUse, data: MediaUseInput, expected_revision: int | None
    ) -> MediaUse:
        """Replace a use's details; with `expected_revision`, only if still at it."""
        assignments = ", ".join(f"{column.strip()} = ?" for column in USE_WRITE.split(","))
        with self.database.connect() as connection:
            row = connection.execute(
                f"UPDATE episode_media_uses SET {assignments}, revision = revision + 1,"
                " updated_at = ? WHERE id = ? AND (? IS NULL OR revision = ?)"
                f" RETURNING {USE_COLUMNS}",
                (
                    *use_values(data),
                    self.database.now(),
                    str(use.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            current = self.use(use.channel, use.number, use.id).revision
            raise PartChanged(use_label(use.id), current)
        return to_use(row)

    def delete_use(self, use: MediaUse, expected_revision: int | None) -> None:
        """Take a use out of the episode; the item stays in the library."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM episode_media_uses WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(use.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.use(use.channel, use.number, use.id).revision
            raise PartChanged(use_label(use.id), current)

    def items_of(self, channel: str, media_ids: set[UUID]) -> dict[UUID, ExternalMedia]:
        """The channel's items with these ids, by id."""
        if not media_ids:
            return {}
        marks = ", ".join("?" for _ in media_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM external_media WHERE channel_slug = ?"
                f" AND id IN ({marks})",
                (channel, *(str(media_id) for media_id in media_ids)),
            ).fetchall()
        return {item.id: item for item in (to_item(row) for row in rows)}

    def all_uses(self, channel: str, number: int) -> builtins.list[MediaUse]:
        """Every use of the episode, for its credits; an episode uses a handful, not thousands."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {USE_COLUMNS} FROM episode_media_uses"
                " WHERE channel_slug = ? AND number = ? ORDER BY position",
                (channel, number),
            ).fetchall()
        return [to_use(row) for row in rows]
