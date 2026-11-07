"""The application database: channels, episodes, users, sessions, memberships and invitations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sreditor.channels import normalized_handle

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    slug TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS account_handles (
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    PRIMARY KEY (platform, handle)
);
CREATE INDEX IF NOT EXISTS account_handles_by_channel ON account_handles (channel_slug);
CREATE TABLE IF NOT EXISTS episodes (
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number)
);
CREATE TABLE IF NOT EXISTS episode_research (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS episode_screenplays (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS channel_library (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel_slug, kind, name_key)
);
CREATE INDEX IF NOT EXISTS channel_library_by_kind ON channel_library (channel_slug, kind, position);
CREATE TABLE IF NOT EXISTS episode_sources (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    claim TEXT NOT NULL DEFAULT '',
    accessed_on TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_sources_by_episode
    ON episode_sources (channel_slug, number, position);
CREATE TABLE IF NOT EXISTS episode_media (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_media_by_episode
    ON episode_media (channel_slug, number, kind, position);
CREATE TABLE IF NOT EXISTS episode_waivers (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    signer TEXT NOT NULL,
    covers TEXT NOT NULL DEFAULT '',
    signed_on TEXT,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_waivers_by_episode
    ON episode_waivers (channel_slug, number, position);
-- The visual style an episode picked: the library entry's id, kept as the part's text.
CREATE TABLE IF NOT EXISTS episode_styles (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS episode_counters (
    channel_slug TEXT PRIMARY KEY REFERENCES channels (slug) ON DELETE CASCADE,
    last_number INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    email_verified_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS memberships (
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    added_by TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, user_id)
);
CREATE TABLE IF NOT EXISTS invitations (
    id TEXT PRIMARY KEY,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    invited_by TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER,
    response_body TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients (client_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pitches (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pitches_by_channel ON pitches (channel_slug, position);
CREATE TABLE IF NOT EXISTS pitch_notes (
    pitch_id TEXT PRIMARY KEY REFERENCES pitches (id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pitch_sources (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    pitch_id TEXT NOT NULL REFERENCES pitches (id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    claim TEXT NOT NULL DEFAULT '',
    accessed_on TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pitch_sources_by_pitch ON pitch_sources (pitch_id, position);
-- SET NULL only matters when the channel goes; the app refuses to delete a pitch in use.
CREATE TABLE IF NOT EXISTS episode_pitches (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    pitch_id TEXT REFERENCES pitches (id) ON DELETE SET NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_pitches_by_pitch ON episode_pitches (pitch_id);
CREATE INDEX IF NOT EXISTS memberships_by_user ON memberships (user_id);
CREATE INDEX IF NOT EXISTS api_tokens_by_user ON api_tokens (user_id);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id);
"""

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The current time in UTC; the default clock, which tests replace."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """Format a moment as a UTC ISO 8601 string, the form stored in the database."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse(value: str | None) -> datetime | None:
    """Read back a stored timestamp; empty values mean no moment."""
    return datetime.fromisoformat(value) if value else None


def migrate(connection: sqlite3.Connection) -> None:
    """Bring databases created by earlier versions up to the current schema."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(channels)")}
    if "revision" not in columns:
        # Databases created before channels had an ETag (rule agentic.concurrency.etag).
        connection.execute("ALTER TABLE channels ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")


def index_handles(connection: sqlite3.Connection) -> None:
    """Fill account_handles from the channel documents, for databases from before it existed.

    If two channels already share an account, the first by slug keeps it; the other is refused
    the next time it is saved, with an error that says why.
    """
    if connection.execute("SELECT 1 FROM account_handles LIMIT 1").fetchone():
        return
    rows = connection.execute("SELECT slug, document FROM channels ORDER BY slug").fetchall()
    for row in rows:
        for account in json.loads(row["document"]).get("accounts", []):
            connection.execute(
                "INSERT OR IGNORE INTO account_handles (platform, handle, channel_slug)"
                " VALUES (?, ?, ?)",
                (account["platform"], normalized_handle(account["handle"]), row["slug"]),
            )


class Database:
    """The SQLite database shared by the stores, created and migrated on first use."""

    def __init__(self, path: Path, clock: Clock = utc_now) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            migrate(connection)
            index_handles(connection)

    def now(self) -> str:
        """The current time from the database's clock, as a stored timestamp."""
        return stamp(self.clock())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection whose block commits on success and rolls back on error."""
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()
