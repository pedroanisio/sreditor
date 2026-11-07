"""Channel records. Each row holds one validated Channel document."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from sreditor.channels import Channel, Platform, normalized_handle
from sreditor.database import Database
from sreditor.members import Role


class ChannelExists(ValueError):
    """A channel with that slug already exists."""


class ChannelNotFound(LookupError):
    """No channel has that slug."""


class RevisionMismatch(ValueError):
    """The channel changed since the revision the caller expected."""

    def __init__(self, slug: str, current: int) -> None:
        super().__init__(slug)
        self.current = current


class AccountTaken(ValueError):
    """Another channel already has this account; which one stays private to its members."""

    def __init__(self, platform: Platform, handle: str) -> None:
        super().__init__(f"The {platform} account {handle} already belongs to another channel")
        self.platform = platform
        self.handle = handle


def index_accounts(connection: sqlite3.Connection, channel: Channel) -> None:
    """Record which channel holds each account, in the caller's transaction."""
    connection.execute("DELETE FROM account_handles WHERE channel_slug = ?", (channel.slug,))
    for account in channel.accounts:
        try:
            connection.execute(
                "INSERT INTO account_handles (platform, handle, channel_slug) VALUES (?, ?, ?)",
                (account.platform.value, normalized_handle(account.handle), channel.slug),
            )
        except sqlite3.IntegrityError as taken:
            raise AccountTaken(account.platform, account.handle) from taken


class ChannelRepository:
    """Stores channels as JSON documents in the database, each with a revision for ETags."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self, slugs: list[str] | None = None, after: str | None = None, limit: int | None = None
    ) -> list[Channel]:
        """Channels ordered by slug: all, or only those in `slugs`; after a slug, up to `limit`."""
        if slugs is not None and not slugs:
            return []
        clauses: list[str] = []
        params: list[str | int] = []
        if slugs is not None:
            clauses.append(f"slug IN ({', '.join('?' for _ in slugs)})")
            params.extend(slugs)
        if after is not None:
            clauses.append("slug > ?")
            params.append(after)
        query = "SELECT document FROM channels"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY slug"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [Channel.model_validate_json(row["document"]) for row in rows]

    def get(self, slug: str) -> Channel:
        """The channel with this slug; raises ChannelNotFound when there is none."""
        return self.get_with_revision(slug)[0]

    def get_with_revision(self, slug: str) -> tuple[Channel, int]:
        """The channel and its revision, which a later change must match; raises ChannelNotFound."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT document, revision FROM channels WHERE slug = ?", (slug,)
            ).fetchone()
        if row is None:
            raise ChannelNotFound(slug)
        return Channel.model_validate_json(row["document"]), int(row["revision"])

    def create(self, channel: Channel, owner: UUID | None = None) -> Channel:
        """Insert the channel and, in the same transaction, make `owner` its owner."""
        now = self.database.now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO channels (slug, document, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (channel.slug, channel.model_dump_json(), now, now),
                )
                index_accounts(connection, channel)
                if owner is not None:
                    connection.execute(
                        "INSERT INTO memberships (channel_slug, user_id, role, added_by, created_at)"
                        " VALUES (?, ?, ?, NULL, ?)",
                        (channel.slug, str(owner), Role.OWNER.value, now),
                    )
        except sqlite3.IntegrityError as duplicate:
            raise ChannelExists(channel.slug) from duplicate
        return channel

    def replace(self, channel: Channel, expected_revision: int | None = None) -> int:
        """Replace the channel and return its new revision.

        With `expected_revision`, the write happens only if nobody changed the channel since.
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE channels SET document = ?, updated_at = ?, revision = revision + 1"
                " WHERE slug = ? AND (? IS NULL OR revision = ?) RETURNING revision",
                (
                    channel.model_dump_json(),
                    self.database.now(),
                    channel.slug,
                    expected_revision,
                    expected_revision,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                index_accounts(connection, channel)
        if row is None:
            _, current = self.get_with_revision(channel.slug)
            raise RevisionMismatch(channel.slug, current)
        return int(row["revision"])

    def delete(self, slug: str, expected_revision: int | None = None) -> None:
        """Delete the channel with its memberships and invitations."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM channels WHERE slug = ? AND (? IS NULL OR revision = ?)",
                (slug, expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            _, current = self.get_with_revision(slug)
            raise RevisionMismatch(slug, current)
