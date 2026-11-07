"""The context page of an item: its six answers, links to other items and where it is mentioned."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"
EPISODE = f"{CHANNEL}/items/episode/1"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    client.get(submit(client, f"{CHANNEL}/episodes", {"title": "Pilot"}).headers["location"])
    return client


@pytest.fixture
def rex(owner: TestClient) -> str:
    """Rex, a character of the library, by id."""
    owner.get(submit(owner, f"{CHANNEL}/library/characters", {"name": "Rex"}).headers["location"])
    page = owner.get(f"{CHANNEL}/library").text
    return page.split(f'href="{CHANNEL}/library/characters/')[1].split("/")[0]


def test_episode_page_links_to_its_context(owner: TestClient) -> None:
    assert f'href="{EPISODE}"' in owner.get(f"{CHANNEL}/episodes/1").text


def test_context_page_saves_answers(owner: TestClient) -> None:
    fields = {"what": "A dog fakes a sick day.", "revision": "0"}
    page = flash_after(owner, submit(owner, f"{EPISODE}/context", fields))
    assert "Changes saved." in page and "A dog fakes a sick day." in page


def test_context_page_stale_revision_shows_conflict(owner: TestClient) -> None:
    submit(owner, f"{EPISODE}/context", {"what": "One", "revision": "0"})
    response = submit(owner, f"{EPISODE}/context", {"what": "Two", "revision": "0"})
    assert response.status_code == 409 and "Someone changed these answers" in response.text


def test_context_page_too_long_answer_shows_error_next_to_it(owner: TestClient) -> None:
    response = submit(owner, f"{EPISODE}/context", {"why": "x" * 2001})
    assert response.status_code == 422 and 'id="why-error"' in response.text


def test_context_page_link_to_character_shows_on_both(owner: TestClient, rex: str) -> None:
    fields = {"question": "who", "target": f"character:{rex}", "note": "The dog"}
    page = flash_after(owner, submit(owner, f"{EPISODE}/links", fields))
    assert "Link added." in page and "Rex" in page
    mentioned = owner.get(f"{CHANNEL}/items/character/{rex}").text
    assert "Pilot" in mentioned and f'href="{EPISODE}"' in mentioned


def test_context_page_link_twice_shows_error_next_to_item(owner: TestClient, rex: str) -> None:
    fields = {"question": "who", "target": f"character:{rex}"}
    submit(owner, f"{EPISODE}/links", fields)
    response = submit(owner, f"{EPISODE}/links", fields)
    assert response.status_code == 422 and "already has that link" in response.text


def test_context_page_malformed_target_shows_error(owner: TestClient) -> None:
    response = submit(owner, f"{EPISODE}/links", {"question": "who", "target": "nope"})
    assert response.status_code == 422 and 'id="target-error"' in response.text


def test_context_page_remove_link_drops_it(owner: TestClient, rex: str) -> None:
    submit(owner, f"{EPISODE}/links", {"question": "who", "target": f"character:{rex}"})
    page = owner.get(EPISODE).text
    link_id = page.split('name="link_id" value="')[1].split('"')[0]
    after = flash_after(owner, submit(owner, f"{EPISODE}/links/remove", {"link_id": link_id}))
    assert "Removed." in after and "No links yet." in after


def test_context_page_remove_unknown_link_shows_note(owner: TestClient) -> None:
    response = submit(owner, f"{EPISODE}/links/remove", {"link_id": "x"})
    assert response.status_code == 409 and "already removed" in response.text


def test_context_page_of_unknown_item_is_not_found(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/items/pitch/nope").status_code == 404


def test_context_page_for_viewer_shows_answers_without_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, f"{EPISODE}/context", {"where": "The office"})
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(EPISODE).text
    assert "The office" in page and 'name="where"' not in page


def test_context_page_post_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, f"{EPISODE}/context", {"what": "x"}).status_code == 403
