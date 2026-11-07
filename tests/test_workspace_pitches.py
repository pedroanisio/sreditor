"""The pitch pages: the list, one pitch with its research, and choosing it for an episode."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

PITCHES = "/app/channels/funny-memes/pitches"
RESEARCH = "/app/channels/funny-memes/episodes/1/research"
EPISODE_PITCH = "/app/channels/funny-memes/episodes/1/pitch"
STUDY = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": "Most people dread Mondays.",
    "accessed_on": "2026-11-01",
}


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


@pytest.fixture
def pitch(owner: TestClient) -> str:
    """The address of a new pitch's page."""
    created = submit(owner, PITCHES, {"title": "Why Mondays hurt", "summary": "A study"})
    location: str = created.headers["location"]
    owner.get(location)
    return location


def form_value(page: str, name: str) -> str:
    """The value of the first form field with this name."""
    return page.split(f'name="{name}" value="')[1].split('"')[0]


def test_channel_menu_links_to_pitches(owner: TestClient) -> None:
    assert f'href="{PITCHES}"' in owner.get("/app/channels/funny-memes").text


def test_pitch_list_without_pitches_shows_empty_state(owner: TestClient) -> None:
    assert "No pitches yet" in owner.get(PITCHES).text


def test_pitch_create_opens_it_with_note(owner: TestClient) -> None:
    created = submit(owner, PITCHES, {"title": "Why Mondays hurt"})
    page = flash_after(owner, created)
    assert "Pitch created." in page and "Why Mondays hurt" in page


def test_pitch_create_without_title_shows_error(owner: TestClient) -> None:
    response = submit(owner, PITCHES, {"title": " "})
    assert response.status_code == 422 and 'id="title-error"' in response.text


def test_pitch_list_filtered_by_status_hides_others(owner: TestClient, pitch: str) -> None:
    submit(owner, pitch, {"title": "Why Mondays hurt", "status": "approved", "revision": "1"})
    submit(owner, PITCHES, {"title": "Still an idea"})
    page = owner.get(PITCHES, params={"status": "approved"}).text
    assert "Why Mondays hurt" in page and "Still an idea" not in page


def test_pitch_save_changes_status_with_note(owner: TestClient, pitch: str) -> None:
    response = submit(owner, pitch, {"title": "Mondays", "status": "research", "revision": "1"})
    page = flash_after(owner, response)
    assert "Changes saved." in page and 'value="research" selected' in page


def test_pitch_save_stale_revision_shows_conflict(owner: TestClient, pitch: str) -> None:
    submit(owner, pitch, {"title": "First", "revision": "1"})
    response = submit(owner, pitch, {"title": "Mine", "revision": "1"})
    assert response.status_code == 409 and "Mine" in response.text


def test_pitch_save_too_long_title_shows_error(owner: TestClient, pitch: str) -> None:
    response = submit(owner, pitch, {"title": "x" * 201, "revision": "1"})
    assert response.status_code == 422


def test_pitch_notes_save_keeps_text(owner: TestClient, pitch: str) -> None:
    page = flash_after(owner, submit(owner, f"{pitch}/research", {"text": "Lead", "revision": "0"}))
    assert "Changes saved." in page and "Lead" in page


def test_pitch_notes_stale_revision_shows_conflict(owner: TestClient, pitch: str) -> None:
    submit(owner, f"{pitch}/research", {"text": "First", "revision": "0"})
    response = submit(owner, f"{pitch}/research", {"text": "Mine", "revision": "0"})
    assert response.status_code == 409 and 'id="text-error"' in response.text


def test_pitch_notes_too_long_shows_error(owner: TestClient, pitch: str) -> None:
    response = submit(owner, f"{pitch}/research", {"text": "x" * 20001})
    assert response.status_code == 422


def test_pitch_source_add_lists_it_and_remove_drops_it(owner: TestClient, pitch: str) -> None:
    page = flash_after(owner, submit(owner, f"{pitch}/sources", STUDY))
    assert "Source added." in page and "Mondays study" in page
    fields = {"source_id": form_value(page, "source_id"), "revision": "1"}
    removed = flash_after(owner, submit(owner, f"{pitch}/sources/remove", fields))
    assert "Removed." in removed and "No sources yet." in removed


def test_pitch_source_bad_link_shows_error(owner: TestClient, pitch: str) -> None:
    response = submit(owner, f"{pitch}/sources", {**STUDY, "url": "mondays"})
    assert response.status_code == 422


def test_pitch_source_remove_malformed_id_shows_conflict(owner: TestClient, pitch: str) -> None:
    assert submit(owner, f"{pitch}/sources/remove", {"source_id": "nope"}).status_code == 409


def test_pitch_page_of_unknown_or_malformed_id_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{PITCHES}/{uuid4()}").status_code == 404
    assert owner.get(f"{PITCHES}/nope").status_code == 404


def test_research_page_offers_channel_pitches(owner: TestClient, pitch: str) -> None:
    page = owner.get(RESEARCH).text
    assert "Why Mondays hurt" in page and 'name="pitch_id"' in page


def test_research_page_without_pitches_links_to_add_one(owner: TestClient) -> None:
    assert "The channel has no pitches yet." in owner.get(RESEARCH).text


def test_episode_choose_pitch_shows_its_research(owner: TestClient, pitch: str) -> None:
    submit(owner, f"{pitch}/research", {"text": "Shared lead"})
    submit(owner, f"{pitch}/sources", STUDY)
    pitch_id = pitch.rsplit("/", 1)[1]
    page = flash_after(owner, submit(owner, EPISODE_PITCH, {"pitch_id": pitch_id, "revision": "0"}))
    assert 'id="pitch-research-title"' in page
    assert "Shared lead" in page and "Mondays study" in page
    assert "#1" in owner.get(pitch).text


def test_episode_choose_unknown_pitch_shows_error(owner: TestClient) -> None:
    response = submit(owner, EPISODE_PITCH, {"pitch_id": str(uuid4())})
    assert response.status_code == 422 and 'id="pitch_id-error"' in response.text


def test_episode_choose_malformed_pitch_shows_error(owner: TestClient) -> None:
    assert submit(owner, EPISODE_PITCH, {"pitch_id": "nope"}).status_code == 422


def test_episode_choose_pitch_stale_revision_shows_conflict(owner: TestClient) -> None:
    submit(owner, EPISODE_PITCH, {"pitch_id": ""})
    assert submit(owner, EPISODE_PITCH, {"pitch_id": "", "revision": "0"}).status_code == 409


def test_pitch_delete_in_use_shows_conflict_then_succeeds_once_cleared(
    owner: TestClient, pitch: str
) -> None:
    submit(owner, EPISODE_PITCH, {"pitch_id": pitch.rsplit("/", 1)[1]})
    refused = submit(owner, f"{pitch}/delete", {"revision": "1"})
    assert refused.status_code == 409 and "Episodes still use this pitch." in refused.text
    submit(owner, EPISODE_PITCH, {"pitch_id": ""})
    page = flash_after(owner, submit(owner, f"{pitch}/delete", {"revision": "1"}))
    assert "Pitch deleted." in page


def test_pitch_delete_stale_revision_shows_conflict(owner: TestClient, pitch: str) -> None:
    submit(owner, pitch, {"title": "Changed", "revision": "1"})
    assert submit(owner, f"{pitch}/delete", {"revision": "1"}).status_code == 409


def test_pitch_page_for_viewer_shows_research_without_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, pitch: str
) -> None:
    submit(owner, f"{pitch}/research", {"text": "Lead"})
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(pitch).text
    assert "Lead" in page and 'name="text"' not in page and 'id="danger"' not in page
    assert "This episode is not built on a pitch." in viewer.get(RESEARCH).text


def test_pitch_create_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, PITCHES, {"title": "No"}).status_code == 403


def test_pitch_list_in_portuguese_says_pautas(owner: TestClient) -> None:
    assert "Nenhuma pauta ainda" in owner.get(PITCHES, params={"lang": "pt-BR"}).text
