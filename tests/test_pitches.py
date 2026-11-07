"""Pitches (pautas): the store, the API and the pitch an episode refers to."""

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
from sreditor.episodes import EpisodeInput, EpisodeStore
from sreditor.pagination import encode_cursor
from sreditor.pitch_tools import pitch_error_message
from sreditor.pitches import (
    PitchInput,
    PitchInUse,
    PitchList,
    PitchNotesInput,
    PitchStatus,
    PitchStore,
    PositionTable,
)
from sreditor.research import SourceInput, SourceNotFound
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

PITCHES = "/channels/funny-memes/pitches"
EPISODE_PITCH = "/channels/funny-memes/episodes/1/pitch"
EPISODE_PITCHES = "/channels/funny-memes/episodes/1/pitches"
LEGACY_EPISODE_PITCHES = (
    "CREATE TABLE episode_pitches (channel_slug TEXT NOT NULL, number INTEGER NOT NULL,"
    " pitch_id TEXT, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,"
    " PRIMARY KEY (channel_slug, number))"
)
STUDY = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": "Most people dread Mondays.",
    "accessed_on": "2026-11-01",
}


@pytest.fixture
def pitch_store(tmp_path: Path) -> PitchStore:
    """A store over a database with episode 1 of funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    EpisodeStore(database).create("funny-memes", EpisodeInput(title="Pilot"))
    return PitchStore(database)


def idea(title: str = "Mondays") -> PitchInput:
    return PitchInput(title=title)


# Store


def test_pitch_store_new_pitch_is_an_idea_at_revision_one(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    assert (pitch.status, pitch.revision) == (PitchStatus.IDEA, 1)


def test_pitch_store_list_filters_by_status(pitch_store: PitchStore) -> None:
    pitch_store.create("funny-memes", idea("A"))
    pitch_store.create("funny-memes", PitchInput(title="B", status=PitchStatus.APPROVED))
    approved = pitch_store.list("funny-memes", None, 10, PitchStatus.APPROVED)
    assert [pitch.title for pitch in approved] == ["B"]


def test_pitch_store_replace_stale_revision_raises_changed(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    pitch_store.replace(pitch, idea("New"), 1)
    with pytest.raises(PartChanged) as raised:
        pitch_store.replace(pitch, idea("Again"), 1)
    assert raised.value.current == 2


def test_pitch_store_notes_second_write_expecting_zero_raises_changed(
    pitch_store: PitchStore,
) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    pitch_store.write_notes(pitch.id, PitchNotesInput(text="Lead"), 0)
    with pytest.raises(PartChanged):
        pitch_store.write_notes(pitch.id, PitchNotesInput(text="Other"), 0)


def test_pitch_store_notes_write_with_current_revision_succeeds(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    pitch_store.write_notes(pitch.id, PitchNotesInput(text="Lead"), None)
    assert pitch_store.write_notes(pitch.id, PitchNotesInput(text="More"), 1).revision == 2


def test_pitch_store_source_replace_keeps_date_when_left_out(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    source = pitch_store.add_source(pitch.id, SourceInput.model_validate(STUDY))
    changed = pitch_store.replace_source(
        source, SourceInput(url=STUDY["url"], title="Renamed"), None
    )
    assert changed.accessed_on == source.accessed_on and changed.revision == 2


def test_pitch_store_source_delete_stale_revision_raises_changed(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    source = pitch_store.add_source(pitch.id, SourceInput.model_validate(STUDY))
    pitch_store.replace_source(source, SourceInput.model_validate(STUDY), None)
    with pytest.raises(PartChanged):
        pitch_store.delete_source(source, 1)


def test_pitch_store_source_replace_stale_revision_raises_changed(
    pitch_store: PitchStore,
) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    source = pitch_store.add_source(pitch.id, SourceInput.model_validate(STUDY))
    with pytest.raises(PartChanged):
        pitch_store.replace_source(source, SourceInput.model_validate(STUDY), 5)


def test_pitch_store_unknown_source_position_raises_not_found(pitch_store: PitchStore) -> None:
    with pytest.raises(SourceNotFound):
        pitch_store.position(PositionTable.SOURCES, uuid4())


def test_pitch_store_delete_in_use_raises_with_episodes(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    pitch_store.set_episode_pitches("funny-memes", 1, PitchList(pitch_ids=[pitch.id]), None)
    with pytest.raises(PitchInUse) as raised:
        pitch_store.delete(pitch, None)
    assert raised.value.episodes == [1]


def test_pitch_store_delete_stale_revision_raises_changed(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    with pytest.raises(PartChanged):
        pitch_store.delete(pitch, 7)


def test_pitch_store_episode_never_chosen_has_no_pitch_at_revision_zero(
    pitch_store: PitchStore,
) -> None:
    chosen = pitch_store.episode_pitch("funny-memes", 1)
    assert (chosen.pitch_id, chosen.revision) == (None, 0)


def test_pitch_store_set_episode_pitches_with_current_revision_moves_on(
    pitch_store: PitchStore,
) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    pitch_store.set_episode_pitches("funny-memes", 1, PitchList(pitch_ids=[pitch.id]), 0)
    chosen = pitch_store.set_episode_pitches("funny-memes", 1, PitchList(), 1)
    assert (chosen.pitch_ids, chosen.revision) == ([], 2)


def test_pitch_store_set_episode_pitches_stale_revision_raises_changed(
    pitch_store: PitchStore,
) -> None:
    pitch_store.set_episode_pitches("funny-memes", 1, PitchList(), None)
    with pytest.raises(PartChanged):
        pitch_store.set_episode_pitches("funny-memes", 1, PitchList(), 0)


def test_pitch_store_set_episode_pitches_keeps_the_given_order(pitch_store: PitchStore) -> None:
    first = pitch_store.create("funny-memes", idea("First"))
    second = pitch_store.create("funny-memes", idea("Second"))
    pitch_store.set_episode_pitches(
        "funny-memes", 1, PitchList(pitch_ids=[second.id, first.id]), None
    )
    assert pitch_store.episode_pitches("funny-memes", 1).pitch_ids == [second.id, first.id]


def test_pitch_store_episode_pitch_is_the_first_one(pitch_store: PitchStore) -> None:
    first = pitch_store.create("funny-memes", idea("First"))
    second = pitch_store.create("funny-memes", idea("Second"))
    pitch_store.set_episode_pitches(
        "funny-memes", 1, PitchList(pitch_ids=[second.id, first.id]), None
    )
    assert pitch_store.episode_pitch("funny-memes", 1).pitch_id == second.id


def test_pitch_list_repeating_a_pitch_is_refused() -> None:
    pitch_id = uuid4()
    with pytest.raises(ValueError, match="once"):
        PitchList(pitch_ids=[pitch_id, pitch_id])


def test_pitch_store_delete_pitch_after_the_second_slot_is_still_in_use(
    pitch_store: PitchStore,
) -> None:
    first = pitch_store.create("funny-memes", idea("First"))
    second = pitch_store.create("funny-memes", idea("Second"))
    pitch_store.set_episode_pitches(
        "funny-memes", 1, PitchList(pitch_ids=[first.id, second.id]), None
    )
    with pytest.raises(PitchInUse):
        pitch_store.delete(second, None)


def test_database_single_pitch_references_move_to_position_zero(pitch_store: PitchStore) -> None:
    pitch = pitch_store.create("funny-memes", idea())
    database = pitch_store.database
    with database.connect() as connection:
        connection.execute("DROP TABLE episode_pitches")
        connection.execute("DROP TABLE episode_pitch_revisions")
        connection.execute(LEGACY_EPISODE_PITCHES)
        connection.execute(
            "INSERT INTO episode_pitches VALUES ('funny-memes', 1, ?, 4, '2026-01-01')",
            (str(pitch.id),),
        )
    reopened = PitchStore(Database(database.path))
    chosen = reopened.episode_pitches("funny-memes", 1)
    assert (chosen.pitch_ids, chosen.revision) == ([pitch.id], 4)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def pitch(owner: TestClient) -> dict[str, Any]:
    response = owner.post(PITCHES, json={"title": "Why Mondays hurt", "summary": "A study"})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def pitch_path(pitch: dict[str, Any]) -> str:
    return f"{PITCHES}/{pitch['id']}"


def test_pitch_create_returns_idea_with_id(pitch: dict[str, Any]) -> None:
    assert pitch["status"] == "idea" and pitch["revision"] == 1


def test_pitch_create_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "pitch-1"}
    first = owner.post(PITCHES, json={"title": "Once"}, headers=headers)
    again = owner.post(PITCHES, json={"title": "Once"}, headers=headers)
    assert again.json()["id"] == first.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true"


def test_pitch_create_without_title_returns_unprocessable(owner: TestClient) -> None:
    assert owner.post(PITCHES, json={"title": " "}).status_code == 422


def test_pitch_list_paginates(owner: TestClient, pitch: dict[str, Any]) -> None:
    owner.post(PITCHES, json={"title": "Second"})
    first = owner.get(PITCHES, params={"limit": 1}).json()
    second = owner.get(PITCHES, params={"cursor": first["next_cursor"]}).json()
    assert [item["title"] for item in first["items"] + second["items"]] == [
        "Why Mondays hurt",
        "Second",
    ]
    assert second["next_cursor"] is None


def test_pitch_list_by_status_shows_only_that_status(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    owner.post(PITCHES, json={"title": "Ready", "status": "approved"})
    found = owner.get(PITCHES, params={"status": "approved"}).json()["items"]
    assert [item["title"] for item in found] == ["Ready"]


def test_pitch_list_bad_cursor_returns_bad_request(owner: TestClient) -> None:
    assert owner.get(PITCHES, params={"cursor": encode_cursor("x")}).status_code == 400


def test_pitch_get_returns_etag(owner: TestClient, pitch: dict[str, Any]) -> None:
    assert owner.get(pitch_path(pitch)).headers["etag"] == '"1"'


def test_pitch_get_unknown_returns_not_found_naming_the_fix(owner: TestClient) -> None:
    response = owner.get(f"{PITCHES}/{uuid4()}")
    assert response.status_code == 404 and "list_pitches" in response.json()["detail"]


def test_pitch_put_with_etag_changes_status(owner: TestClient, pitch: dict[str, Any]) -> None:
    body = {"title": "Why Mondays hurt", "status": "research"}
    response = owner.put(pitch_path(pitch), json=body, headers={"If-Match": '"1"'})
    assert response.json()["status"] == "research" and response.headers["etag"] == '"2"'


def test_pitch_put_stale_etag_returns_precondition_failed(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    owner.put(pitch_path(pitch), json={"title": "New"})
    response = owner.put(pitch_path(pitch), json={"title": "X"}, headers={"If-Match": '"1"'})
    assert response.status_code == 412 and response.headers["etag"] == '"2"'


def test_pitch_delete_unused_returns_no_content(owner: TestClient, pitch: dict[str, Any]) -> None:
    assert owner.delete(pitch_path(pitch), headers={"If-Match": '"1"'}).status_code == 204
    assert owner.get(pitch_path(pitch)).status_code == 404


def test_pitch_delete_in_use_returns_conflict_naming_episodes(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    owner.put(EPISODE_PITCH, json={"pitch_id": pitch["id"]})
    response = owner.delete(pitch_path(pitch))
    assert response.status_code == 409 and response.json()["episodes"] == [1]


def test_pitch_notes_put_returns_new_etag(owner: TestClient, pitch: dict[str, Any]) -> None:
    path = f"{pitch_path(pitch)}/research"
    response = owner.put(path, json={"text": "Lead"}, headers={"If-Match": '"0"'})
    assert response.headers["etag"] == '"1"' and owner.get(path).json()["text"] == "Lead"


def test_pitch_source_add_list_and_read(owner: TestClient, pitch: dict[str, Any]) -> None:
    sources = f"{pitch_path(pitch)}/sources"
    created = owner.post(sources, json=STUDY).json()
    owner.post(sources, json={**STUDY, "title": "Second"})
    first = owner.get(sources, params={"limit": 1}).json()
    assert first["items"][0]["id"] == created["id"] and first["next_cursor"]
    assert owner.get(f"{sources}/{created['id']}").headers["etag"] == '"1"'


def test_pitch_source_put_and_delete_with_etags(owner: TestClient, pitch: dict[str, Any]) -> None:
    sources = f"{pitch_path(pitch)}/sources"
    source = owner.post(sources, json=STUDY).json()
    path = f"{sources}/{source['id']}"
    changed = owner.put(path, json={**STUDY, "title": "New"}, headers={"If-Match": '"1"'})
    assert changed.headers["etag"] == '"2"'
    assert owner.delete(path, headers={"If-Match": '"1"'}).status_code == 412
    assert owner.delete(path, headers={"If-Match": '"2"'}).status_code == 204


def test_pitch_source_of_unknown_id_returns_not_found(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    assert owner.get(f"{pitch_path(pitch)}/sources/{uuid4()}").status_code == 404


def test_episode_pitch_unset_reads_null_at_etag_zero(owner: TestClient) -> None:
    response = owner.get(EPISODE_PITCH)
    assert response.json()["pitch_id"] is None and response.headers["etag"] == '"0"'


def test_episode_pitch_set_is_shared_by_several_episodes(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    owner.post("/channels/funny-memes/episodes", json={"title": "Part two"})
    owner.put(EPISODE_PITCH, json={"pitch_id": pitch["id"]}, headers={"If-Match": '"0"'})
    owner.put("/channels/funny-memes/episodes/2/pitch", json={"pitch_id": pitch["id"]})
    assert owner.get(f"{pitch_path(pitch)}/episodes").json() == [1, 2]


def test_episode_pitch_cleared_frees_pitch_for_delete(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    owner.put(EPISODE_PITCH, json={"pitch_id": pitch["id"]})
    owner.put(EPISODE_PITCH, json={"pitch_id": None}, headers={"If-Match": '"1"'})
    assert owner.delete(pitch_path(pitch)).status_code == 204


def two_pitches(owner: TestClient) -> list[str]:
    ids = []
    for title in ("First", "Second"):
        ids.append(owner.post(PITCHES, json={"title": title}).json()["id"])
    return ids


def test_episode_pitches_put_keeps_order_and_returns_etag(owner: TestClient) -> None:
    first, second = two_pitches(owner)
    response = owner.put(
        EPISODE_PITCHES, json={"pitch_ids": [second, first]}, headers={"If-Match": '"0"'}
    )
    assert (response.json()["pitch_ids"], response.headers["etag"]) == ([second, first], '"1"')


def test_episode_pitches_put_stale_etag_returns_precondition_failed(owner: TestClient) -> None:
    owner.put(EPISODE_PITCHES, json={"pitch_ids": []})
    response = owner.put(EPISODE_PITCHES, json={"pitch_ids": []}, headers={"If-Match": '"0"'})
    assert response.status_code == 412


def test_episode_pitches_put_repeated_pitch_returns_unprocessable(owner: TestClient) -> None:
    first, _ = two_pitches(owner)
    response = owner.put(EPISODE_PITCHES, json={"pitch_ids": [first, first]})
    assert response.status_code == 422


def test_episode_pitches_put_unknown_pitch_returns_not_found(owner: TestClient) -> None:
    response = owner.put(EPISODE_PITCHES, json={"pitch_ids": [str(uuid4())]})
    assert response.status_code == 404


def test_episode_pitch_put_replaces_the_whole_list_with_one_pitch(owner: TestClient) -> None:
    first, second = two_pitches(owner)
    owner.put(EPISODE_PITCHES, json={"pitch_ids": [first, second]})
    owner.put(EPISODE_PITCH, json={"pitch_id": second})
    assert owner.get(EPISODE_PITCHES).json()["pitch_ids"] == [second]


def test_episode_pitch_get_shows_the_main_pitch_of_several(owner: TestClient) -> None:
    first, second = two_pitches(owner)
    owner.put(EPISODE_PITCHES, json={"pitch_ids": [second, first]})
    assert owner.get(EPISODE_PITCH).json()["pitch_id"] == second


def test_pitch_delete_used_as_second_pitch_returns_conflict(owner: TestClient) -> None:
    first, second = two_pitches(owner)
    owner.put(EPISODE_PITCHES, json={"pitch_ids": [first, second]})
    assert owner.delete(f"{PITCHES}/{second}").status_code == 409


def test_episode_pitch_of_another_channel_returns_not_found(
    owner: TestClient, pitch: dict[str, Any]
) -> None:
    other = {**FUNNY_MEMES, "slug": "other-channel", "name": "Other", "accounts": []}
    assert owner.post("/channels", json=other).status_code == 201
    owner.post("/channels/other-channel/episodes", json={"title": "Pilot"})
    response = owner.put("/channels/other-channel/episodes/1/pitch", json={"pitch_id": pitch["id"]})
    assert response.status_code == 404


def test_episode_pitch_of_missing_episode_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/funny-memes/episodes/9/pitch").status_code == 404


def test_pitch_create_as_viewer_returns_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(PITCHES, json={"title": "No"}).status_code == 403


def test_pitch_delete_as_contributor_returns_forbidden(
    owner: TestClient,
    pitch: dict[str, Any],
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.delete(pitch_path(pitch)).status_code == 403


def test_pitch_create_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "pitch.create")
    response = agent(app, new_token(owner)).post(PITCHES, json={"title": "Blocked"})
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


def test_pitch_create_with_read_only_token_returns_forbidden(
    app: FastAPI, owner: TestClient
) -> None:
    bot = agent(app, new_token(owner, ["channels:read"]))
    assert bot.post(PITCHES, json={"title": "No"}).status_code == 403


# MCP

ZERO_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def created_pitch(mcp: TestClient, title: str = "Mondays") -> dict[str, Any]:
    result = tool(mcp, "create_pitch", {"slug": "funny-memes", "pitch": {"title": title}})
    pitch: dict[str, Any] = result["structuredContent"]
    return pitch


def test_mcp_create_pitch_then_list_returns_it(mcp: TestClient) -> None:
    created_pitch(mcp)
    found = tool(mcp, "list_pitches", {"slug": "funny-memes", "status": "idea"})
    assert [item["title"] for item in found["structuredContent"]["items"]] == ["Mondays"]


def test_mcp_get_pitch_returns_notes_sources_and_episodes(mcp: TestClient) -> None:
    pitch = created_pitch(mcp)
    ids = {"slug": "funny-memes", "pitch_id": pitch["id"]}
    tool(mcp, "write_pitch_notes", {**ids, "notes": {"text": "Lead"}, "expected_revision": 0})
    tool(mcp, "add_pitch_source", {**ids, "source": STUDY})
    tool(mcp, "set_episode_pitch", {"slug": "funny-memes", "number": 1, "pitch_id": pitch["id"]})
    found = tool(mcp, "get_pitch", ids)["structuredContent"]
    assert found["notes"]["text"] == "Lead" and found["episodes"] == [1]
    assert [item["title"] for item in found["sources"]["items"]] == ["Mondays study"]


def test_mcp_update_pitch_moves_status(mcp: TestClient) -> None:
    pitch = created_pitch(mcp)
    arguments = {
        "slug": "funny-memes",
        "pitch_id": pitch["id"],
        "pitch": {"title": "Mondays", "status": "approved"},
        "expected_revision": 1,
    }
    assert tool(mcp, "update_pitch", arguments)["structuredContent"]["status"] == "approved"


def test_mcp_remove_pitch_source_reports_what_it_removed(mcp: TestClient) -> None:
    pitch = created_pitch(mcp)
    ids = {"slug": "funny-memes", "pitch_id": pitch["id"]}
    added = tool(mcp, "add_pitch_source", {**ids, "source": STUDY})["structuredContent"]
    removed = tool(mcp, "remove_pitch_source", {**ids, "source_id": added["id"]})
    assert removed["structuredContent"] == {"kind": "pitch_source", "id": added["id"]}


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "get_pitch",
            {"slug": "funny-memes", "pitch_id": ZERO_ID},
            f"Pitch {ZERO_ID} not found; call list_pitches",
        ),
        (
            "set_episode_pitch",
            {"slug": "funny-memes", "number": 9, "pitch_id": None},
            "Episode funny-memes #9 not found; call list_episodes",
        ),
    ],
)
def test_mcp_refused_pitch_call_names_the_fix(
    mcp: TestClient, name: str, arguments: dict[str, Any], message: str
) -> None:
    result = tool(mcp, name, arguments)
    assert result["isError"] and message in result["content"][0]["text"]


def test_mcp_remove_unknown_pitch_source_points_at_get_pitch(mcp: TestClient) -> None:
    pitch = created_pitch(mcp)
    arguments = {"slug": "funny-memes", "pitch_id": pitch["id"], "source_id": ZERO_ID}
    result = tool(mcp, "remove_pitch_source", arguments)
    assert result["isError"] and "call get_pitch" in result["content"][0]["text"]


def test_mcp_set_episode_pitches_then_get_research_shows_every_pitch_live(
    mcp: TestClient,
) -> None:
    first, second = created_pitch(mcp, "First"), created_pitch(mcp, "Second")
    tool(
        mcp,
        "write_pitch_notes",
        {"slug": "funny-memes", "pitch_id": second["id"], "notes": {"text": "Live"}},
    )
    arguments = {"slug": "funny-memes", "number": 1, "pitch_ids": [second["id"], first["id"]]}
    assert tool(mcp, "set_episode_pitches", arguments)["structuredContent"]["pitch_ids"] == [
        second["id"],
        first["id"],
    ]
    research = tool(mcp, "get_research", {"slug": "funny-memes", "number": 1})["structuredContent"]
    assert [item["pitch"]["title"] for item in research["pitches"]] == ["Second", "First"]
    assert research["pitches"][0]["notes"]["text"] == "Live"


def test_mcp_set_episode_pitches_repeated_pitch_names_the_fix(mcp: TestClient) -> None:
    pitch = created_pitch(mcp)
    arguments = {"slug": "funny-memes", "number": 1, "pitch_ids": [pitch["id"], pitch["id"]]}
    result = tool(mcp, "set_episode_pitches", arguments)
    assert result["isError"] and "once" in result["content"][0]["text"]


def test_pitch_error_message_in_use_names_set_episode_pitches() -> None:
    message = pitch_error_message(PitchInUse(uuid4(), [1, 3]))
    assert message is not None and "episodes 1, 3" in message and "set_episode_pitches" in message


def test_pitch_error_message_other_error_is_none() -> None:
    assert pitch_error_message(ValueError("x")) is None
