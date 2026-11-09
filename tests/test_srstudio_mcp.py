"""MCP tools for srstudio sheets, episode slugs and relationships (rules project.srstudio.*, agentic.mcp)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.library import LibraryEntry, LibraryKind
from sreditor.srstudio import (
    EntrySheet,
    EpisodeSlug,
    ExportedSheet,
    RelationshipNotFound,
    SameCharacter,
    SheetMismatch,
    SlugLocked,
    SlugTaken,
)
from sreditor.srstudio_operations import FactNotPublished
from sreditor.srstudio_tools import srstudio_error_message
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES

SLUG = "funny-memes"
CHANNEL = f"/channels/{SLUG}"
DOG = {"kind": "character", "world": "okayville", "character_kind": "animal", "rig": "toon-dog"}
OFFICE = {"kind": "location", "world": "okayville", "rig": "office"}
LAPTOP = {"kind": "prop", "rig": "laptop"}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Whose Milk"}).status_code == 201
    return client


def add_entry(owner: TestClient, plural: str, name: str) -> str:
    response = owner.post(f"{CHANNEL}/{plural}", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
def rex(owner: TestClient) -> str:
    return add_entry(owner, "characters", "Rex")


@pytest.fixture
def fern(owner: TestClient) -> str:
    return add_entry(owner, "characters", "Fern")


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def text_of(done: dict[str, Any]) -> str:
    return str(done["content"][0]["text"])


def save_sheet(client: TestClient, entry_id: str, **arguments: Any) -> dict[str, Any]:
    return tool(client, "save_library_sheet", {"slug": SLUG, "entry_id": entry_id, **arguments})


def save_relationship(client: TestClient, **arguments: Any) -> dict[str, Any]:
    return tool(client, "save_relationship", {"slug": SLUG, **arguments})


def pair(first: str, second: str, **fields: Any) -> dict[str, Any]:
    return {"first": first, "second": second, "relation": "housemates", **fields}


def publish_first_episode(owner: TestClient) -> None:
    response = owner.put(
        f"{CHANNEL}/episodes/1", json={"title": "Whose Milk", "status": "published"}
    )
    assert response.status_code == 200, response.text


# Library sheets


def test_get_library_sheet_unsaved_entry_returns_proposal(mcp: TestClient, rex: str) -> None:
    done = tool(mcp, "get_library_sheet", {"slug": SLUG, "kind": "character", "entry_id": rex})[
        "structuredContent"
    ]
    assert (done["entry"]["name"], done["sheet"]["slug"], done["sheet"]["revision"]) == (
        "Rex",
        "rex",
        0,
    )


def test_save_library_sheet_character_saves_world_and_kind(mcp: TestClient, rex: str) -> None:
    saved = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG}, expected_revision=0)
    sheet = saved["structuredContent"]["sheet"]
    assert (sheet["revision"], sheet["sheet"]["world"], sheet["sheet"]["character_kind"]) == (
        1,
        "okayville",
        "animal",
    )


def test_save_library_sheet_location_saves_world(mcp: TestClient, owner: TestClient) -> None:
    office = add_entry(owner, "locations", "Office")
    saved = save_sheet(mcp, office, sheet={"slug": "office", "sheet": OFFICE})
    assert saved["structuredContent"]["sheet"]["sheet"]["world"] == "okayville"


def test_save_library_sheet_prop_saves_rig(mcp: TestClient, owner: TestClient) -> None:
    laptop = add_entry(owner, "props", "Laptop")
    saved = save_sheet(mcp, laptop, sheet={"slug": "laptop", "sheet": LAPTOP})
    assert saved["structuredContent"]["sheet"]["sheet"]["rig"] == "laptop"


def test_get_library_sheet_after_save_returns_saved_rig(mcp: TestClient, rex: str) -> None:
    save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG})
    done = tool(mcp, "get_library_sheet", {"slug": SLUG, "kind": "character", "entry_id": rex})
    assert done["structuredContent"]["sheet"]["sheet"]["rig"] == "toon-dog"


def test_save_library_sheet_stale_revision_names_the_fix(mcp: TestClient, rex: str) -> None:
    save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG})
    stale = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG}, expected_revision=0)
    assert stale["isError"] and "expected_revision=1" in text_of(stale)


def test_save_library_sheet_taken_slug_names_sheet_slug(
    mcp: TestClient, rex: str, fern: str
) -> None:
    save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG})
    taken = save_sheet(mcp, fern, sheet={"slug": "rex", "sheet": DOG})
    assert taken["isError"] and "choose another sheet_slug" in text_of(taken)


def test_save_library_sheet_locked_slug_names_the_current_slug(
    app: FastAPI, mcp: TestClient, rex: str
) -> None:
    saved = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": DOG})["structuredContent"]
    lock_sheet(app, saved, LibraryKind.CHARACTER)
    locked = save_sheet(mcp, rex, sheet={"slug": "rexy", "sheet": DOG})
    assert locked["isError"] and "Send rex as sheet_slug" in text_of(locked)


def lock_sheet(app: FastAPI, saved: dict[str, Any], kind: LibraryKind) -> None:
    """Mark the saved sheet as carried by an srstudio export."""
    services = app.state.services
    entry: LibraryEntry = services.library.get(SLUG, kind, saved["entry"]["id"])
    sheet = EntrySheet.model_validate(saved["sheet"])
    services.srstudio.mark_exported([], [ExportedSheet(entry=entry, sheet=sheet)])


def test_save_library_sheet_unpublished_fact_names_list_episodes(mcp: TestClient, rex: str) -> None:
    canon = {
        "fact": "Rex labels his milk.",
        "field": "identity",
        "episode": 1,
        "kind": "line",
        "quote": "Mine.",
    }
    done = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": {**DOG, "canon": [canon]}})
    assert done["isError"] and "list_episodes" in text_of(done)


def test_save_library_sheet_unknown_toon_rig_lists_the_toon_rigs(mcp: TestClient, rex: str) -> None:
    done = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": {**DOG, "rig": "toon-host"}})
    assert done["isError"] and "toon-dog" in text_of(done) and "toon-host" in text_of(done)


def test_save_library_sheet_kind_of_another_entry_names_list_library(
    mcp: TestClient, rex: str
) -> None:
    done = save_sheet(mcp, rex, sheet={"slug": "rex", "sheet": OFFICE})
    assert done["isError"] and "list_library with kind location" in text_of(done)


def test_get_library_sheet_unknown_entry_names_list_library(mcp: TestClient) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    done = tool(mcp, "get_library_sheet", {"slug": SLUG, "kind": "prop", "entry_id": missing})
    assert done["isError"] and "list_library with kind prop" in text_of(done)


def test_save_library_sheet_token_without_content_write_names_the_scope(
    app: FastAPI, owner: TestClient, rex: str
) -> None:
    secret = new_token(owner, ["channels:read"])
    with agent(app, secret, PUBLIC_URL) as reader:
        done = save_sheet(reader, rex, sheet={"slug": "rex", "sheet": DOG})
    assert done["isError"] and "content:write" in text_of(done)


def test_get_library_sheet_token_with_channels_read_reads(
    app: FastAPI, owner: TestClient, rex: str
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        done = tool(
            reader, "get_library_sheet", {"slug": SLUG, "kind": "character", "entry_id": rex}
        )
    assert not done.get("isError")


# Episode slug and seed


def test_set_episode_slug_saves_slug_and_seed(mcp: TestClient) -> None:
    done = tool(
        mcp,
        "set_episode_slug",
        {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "whose-milk", "seed": 42}},
    )["structuredContent"]
    assert (done["slug"], done["seed"], done["revision"]) == ("whose-milk", 42, 1)


def test_set_episode_slug_shows_in_export_check(mcp: TestClient) -> None:
    arguments = {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "milk", "seed": 7}}
    tool(mcp, "set_episode_slug", arguments)
    checked = tool(mcp, "check_srstudio_export", {"slug": SLUG, "number": 1})["structuredContent"]
    assert (checked["slug"], checked["seed"]) == ("milk", 7)


def test_set_episode_slug_stale_revision_names_the_fix(mcp: TestClient) -> None:
    arguments = {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "milk", "seed": 7}}
    tool(mcp, "set_episode_slug", arguments)
    stale = tool(mcp, "set_episode_slug", {**arguments, "expected_revision": 0})
    assert stale["isError"] and "expected_revision=1" in text_of(stale)


def test_set_episode_slug_taken_slug_names_the_field(mcp: TestClient, owner: TestClient) -> None:
    owner.post(f"{CHANNEL}/episodes", json={"title": "Second"})
    one = {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "milk", "seed": 1}}
    tool(mcp, "set_episode_slug", one)
    two = tool(mcp, "set_episode_slug", {**one, "number": 2})
    assert two["isError"] and "choose another srstudio_slug.slug" in text_of(two)


def test_set_episode_slug_locked_slug_is_refused_with_the_current_one(
    app: FastAPI, mcp: TestClient
) -> None:
    arguments = {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "milk", "seed": 1}}
    saved = tool(mcp, "set_episode_slug", arguments)["structuredContent"]
    app.state.services.srstudio.mark_exported([EpisodeSlug.model_validate(saved)], [])
    locked = tool(
        mcp, "set_episode_slug", {**arguments, "srstudio_slug": {"slug": "other", "seed": 1}}
    )
    assert locked["isError"] and "Send milk as srstudio_slug.slug" in text_of(locked)


def test_set_episode_slug_unknown_episode_names_list_episodes(mcp: TestClient) -> None:
    done = tool(
        mcp,
        "set_episode_slug",
        {"slug": SLUG, "number": 9, "srstudio_slug": {"slug": "milk", "seed": 1}},
    )
    assert done["isError"] and "list_episodes" in text_of(done)


def test_set_episode_slug_token_without_content_write_names_the_scope(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        done = tool(
            reader,
            "set_episode_slug",
            {"slug": SLUG, "number": 1, "srstudio_slug": {"slug": "milk", "seed": 1}},
        )
    assert done["isError"] and "content:write" in text_of(done)


# Relationships


def test_save_relationship_new_pair_is_listed(mcp: TestClient, rex: str, fern: str) -> None:
    saved = save_relationship(mcp, relationship=pair(rex, fern))["structuredContent"]
    listed = tool(mcp, "list_relationships", {"slug": SLUG})["structuredContent"]
    assert [item["id"] for item in listed["items"]] == [saved["id"]]


def test_save_relationship_existing_id_replaces_it(mcp: TestClient, rex: str, fern: str) -> None:
    first = save_relationship(mcp, relationship=pair(rex, fern))["structuredContent"]
    change = pair(
        rex,
        fern,
        relation="rivals",
        relationship_id=first["id"],
        expected_revision=first["revision"],
    )
    saved = save_relationship(mcp, relationship=change)["structuredContent"]
    assert (saved["id"], saved["relation"], saved["revision"]) == (
        first["id"],
        "rivals",
        first["revision"] + 1,
    )


def test_save_relationship_same_idempotency_key_records_one(
    mcp: TestClient, rex: str, fern: str
) -> None:
    save_relationship(mcp, relationship=pair(rex, fern), idempotency_key="pair-1")
    again = save_relationship(mcp, relationship=pair(rex, fern), idempotency_key="pair-1")
    listed = tool(mcp, "list_relationships", {"slug": SLUG})["structuredContent"]
    assert len(listed["items"]) == 1 and again["_meta"]["sreditor/replayed"] is True


def test_save_relationship_same_character_twice_names_list_library(
    mcp: TestClient, rex: str
) -> None:
    done = save_relationship(mcp, relationship=pair(rex, rex))
    assert done["isError"] and "two different ids" in text_of(done)


def test_save_relationship_unknown_character_names_list_library(mcp: TestClient, rex: str) -> None:
    done = save_relationship(mcp, relationship=pair(rex, "00000000-0000-4000-8000-000000000000"))
    assert done["isError"] and "list_library" in text_of(done)


def test_save_relationship_unpublished_history_names_list_episodes(
    mcp: TestClient, rex: str, fern: str
) -> None:
    history = [
        {"fact": "They met.", "field": "identity", "episode": 1, "kind": "line", "quote": "Hi."}
    ]
    done = save_relationship(mcp, relationship=pair(rex, fern, history=history))
    assert done["isError"] and "list_episodes" in text_of(done)


def test_save_relationship_published_history_is_kept(
    mcp: TestClient, owner: TestClient, rex: str, fern: str
) -> None:
    publish_first_episode(owner)
    history = [
        {"fact": "They met.", "field": "identity", "episode": 1, "kind": "line", "quote": "Hi."}
    ]
    saved = save_relationship(mcp, relationship=pair(rex, fern, history=history))
    assert saved["structuredContent"]["history"][0]["episode"] == 1


def test_save_relationship_stale_revision_names_the_fix(
    mcp: TestClient, rex: str, fern: str
) -> None:
    first = save_relationship(mcp, relationship=pair(rex, fern))["structuredContent"]
    change = {**pair(rex, fern), "relationship_id": first["id"], "expected_revision": 0}
    stale = save_relationship(mcp, relationship=change)
    assert stale["isError"] and "expected_revision=1" in text_of(stale)


def test_save_relationship_token_without_content_write_names_the_scope(
    app: FastAPI, owner: TestClient, rex: str, fern: str
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        done = save_relationship(reader, relationship=pair(rex, fern))
    assert done["isError"] and "content:write" in text_of(done)


def test_list_relationships_pages_in_recorded_order(mcp: TestClient, rex: str, fern: str) -> None:
    ids = [
        save_relationship(mcp, relationship=pair(rex, fern, relation=name))["structuredContent"][
            "id"
        ]
        for name in ("a", "b", "c")
    ]
    first = tool(mcp, "list_relationships", {"slug": SLUG, "limit": 2})["structuredContent"]
    rest = tool(
        mcp, "list_relationships", {"slug": SLUG, "limit": 2, "cursor": first["next_cursor"]}
    )["structuredContent"]
    assert [item["id"] for item in first["items"] + rest["items"]] == ids
    assert rest["next_cursor"] is None


def test_list_relationships_invalid_cursor_is_a_tool_error(mcp: TestClient) -> None:
    done = tool(mcp, "list_relationships", {"slug": SLUG, "cursor": "bm9wZQ"})
    assert done["isError"] and "Invalid cursor" in text_of(done)


def test_remove_relationship_removes_it_and_keeps_the_characters(
    mcp: TestClient, rex: str, fern: str
) -> None:
    saved = save_relationship(mcp, relationship=pair(rex, fern))["structuredContent"]
    tool(mcp, "remove_relationship", {"slug": SLUG, "relationship_id": saved["id"]})
    listed = tool(mcp, "list_relationships", {"slug": SLUG})["structuredContent"]
    assert listed["items"] == []


def test_remove_relationship_unknown_id_names_list_relationships(mcp: TestClient) -> None:
    done = tool(
        mcp,
        "remove_relationship",
        {"slug": SLUG, "relationship_id": "00000000-0000-4000-8000-000000000000"},
    )
    assert done["isError"] and "list_relationships" in text_of(done)


def test_remove_relationship_token_without_channels_write_names_the_scope(
    app: FastAPI, owner: TestClient, rex: str, fern: str
) -> None:
    created = owner.post(f"{CHANNEL}/relationships", json=pair(rex, fern))
    assert created.status_code == 201, created.text
    with agent(app, new_token(owner, ["channels:read", "content:write"]), PUBLIC_URL) as writer:
        done = tool(
            writer, "remove_relationship", {"slug": SLUG, "relationship_id": created.json()["id"]}
        )
    assert done["isError"] and "channels:write" in text_of(done)


# Visual style tool errors


def test_save_visual_style_locked_slug_names_the_field(
    app: FastAPI, mcp: TestClient, owner: TestClient
) -> None:
    style = add_entry(owner, "styles", "Office")
    saved = tool(
        mcp,
        "save_visual_style",
        {"slug": SLUG, "style_id": style, "data": {"style": "toon", "style_slug": "office"}},
    )["structuredContent"]
    lock_sheet(app, saved, LibraryKind.STYLE)
    locked = tool(
        mcp,
        "save_visual_style",
        {"slug": SLUG, "style_id": style, "data": {"style": "toon", "style_slug": "other"}},
    )
    assert locked["isError"] and "Send office as data.style_slug" in text_of(locked)


# Error messages that no tool call can reach


def test_srstudio_error_message_sheet_mismatch_names_the_expected_kind() -> None:
    message = srstudio_error_message(
        SheetMismatch(LibraryKind.CHARACTER, LibraryKind.LOCATION), "sheet_slug"
    )
    assert message is not None and "list_library with kind character" in message


def test_srstudio_error_message_other_error_returns_none() -> None:
    assert srstudio_error_message(ValueError("x"), "sheet_slug") is None


@pytest.mark.parametrize(
    ("error", "fix"),
    [
        (SlugTaken("rex"), "choose another sheet_slug"),
        (SlugLocked("rex"), "Send rex as sheet_slug"),
        (FactNotPublished(3), "list_episodes"),
        (RelationshipNotFound("abc"), "list_relationships"),
        (SameCharacter("abc"), "two different ids"),
    ],
)
def test_srstudio_error_message_refusal_names_its_fix(error: Exception, fix: str) -> None:
    assert fix in (srstudio_error_message(error, "sheet_slug") or "")
