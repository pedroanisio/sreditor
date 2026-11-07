from __future__ import annotations

from pathlib import Path

import pytest

from sreditor.channels import Channel
from sreditor.database import Database
from sreditor.repository import ChannelExists, ChannelNotFound, ChannelRepository


@pytest.fixture
def repository(tmp_path: Path) -> ChannelRepository:
    return ChannelRepository(Database(tmp_path / "nested" / "channels.sqlite"))


@pytest.fixture
def channel_alpha() -> Channel:
    return Channel(slug="a", name="A")


@pytest.fixture
def stocked_repository(repository: ChannelRepository, channel_alpha: Channel) -> ChannelRepository:
    repository.create(Channel(slug="b", name="B"))
    repository.create(channel_alpha)
    return repository


def test_get_created_channel_returns_it(
    stocked_repository: ChannelRepository, channel_alpha: Channel
) -> None:
    assert stocked_repository.get("a") == channel_alpha


def test_list_created_channels_orders_by_slug(stocked_repository: ChannelRepository) -> None:
    assert [channel.slug for channel in stocked_repository.list()] == ["a", "b"]


def test_replace_existing_channel_updates_name(
    stocked_repository: ChannelRepository, channel_alpha: Channel
) -> None:
    stocked_repository.replace(channel_alpha.model_copy(update={"name": "A2"}))
    assert stocked_repository.get("a").name == "A2"


def test_delete_existing_channel_removes_it_from_list(
    stocked_repository: ChannelRepository,
) -> None:
    stocked_repository.delete("a")
    assert [channel.slug for channel in stocked_repository.list()] == ["b"]


def test_create_duplicate_slug_raises_channel_exists(repository: ChannelRepository) -> None:
    repository.create(Channel(slug="a", name="A"))
    with pytest.raises(ChannelExists):
        repository.create(Channel(slug="a", name="Other"))


@pytest.mark.parametrize("operation", ["get", "replace", "delete"])
def test_operation_on_missing_channel_raises_channel_not_found(
    repository: ChannelRepository, operation: str
) -> None:
    with pytest.raises(ChannelNotFound):
        if operation == "replace":
            repository.replace(Channel(slug="nope", name="N"))
        else:
            getattr(repository, operation)("nope")


def test_get_after_reopening_database_returns_stored_channel(tmp_path: Path) -> None:
    ChannelRepository(Database(tmp_path / "c.sqlite")).create(Channel(slug="a", name="A"))
    assert ChannelRepository(Database(tmp_path / "c.sqlite")).get("a").name == "A"


def test_list_with_selected_slugs_returns_only_those_ordered_by_slug(
    repository: ChannelRepository,
) -> None:
    for slug in ("a", "b", "c"):
        repository.create(Channel(slug=slug, name=slug.upper()))
    assert [channel.slug for channel in repository.list(["c", "a"])] == ["a", "c"]


def test_list_with_empty_slugs_returns_nothing(repository: ChannelRepository) -> None:
    repository.create(Channel(slug="a", name="A"))
    assert repository.list([]) == []
