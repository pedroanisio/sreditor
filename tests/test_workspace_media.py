"""The episode's media page: uploading, playing, renaming and removing files."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
MEDIA = f"{EPISODE}/media"
VOICE_OVER = f"{MEDIA}/upload/voice-over"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def send(
    client: TestClient, path: str = VOICE_OVER, content: bytes = sample.MP3_TAGGED, **fields: str
) -> Response:
    return submit(client, path, fields, files={"file": ("take 1.mp3", content, "audio/mpeg")})


def file_id(client: TestClient) -> str:
    """The id of the first file, as its edit dialog carries it."""
    return client.get(MEDIA).text.split('data-dialog="edit-')[1].split('"')[0]


@pytest.fixture
def take(owner: TestClient) -> str:
    owner.get(send(owner).headers["location"])
    return file_id(owner)


def test_episode_page_links_to_media_tab(owner: TestClient) -> None:
    assert f'href="{MEDIA}"' in owner.get(EPISODE).text


def test_media_page_without_files_shows_every_empty_state(owner: TestClient) -> None:
    page = owner.get(MEDIA).text
    assert "No voice-over yet." in page and "No visual effects yet." in page


def test_media_upload_shows_note_name_and_player(owner: TestClient) -> None:
    page = flash_after(owner, send(owner))
    assert "File uploaded." in page and "<strong>take 1</strong>" in page
    assert '<audio class="player"' in page


def test_media_upload_offers_size_limit_to_the_picker(owner: TestClient) -> None:
    assert 'data-max-bytes="99614720"' in owner.get(MEDIA).text


def test_media_upload_wrong_type_shows_error_in_open_dialog(owner: TestClient) -> None:
    response = send(owner, content=sample.MP4)
    assert response.status_code == 422
    text = response.text
    assert 'id="voice-over-file-error"' in text and "MP3, WAV, M4A" in text


def test_media_upload_without_file_asks_for_one(owner: TestClient) -> None:
    response = submit(owner, VOICE_OVER, {"name": "Intro"})
    assert response.status_code == 422 and "Choose a file." in response.text


def test_media_upload_name_too_long_shows_error(owner: TestClient) -> None:
    response = send(owner, name="x" * 200)
    assert 'id="voice-over-name-error"' in response.text


def test_media_upload_unknown_kind_answers_not_found(owner: TestClient) -> None:
    assert send(owner, f"{MEDIA}/upload/trailers").status_code == 404


def test_media_visual_effect_plays_in_video(owner: TestClient) -> None:
    send(owner, f"{MEDIA}/upload/visual-effects", sample.WEBM)
    assert '<video class="player"' in owner.get(MEDIA).text


def test_media_file_plays_with_security_headers(owner: TestClient, take: str) -> None:
    response = owner.get(f"{MEDIA}/{take}/file")
    assert response.content == sample.MP3_TAGGED
    assert response.headers["content-disposition"].startswith("inline")
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_media_file_unknown_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{MEDIA}/nope/file").status_code == 404


def test_media_save_renames_it(owner: TestClient, take: str) -> None:
    response = submit(owner, f"{MEDIA}/{take}", {"name": "Intro", "note": "Best", "revision": "1"})
    assert "<strong>Intro</strong>" in flash_after(owner, response)


def test_media_save_stale_revision_shows_conflict(owner: TestClient, take: str) -> None:
    submit(owner, f"{MEDIA}/{take}", {"name": "Intro", "revision": "1"})
    response = submit(owner, f"{MEDIA}/{take}", {"name": "Outro", "revision": "1"})
    assert response.status_code == 409 and "Reload the page." in response.text


def test_media_remove_drops_it(owner: TestClient, take: str) -> None:
    page = flash_after(owner, submit(owner, f"{MEDIA}/{take}/remove", {"revision": "1"}))
    assert "Removed." in page and "No voice-over yet." in page


def test_media_remove_malformed_id_shows_conflict(owner: TestClient) -> None:
    assert submit(owner, f"{MEDIA}/nope/remove", {"revision": "1"}).status_code == 409


def test_media_page_for_viewer_plays_without_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, take: str
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(MEDIA).text
    assert '<audio class="player"' in page and "upload-voice-over" not in page
    assert send(viewer).status_code == 403


def test_media_page_of_missing_episode_answers_not_found(owner: TestClient) -> None:
    assert owner.get("/app/channels/funny-memes/episodes/8/media").status_code == 404


def test_media_page_in_portuguese_names_the_tab(owner: TestClient) -> None:
    assert "Locução" in owner.get(f"{MEDIA}?lang=pt-BR").text


def test_media_upload_over_rate_limit_answers_429(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    assert send(owner).status_code == 429


@pytest.mark.parametrize(
    "path", [VOICE_OVER, f"{MEDIA}/any", f"{MEDIA}/any/remove"], ids=["upload", "save", "remove"]
)
def test_media_form_with_forged_csrf_is_refused(owner: TestClient, path: str) -> None:
    response = owner.post(path, data={"csrf_token": "forged", "name": "Y"})
    assert response.status_code == 403 and "This form has expired" in response.text


def test_media_upload_returns_to_page_without_fragment(owner: TestClient) -> None:
    # A redirect that changes only the #fragment would not reload, hiding the note.
    assert send(owner).headers["location"] == MEDIA


# Visual style


STYLE = f"{EPISODE}/style"


def add_style(client: TestClient, name: str = "Warm") -> str:
    """Add a style through the library page; its id, as the media page's picker offers it."""
    submit(client, "/app/channels/funny-memes/library/styles", {"name": name})
    return (
        client.get(MEDIA).text.split(f">{name}</option>")[0].rsplit('value="', 1)[1].split('"')[0]
    )


def test_media_page_without_styles_points_to_library(owner: TestClient) -> None:
    page = owner.get(MEDIA).text
    assert "No style picked." in page and 'name="style_id"' not in page


def test_media_style_pick_shows_it(owner: TestClient) -> None:
    style_id = add_style(owner)
    response = submit(owner, STYLE, {"style_id": style_id, "revision": "0"})
    assert response.headers["location"] == MEDIA
    assert "<strong>Warm</strong>" in flash_after(owner, response)


def test_media_style_stale_revision_shows_conflict(owner: TestClient) -> None:
    style_id = add_style(owner)
    submit(owner, STYLE, {"style_id": style_id, "revision": "0"})
    response = submit(owner, STYLE, {"style_id": "", "revision": "0"})
    assert response.status_code == 409 and "Someone picked a style" in response.text


def test_media_style_malformed_id_shows_error(owner: TestClient) -> None:
    add_style(owner)
    response = submit(owner, STYLE, {"style_id": "nope", "revision": "0"})
    assert response.status_code == 422 and "no longer in the library" in response.text


def test_media_style_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, STYLE, {"style_id": ""}).status_code == 403


def test_media_style_with_forged_csrf_is_refused(owner: TestClient) -> None:
    assert owner.post(STYLE, data={"csrf_token": "forged"}).status_code == 403
