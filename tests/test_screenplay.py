"""Episode screenplays in the store, the API and MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeInput, EpisodeStore
from sreditor.research import NotesInput, ResearchStore
from sreditor.screenplay import SCREENPLAY_LENGTH, ScreenplayInput, ScreenplayStore
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

SCREENPLAY = "/channels/funny-memes/episodes/1/screenplay"
PILOT = "INT. KITCHEN - DAY\n\nANA\nIs it Monday again?\n"


@pytest.fixture
def screenplays(tmp_path: Path) -> ScreenplayStore:
    """A store over a database with episode 1 of funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    EpisodeStore(database).create("funny-memes", EpisodeInput(title="Pilot"))
    return ScreenplayStore(database)


# Store


def test_screenplay_never_written_is_empty_at_revision_zero(screenplays: ScreenplayStore) -> None:
    found = screenplays.screenplay("funny-memes", 1)
    assert found.text == "" and found.revision == 0 and found.scenes == []


def test_screenplay_write_reads_scenes_and_characters(screenplays: ScreenplayStore) -> None:
    written = screenplays.write("funny-memes", 1, ScreenplayInput(text=PILOT), 0)
    assert written.scenes == ["INT. KITCHEN - DAY"] and written.characters == ["ANA"]


def test_screenplay_write_stale_revision_raises_changed(screenplays: ScreenplayStore) -> None:
    screenplays.write("funny-memes", 1, ScreenplayInput(text=PILOT), None)
    with pytest.raises(PartChanged) as changed:
        screenplays.write("funny-memes", 1, ScreenplayInput(text="X"), 0)
    assert changed.value.current == 1 and str(changed.value) == "Screenplay of funny-memes #1"


def test_screenplay_write_leaves_research_notes_revision_alone(
    screenplays: ScreenplayStore,
) -> None:
    research = ResearchStore(screenplays.texts.database)
    research.write_notes("funny-memes", 1, NotesInput(text="Lead"), 0)
    screenplays.write("funny-memes", 1, ScreenplayInput(text=PILOT), 0)
    screenplays.write("funny-memes", 1, ScreenplayInput(text=PILOT), 1)
    assert research.notes("funny-memes", 1).revision == 1


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def test_screenplay_get_unwritten_has_etag_zero(owner: TestClient) -> None:
    response = owner.get(SCREENPLAY)
    assert response.headers["etag"] == '"0"' and response.json()["text"] == ""


def test_screenplay_put_returns_outline_and_new_etag(owner: TestClient) -> None:
    response = owner.put(SCREENPLAY, json={"text": PILOT}, headers={"If-Match": '"0"'})
    assert response.headers["etag"] == '"1"' and response.json()["characters"] == ["ANA"]


def test_screenplay_put_stale_etag_returns_precondition_failed(owner: TestClient) -> None:
    owner.put(SCREENPLAY, json={"text": PILOT})
    response = owner.put(SCREENPLAY, json={"text": "X"}, headers={"If-Match": '"0"'})
    assert response.status_code == 412 and response.headers["etag"] == '"1"'
    assert response.json()["detail"].startswith("Screenplay of funny-memes #1 changed")


def test_screenplay_put_too_long_returns_unprocessable(owner: TestClient) -> None:
    response = owner.put(SCREENPLAY, json={"text": "x" * (SCREENPLAY_LENGTH + 1)})
    assert response.status_code == 422


def test_screenplay_of_missing_episode_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/funny-memes/episodes/9/screenplay").status_code == 404


def test_screenplay_goes_with_its_episode(owner: TestClient) -> None:
    owner.put(SCREENPLAY, json={"text": PILOT})
    owner.delete("/channels/funny-memes/episodes/1")
    owner.post("/channels/funny-memes/episodes", json={"title": "Next"})
    assert owner.get("/channels/funny-memes/episodes/2/screenplay").json()["text"] == ""


def test_screenplay_put_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.put(SCREENPLAY, json={"text": PILOT}).status_code == 403


def test_screenplay_write_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "screenplay.write")
    response = agent(app, new_token(owner)).put(SCREENPLAY, json={"text": PILOT})
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_screenplay_then_get_returns_scenes(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "screenplay": {"text": PILOT}}
    tool(mcp, "write_screenplay", {**arguments, "expected_revision": 0})
    found = tool(mcp, "get_screenplay", {"slug": "funny-memes", "number": 1})
    assert found["structuredContent"]["screenplay"]["scenes"] == ["INT. KITCHEN - DAY"]


def test_mcp_write_screenplay_stale_revision_names_the_cause(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "screenplay": {"text": PILOT}}
    result = tool(mcp, "write_screenplay", {**arguments, "expected_revision": 4})
    message = "Screenplay of funny-memes #1 changed; it is now at revision 0. Read it again"
    assert result["isError"] and message in result["content"][0]["text"]
