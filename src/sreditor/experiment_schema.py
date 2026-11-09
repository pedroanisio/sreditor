"""The table of A/B experiments (migration 4; rule project.experiments).

An experiment links two episodes of one channel, so both foreign keys name the channel and the
database refuses a pair across channels. The foreign keys to the episodes do not cascade: an
episode that an experiment compares cannot be deleted until the experiment goes, as a pitch that
episodes use cannot (the channel's own deletion still removes both, since the experiment
cascades from the channel and SQLite checks a no-action key once the statement is done).
"""

from __future__ import annotations

import sqlite3

from sreditor.migrations import Migration, execute_script

EXPERIMENTS_VERSION = 5

EXPERIMENTS_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    title TEXT NOT NULL,
    episode_a INTEGER NOT NULL,
    episode_b INTEGER NOT NULL,
    change TEXT NOT NULL DEFAULT '',
    hypothesis TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL,
    snapshot_age TEXT NOT NULL,
    platform TEXT,
    status TEXT NOT NULL,
    started_on TEXT,
    reading TEXT NOT NULL DEFAULT '',
    verdict TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (episode_a <> episode_b),
    FOREIGN KEY (channel_slug, episode_a) REFERENCES episodes (channel_slug, number),
    FOREIGN KEY (channel_slug, episode_b) REFERENCES episodes (channel_slug, number)
);
CREATE INDEX IF NOT EXISTS experiments_by_channel ON experiments (channel_slug, position);
CREATE INDEX IF NOT EXISTS experiments_by_episode_a ON experiments (channel_slug, episode_a);
CREATE INDEX IF NOT EXISTS experiments_by_episode_b ON experiments (channel_slug, episode_b);
"""


def create_experiments(connection: sqlite3.Connection) -> None:
    """Migration 4: create the experiments table; idempotent, so a rerun changes nothing."""
    execute_script(connection, EXPERIMENTS_SQL)


EXPERIMENTS = Migration(
    version=EXPERIMENTS_VERSION, name="A/B experiments", apply=create_experiments
)
