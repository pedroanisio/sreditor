"""Visual styles: library entries an episode picks one of (rule project.visual-style)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.settings import AppSettings
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES

STYLES = "/channels/funny-memes/styles"
STYLE = "/channels/funny-memes/episodes/1/style"


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def warm(owner: TestClient) -> dict[str, Any]:
    response = owner.post(STYLES, json={"name": "Warm", "description": "Handheld, natural light"})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_style_create_is_a_library_entry_of_kind_style(warm: dict[str, Any]) -> None:
    assert warm["kind"] == "style"


def test_episode_style_unpicked_is_none_at_revision_zero(owner: TestClient) -> None:
    response = owner.get(STYLE)
    assert response.json()["style"] is None and response.headers["etag"] == '"0"'


def test_episode_style_pick_returns_the_entry(owner: TestClient, warm: dict[str, Any]) -> None:
    response = owner.put(STYLE, json={"style_id": warm["id"]}, headers={"If-Match": '"0"'})
    assert response.json()["style"]["name"] == "Warm" and response.headers["etag"] == '"1"'


def test_episode_style_clear_returns_none(owner: TestClient, warm: dict[str, Any]) -> None:
    owner.put(STYLE, json={"style_id": warm["id"]})
    assert owner.put(STYLE, json={"style_id": None}).json()["style"] is None


def test_episode_style_unknown_id_returns_problem_naming_field(owner: TestClient) -> None:
    response = owner.put(STYLE, json={"style_id": str(uuid4())})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "style_id"]


def test_episode_style_character_id_is_not_a_style(owner: TestClient) -> None:
    character = owner.post("/channels/funny-memes/characters", json={"name": "ANA"}).json()
    assert owner.put(STYLE, json={"style_id": character["id"]}).status_code == 422


def test_episode_style_stale_etag_returns_precondition_failed(
    owner: TestClient, warm: dict[str, Any]
) -> None:
    owner.put(STYLE, json={"style_id": warm["id"]})
    response = owner.put(STYLE, json={"style_id": None}, headers={"If-Match": '"0"'})
    assert response.status_code == 412


def test_episode_style_removed_from_library_leaves_none(
    owner: TestClient, warm: dict[str, Any]
) -> None:
    owner.put(STYLE, json={"style_id": warm["id"]})
    owner.delete(f"{STYLES}/{warm['id']}")
    assert owner.get(STYLE).json()["style"] is None


def test_episode_style_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, warm: dict[str, Any], app_settings: AppSettings
) -> None:
    block(app_settings, "/", "episode_style.write")
    response = agent(app, new_token(owner)).put(STYLE, json={"style_id": warm["id"]})
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_set_episode_style_then_get_returns_it(mcp: TestClient, warm: dict[str, Any]) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "style_id": warm["id"]}
    tool(mcp, "set_episode_style", {**arguments, "expected_revision": 0})
    read = tool(mcp, "get_episode_style", {"slug": "funny-memes", "number": 1})
    assert read["structuredContent"]["style"]["id"] == warm["id"]


def test_mcp_set_unknown_style_says_how_to_fix_it(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "style_id": str(uuid4())}
    result = tool(mcp, "set_episode_style", arguments)
    assert result["isError"] and "kind style" in result["content"][0]["text"]


def test_mcp_list_library_styles_lists_them(mcp: TestClient, warm: dict[str, Any]) -> None:
    listed = tool(mcp, "list_library", {"slug": "funny-memes", "kind": "style"})
    assert [item["name"] for item in listed["structuredContent"]["items"]] == ["Warm"]
