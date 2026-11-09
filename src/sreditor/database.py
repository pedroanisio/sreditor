"""The application database: channels, episodes, users, sessions, memberships and invitations.

Source of truth for the schema (rule code.data.migrations): `SCHEMA` below is the baseline,
migration 1, a snapshot of every table, index and trigger that existed when numbered migrations
began. It stays as `CREATE ... IF NOT EXISTS` statements because that is idempotent, so the
baseline can adopt any database that has no `schema_version` yet: a new one gets the whole
schema; one made by a release before the baseline is first brought up by the older, hand-written
checks `upgrade_legacy_schema` still holds; one already at the current schema (the deployed
databases) passes through unchanged and is only stamped version 1. SCHEMA is frozen: every later
change to the schema is a numbered migration appended to `MIGRATIONS`, never an edit to SCHEMA,
so there is one place to read what changed after the baseline and one order it happened in.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sreditor.channels import normalized_handle
from sreditor.experiment_schema import EXPERIMENTS
from sreditor.migrations import Migration, MigrationError, execute_script, run_migrations
from sreditor.search_index import SEARCH_INDEX

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
    license TEXT,
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
-- Lets rows that link two items name the channel in the foreign key, so both are of one channel.
CREATE UNIQUE INDEX IF NOT EXISTS channel_library_in_channel ON channel_library (channel_slug, id);
CREATE TABLE IF NOT EXISTS library_sheets (
    entry_id TEXT PRIMARY KEY REFERENCES channel_library (id) ON DELETE CASCADE,
    channel_slug TEXT NOT NULL,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    sheet TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    exported_at TEXT,
    UNIQUE (channel_slug, kind, slug)
);
CREATE TABLE IF NOT EXISTS library_relationships (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    first_id TEXT NOT NULL,
    second_id TEXT NOT NULL,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL,
    FOREIGN KEY (channel_slug, first_id) REFERENCES channel_library (channel_slug, id)
        ON DELETE CASCADE,
    FOREIGN KEY (channel_slug, second_id) REFERENCES channel_library (channel_slug, id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS episode_slugs (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    slug TEXT NOT NULL,
    seed INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    exported_at TEXT,
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
CREATE TABLE IF NOT EXISTS media_measures (
    sha256 TEXT PRIMARY KEY,
    duration_seconds REAL NOT NULL,
    width INTEGER,
    height INTEGER,
    loudness_lufs REAL
);
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
CREATE UNIQUE INDEX IF NOT EXISTS pitches_in_channel ON pitches (channel_slug, id);
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
-- An episode's pitches in order, the first its main one. CASCADE only matters when the channel
-- goes; the app refuses to delete a pitch in use.
CREATE TABLE IF NOT EXISTS episode_pitches (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    pitch_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (channel_slug, number, pitch_id),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE,
    FOREIGN KEY (channel_slug, pitch_id) REFERENCES pitches (channel_slug, id) ON DELETE CASCADE
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
CREATE UNIQUE INDEX IF NOT EXISTS external_media_in_channel ON external_media (channel_slug, id);
CREATE TABLE IF NOT EXISTS episode_media_uses (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    media_id TEXT NOT NULL,
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
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number) ON DELETE CASCADE,
    -- No cascade: an item in use cannot be removed until its episodes drop it.
    FOREIGN KEY (channel_slug, media_id) REFERENCES external_media (channel_slug, id)
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
-- A token limited to some channels stops naming one that goes, so a new channel that later takes
-- the same slug is not reached by it. A list left empty reaches no channel, never all of them.
CREATE TRIGGER IF NOT EXISTS channels_leave_token_lists AFTER DELETE ON channels BEGIN
    UPDATE api_tokens
        SET channels = TRIM(REPLACE(' ' || channels || ' ', ' ' || OLD.slug || ' ', ' '))
        WHERE instr(' ' || channels || ' ', ' ' || OLD.slug || ' ') > 0;
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
    builder_version TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS file_links (
    link_hash TEXT PRIMARY KEY,
    token_id TEXT NOT NULL REFERENCES api_tokens (id) ON DELETE CASCADE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    number INTEGER,
    reference TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS file_links_by_expiry ON file_links (expires_at);
CREATE TABLE IF NOT EXISTS episode_pitch_revisions (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS episode_claims (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    quote TEXT NOT NULL,
    place_kind TEXT NOT NULL,
    place_ref TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS episode_claims_by_episode
    ON episode_claims (channel_slug, number, position);
-- A claim's backing sources; the source lives in the episode or in one of its pitches, so it is
-- named by id and not by a foreign key (triggers drop the backing when the source goes).
CREATE TABLE IF NOT EXISTS claim_sources (
    claim_id TEXT NOT NULL REFERENCES episode_claims (id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    is_primary INTEGER NOT NULL,
    is_checked INTEGER NOT NULL DEFAULT 0,
    checked_by_type TEXT,
    checked_by_id TEXT,
    checked_at TEXT,
    PRIMARY KEY (claim_id, source_id)
);
CREATE TRIGGER IF NOT EXISTS episode_sources_forget_claims AFTER DELETE ON episode_sources BEGIN
    DELETE FROM claim_sources WHERE source_id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS pitch_sources_forget_claims AFTER DELETE ON pitch_sources BEGIN
    DELETE FROM claim_sources WHERE source_id = OLD.id;
END;
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


def add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    """Add a column, named first in `definition`, to a table that does not have it yet."""
    name = definition.split()[0]
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


# Tables whose foreign keys did not name the channel, with the parent each must now share it with.
CHANNEL_SCOPED_LINKS = {
    "library_relationships": "channel_library",
    "episode_pitches": "pitches",
    "episode_media_uses": "external_media",
}
LEGACY_SUFFIX = "_legacy"


def table_definitions(table: str) -> list[str]:
    """The CREATE TABLE and CREATE INDEX statements SCHEMA holds for one table."""
    created = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", SCHEMA, re.DOTALL)
    if created is None:
        raise KeyError(table)
    indexes = re.findall(
        rf"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON {table} \(.*?\);", SCHEMA
    )
    return [created.group(0), *indexes]


def column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    """The table's columns, in order."""
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def rebuild_table(connection: sqlite3.Connection, table: str) -> None:
    """Recreate a table from SCHEMA and copy its rows, since SQLite cannot alter constraints.

    Runs in a savepoint, so a row the new constraints refuse leaves the old table as it was.
    """
    legacy = table + LEGACY_SUFFIX
    connection.execute("SAVEPOINT rebuild")
    try:
        old_columns = set(column_names(connection, table))
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        for index in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (legacy,),
        ).fetchall():
            connection.execute(f"DROP INDEX {index['name']}")
        for statement in table_definitions(table):
            connection.execute(statement)
        kept = ", ".join(name for name in column_names(connection, table) if name in old_columns)
        connection.execute(f"INSERT INTO {table} ({kept}) SELECT {kept} FROM {legacy}")
        connection.execute(f"DROP TABLE {legacy}")
    except sqlite3.IntegrityError as error:
        connection.execute("ROLLBACK TO rebuild")
        connection.execute("RELEASE rebuild")
        raise MigrationError(
            f"{table}: a stored row breaks the new constraints ({error})"
        ) from error
    connection.execute("RELEASE rebuild")


def names_channel_in_link(connection: sqlite3.Connection, table: str, parent: str) -> bool:
    """Whether the table's foreign key to `parent` already includes the channel."""
    return any(
        row["table"] == parent and row["from"] == "channel_slug"
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    )


def scope_links_to_channel(connection: sqlite3.Connection) -> None:
    """Rebuild link tables whose foreign keys let them join items of two channels."""
    for table, parent in CHANNEL_SCOPED_LINKS.items():
        if not names_channel_in_link(connection, table, parent):
            rebuild_table(connection, table)
    # Links once stored their user too, which their token already names.
    if "user_id" in column_names(connection, "file_links"):
        rebuild_table(connection, "file_links")


def upgrade_legacy_schema(connection: sqlite3.Connection) -> None:
    """Bring databases created before numbered migrations up to the baseline schema.

    These checks run only inside migration 1; they are idempotent, so a database that is already
    current passes through them untouched.
    """
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
    # Slugs saved before srstudio exports fixed them (rule project.srstudio.slugs) stay editable.
    for table in ("library_sheets", "episode_slugs"):
        add_column(connection, table, "exported_at TEXT")
    # Takes from before license records get theirs when BlockTakeStore opens.
    add_column(connection, "voice_block_takes", "license TEXT")
    # Jobs queued before the srstudio commit was pinned name none, so no worker claims them and an
    # export requested again renders anew.
    add_column(connection, "render_jobs", "builder_version TEXT NOT NULL DEFAULT ''")
    move_single_pitches(connection)
    scope_links_to_channel(connection)


LEGACY_PITCHES = "episode_pitches_single"


def retire_single_pitch_table(connection: sqlite3.Connection) -> None:
    """Set aside the table that held one pitch per episode, before the schema is applied.

    The new episode_pitches has another shape under the same name; move_single_pitches copies
    the old rows over once the schema has created it.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(episode_pitches)")}
    if columns and "position" not in columns:
        connection.execute("DROP INDEX IF EXISTS episode_pitches_by_pitch")
        connection.execute(f"ALTER TABLE episode_pitches RENAME TO {LEGACY_PITCHES}")


def move_single_pitches(connection: sqlite3.Connection) -> None:
    """Copy each episode's single pitch into the ordered list, as its main pitch (position 0)."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (LEGACY_PITCHES,)
    ).fetchone()
    if exists is None:
        return
    connection.execute(
        "INSERT INTO episode_pitches (channel_slug, number, pitch_id, position)"
        f" SELECT channel_slug, number, pitch_id, 0 FROM {LEGACY_PITCHES}"
        " WHERE pitch_id IS NOT NULL"
    )
    connection.execute(
        "INSERT INTO episode_pitch_revisions (channel_slug, number, revision, updated_at)"
        f" SELECT channel_slug, number, revision, updated_at FROM {LEGACY_PITCHES}"
    )
    connection.execute(f"DROP TABLE {LEGACY_PITCHES}")


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


def apply_baseline(connection: sqlite3.Connection) -> None:
    """Migration 1: create the baseline schema, adopting a database that already has some of it."""
    retire_single_pitch_table(connection)
    execute_script(connection, SCHEMA)
    upgrade_legacy_schema(connection)
    index_handles(connection)


BASELINE = Migration(version=1, name="baseline schema", apply=apply_baseline)

# Tokens an OAuth approval creates are named after their client (see sreditor.oauth).
OAUTH_TOKEN_NAME_SUFFIX = " (OAuth)"
OAUTH_TOKEN_CLIENT_NAME_LENGTH = 60


def track_oauth_client_use(connection: sqlite3.Connection) -> None:
    """Migration 3: record when OAuth clients were used and index what retention scans.

    Clients with no such record are purged after a while, so clients registered before this
    column existed that already hold a token count as used. Expired codes are found by expiry.
    """
    # Idempotent like the baseline, so a rerun over a migrated database changes nothing.
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(oauth_clients)")}
    if "last_used_at" not in columns:
        connection.execute("ALTER TABLE oauth_clients ADD COLUMN last_used_at TEXT")
        connection.execute(
            "UPDATE oauth_clients SET last_used_at = created_at WHERE EXISTS ("
            " SELECT 1 FROM api_tokens WHERE api_tokens.name ="
            " substr(oauth_clients.client_name, 1, ?) || ?)",
            (OAUTH_TOKEN_CLIENT_NAME_LENGTH, OAUTH_TOKEN_NAME_SUFFIX),
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS oauth_clients_unused ON oauth_clients (created_at)"
        " WHERE last_used_at IS NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS oauth_codes_by_expiry ON oauth_codes (expires_at)"
    )


OAUTH_CLIENT_USE = Migration(
    version=3, name="OAuth client use and code expiry", apply=track_oauth_client_use
)
EVENTS_SCRIPT = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    episode_number INTEGER,
    item_id TEXT,
    status TEXT,
    actor_type TEXT,
    actor_id TEXT,
    CHECK ((actor_type IS NULL) = (actor_id IS NULL))
);
CREATE INDEX IF NOT EXISTS events_by_channel ON events (channel_slug, id);
CREATE INDEX IF NOT EXISTS events_by_time ON events (occurred_at);
-- The newest event of a channel that retention removed: a cursor older than this has missed some.
CREATE TABLE IF NOT EXISTS event_horizons (
    channel_slug TEXT PRIMARY KEY REFERENCES channels (slug) ON DELETE CASCADE,
    pruned_through INTEGER NOT NULL
);
"""


def add_events(connection: sqlite3.Connection) -> None:
    """Migration 4: the change feed of each channel and what retention has removed from it."""
    execute_script(connection, EVENTS_SCRIPT)


EVENTS = Migration(version=4, name="change feed events", apply=add_events)

EPISODE_REVIEWS_SQL = """
-- A review may name a file of its own episode only: the key carries channel and episode.
CREATE UNIQUE INDEX IF NOT EXISTS episode_media_in_episode
    ON episode_media (channel_slug, number, id);
CREATE TABLE IF NOT EXISTS episode_reviews (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL,
    token_id TEXT,
    reviewed_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    media_id TEXT,
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE,
    FOREIGN KEY (channel_slug, number, media_id)
        REFERENCES episode_media (channel_slug, number, id)
);
CREATE INDEX IF NOT EXISTS episode_reviews_by_episode
    ON episode_reviews (channel_slug, number, reviewed_at, position);
CREATE TABLE IF NOT EXISTS review_ranges (
    review_id TEXT NOT NULL REFERENCES episode_reviews (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    PRIMARY KEY (review_id, position)
);
-- A review outlives the file it looked at; it just stops pointing to it.
CREATE TRIGGER IF NOT EXISTS episode_media_forget_reviews AFTER DELETE ON episode_media BEGIN
    UPDATE episode_reviews SET media_id = NULL WHERE media_id = OLD.id;
END;
"""


def create_episode_reviews(connection: sqlite3.Connection) -> None:
    """Migration 6: an episode's reviews, each its own row, with the time ranges seen."""
    execute_script(connection, EPISODE_REVIEWS_SQL)


EPISODE_REVIEWS = Migration(version=6, name="episode reviews", apply=create_episode_reviews)
# Append new migrations here, numbered in order; never change one that has been released.
MIGRATIONS = (BASELINE, SEARCH_INDEX, OAUTH_CLIENT_USE, EVENTS, EXPERIMENTS, EPISODE_REVIEWS)


def open_connection(path: Path, *, is_autocommit: bool = False) -> sqlite3.Connection:
    """A connection to the database file with rows by name and foreign keys enforced.

    Autocommit leaves every transaction to the caller; otherwise Python opens one before a write.
    """
    connection = sqlite3.connect(
        path, timeout=5, isolation_level=None if is_autocommit else "DEFERRED"
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class Database:
    """The SQLite database shared by the stores, created and migrated on first use."""

    def __init__(self, path: Path, clock: Clock = utc_now) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, because run_migrations opens one transaction per migration itself.
        connection = open_connection(path, is_autocommit=True)
        try:
            run_migrations(connection, MIGRATIONS, self.now())
        finally:
            connection.close()

    def now(self) -> str:
        """The current time from the database's clock, as a stored timestamp."""
        return stamp(self.clock())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection whose block commits on success and rolls back on error."""
        connection = open_connection(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()
