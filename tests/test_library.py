"""The channel library of characters and locations: store, API, cast and MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.library import EntryExists, EntryInput, LibraryKind, LibraryStore, name_key
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

CHARACTERS = "/channels/funny-memes/characters"
LOCATIONS = "/channels/funny-memes/locations"
ANA = {"name": "Ana", "description": "The host."}
PILOT = "INT. KITCHEN - DAY\n\nANA\nIs it Monday again?\n\nBRUNO\nYes.\n"


@pytest.fixture
def library(tmp_path: Path) -> LibraryStore:
    """A store over a database with the funny-memes channel."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    return LibraryStore(database)


def character(library: LibraryStore, name: str = "Ana") -> Any:
    return library.create("funny-memes", LibraryKind.CHARACTER, EntryInput(name=name))


# Store


def test_name_key_ignores_case_and_extra_spaces() -> None:
    assert name_key("  Dona   MARIA ") == name_key("dona maria")


def test_library_create_same_name_other_case_raises_exists(library: LibraryStore) -> None:
    character(library)
    with pytest.raises(EntryExists):
        character(library, "ANA")


def test_library_same_name_as_character_and_location_is_allowed(library: LibraryStore) -> None:
    character(library, "Kitchen")
    place = library.create("funny-memes", LibraryKind.LOCATION, EntryInput(name="Kitchen"))
    assert place.kind == LibraryKind.LOCATION


def test_library_rename_onto_taken_name_raises_exists(library: LibraryStore) -> None:
    character(library)
    bruno = character(library, "Bruno")
    with pytest.raises(EntryExists):
        library.replace(bruno, EntryInput(name="ana"), None)


def test_library_replace_stale_revision_raises_changed(library: LibraryStore) -> None:
    with pytest.raises(PartChanged):
        library.replace(character(library), EntryInput(name="Ana"), 7)


def test_library_delete_stale_revision_raises_changed(library: LibraryStore) -> None:
    with pytest.raises(PartChanged):
        library.delete(character(library), 7)


def test_library_named_matches_screenplay_capitals(library: LibraryStore) -> None:
    ana = character(library)
    assert library.named("funny-memes", LibraryKind.CHARACTER, ["ANA", "BRUNO"]) == {"ana": ana}


def test_library_named_without_names_is_empty(library: LibraryStore) -> None:
    assert library.named("funny-memes", LibraryKind.CHARACTER, []) == {}


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


@pytest.fixture
def ana(owner: TestClient) -> dict[str, Any]:
    response = owner.post(CHARACTERS, json=ANA)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_library_character_add_returns_entry(ana: dict[str, Any]) -> None:
    assert ana["kind"] == "character" and ana["revision"] == 1


def test_library_character_add_taken_name_returns_conflict(
    owner: TestClient, ana: dict[str, Any]
) -> None:
    response = owner.post(CHARACTERS, json={"name": "ANA"})
    assert response.status_code == 409 and response.json()["type"].endswith("entry-exists")


def test_library_character_add_empty_name_returns_unprocessable(owner: TestClient) -> None:
    assert owner.post(CHARACTERS, json={"name": "  "}).status_code == 422


def test_library_characters_list_paginates(owner: TestClient, ana: dict[str, Any]) -> None:
    owner.post(CHARACTERS, json={"name": "Bruno"})
    first = owner.get(CHARACTERS, params={"limit": 1}).json()
    second = owner.get(CHARACTERS, params={"cursor": first["next_cursor"]}).json()
    assert [item["name"] for item in second["items"]] == ["Bruno"]


def test_library_locations_list_leaves_characters_out(
    owner: TestClient, ana: dict[str, Any]
) -> None:
    assert owner.get(LOCATIONS).json()["items"] == []


def test_library_character_get_returns_etag(owner: TestClient, ana: dict[str, Any]) -> None:
    assert owner.get(f"{CHARACTERS}/{ana['id']}").headers["etag"] == '"1"'


def test_library_character_get_as_location_returns_not_found(
    owner: TestClient, ana: dict[str, Any]
) -> None:
    response = owner.get(f"{LOCATIONS}/{ana['id']}")
    assert response.status_code == 404 and response.json()["detail"].startswith("Library entry")


def test_library_character_put_returns_next_etag(owner: TestClient, ana: dict[str, Any]) -> None:
    body = {"name": "Ana", "description": "The host, tired."}
    response = owner.put(f"{CHARACTERS}/{ana['id']}", json=body, headers={"If-Match": '"1"'})
    assert (
        response.headers["etag"] == '"2"' and response.json()["description"] == body["description"]
    )


def test_library_character_put_stale_etag_returns_precondition_failed(
    owner: TestClient, ana: dict[str, Any]
) -> None:
    response = owner.put(f"{CHARACTERS}/{ana['id']}", json=ANA, headers={"If-Match": '"5"'})
    assert response.status_code == 412


def test_library_character_delete_returns_no_content(
    owner: TestClient, ana: dict[str, Any]
) -> None:
    assert owner.delete(f"{CHARACTERS}/{ana['id']}").status_code == 204
    assert owner.get(CHARACTERS).json()["items"] == []


def test_library_character_add_by_contributor_is_allowed_but_delete_forbidden(
    owner: TestClient,
    ana: dict[str, Any],
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.post(CHARACTERS, json={"name": "Bruno"}).status_code == 201
    assert contributor.delete(f"{CHARACTERS}/{ana['id']}").status_code == 403


def test_library_character_add_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(CHARACTERS, json=ANA).status_code == 403


def test_library_add_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "location.create")
    response = agent(app, new_token(owner)).post(LOCATIONS, json={"name": "Kitchen"})
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


def test_library_add_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "ana-1"}
    owner.post(CHARACTERS, json=ANA, headers=headers)
    replay = owner.post(CHARACTERS, json=ANA, headers=headers)
    assert replay.headers["idempotent-replayed"] == "true"


def test_library_entries_go_with_their_channel(owner: TestClient, ana: dict[str, Any]) -> None:
    owner.delete("/channels/funny-memes")
    owner.post("/channels", json=FUNNY_MEMES)
    assert owner.get(CHARACTERS).json()["items"] == []


# Cast


@pytest.fixture
def written(owner: TestClient, ana: dict[str, Any]) -> TestClient:
    owner.post("/channels/funny-memes/episodes", json={"title": "Pilot"})
    owner.put("/channels/funny-memes/episodes/1/screenplay", json={"text": PILOT})
    return owner


def test_episode_cast_matches_library_by_name(written: TestClient) -> None:
    cast = written.get("/channels/funny-memes/episodes/1/cast").json()
    found = {member["name"]: member["entry"] for member in cast["characters"]}
    assert found["ANA"]["name"] == "Ana" and found["BRUNO"] is None


def test_episode_cast_lists_unmatched_locations(written: TestClient) -> None:
    cast = written.get("/channels/funny-memes/episodes/1/cast").json()
    assert cast["locations"] == [{"name": "KITCHEN", "entry": None}]


def test_episode_cast_of_missing_episode_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/funny-memes/episodes/4/cast").status_code == 404


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_save_location_then_list_returns_it(mcp: TestClient) -> None:
    entry = {"slug": "funny-memes", "kind": "location", "entry": {"name": "Kitchen"}}
    tool(mcp, "save_library_entry", entry)
    listed = tool(mcp, "list_library", {"slug": "funny-memes", "kind": "location"})
    assert [item["name"] for item in listed["structuredContent"]["items"]] == ["Kitchen"]


def test_mcp_save_with_entry_id_renames_it(mcp: TestClient) -> None:
    entry = {"slug": "funny-memes", "kind": "character", "entry": ANA}
    added = tool(mcp, "save_library_entry", entry)
    change = {"name": "Ana Maria", "entry_id": added["structuredContent"]["id"]}
    arguments = {**entry, "entry": {**change, "expected_revision": 1}}
    assert tool(mcp, "save_library_entry", arguments)["structuredContent"]["name"] == "Ana Maria"


def test_mcp_remove_library_entry_returns_what_it_removed(mcp: TestClient) -> None:
    entry = {"slug": "funny-memes", "kind": "character", "entry": ANA}
    entry_id = tool(mcp, "save_library_entry", entry)["structuredContent"]["id"]
    arguments = {"slug": "funny-memes", "kind": "character", "entry_id": entry_id}
    removed = tool(mcp, "remove_library_entry", arguments)["structuredContent"]
    assert removed == {"kind": "character", "id": entry_id}


def test_mcp_get_screenplay_names_missing_characters(mcp: TestClient, written: TestClient) -> None:
    found = tool(mcp, "get_screenplay", {"slug": "funny-memes", "number": 1})
    cast = found["structuredContent"]["cast"]["characters"]
    assert cast[0]["entry"] is not None and cast[1]["entry"] is None


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "save_library_entry",
            {"slug": "funny-memes", "kind": "character", "entry": {"name": "ana"}},
            "A character named ana is already in the library; call list_library",
        ),
        (
            "remove_library_entry",
            {"slug": "funny-memes", "kind": "location", "entry_id": str(uuid4())},
            "not found; call list_library",
        ),
    ],
)
def test_mcp_refused_library_call_says_how_to_fix_it(
    mcp: TestClient, ana: dict[str, Any], name: str, arguments: dict[str, Any], message: str
) -> None:
    result = tool(mcp, name, arguments)
    assert result["isError"] and message in result["content"][0]["text"]
