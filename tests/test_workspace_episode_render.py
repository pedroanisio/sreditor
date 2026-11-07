"""An episode's render page: request, follow, cancel and play renders (rules project.render*)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import render_operations
from sreditor.deps import ChannelAccess, Services
from sreditor.srstudio_export import NO_STYLE, Missing, SrstudioNotReady
from sreditor.web.workspace import episode_render
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
PAGE = f"{EPISODE}/render"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """An episode srstudio can render: nothing missing, and a small zip as its export."""

    def snapshot(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
        target = folder / "episode.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("timeline.json", "first cut")
        return target

    monkeypatch.setattr(render_operations, "snapshot_export", snapshot)
    monkeypatch.setattr(episode_render, "missing_pieces", lambda app, access, number: [])


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Whose Milk"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def queued_id(app: FastAPI) -> str:
    """The id of the episode's newest render."""
    services: Services = app.state.services
    return str(services.renders.of_episode("funny-memes", 1, None, 1)[0].id)


def test_episode_page_shows_render_tab(owner: TestClient) -> None:
    assert f'href="{PAGE}"' in owner.get(EPISODE).text


def test_render_page_without_renders_says_none(owner: TestClient) -> None:
    page = owner.get(PAGE).text
    assert "No renders yet." in page and 'name="preset"' in page


def test_render_page_lists_missing_pieces_instead_of_form(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = [Missing(message=NO_STYLE)]
    monkeypatch.setattr(episode_render, "missing_pieces", lambda app, access, number: missing)
    page = owner.get(PAGE).text
    assert 'name="preset"' not in page and f'href="{EPISODE}/srstudio"' in page


def test_render_request_queues_and_polls(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, PAGE, {"preset": "master"}))
    assert "Render queued." in page and "Queued" in page
    assert 'hx-trigger="every 5s"' in page and "Place in the queue: 1" in page


def test_render_request_again_reuses_earlier(owner: TestClient) -> None:
    submit(owner, PAGE, {"preset": "preview"})
    page = flash_after(owner, submit(owner, PAGE, {"preset": "preview"}))
    assert "its video was reused at no cost" in page


def test_render_request_unknown_preset_shows_error(owner: TestClient) -> None:
    response = submit(owner, PAGE, {"preset": "huge"})
    assert response.status_code == 422 and 'aria-invalid="true"' in response.text


def test_render_request_not_ready_shows_conflict(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
        raise SrstudioNotReady([Missing(message=NO_STYLE)])

    monkeypatch.setattr(render_operations, "snapshot_export", refuse)
    response = submit(owner, PAGE, {"preset": "master"})
    assert response.status_code == 409 and "not ready to render yet" in response.text


def test_render_cancel_stops_queued(app: FastAPI, owner: TestClient) -> None:
    submit(owner, PAGE, {"preset": "master"})
    page = flash_after(owner, submit(owner, f"{PAGE}/{queued_id(app)}/cancel", {}))
    assert "Render canceled." in page and 'hx-trigger="every 5s"' not in page


def test_render_cancel_ended_shows_conflict(app: FastAPI, owner: TestClient) -> None:
    submit(owner, PAGE, {"preset": "master"})
    cancel = f"{PAGE}/{queued_id(app)}/cancel"
    submit(owner, cancel, {})
    response = submit(owner, cancel, {})
    assert response.status_code == 409 and "already ended" in response.text


def test_render_cancel_unknown_answers_not_found(owner: TestClient) -> None:
    assert submit(owner, f"{PAGE}/{UNKNOWN_ID}/cancel", {}).status_code == 404


def test_render_failed_shows_reason(app: FastAPI, owner: TestClient) -> None:
    submit(owner, PAGE, {"preset": "master"})
    services: Services = app.state.services
    job = services.renders.of_episode("funny-memes", 1, None, 1)[0]
    services.renders.fail(job, "scene-render crashed", is_retryable=False)
    page = owner.get(PAGE).text
    assert "Failed" in page and "scene-render crashed" in page


def test_render_page_contributor_sees_list_but_no_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    page = contributor.get(PAGE)
    assert page.status_code == 200 and 'name="preset"' not in page.text
    assert submit(contributor, PAGE, {"preset": "master"}).status_code == 403
