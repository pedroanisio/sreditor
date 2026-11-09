"""The change feed: what is recorded, who sees it, paging, retention and MCP (rule agentic.events)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.deps import Services
from sreditor.events import READ_WITH, Event
from sreditor.pagination import encode_cursor
from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.test_agentic import ALL_SCOPES, PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined
from tests.test_mcp_contract import registered

CHANNEL = "/channels/funny-memes"
EVENTS = f"{CHANNEL}/events"
EPISODE = f"{CHANNEL}/episodes/1"
SECRET_WORDS = "A line nobody else may read from the feed."
WITH_WAIVERS = [*ALL_SCOPES, "waivers:read"]
LONG_AGO = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def feed(client: TestClient, query: str = "") -> dict[str, Any]:
    response = client.get(f"{EVENTS}{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def operations(body: dict[str, Any]) -> list[str]:
    return [item["operation"] for item in body["items"]]


def write_screenplay(client: TestClient, text: str = SECRET_WORDS) -> None:
    assert client.put(f"{EPISODE}/screenplay", json={"text": text}).status_code == 200


def add_waiver(client: TestClient) -> None:
    files = {"file": ("ana.pdf", sample.PDF)}
    response = client.post(f"{EPISODE}/waivers", data={"signer": "Ana Souza"}, files=files)
    assert response.status_code == 201, response.text


def age_events(app: FastAPI) -> None:
    """Make every recorded event older than the retention period."""
    services: Services = app.state.services
    with services.events.database.connect() as connection:
        connection.execute("UPDATE events SET occurred_at = ?", (LONG_AGO,))


# What is recorded


def test_events_token_write_records_one_event_naming_the_token(
    app: FastAPI, owner: TestClient
) -> None:
    secret = new_token(owner)
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    before = len(feed(owner)["items"])
    write_screenplay(agent(app, secret))
    added = feed(owner)["items"][before:]
    assert [(item["operation"], item["episode_number"]) for item in added] == [
        ("screenplay.write", 1)
    ]
    assert added[0]["actor"] == {"type": "token", "id": token_id}
    assert added[0]["read_with"] == "get_screenplay"


def test_events_session_write_records_one_event_naming_the_user(owner: TestClient) -> None:
    before = len(feed(owner)["items"])
    write_screenplay(owner)
    added = feed(owner)["items"][before:]
    assert [item["operation"] for item in added] == ["screenplay.write"]
    assert added[0]["actor"] == {"type": "user", "id": owner.user_id}  # type: ignore[attr-defined]


def test_events_episode_create_names_the_new_episode(owner: TestClient) -> None:
    created = [item for item in feed(owner)["items"] if item["operation"] == "episode.create"]
    assert [item["episode_number"] for item in created] == [1]


def test_events_write_to_a_channel_item_names_no_episode(owner: TestClient) -> None:
    pitch = {"title": "Mondays", "summary": "Why we hate them"}
    assert owner.post(f"{CHANNEL}/pitches", json=pitch).status_code == 201
    latest = feed(owner)["items"][-1]
    assert (latest["operation"], latest["episode_number"]) == ("pitch.create", None)


def test_events_failed_write_records_nothing(owner: TestClient) -> None:
    before = len(feed(owner)["items"])
    assert owner.put(f"{CHANNEL}/episodes/99/screenplay", json={"text": "x"}).status_code == 404
    assert len(feed(owner)["items"]) == before


def test_events_record_failure_keeps_the_committed_write(
    app: FastAPI, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    services: Services = app.state.services

    def broken(_new: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(services.events, "record", broken)
    write_screenplay(owner, "Kept even when the feed fails.")
    assert owner.get(f"{EPISODE}/screenplay").json()["text"] == "Kept even when the feed fails."


def test_events_gate_refused_write_records_nothing(app: FastAPI, owner: TestClient) -> None:
    before = len(feed(owner)["items"])
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert reader.put(f"{EPISODE}/screenplay", json={"text": "x"}).status_code == 403
    assert len(feed(owner)["items"]) == before


def test_events_channel_delete_by_token_is_not_recorded_and_succeeds(
    app: FastAPI, owner: TestClient
) -> None:
    response = agent(app, new_token(owner)).delete(CHANNEL)
    assert response.status_code == 204, response.text


def test_events_carry_no_text_people_wrote(owner: TestClient) -> None:
    write_screenplay(owner)
    response = owner.get(EVENTS)
    assert SECRET_WORDS not in response.text
    assert set(response.json()["items"][0]) == set(Event.model_fields)


# Reading


def test_events_without_since_return_the_latest_page_and_a_cursor(owner: TestClient) -> None:
    for _ in range(3):
        write_screenplay(owner)
    page = feed(owner, "?limit=2")
    assert operations(page) == ["screenplay.write", "screenplay.write"]
    assert page["has_more"] is False
    assert feed(owner, f"?since={page['next_cursor']}")["items"] == []


def test_events_since_a_cursor_pages_oldest_first(owner: TestClient) -> None:
    start = feed(owner)["next_cursor"]
    for index in range(5):
        write_screenplay(owner, f"Take {index}")
    seen: list[str] = []
    cursor = start
    while True:
        page = feed(owner, f"?since={cursor}&limit=2")
        seen.extend(item["cursor"] for item in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 5 and seen == sorted(seen, key=lambda value: (len(value), value))
    assert cursor == seen[-1]


def test_events_empty_page_keeps_the_cursor_it_was_given(owner: TestClient) -> None:
    cursor = feed(owner)["next_cursor"]
    assert feed(owner, f"?since={cursor}")["next_cursor"] == cursor


def test_events_invalid_since_answers_400(owner: TestClient) -> None:
    for since in ("not-base64!", encode_cursor("abc")):
        assert owner.get(f"{EVENTS}?since={since}").status_code == 400


def test_events_non_member_answers_404(
    app: FastAPI, owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    assert sign_up("stranger@example.com").get(EVENTS).status_code == 404


def test_events_token_limited_to_other_channels_answers_404(
    app: FastAPI, owner: TestClient
) -> None:
    other = {"slug": "other", "name": "Other"}
    assert owner.post("/channels", json=other).status_code == 201
    limited = agent(app, new_token(owner, channels=["other"]))
    assert limited.get(EVENTS).status_code == 404


def test_events_token_without_read_scope_answers_403(app: FastAPI, owner: TestClient) -> None:
    blind = agent(app, new_token(owner, ["editorial:write"]))
    assert blind.get(EVENTS).status_code == 403


# Waivers


def test_events_waiver_events_hidden_from_token_without_waivers_read(
    app: FastAPI, owner: TestClient
) -> None:
    add_waiver(owner)
    assert "waiver.create" in operations(feed(owner))
    assert "waiver.create" not in operations(feed(agent(app, new_token(owner, ALL_SCOPES))))
    assert "waiver.create" in operations(feed(agent(app, new_token(owner, WITH_WAIVERS))))


def test_events_waiver_events_hidden_from_roles_that_cannot_read_waivers(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    add_waiver(owner)
    viewer = joined(owner, sign_up, mailer, "viewer")
    editor = joined(owner, sign_up, mailer, "editor")
    assert "waiver.create" not in operations(feed(viewer))
    assert "waiver.create" in operations(feed(editor))


# Retention


def test_events_older_than_retention_are_pruned(app: FastAPI, owner: TestClient) -> None:
    write_screenplay(owner)
    age_events(app)
    assert feed(owner)["items"] == []


def test_events_expired_cursor_answers_410_naming_how_to_restart(
    app: FastAPI, owner: TestClient
) -> None:
    old_cursor = feed(owner)["items"][0]["cursor"]
    age_events(app)
    write_screenplay(owner)
    response = owner.get(f"{EVENTS}?since={old_cursor}")
    assert response.status_code == 410
    problem = response.json()
    assert problem["type"].endswith("cursor-expired") and "without since" in problem["detail"]


def test_events_cursor_from_after_pruning_is_not_expired(app: FastAPI, owner: TestClient) -> None:
    write_screenplay(owner)
    age_events(app)
    restart = feed(owner)["next_cursor"]
    write_screenplay(owner)
    assert operations(feed(owner, f"?since={restart}")) == ["screenplay.write"]


def test_events_cursor_at_the_last_pruned_event_is_not_expired(
    app: FastAPI, owner: TestClient
) -> None:
    cursor = feed(owner)["next_cursor"]
    age_events(app)
    assert feed(owner, f"?since={cursor}")["items"] == []


# MCP


def test_mcp_list_events_is_read_only_with_an_output_schema(mcp: TestClient) -> None:
    listed = registered(mcp)["list_events"]
    assert listed["annotations"] == {
        "title": "List what changed in a channel",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert {"items", "next_cursor", "has_more"} <= set(listed["outputSchema"]["properties"])


def test_mcp_list_events_returns_a_write_made_through_the_api(
    owner: TestClient, mcp: TestClient
) -> None:
    write_screenplay(owner)
    result = tool(mcp, "list_events", {"slug": "funny-memes", "limit": 1})
    content = result["structuredContent"]
    assert content["items"][0]["operation"] == "screenplay.write"
    assert content["items"][0]["read_with"] == "get_screenplay"


def test_mcp_list_events_expired_cursor_is_a_tool_error_naming_the_fix(
    app: FastAPI, owner: TestClient, mcp: TestClient
) -> None:
    old = feed(owner)["items"][0]["cursor"]
    age_events(app)
    result = tool(mcp, "list_events", {"slug": "funny-memes", "since": old})
    assert result["isError"] and "without since" in result["content"][0]["text"]


def test_events_read_tools_are_all_registered(mcp: TestClient) -> None:
    assert set(READ_WITH.values()) - set(registered(mcp)) == set()
