"""The schema keeps links inside one channel and token channel lists free of deleted channels."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sreditor.database import Database, MigrationError
from tests.conftest import forget_schema_version

NOW = "2026-11-07T12:00:00+00:00"
OLD_EPISODE_PITCHES = """
CREATE TABLE episode_pitches (
    channel_slug TEXT NOT NULL,
    number INTEGER NOT NULL,
    pitch_id TEXT NOT NULL REFERENCES pitches (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (channel_slug, number, pitch_id),
    FOREIGN KEY (channel_slug, number) REFERENCES episodes (channel_slug, number)
        ON DELETE CASCADE
)
"""
OLD_FILE_LINKS = """
CREATE TABLE file_links (
    link_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
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
)
"""


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """A new database with channels a and b, episode a/1 and a pitch in each channel."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        for slug in ("a", "b"):
            connection.execute(
                "INSERT INTO channels (slug, document, created_at, updated_at) VALUES (?, '{}', ?, ?)",
                (slug, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO pitches (id, channel_slug, title, status, revision, created_at,"
                " updated_at) VALUES (?, ?, 'Pitch', 'idea', 1, ?, ?)",
                (f"pitch-{slug}", slug, NOW, NOW),
            )
        connection.execute(
            "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
            " VALUES ('a', 1, 'Episode', 'idea', ?, ?)",
            (NOW, NOW),
        )
    return database


def add_user_token(connection: sqlite3.Connection, token_id: str, channels: str | None) -> None:
    """Store a user (once) and an API token of theirs limited to `channels`."""
    connection.execute(
        "INSERT OR IGNORE INTO users (id, email, display_name, password_hash, status, created_at)"
        " VALUES ('u', 'u@example.com', 'U', 'hash', 'active', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO api_tokens (id, user_id, name, token_hash, prefix, scopes, created_at,"
        " expires_at, channels) VALUES (?, 'u', 'agent', ?, 'sre_', '', ?, ?, ?)",
        (token_id, f"hash-{token_id}", NOW, NOW, channels),
    )


def add_library_entry(connection: sqlite3.Connection, entry_id: str, channel: str) -> None:
    """Store a character in a channel's library."""
    connection.execute(
        "INSERT INTO channel_library (id, channel_slug, kind, name, name_key, revision, created_at,"
        " updated_at) VALUES (?, ?, 'character', ?, ?, 1, ?, ?)",
        (entry_id, channel, entry_id, entry_id, NOW, NOW),
    )


def test_episode_pitches_pitch_of_another_channel_is_refused(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.connect() as connection:
        connection.execute(
            "INSERT INTO episode_pitches (channel_slug, number, pitch_id, position)"
            " VALUES ('a', 1, 'pitch-b', 0)"
        )


def test_episode_pitches_pitch_of_same_channel_is_stored(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO episode_pitches (channel_slug, number, pitch_id, position)"
            " VALUES ('a', 1, 'pitch-a', 0)"
        )
        count = connection.execute("SELECT COUNT(*) FROM episode_pitches").fetchone()[0]
    assert count == 1


def test_episode_media_uses_item_of_another_channel_is_refused(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.connect() as connection:
        connection.execute(
            "INSERT INTO external_media (id, channel_slug, kind, title, url, accessed_on, rights,"
            " revision, created_at, updated_at) VALUES ('item-b', 'b', 'image', 'Photo',"
            " 'https://example.com', '2026-11-07', 'unknown', 1, ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO episode_media_uses (id, channel_slug, number, media_id, purpose,"
            " revision, created_at, updated_at) VALUES ('use', 'a', 1, 'item-b', 'b-roll', 1, ?, ?)",
            (NOW, NOW),
        )


def test_library_relationships_between_channels_is_refused(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.connect() as connection:
        add_library_entry(connection, "ana", "a")
        add_library_entry(connection, "bia", "b")
        connection.execute(
            "INSERT INTO library_relationships (id, channel_slug, first_id, second_id, data,"
            " revision) VALUES ('rel', 'a', 'ana', 'bia', '{}', 1)"
        )


def test_channel_delete_removes_slug_from_token_lists(database: Database) -> None:
    with database.connect() as connection:
        add_user_token(connection, "both", "a b")
        add_user_token(connection, "only", "a")
        add_user_token(connection, "every", None)
        connection.execute("DELETE FROM channels WHERE slug = 'a'")
        rows = connection.execute("SELECT id, channels FROM api_tokens").fetchall()
    # An emptied list reaches no channel; NULL, which reaches all, stays NULL.
    assert {row["id"]: row["channels"] for row in rows} == {
        "both": "b",
        "only": "",
        "every": None,
    }


def test_database_from_before_channel_scoped_links_is_rebuilt(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("DROP TABLE episode_pitches")
        connection.execute(OLD_EPISODE_PITCHES)
        connection.execute("INSERT INTO episode_pitches VALUES ('a', 1, 'pitch-a', 0)")
        forget_schema_version(connection)
        connection.execute("DROP TABLE file_links")
        connection.execute(OLD_FILE_LINKS)
        add_user_token(connection, "token", None)
        connection.execute(
            "INSERT INTO file_links (link_hash, user_id, token_id, channel_slug, kind, reference,"
            " created_at, expires_at) VALUES ('link', 'u', 'token', 'a', 'media_file', 'm', ?, ?)",
            (NOW, NOW),
        )
    reopened = Database(database.path)
    with reopened.connect() as connection:
        keys = connection.execute("PRAGMA foreign_key_list(episode_pitches)").fetchall()
        link_columns = {row["name"] for row in connection.execute("PRAGMA table_info(file_links)")}
        pitches = connection.execute("SELECT pitch_id FROM episode_pitches").fetchall()
        links = connection.execute("SELECT token_id FROM file_links").fetchall()
    assert any(key["table"] == "pitches" and key["from"] == "channel_slug" for key in keys)
    assert "user_id" not in link_columns
    assert [row["pitch_id"] for row in pitches] == ["pitch-a"]
    assert [row["token_id"] for row in links] == ["token"]


def test_database_with_cross_channel_link_refuses_upgrade(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("DROP TABLE episode_pitches")
        connection.execute(OLD_EPISODE_PITCHES)
        connection.execute("INSERT INTO episode_pitches VALUES ('a', 1, 'pitch-b', 0)")
        forget_schema_version(connection)
    with pytest.raises(MigrationError, match="episode_pitches"):
        Database(database.path)
    with sqlite3.connect(database.path) as connection:
        kept = connection.execute("SELECT pitch_id FROM episode_pitches").fetchall()
    assert kept == [("pitch-b",)]
