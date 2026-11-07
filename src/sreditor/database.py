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
CREATE TABLE IF NOT EXISTS episode_short_scripts (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS voice_takes (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    take_key TEXT NOT NULL,
    media_id TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    language TEXT NOT NULL,
    duration_s REAL NOT NULL,
    timings TEXT,
    suggested TEXT,
    created_at TEXT NOT NULL,
    blocks TEXT,
    PRIMARY KEY (channel_slug, number, take_key),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS voice_block_takes (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    take_key TEXT NOT NULL,
    text TEXT NOT NULL,
    voice TEXT NOT NULL,
    file TEXT NOT NULL,
    duration_s REAL NOT NULL,
    words TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number, take_key),
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
CREATE TABLE IF NOT EXISTS library_sheets (
    entry_id TEXT PRIMARY KEY REFERENCES channel_library (id) ON DELETE CASCADE,
    channel_slug TEXT NOT NULL,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    sheet TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel_slug, kind, slug)
);
CREATE TABLE IF NOT EXISTS library_relationships (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    first_id TEXT NOT NULL REFERENCES channel_library (id) ON DELETE CASCADE,
    second_id TEXT NOT NULL REFERENCES channel_library (id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS episode_slugs (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    slug TEXT NOT NULL,
    seed INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    UNIQUE (channel_slug, slug),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number) ON DELETE CASCADE
);
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
CREATE TABLE IF NOT EXISTS episode_classifications (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS channel_profiles (
    channel_slug TEXT PRIMARY KEY REFERENCES channels (slug) ON DELETE CASCADE,
    document TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
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
    revoked_at TEXT,
    channels TEXT  -- space-separated slugs; NULL reaches every channel of the user
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
CREATE TABLE IF NOT EXISTS external_media (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    creator TEXT NOT NULL DEFAULT '',
    published_on TEXT,
    accessed_on TEXT NOT NULL,
    rights TEXT NOT NULL,
    license_name TEXT NOT NULL DEFAULT '',
    license_url TEXT,
    attribution TEXT NOT NULL DEFAULT '',
    rights_note TEXT NOT NULL DEFAULT '',
    copy_media_type TEXT,
    copy_size_bytes INTEGER,
    copy_sha256 TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS external_media_by_channel ON external_media (channel_slug, position);
CREATE TABLE IF NOT EXISTS episode_media_uses (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    media_id TEXT NOT NULL REFERENCES external_media (id),
    purpose TEXT NOT NULL,
    start_seconds INTEGER,
    end_seconds INTEGER,
    pages TEXT NOT NULL DEFAULT '',
    scene TEXT NOT NULL DEFAULT '',
    credit TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_media_uses_by_episode
    ON episode_media_uses (channel_slug, number, position);
CREATE INDEX IF NOT EXISTS episode_media_uses_by_media ON episode_media_uses (media_id);
-- An item's context: answers to what, who, where, when, why and how (rule project.context).
CREATE TABLE IF NOT EXISTS item_contexts (
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    answers TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, item_kind, item_id)
);
-- Links point at items of several tables, so no foreign key can follow them: the triggers below
-- remove an item's context and links when the item goes.
CREATE TABLE IF NOT EXISTS item_links (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    question TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (channel_slug, source_kind, source_id, target_kind, target_id, question)
);
CREATE INDEX IF NOT EXISTS item_links_by_source
    ON item_links (channel_slug, source_kind, source_id, position);
CREATE INDEX IF NOT EXISTS item_links_by_target
    ON item_links (channel_slug, target_kind, target_id, position);
CREATE TRIGGER IF NOT EXISTS episodes_forget_context AFTER DELETE ON episodes BEGIN
    DELETE FROM item_contexts WHERE channel_slug = OLD.channel_slug
        AND item_kind = 'episode' AND item_id = CAST(OLD.number AS TEXT);
    DELETE FROM item_links WHERE channel_slug = OLD.channel_slug
        AND ((source_kind = 'episode' AND source_id = CAST(OLD.number AS TEXT))
        OR (target_kind = 'episode' AND target_id = CAST(OLD.number AS TEXT)));
END;
CREATE TRIGGER IF NOT EXISTS pitches_forget_context AFTER DELETE ON pitches BEGIN
    DELETE FROM item_contexts WHERE item_kind = 'pitch' AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = 'pitch' AND source_id = OLD.id)
        OR (target_kind = 'pitch' AND target_id = OLD.id);
END;
CREATE TRIGGER IF NOT EXISTS pitch_sources_forget_context AFTER DELETE ON pitch_sources BEGIN
    DELETE FROM item_contexts WHERE item_kind = 'pitch_source' AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = 'pitch_source' AND source_id = OLD.id)
        OR (target_kind = 'pitch_source' AND target_id = OLD.id);
END;
CREATE TRIGGER IF NOT EXISTS episode_sources_forget_context AFTER DELETE ON episode_sources BEGIN
    DELETE FROM item_contexts WHERE item_kind = 'source' AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = 'source' AND source_id = OLD.id)
        OR (target_kind = 'source' AND target_id = OLD.id);
END;
CREATE TRIGGER IF NOT EXISTS episode_media_forget_context AFTER DELETE ON episode_media BEGIN
    DELETE FROM item_contexts WHERE item_kind = 'media' AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = 'media' AND source_id = OLD.id)
        OR (target_kind = 'media' AND target_id = OLD.id);
END;
CREATE TRIGGER IF NOT EXISTS external_media_forget_context AFTER DELETE ON external_media BEGIN
    DELETE FROM item_contexts WHERE item_kind = 'external_media' AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = 'external_media' AND source_id = OLD.id)
        OR (target_kind = 'external_media' AND target_id = OLD.id);
END;
CREATE TRIGGER IF NOT EXISTS channel_library_forget_context AFTER DELETE ON channel_library BEGIN
    DELETE FROM item_contexts WHERE item_kind = OLD.kind AND item_id = OLD.id;
    DELETE FROM item_links WHERE (source_kind = OLD.kind AND source_id = OLD.id)
        OR (target_kind = OLD.kind AND target_id = OLD.id);
END;
CREATE INDEX IF NOT EXISTS memberships_by_user ON memberships (user_id);
CREATE INDEX IF NOT EXISTS api_tokens_by_user ON api_tokens (user_id);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS render_workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    engine_version TEXT,
    gpus TEXT NOT NULL DEFAULT '[]',
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS render_jobs (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    preset TEXT NOT NULL,
    export_sha256 TEXT NOT NULL,
    export_path TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    worker_id TEXT REFERENCES render_workers (id) ON DELETE SET NULL,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    failure TEXT,
    findings TEXT NOT NULL DEFAULT '[]',
    media_id TEXT,
    scene_path TEXT,
    requested_by TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS render_jobs_by_episode ON render_jobs (channel_slug, number, position);
CREATE INDEX IF NOT EXISTS render_jobs_by_status ON render_jobs (status, position);
CREATE INDEX IF NOT EXISTS render_jobs_by_export
    ON render_jobs (channel_slug, number, export_sha256, preset, engine_version);
-- An episode's delivery package, kept as JSON like its other text parts (rule project.delivery).
CREATE TABLE IF NOT EXISTS episode_deliveries (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
-- Dated numbers of one post, entered by hand or from a CSV (rule project.metrics); a post is
-- the episode's platform, and a second entry for the same day merges into the first.
CREATE TABLE IF NOT EXISTS post_snapshots (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    platform TEXT NOT NULL,
    captured_on TEXT NOT NULL,
    views INTEGER,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    saves INTEGER,
    avg_watch_s REAL,
    retention TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel_slug, number, platform, captured_on),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS post_snapshots_by_channel
    ON post_snapshots (channel_slug, number, platform, captured_on);
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
    token_columns = {row["name"] for row in connection.execute("PRAGMA table_info(api_tokens)")}
    if "channels" not in token_columns:
        # Tokens from before they could be limited to some channels reach all of them.
        connection.execute("ALTER TABLE api_tokens ADD COLUMN channels TEXT")
    take_columns = {row["name"] for row in connection.execute("PRAGMA table_info(voice_takes)")}
    if "blocks" not in take_columns:
        # Takes from before each block was its own take have no block takes to export.
        connection.execute("ALTER TABLE voice_takes ADD COLUMN blocks TEXT")


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
