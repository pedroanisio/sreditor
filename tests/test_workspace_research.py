"""The episode's research page: notes, sources, roles and errors."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
RESEARCH = f"{EPISODE}/research"
SOURCES = f"{EPISODE}/sources"
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


def source_id(owner: TestClient) -> str:
    """The id of the first source, as the page's removal form carries it."""
    page = owner.get(RESEARCH).text
    return page.split('name="source_id" value="')[1].split('"')[0]


@pytest.fixture
def study(owner: TestClient) -> None:
    owner.get(submit(owner, SOURCES, STUDY).headers["location"])


def test_episode_page_links_to_research_tab(owner: TestClient) -> None:
    assert f'href="{RESEARCH}"' in owner.get(EPISODE).text


def test_research_page_without_research_shows_empty_states(owner: TestClient) -> None:
    page = owner.get(RESEARCH).text
    assert "No sources yet." in page and 'name="text"' in page


def test_research_notes_save_keeps_text_with_note(owner: TestClient) -> None:
    response = submit(owner, RESEARCH, {"text": "Lead one\nLead two", "revision": "0"})
    page = flash_after(owner, response)
    assert "Changes saved." in page and "Lead one\nLead two" in page


def test_research_notes_stale_revision_shows_conflict_and_keeps_text(owner: TestClient) -> None:
    submit(owner, RESEARCH, {"text": "First", "revision": "0"})
    response = submit(owner, RESEARCH, {"text": "Mine", "revision": "0"})
    assert response.status_code == 409 and "Mine" in response.text
    assert 'id="text-error"' in response.text


def test_research_notes_too_long_shows_error(owner: TestClient) -> None:
    response = submit(owner, RESEARCH, {"text": "x" * 20001})
    assert response.status_code == 422 and 'id="text-error"' in response.text


def test_research_source_add_lists_it_with_note(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SOURCES, STUDY))
    assert "Source added." in page and "Mondays study" in page and "2026-11-01" in page


def test_research_source_bad_link_shows_error_next_to_field(owner: TestClient) -> None:
    response = submit(owner, SOURCES, {**STUDY, "url": "mondays"})
    assert response.status_code == 422
    assert "Enter a full link, starting with https://." in response.text


def test_research_source_bad_link_in_portuguese_is_translated(owner: TestClient) -> None:
    owner.get(f"{RESEARCH}?lang=pt-BR")
    response = submit(owner, SOURCES, {**STUDY, "url": "mondays"})
    assert "Informe o link completo" in response.text


def test_research_source_bad_date_shows_error(owner: TestClient) -> None:
    response = submit(owner, SOURCES, {**STUDY, "accessed_on": "yesterday"})
    assert 'id="accessed_on-error"' in response.text


def test_research_source_remove_drops_it(owner: TestClient, study: None) -> None:
    fields = {"source_id": source_id(owner), "revision": "1"}
    page = flash_after(owner, submit(owner, f"{SOURCES}/remove", fields))
    assert "Removed." in page and "Mondays study" not in page


def test_research_source_remove_twice_shows_conflict(owner: TestClient, study: None) -> None:
    fields = {"source_id": source_id(owner), "revision": "1"}
    submit(owner, f"{SOURCES}/remove", fields)
    assert submit(owner, f"{SOURCES}/remove", fields).status_code == 409


def test_research_source_remove_malformed_id_shows_conflict(owner: TestClient) -> None:
    response = submit(owner, f"{SOURCES}/remove", {"source_id": "nope"})
    assert response.status_code == 409


def test_research_page_for_viewer_shows_notes_without_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, study: None
) -> None:
    submit(owner, RESEARCH, {"text": "Lead"})
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(RESEARCH).text
    assert "Lead" in page and 'name="text"' not in page and 'id="new-source"' not in page


def test_research_source_add_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, SOURCES, STUDY).status_code == 403


def test_research_page_of_missing_episode_answers_not_found(owner: TestClient) -> None:
    assert owner.get("/app/channels/funny-memes/episodes/8/research").status_code == 404
