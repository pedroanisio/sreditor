"""Numbered schema migrations: adopting deployed databases, a fresh start, reruns and failures."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sreditor.database import (
    MIGRATIONS,
    SCHEMA,
    Database,
    MigrationError,
    retire_single_pitch_table,
    upgrade_legacy_schema,
)
from sreditor.migrations import (
    Migration,
    apply_one,
    check_numbering,
    current_version,
    execute_script,
    run_migrations,
)

NOW = "2026-11-07T12:00:00+00:00"
LATEST = MIGRATIONS[-1].version

# A database as releases before numbered migrations left it: channels without a revision, tokens
# without a channel list, takes without blocks and one pitch per episode.
PRE_BASELINE = """
CREATE TABLE channels (slug TEXT PRIMARY KEY, document TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, status TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, email_verified_at TEXT);
CREATE TABLE api_tokens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users (id),
    name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
    scopes TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    last_used_at TEXT, revoked_at TEXT);
CREATE TABLE episodes (channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    number INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (channel_slug, number));
CREATE TABLE pitches (position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE episode_pitches (channel_slug TEXT NOT NULL, number INTEGER NOT NULL,
    pitch_id TEXT, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_slug, number));
CREATE TABLE voice_takes (channel_slug TEXT NOT NULL, number INTEGER NOT NULL,
    take_key TEXT NOT NULL, media_id TEXT NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL,
    voice_id TEXT NOT NULL, language TEXT NOT NULL, duration_s REAL NOT NULL, timings TEXT,
    suggested TEXT, created_at TEXT NOT NULL, PRIMARY KEY (channel_slug, number, take_key));
"""


def old_style_database(path: Path) -> None:
    """A database made the way Database() did before migrations: SCHEMA, then the hand checks."""
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        retire_single_pitch_table(connection)
        connection.executescript(SCHEMA)
        upgrade_legacy_schema(connection)
        connection.commit()


def fill(path: Path) -> None:
    """Rows a deployed database holds: a channel, its episode, research, a pitch and a token."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at, revision)"
            " VALUES ('memes', '{\"accounts\": []}', ?, ?, 3)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
            " VALUES ('memes', 1, 'Pilot', 'idea', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO episode_research (channel_slug, number, text, revision, updated_at)"
            " VALUES ('memes', 1, 'Mondays are hated', 2, ?)",
            (NOW,),
        )
        connection.execute(
            "INSERT INTO pitches (id, channel_slug, title, status, revision, created_at,"
            " updated_at) VALUES ('0b0a8a3e-5f0c-4c52-9a43-6d0c0c4a1111', 'memes', 'Mondays', 'idea', 1, ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO users (id, email, display_name, password_hash, status, created_at)"
            " VALUES ('u', 'u@example.com', 'U', 'hash', 'active', ?)",
            (NOW,),
        )
        connection.execute(
            "INSERT INTO api_tokens (id, user_id, name, token_hash, prefix, scopes, created_at,"
            " expires_at, channels) VALUES ('t', 'u', 'agent', 'h', 'sre_', '', ?, ?, 'memes')",
            (NOW, NOW),
        )


def table_names(connection: sqlite3.Connection) -> set[str]:
    """The names of the tables a database holds."""
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def dump(path: Path, tables: set[str]) -> dict[str, list[tuple[object, ...]]]:
    """Every row of the given tables, in a stable order, to compare before and after."""
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in sorted(tables)
        }


def versions(path: Path) -> list[int]:
    """The versions recorded in schema_version, oldest first."""
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT version FROM schema_version ORDER BY version")
        return [row[0] for row in rows]


def memory() -> sqlite3.Connection:
    """An in-memory connection in autocommit mode, as the migration runner expects."""
    return sqlite3.connect(":memory:", isolation_level=None)


def create_table(name: str) -> Migration:
    """A migration that creates one table."""

    def apply(connection: sqlite3.Connection) -> None:
        connection.execute(f"CREATE TABLE {name} (id INTEGER)")

    return Migration(version=0, name=f"create {name}", apply=apply)


def numbered(version: int, migration: Migration) -> Migration:
    """The migration under another number."""
    return migration.model_copy(update={"version": version})


# A new database


def test_database_new_file_reaches_latest_version(tmp_path: Path) -> None:
    Database(tmp_path / "app.sqlite")
    assert versions(tmp_path / "app.sqlite") == list(range(1, LATEST + 1))


def test_database_opened_twice_applies_nothing_the_second_time(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    Database(path)
    with closing(sqlite3.connect(path)) as connection:
        first = connection.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
    Database(path, clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    with closing(sqlite3.connect(path)) as connection:
        second = connection.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
    assert first == second


def test_database_newer_than_the_application_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    Database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO schema_version VALUES (?, 'from the future', ?)", (99, NOW))
    with pytest.raises(MigrationError, match="schema version 99"):
        Database(path)


# Adopting databases that have no version


def test_database_at_current_schema_without_version_keeps_every_row(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    fill(path)
    with closing(sqlite3.connect(path)) as connection:
        # SQLite's own counter table gains a row for each new AUTOINCREMENT table.
        before_tables = table_names(connection) - {"sqlite_sequence"}
    before = dump(path, before_tables)
    Database(path)
    assert dump(path, before_tables) == before


def test_database_at_current_schema_without_version_is_stamped_with_every_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    fill(path)
    Database(path)
    assert versions(path) == list(range(1, LATEST + 1))


def test_database_at_current_schema_without_version_keeps_its_tables_and_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    with closing(sqlite3.connect(path)) as connection:
        before = {
            (row[0], row[1]) for row in connection.execute("SELECT type, name FROM sqlite_master")
        }
    Database(path)
    with closing(sqlite3.connect(path)) as connection:
        after = {
            (row[0], row[1]) for row in connection.execute("SELECT type, name FROM sqlite_master")
        }
    assert before <= after


def test_database_before_the_baseline_is_brought_up_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(PRE_BASELINE)
        connection.execute(
            'INSERT INTO channels VALUES (\'memes\', \'{"accounts": [{"platform": "tiktok",'
            ' "handle": "@Funny"}]}\', ?, ?)',
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
            " VALUES ('memes', 1, 'Pilot', 'idea', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO pitches (id, channel_slug, title, status, revision, created_at,"
            " updated_at) VALUES ('pitch-1', 'memes', 'Mondays', 'idea', 1, ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO episode_pitches VALUES ('memes', 1, 'pitch-1', 4, ?)", (NOW,)
        )
    Database(path)
    with closing(sqlite3.connect(path)) as connection:
        channel = connection.execute("SELECT slug, revision FROM channels").fetchall()
        token_columns = [row[1] for row in connection.execute("PRAGMA table_info(api_tokens)")]
        take_columns = [row[1] for row in connection.execute("PRAGMA table_info(voice_takes)")]
        chosen = connection.execute(
            "SELECT pitch_id, position FROM episode_pitches WHERE number = 1"
        ).fetchall()
        revision = connection.execute("SELECT revision FROM episode_pitch_revisions").fetchone()
        handles = connection.execute("SELECT platform, handle, channel_slug FROM account_handles")
        handle_rows = handles.fetchall()
        episodes = connection.execute("SELECT title FROM episodes").fetchall()
    assert channel == [("memes", 1)]
    assert "channels" in token_columns and "blocks" in take_columns
    assert chosen == [("pitch-1", 0)] and revision == (4,)
    assert handle_rows == [("tiktok", "funny", "memes")]
    assert episodes == [("Pilot",)]
    assert versions(path) == list(range(1, LATEST + 1))


# The runner


def test_run_migrations_applies_each_once_in_order_and_records_them() -> None:
    connection = memory()
    migrations = [numbered(1, create_table("a")), numbered(2, create_table("b"))]
    run_migrations(connection, migrations, NOW)
    run_migrations(connection, migrations, NOW)
    rows = connection.execute("SELECT version, name, applied_at FROM schema_version").fetchall()
    assert rows == [(1, "create a", NOW), (2, "create b", NOW)]


def test_run_migrations_only_applies_the_ones_after_the_stored_version() -> None:
    connection = memory()
    first = numbered(1, create_table("a"))
    run_migrations(connection, [first], NOW)
    run_migrations(connection, [first, numbered(2, create_table("b"))], NOW)
    assert {"a", "b"} <= table_names(connection)


def test_run_migrations_failing_migration_rolls_back_and_keeps_the_version() -> None:
    connection = memory()

    def fails(inner: sqlite3.Connection) -> None:
        inner.execute("CREATE TABLE half_done (id INTEGER)")
        inner.execute("INSERT INTO missing_table VALUES (1)")

    migrations = [
        numbered(1, create_table("a")),
        Migration(version=2, name="fails", apply=fails),
    ]
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(connection, migrations, NOW)
    assert current_version(connection) == 1
    assert "half_done" not in table_names(connection)


def test_run_migrations_failing_first_migration_leaves_no_version_table() -> None:
    connection = memory()

    def fails(inner: sqlite3.Connection) -> None:
        inner.execute("CREATE TABLE half_done (id INTEGER)")
        raise MigrationError("refused")

    with pytest.raises(MigrationError):
        run_migrations(connection, [Migration(version=1, name="fails", apply=fails)], NOW)
    assert table_names(connection) == set()


def test_run_migrations_retry_after_a_failure_applies_the_fixed_migration() -> None:
    connection = memory()
    first = numbered(1, create_table("a"))

    def fails(inner: sqlite3.Connection) -> None:
        raise MigrationError("refused")

    with pytest.raises(MigrationError):
        run_migrations(connection, [first, Migration(version=2, name="b", apply=fails)], NOW)
    run_migrations(connection, [first, numbered(2, create_table("b"))], NOW)
    assert current_version(connection) == 2


def test_apply_one_migration_already_recorded_by_another_process_is_not_applied_again() -> None:
    connection = memory()
    migration = numbered(1, create_table("a"))
    run_migrations(connection, [migration], NOW)
    apply_one(connection, migration, NOW)
    assert current_version(connection) == 1


def test_check_numbering_with_a_gap_is_refused() -> None:
    with pytest.raises(ValueError, match="without gaps"):
        check_numbering([numbered(1, create_table("a")), numbered(3, create_table("b"))])


def test_execute_script_keeps_a_trigger_in_one_statement() -> None:
    connection = memory()
    execute_script(
        connection,
        "CREATE TABLE a (id INTEGER);\n"
        "CREATE TABLE log (id INTEGER);\n"
        "-- Copies each new row.\n"
        "CREATE TRIGGER copy AFTER INSERT ON a BEGIN INSERT INTO log VALUES (NEW.id); END;\n",
    )
    connection.execute("INSERT INTO a VALUES (7)")
    assert connection.execute("SELECT id FROM log").fetchall() == [(7,)]


def test_current_version_without_a_version_table_is_zero() -> None:
    assert current_version(memory()) == 0
