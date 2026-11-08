"""Slugs fixed by the first srstudio export (rule project.srstudio.slugs)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.deps import Services
from sreditor.srstudio import EpisodeSlugInput, SlugLocked, SrstudioStore
from tests.conftest import forget_schema_version
from tests.test_api import FUNNY_MEMES

CHANNEL = "funny-memes"
START = datetime(2026, 11, 7, 12, tzinfo=UTC)


class Ticking:
    """A clock that moves an hour each time it is read, so stored moments differ."""

    def __init__(self) -> None:
        self.moment = START

    def __call__(self) -> datetime:
        self.moment += timedelta(hours=1)
        return self.moment


@pytest.fixture
def store(app: FastAPI, sign_up: Callable[..., TestClient]) -> SrstudioStore:
    """The srstudio store of a channel with two episodes, on a clock that ticks."""
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    for title in ("Whose Milk", "Second One"):
        assert (
            client.post(f"/channels/{CHANNEL}/episodes", json={"title": title}).status_code == 201
        )
    services: Services = app.state.services
    return SrstudioStore(Database(services.srstudio.database.path, Ticking()))


def test_database_from_before_exports_gains_exported_at(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE episode_slugs DROP COLUMN exported_at")
        forget_schema_version(connection)
    Database(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(episode_slugs)")}
    assert "exported_at" in columns


def test_mark_exported_twice_keeps_first_moment(store: SrstudioStore) -> None:
    store.mark_exported([store.episode_slug(CHANNEL, 1, "Whose Milk")], [])
    first = store.episode_slug(CHANNEL, 1, "Whose Milk").exported_at
    store.mark_exported([store.episode_slug(CHANNEL, 1, "Whose Milk")], [])
    assert first is not None and store.episode_slug(CHANNEL, 1, "Whose Milk").exported_at == first


def test_mark_exported_proposal_whose_slug_is_taken_stays_proposal(store: SrstudioStore) -> None:
    second = store.episode_slug(CHANNEL, 2, "Second One")
    store.save_episode_slug(second, EpisodeSlugInput(slug="whose-milk", seed=2), 0)
    store.mark_exported([store.episode_slug(CHANNEL, 1, "Whose Milk")], [])
    assert store.episode_slug(CHANNEL, 1, "Whose Milk").revision == 0


def test_save_episode_slug_locked_refuses_other_slug(store: SrstudioStore) -> None:
    store.mark_exported([store.episode_slug(CHANNEL, 1, "Whose Milk")], [])
    locked = store.episode_slug(CHANNEL, 1, "Whose Milk")
    with pytest.raises(SlugLocked):
        store.save_episode_slug(locked, EpisodeSlugInput(slug="milk-heist", seed=1), 1)
