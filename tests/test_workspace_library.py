"""The library page, and adding to the library from a screenplay."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

LIBRARY = "/app/channels/funny-memes/library"
CHARACTERS = f"{LIBRARY}/characters"
LOCATIONS = f"{LIBRARY}/locations"
SCREENPLAY = "/app/channels/funny-memes/episodes/1/screenplay"
PILOT = "INT. KITCHEN - DAY\n\nANA\nIs it Monday again?\n"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    return client


def entry_id(owner: TestClient) -> str:
    """The id of the first entry, as its edit dialog carries it."""
    page = owner.get(LIBRARY).text
    return page.split('data-dialog="edit-')[1].split('"')[0]


@pytest.fixture
def ana(owner: TestClient) -> str:
    owner.get(
        submit(owner, CHARACTERS, {"name": "Ana", "description": "The host."}).headers["location"]
    )
    return entry_id(owner)


def test_channel_menu_links_to_library(owner: TestClient) -> None:
    assert f'href="{LIBRARY}"' in owner.get("/app/channels/funny-memes").text


def test_library_page_without_entries_shows_both_empty_states(owner: TestClient) -> None:
    page = owner.get(LIBRARY).text
    assert "No characters yet" in page and "No locations yet" in page


def test_library_character_add_lists_it_with_note(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, CHARACTERS, {"name": "Ana"}))
    assert "Added to the library." in page and "<strong>Ana</strong>" in page


def test_library_character_add_taken_name_shows_error_in_open_dialog(
    owner: TestClient, ana: str
) -> None:
    response = submit(owner, CHARACTERS, {"name": "ANA"})
    assert response.status_code == 409 and 'id="new-character-name-error"' in response.text
    assert "There is already a character with this name." in response.text


def test_library_location_add_empty_name_shows_error(owner: TestClient) -> None:
    response = submit(owner, LOCATIONS, {"name": " "})
    assert response.status_code == 422 and 'id="new-location-name-error"' in response.text


def test_library_unknown_kind_answers_not_found(owner: TestClient) -> None:
    assert submit(owner, f"{LIBRARY}/vehicles", {"name": "Hat"}).status_code == 404


def test_library_character_save_renames_it(owner: TestClient, ana: str) -> None:
    response = submit(owner, f"{CHARACTERS}/{ana}", {"name": "Ana Maria", "revision": "1"})
    assert "<strong>Ana Maria</strong>" in flash_after(owner, response)


def test_library_character_save_stale_revision_shows_conflict(owner: TestClient, ana: str) -> None:
    submit(owner, f"{CHARACTERS}/{ana}", {"name": "Ana", "revision": "1"})
    response = submit(owner, f"{CHARACTERS}/{ana}", {"name": "Ana B", "revision": "1"})
    assert response.status_code == 409 and "Reload the page." in response.text


def test_library_save_malformed_id_shows_conflict(owner: TestClient) -> None:
    assert submit(owner, f"{CHARACTERS}/nope", {"name": "Ana"}).status_code == 409


def test_library_character_remove_drops_it(owner: TestClient, ana: str) -> None:
    page = flash_after(owner, submit(owner, f"{CHARACTERS}/{ana}/remove", {"revision": "1"}))
    assert "Removed." in page and "No characters yet" in page


def test_library_character_remove_by_contributor_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, ana: str
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    page = contributor.get(LIBRARY).text
    assert f"{CHARACTERS}/{ana}/remove" not in page
    assert submit(contributor, f"{CHARACTERS}/{ana}/remove", {"revision": "1"}).status_code == 403


def test_library_page_for_viewer_shows_entries_without_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, ana: str
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(LIBRARY).text
    assert "<strong>Ana</strong>" in page and 'data-dialog="add-character"' not in page


@pytest.fixture
def written(owner: TestClient) -> TestClient:
    created = submit(owner, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    owner.get(created.headers["location"])
    owner.get(submit(owner, SCREENPLAY, {"text": PILOT}).headers["location"])
    return owner


def test_screenplay_lists_unknown_names_with_add_buttons(written: TestClient) -> None:
    page = written.get(SCREENPLAY).text
    assert page.count("Add to library") == 2 and "<strong>KITCHEN</strong>" in page


def test_screenplay_add_to_library_returns_to_screenplay_matched(written: TestClient) -> None:
    response = submit(written, CHARACTERS, {"name": "ANA", "episode": "1"})
    assert response.headers["location"] == SCREENPLAY
    page = flash_after(written, response)
    assert "Added to the library." in page and "In the library" in page
    assert page.count("Add to library") == 1


def test_library_add_returns_to_page_without_fragment(owner: TestClient) -> None:
    # A redirect that changes only the #fragment would not reload, hiding the note.
    assert submit(owner, CHARACTERS, {"name": "Ana"}).headers["location"] == LIBRARY


def test_library_prop_add_lists_it(owner: TestClient) -> None:
    submit(owner, f"{LIBRARY}/props", {"name": "Laptop"})
    page = owner.get(LIBRARY).text
    assert 'id="props"' in page and "<strong>Laptop</strong>" in page
