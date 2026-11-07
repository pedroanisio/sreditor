"""Signed, single-use links that move files in and out of sreditor (rule agentic.mcp.files).

An agent in a sandbox cannot use the MCP connection's OAuth token to fetch or send a file, so a
tool hands it a link instead. Like API tokens and OAuth codes, a link is random and only its
hash is stored. It works once, for 15 minutes, for the one target it was issued for: a brand
file, an episode media file, a third-party media copy, an episode's srstudio export, or an
upload of one media kind into one episode. Waivers have no link kind, so none can reach them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.database import Database, stamp
from sreditor.field_types import ChannelSlug
from sreditor.security import hash_token, new_token

LINK_TTL = timedelta(minutes=15)
# Spent and expired links stay this long, so a late use answers 410 and not 404.
RETENTION = timedelta(hours=24)


class LinkKind(StrEnum):
    """What a link is bound to; no kind reaches a waiver."""

    BRAND_FILE = "brand_file"
    MEDIA_FILE = "media_file"
    EXTERNAL_COPY = "external_copy"
    SRSTUDIO_ZIP = "srstudio_zip"
    MEDIA_UPLOAD = "media_upload"

    @property
    def is_upload(self) -> bool:
        """Whether the link receives a file rather than sends one."""
        return self is LinkKind.MEDIA_UPLOAD


class NewLink(BaseModel):
    """A link to issue: the token that asks, for which channel and what it is bound to."""

    model_config = ConfigDict(frozen=True)

    # The token names its user, so the link does not store the user again.
    token_id: UUID
    channel: ChannelSlug
    kind: LinkKind
    number: int | None = None
    # The brand file's path, the media id, the item id, or an upload's media kind.
    reference: str = ""
    # An upload's name and note for the media file it creates.
    name: str = ""
    note: str = ""


class FileLink(NewLink):
    """An issued link as stored: the target plus the moment it expires."""

    expires_at: datetime


class IssuedLink(BaseModel):
    """A new link: its secret, returned only now, and what it is bound to."""

    model_config = ConfigDict(frozen=True)

    secret: str
    link: FileLink


class FileLinkNotFound(LookupError):
    """No such link was ever issued."""


class LinkGone(Exception):
    """The link was used already, or has expired."""


def to_link(row: sqlite3.Row) -> FileLink:
    """A link from its database row."""
    return FileLink(
        token_id=UUID(row["token_id"]),
        channel=row["channel_slug"],
        kind=LinkKind(row["kind"]),
        number=row["number"],
        reference=row["reference"],
        name=row["name"],
        note=row["note"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


class FileLinkStore:
    """File links in the database, kept only as hashes."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def issue(self, new: NewLink) -> IssuedLink:
        """Create a link valid for LINK_TTL; its secret is in the result and never stored."""
        secret = new_token()
        now = self.database.clock().replace(microsecond=0)
        expires_at = now + LINK_TTL
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM file_links WHERE expires_at <= ?", (stamp(now - RETENTION),)
            )
            connection.execute(
                "INSERT INTO file_links (link_hash, token_id, channel_slug, kind, number,"
                " reference, name, note, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?)",
                (
                    hash_token(secret),
                    str(new.token_id),
                    new.channel,
                    new.kind,
                    new.number,
                    new.reference,
                    new.name,
                    new.note,
                    stamp(now),
                    stamp(expires_at),
                ),
            )
        return IssuedLink(secret=secret, link=FileLink(**new.model_dump(), expires_at=expires_at))

    def claim(self, secret: str) -> FileLink:
        """Mark a live link used and return it; FileLinkNotFound or LinkGone when it cannot be used.

        The update is atomic, so of two requests racing for one link only one wins.
        """
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE file_links SET used_at = ? WHERE link_hash = ? AND used_at IS NULL"
                " AND expires_at > ? RETURNING *",
                (now, hash_token(secret), now),
            ).fetchone()
            if row is not None:
                return to_link(row)
            known = connection.execute(
                "SELECT 1 FROM file_links WHERE link_hash = ?", (hash_token(secret),)
            ).fetchone()
        if known is None:
            raise FileLinkNotFound
        raise LinkGone

    def release(self, secret: str) -> None:
        """Give a claimed link back, after a failure that was not the caller's doing to repeat."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE file_links SET used_at = NULL WHERE link_hash = ?", (hash_token(secret),)
            )
