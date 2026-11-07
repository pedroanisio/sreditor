"""The episode's screenplay page: writing it, its outline, roles and errors."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.screenplay import SCREENPLAY_LENGTH
from tests.conftest import RecordingMailer
from tests.workspace_helpers import autosave, flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
SCREENPLAY = f"{EPISODE}/screenplay"
PILOT = "INT. KITCHEN - DAY\n\nANA\nIs it Monday again?\n"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def test_episode_page_links_to_screenplay_tab(owner: TestClient) -> None:
    assert f'href="{SCREENPLAY}"' in owner.get(EPISODE).text


def test_screenplay_page_without_screenplay_shows_empty_states(owner: TestClient) -> None:
    page = owner.get(SCREENPLAY).text
    assert "No scenes yet." in page and "No one speaks yet." in page and 'name="text"' in page


def test_screenplay_save_shows_note_scenes_and_characters(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SCREENPLAY, {"text": PILOT, "revision": "0"}))
    assert "Changes saved." in page and "<li>INT. KITCHEN - DAY</li>" in page
    assert "<strong>ANA</strong>" in page and "Not in the library" in page


def test_screenplay_stale_revision_shows_conflict_and_keeps_text(owner: TestClient) -> None:
    submit(owner, SCREENPLAY, {"text": PILOT, "revision": "0"})
    response = submit(owner, SCREENPLAY, {"text": "Mine", "revision": "0"})
    assert response.status_code == 409 and "Mine" in response.text
    assert "Someone changed the screenplay" in response.text


def test_screenplay_autosave_answers_new_revision_without_page(owner: TestClient) -> None:
    response = autosave(owner, SCREENPLAY, {"text": PILOT, "revision": "0"})
    assert response.status_code == 204 and response.headers["Sreditor-Revision"] == "1"
    assert response.content == b"" and "flash" not in response.headers.get("set-cookie", "")


def test_screenplay_autosave_stale_revision_answers_409_without_page(owner: TestClient) -> None:
    autosave(owner, SCREENPLAY, {"text": PILOT, "revision": "0"})
    response = autosave(owner, SCREENPLAY, {"text": "Mine", "revision": "0"})
    assert response.status_code == 409 and response.content == b""


def test_screenplay_autosave_too_long_answers_422(owner: TestClient) -> None:
    response = autosave(owner, SCREENPLAY, {"text": "x" * (SCREENPLAY_LENGTH + 1)})
    assert response.status_code == 422 and response.content == b""


def test_screenplay_form_saves_as_people_type(owner: TestClient) -> None:
    page = owner.get(SCREENPLAY).text
    assert "data-autosave" in page and 'data-saved="Saved at %(time)s"' in page


def test_screenplay_page_shows_preview_beside_editor(owner: TestClient) -> None:
    submit(owner, SCREENPLAY, {"text": PILOT, "revision": "0"})
    page = owner.get(SCREENPLAY).text
    assert 'class="split"' in page and '<p class="screenplay-scene">INT. KITCHEN - DAY</p>' in page
    assert f'hx-get="{SCREENPLAY}/preview"' in page and "app-content is-wide" in page


def test_screenplay_preview_answers_saved_blocks_alone(owner: TestClient) -> None:
    autosave(owner, SCREENPLAY, {"text": PILOT, "revision": "0"})
    fragment = owner.get(f"{SCREENPLAY}/preview").text
    assert '<p class="screenplay-dialogue">Is it Monday again?</p>' in fragment
    assert "<html" not in fragment


def test_screenplay_preview_without_screenplay_says_it_will_show(owner: TestClient) -> None:
    assert "The formatted screenplay shows here" in owner.get(f"{SCREENPLAY}/preview").text


def test_screenplay_preview_unknown_episode_answers_404(owner: TestClient) -> None:
    missing = SCREENPLAY.replace("/episodes/1/", "/episodes/9/")
    assert owner.get(f"{missing}/preview").status_code == 404


def test_screenplay_too_long_shows_error(owner: TestClient) -> None:
    response = submit(owner, SCREENPLAY, {"text": "x" * (SCREENPLAY_LENGTH + 1)})
    assert response.status_code == 422 and 'id="text-error"' in response.text


def test_screenplay_page_for_viewer_shows_formatted_text_without_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, SCREENPLAY, {"text": PILOT})
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(SCREENPLAY).text
    assert '<p class="screenplay-character">ANA</p>' in page and 'name="text"' not in page


def test_screenplay_page_for_viewer_without_screenplay_says_so(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert "No screenplay yet." in viewer.get(SCREENPLAY).text


def test_screenplay_save_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, SCREENPLAY, {"text": PILOT}).status_code == 403


def test_screenplay_page_of_missing_episode_answers_not_found(owner: TestClient) -> None:
    assert owner.get("/app/channels/funny-memes/episodes/8/screenplay").status_code == 404


def test_screenplay_page_in_portuguese_names_the_tab(owner: TestClient) -> None:
    assert "Roteiro" in owner.get(f"{SCREENPLAY}?lang=pt-BR").text
