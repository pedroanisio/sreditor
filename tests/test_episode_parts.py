"""The counts on an episode's tabs: sources, media files and waivers, waivers only for readers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sreditor.database import Database
from sreditor.episode_operations import count_episode_parts
from sreditor.episodes import EpisodeStore, PartCounts
from sreditor.members import Permission, Role, allows

CHANNEL = "funny-memes"
SEED = (
    (
        "INSERT INTO channels (slug, document, created_at, updated_at)"
        " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
    ),
    (
        "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
        " VALUES ('funny-memes', 1, 'Pilot', 'idea', '2026-01-01', '2026-01-01')"
    ),
    (
        "INSERT INTO episode_waivers (position, id, channel_slug, number, signer, covers, media_type,"
        " size_bytes, sha256, revision, created_at, updated_at) VALUES (1, 'w1', 'funny-memes', 1,"
        " 'Bia', '', 'application/pdf', 1, 'abc', 1, '2026-01-01', '2026-01-01')"
    ),
)


@pytest.fixture
def store(tmp_path: Path) -> EpisodeStore:
    """Episode 1 of funny-memes with one waiver and nothing else."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        for statement in SEED:
            connection.execute(statement)
    return EpisodeStore(database)


def counted(store: EpisodeStore, role: Role) -> PartCounts:
    """The counts as a member with `role` sees them."""
    access = SimpleNamespace(
        channel=SimpleNamespace(slug=CHANNEL),
        require=lambda permission: None,
        can=lambda permission: allows(role, permission),
    )
    app = SimpleNamespace(episodes=store)
    return count_episode_parts(cast(Any, app), cast(Any, access), 1)


def test_part_counts_counts_each_listed_part(store: EpisodeStore) -> None:
    assert store.part_counts(CHANNEL, 1) == PartCounts(sources=0, media=0, waivers=1, uses=0)


def test_count_episode_parts_editor_sees_waivers(store: EpisodeStore) -> None:
    assert allows(Role.EDITOR, Permission.READ_WAIVERS)
    assert counted(store, Role.EDITOR).waivers == 1


def test_count_episode_parts_contributor_sees_no_waivers(store: EpisodeStore) -> None:
    assert counted(store, Role.CONTRIBUTOR).waivers == 0
