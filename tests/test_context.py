"""Items' context (what, who, where, when, why, how) and the links between them, by API and MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.context import ItemKind, ItemRef
from sreditor.database import Database
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

CHANNEL = "/channels/funny-memes"
EPISODE_CONTEXT = f"{CHANNEL}/items/episode/1/context"
ANSWERS = {"what": "A dog fakes a sick day.", "who": "Rex", "where": "The office", "why": "Mondays"}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def created(client: TestClient, path: str, body: dict[str, Any]) -> str:
    response = client.post(f"{CHANNEL}/{path}", json=body)
    assert response.status_code == 201, response.text
    found: str = response.json()["id"]
    return found


@pytest.fixture
def rex(owner: TestClient) -> str:
    return created(owner, "characters", {"name": "Rex"})


@pytest.fixture
def pitch(owner: TestClient) -> str:
    return created(owner, "pitches", {"title": "Sick day"})


def link(client: TestClient, source: str, to: dict[str, str], question: str = "who") -> Any:
    return client.post(f"{CHANNEL}/items/{source}/links", json={"to": to, "question": question})


# Context answers


def test_context_unwritten_is_empty_at_revision_zero(owner: TestClient) -> None:
    response = owner.get(EPISODE_CONTEXT)
    assert response.headers["etag"] == '"0"' and response.json()["what"] == ""


def test_context_write_keeps_answers_and_bumps_etag(owner: TestClient) -> None:
    response = owner.put(EPISODE_CONTEXT, json=ANSWERS, headers={"If-Match": '"0"'})
    assert response.headers["etag"] == '"1"' and response.json()["who"] == "Rex"


def test_context_write_with_stale_etag_is_refused(owner: TestClient) -> None:
    owner.put(EPISODE_CONTEXT, json=ANSWERS)
    response = owner.put(EPISODE_CONTEXT, json=ANSWERS, headers={"If-Match": '"0"'})
    assert response.status_code == 412 and response.headers["etag"] == '"1"'


def test_context_of_missing_item_is_not_found(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/items/episode/9/context").status_code == 404


def test_context_with_malformed_id_is_not_found(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/items/pitch/nope/context").status_code == 404


def test_context_answer_too_long_names_field(owner: TestClient) -> None:
    response = owner.put(EPISODE_CONTEXT, json={"why": "x" * 2001})
    assert response.status_code == 422 and response.json()["errors"][0]["location"][-1] == "why"


def test_context_write_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.get(EPISODE_CONTEXT).status_code == 200
    assert viewer.put(EPISODE_CONTEXT, json=ANSWERS).status_code == 403


def test_context_write_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "item_context.write")
    response = agent(app, new_token(owner)).put(EPISODE_CONTEXT, json=ANSWERS)
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


# Links


def test_link_pitch_to_character_names_both(owner: TestClient, pitch: str, rex: str) -> None:
    response = link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    body = response.json()
    assert response.status_code == 201
    assert (body["source"]["label"], body["target"]["label"], body["question"]) == (
        "Sick day",
        "Rex",
        "who",
    )


def test_link_shows_on_target_as_mention(owner: TestClient, pitch: str, rex: str) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    mentions = owner.get(f"{CHANNEL}/items/character/{rex}/links", params={"direction": "in"})
    assert [item["source"]["label"] for item in mentions.json()["items"]] == ["Sick day"]


def test_link_from_episode_by_number(owner: TestClient, rex: str) -> None:
    response = link(owner, "episode/1", {"kind": "character", "id": rex})
    assert response.json()["source"]["episode"] == 1


def test_link_twice_is_conflict(owner: TestClient, pitch: str, rex: str) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    assert link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex}).status_code == 409


def test_link_same_items_other_question_is_allowed(owner: TestClient, pitch: str, rex: str) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    response = link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex}, "why")
    assert response.status_code == 201


def test_link_to_missing_item_names_to_field(owner: TestClient, pitch: str) -> None:
    missing = {"kind": "location", "id": "00000000-0000-0000-0000-000000000000"}
    response = link(owner, f"pitch/{pitch}", missing)
    assert response.status_code == 422 and response.json()["errors"][0]["location"] == [
        "body",
        "to",
    ]


def test_link_to_itself_is_refused(owner: TestClient, pitch: str) -> None:
    response = link(owner, f"pitch/{pitch}", {"kind": "pitch", "id": pitch})
    assert response.status_code == 422


def test_link_list_pages_with_cursor(owner: TestClient, pitch: str, rex: str) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    link(owner, f"pitch/{pitch}", {"kind": "episode", "id": "1"}, "what")
    path = f"{CHANNEL}/items/pitch/{pitch}/links"
    first = owner.get(path, params={"limit": 1}).json()
    second = owner.get(path, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["target"]["kind"] == "episode" and second["next_cursor"] is None


def test_link_remove_then_read_is_not_found(owner: TestClient, pitch: str, rex: str) -> None:
    link_id = link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex}).json()["id"]
    assert owner.delete(f"{CHANNEL}/links/{link_id}").status_code == 204
    assert owner.get(f"{CHANNEL}/links/{link_id}").status_code == 404


def test_link_remove_unknown_is_not_found(owner: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert owner.delete(f"{CHANNEL}/links/{missing}").status_code == 404


def test_deleting_character_removes_its_links(owner: TestClient, pitch: str, rex: str) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    assert owner.delete(f"{CHANNEL}/characters/{rex}").status_code == 204
    assert owner.get(f"{CHANNEL}/items/pitch/{pitch}/links").json()["items"] == []


def test_deleting_episode_removes_its_context_and_links(
    owner: TestClient, pitch: str, app: FastAPI
) -> None:
    owner.put(EPISODE_CONTEXT, json=ANSWERS)
    link(owner, f"pitch/{pitch}", {"kind": "episode", "id": "1"}, "what")
    assert owner.delete(f"{CHANNEL}/episodes/1").status_code == 204
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        contexts = connection.execute("SELECT COUNT(*) FROM item_contexts").fetchone()[0]
    assert contexts == 0
    assert owner.get(f"{CHANNEL}/items/pitch/{pitch}/links").json()["items"] == []


def test_list_items_names_each_kind(owner: TestClient, pitch: str, rex: str) -> None:
    items = owner.get(f"{CHANNEL}/items").json()["items"]
    assert {(item["kind"], item["label"]) for item in items} == {
        ("episode", "Pilot"),
        ("pitch", "Sick day"),
        ("character", "Rex"),
    }


def test_item_ref_episode_number_is_canonical() -> None:
    assert ItemRef(kind=ItemKind.EPISODE, id="01").id == "1"


def test_item_ref_episode_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="number"):
        ItemRef(kind=ItemKind.EPISODE, id="0")


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_context_with_links_then_get_returns_both(
    mcp: TestClient, rex: str, pitch: str
) -> None:
    arguments = {
        "slug": "funny-memes",
        "item": {"kind": "pitch", "id": pitch},
        "update": {
            "answers": ANSWERS,
            "links": [{"to": {"kind": "character", "id": rex}, "question": "who"}],
        },
    }
    tool(mcp, "write_context", arguments)
    found = tool(
        mcp, "get_context", {"slug": "funny-memes", "item": {"kind": "pitch", "id": pitch}}
    )
    content = found["structuredContent"]
    assert content["context"]["what"] == ANSWERS["what"]
    assert content["links"]["items"][0]["target"]["label"] == "Rex"


def test_mcp_get_context_lists_mentions(
    mcp: TestClient, owner: TestClient, rex: str, pitch: str
) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    found = tool(
        mcp, "get_context", {"slug": "funny-memes", "item": {"kind": "character", "id": rex}}
    )
    assert found["structuredContent"]["mentions"]["items"][0]["source"]["label"] == "Sick day"


def test_mcp_link_to_missing_item_points_to_list_items(mcp: TestClient, pitch: str) -> None:
    missing = {"kind": "character", "id": "00000000-0000-0000-0000-000000000000"}
    arguments = {
        "slug": "funny-memes",
        "item": {"kind": "pitch", "id": pitch},
        "update": {"links": [{"to": missing, "question": "who"}]},
    }
    result = tool(mcp, "write_context", arguments)
    assert result["isError"] and "list_linkable_items" in result["content"][0]["text"]


def test_mcp_unlink_removes_link(mcp: TestClient, owner: TestClient, rex: str, pitch: str) -> None:
    link_id = link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex}).json()["id"]
    tool(mcp, "unlink_items", {"slug": "funny-memes", "link_id": link_id})
    assert owner.get(f"{CHANNEL}/links/{link_id}").status_code == 404


def test_mcp_list_linkable_items_names_them(mcp: TestClient, rex: str) -> None:
    result = tool(mcp, "list_linkable_items", {"slug": "funny-memes"})
    labels = [item["label"] for item in result["structuredContent"]["items"]]
    assert labels == ["Pilot", "Rex"]


def pitch_context(pitch: str, links: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "slug": "funny-memes",
        "item": {"kind": "pitch", "id": pitch},
        "update": {"answers": ANSWERS, "links": links},
    }


def test_mcp_write_context_link_with_other_note_writes_nothing(
    mcp: TestClient, owner: TestClient, rex: str, pitch: str
) -> None:
    link(owner, f"pitch/{pitch}", {"kind": "character", "id": rex})
    wanted = {"to": {"kind": "character", "id": rex}, "question": "who", "note": "Changed"}
    result = tool(mcp, "write_context", pitch_context(pitch, [wanted]))
    assert result["isError"] and "unlink_items" in result["content"][0]["text"]
    assert owner.get(f"{CHANNEL}/items/pitch/{pitch}/context").json()["revision"] == 0


def test_mcp_write_context_self_link_writes_nothing(
    mcp: TestClient, owner: TestClient, pitch: str
) -> None:
    wanted = {"to": {"kind": "pitch", "id": pitch}, "question": "what"}
    result = tool(mcp, "write_context", pitch_context(pitch, [wanted]))
    assert result["isError"] and "itself" in result["content"][0]["text"]
    assert owner.get(f"{CHANNEL}/items/pitch/{pitch}/context").json()["revision"] == 0


def test_mcp_write_context_retried_keeps_one_link(
    mcp: TestClient, owner: TestClient, rex: str, pitch: str
) -> None:
    wanted = {"to": {"kind": "character", "id": rex}, "question": "who"}
    arguments = pitch_context(pitch, [wanted, wanted])
    first = tool(mcp, "write_context", arguments)
    revision = first["structuredContent"]["context"]["revision"]
    arguments["update"]["expected_revision"] = revision
    assert not tool(mcp, "write_context", arguments).get("isError")
    assert len(owner.get(f"{CHANNEL}/items/pitch/{pitch}/links").json()["items"]) == 1
