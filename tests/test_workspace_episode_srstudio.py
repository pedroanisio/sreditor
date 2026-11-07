"""An episode's srstudio page: slug and seed, the missing pieces and the zip (rule project.srstudio)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.deps import Services
from sreditor.web.workspace import episode_srstudio
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
PAGE = f"{EPISODE}/srstudio"
ZIP = "/channels/funny-memes/episodes/1/srstudio.zip"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Whose Milk"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def test_episode_page_shows_srstudio_tab_to_owner(owner: TestClient) -> None:
    assert f'href="{PAGE}"' in owner.get(EPISODE).text


def test_srstudio_page_proposes_slug_and_seed(owner: TestClient) -> None:
    page = owner.get(PAGE).text
    assert 'value="whose-milk"' in page and 'name="seed"' in page


def test_srstudio_page_lists_missing_pieces_with_links(owner: TestClient) -> None:
    page = owner.get(PAGE).text
    assert "The episode has no short script with spoken blocks." in page
    assert f'href="{EPISODE}/short-script"' in page and f'href="{EPISODE}/media"' in page
    assert ZIP not in page


def test_srstudio_page_when_ready_offers_the_zip(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(episode_srstudio, "missing_pieces", lambda app, access, number: [])
    page = owner.get(PAGE).text
    assert f'href="{ZIP}" download' in page


def test_srstudio_save_changes_slug_and_seed(owner: TestClient) -> None:
    fields = {"revision": "0", "slug": "milk-heist", "seed": "42"}
    page = flash_after(owner, submit(owner, PAGE, fields))
    assert "Changes saved." in page
    assert 'value="milk-heist"' in page and 'value="42"' in page


def test_srstudio_save_bad_slug_shows_error_on_field(owner: TestClient) -> None:
    response = submit(owner, PAGE, {"revision": "0", "slug": "Milk Heist", "seed": "1"})
    assert response.status_code == 422 and 'value="Milk Heist"' in response.text


def test_srstudio_save_taken_slug_shows_conflict(owner: TestClient) -> None:
    submit(owner, "/app/channels/funny-memes/episodes", {"title": "Second"})
    submit(owner, PAGE, {"revision": "0", "slug": "second-one", "seed": "1"})
    second = "/app/channels/funny-memes/episodes/2/srstudio"
    response = submit(owner, second, {"revision": "0", "slug": "second-one", "seed": "2"})
    assert response.status_code == 409
    assert "Another episode of this channel already uses this slug." in response.text


def test_srstudio_save_stale_revision_shows_conflict(owner: TestClient) -> None:
    submit(owner, PAGE, {"revision": "0", "slug": "milk-heist", "seed": "1"})
    response = submit(owner, PAGE, {"revision": "0", "slug": "milk-heist", "seed": "2"})
    assert response.status_code == 409 and "Reload the page" in response.text


def test_srstudio_hidden_from_contributor(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    assert f'href="{PAGE}"' not in contributor.get(EPISODE).text
    assert contributor.get(PAGE).status_code == 403
    assert submit(contributor, PAGE, {"slug": "x", "seed": "1"}).status_code == 403


def lock_episode_slug(app: FastAPI) -> None:
    """Fix the episode's slug as its first srstudio export does."""
    services: Services = app.state.services
    services.srstudio.mark_exported(
        [services.srstudio.episode_slug("funny-memes", 1, "Whose Milk")], []
    )


def test_srstudio_page_after_export_shows_slug_fixed(app: FastAPI, owner: TestClient) -> None:
    lock_episode_slug(app)
    page = owner.get(PAGE).text
    assert 'value="whose-milk" required readonly' in page and "Fixed since the first" in page


def test_srstudio_save_after_export_refuses_new_slug(app: FastAPI, owner: TestClient) -> None:
    lock_episode_slug(app)
    response = submit(owner, PAGE, {"revision": "1", "slug": "milk-heist", "seed": "1"})
    assert response.status_code == 409 and "can no longer change" in response.text


def test_srstudio_save_after_export_keeps_slug_and_saves_seed(
    app: FastAPI, owner: TestClient
) -> None:
    lock_episode_slug(app)
    page = flash_after(
        owner, submit(owner, PAGE, {"revision": "1", "slug": "whose-milk", "seed": "9"})
    )
    assert "Changes saved." in page and 'value="9"' in page
