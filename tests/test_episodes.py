"""Episodes: the store, the API, the stmt gate and the MCP tools (rule project.episodes)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.episodes import EpisodeChanged, EpisodeInput, EpisodeNotFound, EpisodeStore
from sreditor.pagination import encode_cursor
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES

EPISODES = "/channels/funny-memes/episodes"
PILOT = {"title": "Pilot", "summary": "Why Mondays hurt."}


@pytest.fixture
def episode_store(tmp_path: Path) -> EpisodeStore:
    """A store over a database with the channel funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    return EpisodeStore(database)


def add(store: EpisodeStore, title: str = "Pilot") -> int:
    return store.create("funny-memes", EpisodeInput(title=title)).episode.number


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


@pytest.fixture
def pilot(owner: TestClient) -> dict[str, Any]:
    response = owner.post(EPISODES, json=PILOT)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def joined(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer, role: str
) -> TestClient:
    """A member of funny-memes with a role, signed in through the API."""
    email = f"{role}@example.com"
    client = sign_up(email)
    owner.post("/channels/funny-memes/invitations", json={"email": email, "role": role})
    accepted = client.post("/invitations/accept", json={"token": mailer.token_for(email)})
    assert accepted.status_code == 200, accepted.text
    return client


# Store


def test_episode_store_first_episode_is_number_one(episode_store: EpisodeStore) -> None:
    assert add(episode_store) == 1


def test_episode_store_after_deleting_latest_never_reuses_its_number(
    episode_store: EpisodeStore,
) -> None:
    add(episode_store)
    episode_store.delete("funny-memes", add(episode_store), None)
    assert add(episode_store) == 3


def test_episode_store_new_episode_starts_as_idea(episode_store: EpisodeStore) -> None:
    created = episode_store.create("funny-memes", EpisodeInput(title="Pilot"))
    assert created.episode.status == "idea" and created.revision == 1


def test_episode_store_list_after_number_returns_later_ones(episode_store: EpisodeStore) -> None:
    for title in ("One", "Two", "Three"):
        add(episode_store, title)
    listed = episode_store.list("funny-memes", after=1, limit=1)
    assert [episode.title for episode in listed] == ["Two"]


def test_episode_store_replace_bumps_revision(episode_store: EpisodeStore) -> None:
    number = add(episode_store)
    data = EpisodeInput(title="Pilot", status="script")
    assert episode_store.replace("funny-memes", number, data, 1).revision == 2


def test_episode_store_replace_stale_revision_raises_changed(episode_store: EpisodeStore) -> None:
    number = add(episode_store)
    with pytest.raises(EpisodeChanged) as changed:
        episode_store.replace("funny-memes", number, EpisodeInput(title="New"), 7)
    assert changed.value.current == 1


def test_episode_store_delete_missing_raises_not_found(episode_store: EpisodeStore) -> None:
    with pytest.raises(EpisodeNotFound):
        episode_store.delete("funny-memes", 4, None)


def test_episode_store_count_counts_the_channel_episodes(episode_store: EpisodeStore) -> None:
    add(episode_store)
    add(episode_store)
    assert episode_store.count("funny-memes") == 2


def test_episode_input_blank_title_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        EpisodeInput(title="   ")


# API


def test_episode_create_returns_number_and_idea_status(pilot: dict[str, Any]) -> None:
    assert pilot["number"] == 1 and pilot["status"] == "idea"
    assert pilot["channel"] == "funny-memes"


def test_episode_list_returns_episodes_by_number(owner: TestClient, pilot: dict[str, Any]) -> None:
    owner.post(EPISODES, json={"title": "Second"})
    titles = [item["title"] for item in owner.get(EPISODES).json()["items"]]
    assert titles == ["Pilot", "Second"]


def test_episode_list_with_limit_returns_next_cursor(
    owner: TestClient, pilot: dict[str, Any]
) -> None:
    owner.post(EPISODES, json={"title": "Second"})
    first = owner.get(EPISODES, params={"limit": 1}).json()
    second = owner.get(EPISODES, params={"cursor": first["next_cursor"]}).json()
    assert [item["number"] for item in second["items"]] == [2]


def test_episode_list_non_numeric_cursor_returns_bad_request(owner: TestClient) -> None:
    response = owner.get(EPISODES, params={"cursor": encode_cursor("abc")})
    assert response.status_code == 400


def test_episode_get_returns_etag(owner: TestClient, pilot: dict[str, Any]) -> None:
    response = owner.get(f"{EPISODES}/1")
    assert response.headers["etag"] == '"1"' and response.json()["title"] == "Pilot"


def test_episode_get_missing_returns_not_found(owner: TestClient) -> None:
    response = owner.get(f"{EPISODES}/9")
    assert response.status_code == 404
    assert response.json()["detail"] == "Episode funny-memes #9 not found"


def test_episode_put_with_current_etag_returns_new_etag(
    owner: TestClient, pilot: dict[str, Any]
) -> None:
    body = {**PILOT, "status": "research"}
    response = owner.put(f"{EPISODES}/1", json=body, headers={"If-Match": '"1"'})
    assert response.headers["etag"] == '"2"' and response.json()["status"] == "research"


def test_episode_put_with_stale_etag_returns_precondition_failed(
    owner: TestClient, pilot: dict[str, Any]
) -> None:
    response = owner.put(f"{EPISODES}/1", json=PILOT, headers={"If-Match": '"5"'})
    assert response.status_code == 412 and response.headers["etag"] == '"1"'


def test_episode_put_unknown_status_returns_unprocessable(
    owner: TestClient, pilot: dict[str, Any]
) -> None:
    response = owner.put(f"{EPISODES}/1", json={**PILOT, "status": "viral"})
    assert response.status_code == 422


def test_episode_delete_returns_no_content(owner: TestClient, pilot: dict[str, Any]) -> None:
    assert owner.delete(f"{EPISODES}/1").status_code == 204
    assert owner.get(f"{EPISODES}/1").status_code == 404


def test_episode_delete_missing_returns_not_found(owner: TestClient) -> None:
    assert owner.delete(f"{EPISODES}/3").status_code == 404


def test_episode_number_zero_is_rejected(owner: TestClient) -> None:
    assert owner.get(f"{EPISODES}/0").status_code == 422


def test_episode_create_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "pilot-1"}
    owner.post(EPISODES, json=PILOT, headers=headers)
    replay = owner.post(EPISODES, json=PILOT, headers=headers)
    assert replay.headers["idempotent-replayed"] == "true"
    assert len(owner.get(EPISODES).json()["items"]) == 1


def test_episode_create_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(EPISODES, json=PILOT).status_code == 403


def test_episode_create_by_contributor_succeeds(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.post(EPISODES, json=PILOT).status_code == 201


def test_episode_delete_by_contributor_is_forbidden(
    owner: TestClient,
    pilot: dict[str, Any],
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.delete(f"{EPISODES}/1").status_code == 403


def test_episode_create_with_token_lacking_content_scope_is_forbidden(
    app: FastAPI, owner: TestClient
) -> None:
    bot = agent(app, new_token(owner, ["channels:read"]))
    response = bot.post(EPISODES, json=PILOT)
    assert response.json()["detail"] == "This token lacks the content:write scope"


def test_episode_create_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "episode.create")
    response = agent(app, new_token(owner)).post(EPISODES, json=PILOT)
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


def test_episode_deleted_with_channel_is_gone(owner: TestClient, pilot: dict[str, Any]) -> None:
    owner.delete("/channels/funny-memes")
    owner.post("/channels", json=FUNNY_MEMES)
    assert owner.get(EPISODES).json()["items"] == []


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def created_by_tool(mcp: TestClient) -> dict[str, Any]:
    created = tool(mcp, "create_episode", {"slug": "funny-memes", "episode": PILOT})
    result: dict[str, Any] = created["structuredContent"]
    return result


def test_mcp_create_episode_returns_episode_with_revision(mcp: TestClient) -> None:
    created = created_by_tool(mcp)
    assert created["episode"]["number"] == 1 and created["revision"] == 1


def test_mcp_list_episodes_returns_created_episode(mcp: TestClient) -> None:
    created_by_tool(mcp)
    listed = tool(mcp, "list_episodes", {"slug": "funny-memes"})["structuredContent"]
    assert [item["title"] for item in listed["items"]] == ["Pilot"]


def test_mcp_get_episode_returns_its_revision(mcp: TestClient) -> None:
    created_by_tool(mcp)
    found = tool(mcp, "get_episode", {"slug": "funny-memes", "number": 1})["structuredContent"]
    assert found["revision"] == 1


def test_mcp_update_episode_with_expected_revision_bumps_revision(mcp: TestClient) -> None:
    created_by_tool(mcp)
    arguments = {
        "slug": "funny-memes",
        "number": 1,
        "episode": {**PILOT, "status": "script"},
        "expected_revision": 1,
    }
    assert tool(mcp, "update_episode", arguments)["structuredContent"]["revision"] == 2


DELETE_PILOT = {"slug": "funny-memes", "number": 1, "expected_revision": 1}


def test_mcp_delete_episode_returns_what_it_deleted(mcp: TestClient) -> None:
    created_by_tool(mcp)
    deleted = tool(mcp, "delete_episode", DELETE_PILOT)["structuredContent"]
    assert deleted == {"slug": "funny-memes", "number": 1}


def test_mcp_delete_episode_leaves_it_unreadable(mcp: TestClient) -> None:
    created_by_tool(mcp)
    tool(mcp, "delete_episode", DELETE_PILOT)
    assert tool(mcp, "get_episode", {"slug": "funny-memes", "number": 1})["isError"]


def test_mcp_delete_episode_without_revision_is_refused(mcp: TestClient) -> None:
    created_by_tool(mcp)
    result = tool(mcp, "delete_episode", {"slug": "funny-memes", "number": 1})
    assert result["isError"]


def test_mcp_delete_episode_with_token_lacking_channel_scope_names_it(
    app: FastAPI, owner: TestClient, pilot: dict[str, Any]
) -> None:
    scopes = ["channels:read", "content:write"]
    with agent(app, new_token(owner, scopes), PUBLIC_URL) as limited:
        result = tool(limited, "delete_episode", DELETE_PILOT)
    assert result["isError"] and "channels:write" in result["content"][0]["text"]


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "get_episode",
            {"slug": "funny-memes", "number": 5},
            "Episode funny-memes #5 not found; call list_episodes",
        ),
        (
            "update_episode",
            {"slug": "funny-memes", "number": 1, "episode": PILOT, "expected_revision": 9},
            (
                "Episode funny-memes #1 changed; it is now at revision 1. Read it again, merge your "
                "change and retry with expected_revision=1"
            ),
        ),
        (
            "delete_episode",
            {"slug": "funny-memes", "number": 1, "expected_revision": 9},
            "Episode funny-memes #1 changed; it is now at revision 1.",
        ),
        (
            "delete_episode",
            {"slug": "funny-memes", "number": 5, "expected_revision": 1},
            "Episode funny-memes #5 not found; call list_episodes",
        ),
    ],
)
def test_mcp_refused_episode_call_names_the_cause(
    mcp: TestClient, name: str, arguments: dict[str, Any], message: str
) -> None:
    created_by_tool(mcp)
    result = tool(mcp, name, arguments)
    assert result["isError"] and message in result["content"][0]["text"]


def test_database_from_before_channel_revisions_gains_revision_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE channels (slug TEXT PRIMARY KEY, document TEXT NOT NULL,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    with Database(path).connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(channels)")}
    assert "revision" in columns
