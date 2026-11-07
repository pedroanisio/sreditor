"""Episode research: notes and sources in the store, the API and MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeInput, EpisodeStore
from sreditor.pagination import encode_cursor
from sreditor.research import NotesInput, ResearchStore, SourceInput
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1"
SOURCES = f"{EPISODE}/sources"
STUDY = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": "Most people dread Mondays.",
    "accessed_on": "2026-11-01",
}


@pytest.fixture
def research_store(tmp_path: Path) -> ResearchStore:
    """A store over a database with episode 1 of funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    EpisodeStore(database).create("funny-memes", EpisodeInput(title="Pilot"))
    return ResearchStore(database)


def notes(text: str) -> NotesInput:
    return NotesInput(text=text)


# Store


def test_research_notes_never_written_are_empty_at_revision_zero(
    research_store: ResearchStore,
) -> None:
    found = research_store.notes("funny-memes", 1)
    assert found.text == "" and found.revision == 0


def test_research_notes_first_write_expecting_zero_is_revision_one(
    research_store: ResearchStore,
) -> None:
    assert research_store.write_notes("funny-memes", 1, notes("Lead"), 0).revision == 1


def test_research_notes_second_write_expecting_zero_raises_changed(
    research_store: ResearchStore,
) -> None:
    research_store.write_notes("funny-memes", 1, notes("Lead"), 0)
    with pytest.raises(PartChanged) as changed:
        research_store.write_notes("funny-memes", 1, notes("Other"), 0)
    assert changed.value.current == 1


def test_research_notes_write_without_expectation_overwrites(
    research_store: ResearchStore,
) -> None:
    research_store.write_notes("funny-memes", 1, notes("Lead"), None)
    written = research_store.write_notes("funny-memes", 1, notes("Better"), None)
    assert written.text == "Better" and written.revision == 2


def test_research_notes_write_with_current_revision_succeeds(
    research_store: ResearchStore,
) -> None:
    research_store.write_notes("funny-memes", 1, notes("Lead"), None)
    assert research_store.write_notes("funny-memes", 1, notes("Next"), 1).text == "Next"


def test_research_source_without_date_is_read_today(research_store: ResearchStore) -> None:
    data = SourceInput.model_validate({**STUDY, "accessed_on": None})
    added = research_store.add_source("funny-memes", 1, data)
    assert added.accessed_on == research_store.database.clock().date()


def test_research_source_replace_keeps_date_when_left_out(research_store: ResearchStore) -> None:
    added = research_store.add_source("funny-memes", 1, SourceInput.model_validate(STUDY))
    data = SourceInput.model_validate({**STUDY, "title": "New", "accessed_on": None})
    replaced = research_store.replace_source(added, data, 1)
    assert replaced.accessed_on == date(2026, 11, 1) and replaced.revision == 2


def test_research_source_delete_stale_revision_raises_changed(
    research_store: ResearchStore,
) -> None:
    added = research_store.add_source("funny-memes", 1, SourceInput.model_validate(STUDY))
    with pytest.raises(PartChanged):
        research_store.delete_source(added, 5)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def study(owner: TestClient) -> dict[str, Any]:
    response = owner.post(SOURCES, json=STUDY)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_research_notes_get_unwritten_has_etag_zero(owner: TestClient) -> None:
    response = owner.get(f"{EPISODE}/research")
    assert response.headers["etag"] == '"0"' and response.json()["text"] == ""


def test_research_notes_put_returns_new_etag(owner: TestClient) -> None:
    response = owner.put(f"{EPISODE}/research", json={"text": "Lead"}, headers={"If-Match": '"0"'})
    assert response.headers["etag"] == '"1"' and response.json()["text"] == "Lead"


def test_research_notes_put_stale_etag_returns_precondition_failed(owner: TestClient) -> None:
    owner.put(f"{EPISODE}/research", json={"text": "Lead"})
    response = owner.put(f"{EPISODE}/research", json={"text": "X"}, headers={"If-Match": '"0"'})
    assert response.status_code == 412 and response.headers["etag"] == '"1"'


def test_research_notes_on_missing_episode_returns_not_found(owner: TestClient) -> None:
    response = owner.get("/channels/funny-memes/episodes/9/research")
    assert response.status_code == 404


def test_research_source_add_returns_source_with_revision(study: dict[str, Any]) -> None:
    assert study["title"] == "Mondays study" and study["revision"] == 1


def test_research_source_add_invalid_url_returns_unprocessable(owner: TestClient) -> None:
    response = owner.post(SOURCES, json={**STUDY, "url": "not a link"})
    assert response.status_code == 422


def test_research_sources_list_paginates(owner: TestClient, study: dict[str, Any]) -> None:
    owner.post(SOURCES, json={**STUDY, "title": "Second"})
    first = owner.get(SOURCES, params={"limit": 1}).json()
    second = owner.get(SOURCES, params={"cursor": first["next_cursor"]}).json()
    assert [item["title"] for item in second["items"]] == ["Second"]


def test_research_sources_bad_cursor_returns_bad_request(owner: TestClient) -> None:
    assert owner.get(SOURCES, params={"cursor": encode_cursor("x")}).status_code == 400


def test_research_source_get_returns_etag(owner: TestClient, study: dict[str, Any]) -> None:
    assert owner.get(f"{SOURCES}/{study['id']}").headers["etag"] == '"1"'


def test_research_source_get_unknown_returns_not_found(owner: TestClient) -> None:
    response = owner.get(f"{SOURCES}/{uuid4()}")
    assert response.status_code == 404 and response.json()["detail"].startswith("Source ")


def test_research_source_put_with_etag_returns_next_etag(
    owner: TestClient, study: dict[str, Any]
) -> None:
    body = {**STUDY, "claim": "Mondays are hard."}
    response = owner.put(f"{SOURCES}/{study['id']}", json=body, headers={"If-Match": '"1"'})
    assert response.headers["etag"] == '"2"' and response.json()["claim"] == "Mondays are hard."


def test_research_source_put_stale_etag_returns_precondition_failed(
    owner: TestClient, study: dict[str, Any]
) -> None:
    response = owner.put(f"{SOURCES}/{study['id']}", json=STUDY, headers={"If-Match": '"4"'})
    assert response.status_code == 412


def test_research_source_delete_returns_no_content(
    owner: TestClient, study: dict[str, Any]
) -> None:
    assert owner.delete(f"{SOURCES}/{study['id']}").status_code == 204
    assert owner.get(SOURCES).json()["items"] == []


def test_research_sources_go_with_their_episode(owner: TestClient, study: dict[str, Any]) -> None:
    owner.delete(EPISODE)
    owner.post("/channels/funny-memes/episodes", json={"title": "Next"})
    assert owner.get(f"{SOURCES}/{study['id']}").status_code == 404


def test_research_source_add_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(SOURCES, json=STUDY).status_code == 403


def test_research_source_add_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "study-1"}
    owner.post(SOURCES, json=STUDY, headers=headers)
    replay = owner.post(SOURCES, json=STUDY, headers=headers)
    assert replay.headers["idempotent-replayed"] == "true"
    assert len(owner.get(SOURCES).json()["items"]) == 1


def test_research_notes_write_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "research.write")
    response = agent(app, new_token(owner)).put(f"{EPISODE}/research", json={"text": "Lead"})
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_research_notes_then_get_returns_them(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "notes": {"text": "Lead"}}
    tool(mcp, "write_research_notes", {**arguments, "expected_revision": 0})
    found = tool(mcp, "get_research", {"slug": "funny-memes", "number": 1})
    assert found["structuredContent"]["notes"]["text"] == "Lead"


def test_mcp_add_source_then_list_returns_it(mcp: TestClient) -> None:
    tool(mcp, "add_source", {"slug": "funny-memes", "number": 1, "source": STUDY})
    found = tool(mcp, "get_research", {"slug": "funny-memes", "number": 1})
    titles = [item["title"] for item in found["structuredContent"]["sources"]["items"]]
    assert titles == ["Mondays study"]


def test_mcp_remove_source_reports_no_error(mcp: TestClient) -> None:
    added = tool(mcp, "add_source", {"slug": "funny-memes", "number": 1, "source": STUDY})
    arguments = {"slug": "funny-memes", "number": 1, "source_id": added["structuredContent"]["id"]}
    removed = tool(mcp, "remove_source", arguments)["structuredContent"]
    assert removed == {"kind": "source", "id": arguments["source_id"]}


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "remove_source",
            {
                "slug": "funny-memes",
                "number": 1,
                "source_id": "00000000-0000-0000-0000-000000000000",
            },
            "Source 00000000-0000-0000-0000-000000000000 not found; call get_research",
        ),
        (
            "write_research_notes",
            {"slug": "funny-memes", "number": 1, "notes": {"text": "X"}, "expected_revision": 3},
            "Research notes of funny-memes #1 changed; it is now at revision 0. Read it again",
        ),
    ],
)
def test_mcp_refused_research_call_names_the_cause(
    mcp: TestClient, name: str, arguments: dict[str, Any], message: str
) -> None:
    result = tool(mcp, name, arguments)
    assert result["isError"] and message in result["content"][0]["text"]
