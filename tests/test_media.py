"""An episode's media: format checks, storage, API, gate and MCP."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.api import create_app
from sreditor.assets import (
    M4A,
    MP3,
    MP4,
    WAV,
    WEBM,
    AssetStore,
    FileTooLarge,
    UnsupportedFile,
    sniff,
)
from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.media import MediaInput, MediaKind, MediaStore, NewMedia
from sreditor.settings import AppSettings
from tests import media_samples as sample
from tests.conftest import RecordingMailer, fast_passwords
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

MEDIA = "/channels/funny-memes/episodes/1/media"


# Formats


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (sample.MP3_TAGGED, MP3),
        (sample.MP3_FRAME, MP3),
        (sample.WAV, WAV),
        (sample.M4A, M4A),
        (sample.MP4, MP4),
        (sample.WEBM, WEBM),
    ],
)
def test_sniff_media_content_names_its_format(content: bytes, expected: Any) -> None:
    assert sniff(content) == expected


def test_sniff_jpeg_is_not_taken_for_mp3() -> None:
    assert sniff(b"\xff\xd8\xff\xe0" + sample.PADDING) != MP3


# Store


@pytest.fixture
def media_store(tmp_path: Path) -> MediaStore:
    """A store over a database with episode 1 of funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
            " VALUES ('funny-memes', 1, 'Pilot', 'idea', '2026-01-01', '2026-01-01')"
        )
    return MediaStore(database, AssetStore(tmp_path / "assets", 1024), 1024)


def voice_over(kind: MediaKind = MediaKind.VOICE_OVER) -> NewMedia:
    return NewMedia(channel="funny-memes", number=1, kind=kind, data=MediaInput(name="Intro"))


def test_media_add_stores_file_by_hash(media_store: MediaStore) -> None:
    added = media_store.add(voice_over(), io.BytesIO(sample.WAV))
    stored = media_store.assets.path_of(added.stored())
    assert stored.read_bytes() == sample.WAV and stored.name == f"{added.sha256}.wav"


def test_media_add_video_as_voice_over_is_unsupported(media_store: MediaStore) -> None:
    with pytest.raises(UnsupportedFile, match="MP3, WAV, M4A"):
        media_store.add(voice_over(), io.BytesIO(sample.MP4))


def test_media_add_over_limit_leaves_no_file(media_store: MediaStore) -> None:
    with pytest.raises(FileTooLarge):
        media_store.add(voice_over(), io.BytesIO(sample.WAV + b"\x00" * 2048))
    assert list((media_store.assets.root / "funny-memes").iterdir()) == []


def test_media_add_empty_file_is_unsupported(media_store: MediaStore) -> None:
    with pytest.raises(UnsupportedFile):
        media_store.add(voice_over(), io.BytesIO(b""))


def test_media_files_filter_by_kind(media_store: MediaStore) -> None:
    media_store.add(voice_over(), io.BytesIO(sample.WAV))
    effect = media_store.add(voice_over(MediaKind.VISUAL_EFFECT), io.BytesIO(sample.WEBM))
    found = media_store.files("funny-memes", 1, MediaKind.VISUAL_EFFECT, None, 10)
    assert [item.id for item in found] == [effect.id]


def test_media_replace_stale_revision_raises_changed(media_store: MediaStore) -> None:
    added = media_store.add(voice_over(), io.BytesIO(sample.WAV))
    with pytest.raises(PartChanged):
        media_store.replace(added, MediaInput(name="Outro"), 4)


def test_media_delete_stale_revision_raises_changed(media_store: MediaStore) -> None:
    added = media_store.add(voice_over(), io.BytesIO(sample.WAV))
    with pytest.raises(PartChanged):
        media_store.delete(added, 4)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def upload(client: TestClient, content: bytes = sample.MP3_TAGGED, **fields: str) -> Any:
    data = {"kind": "voice_over", **fields}
    return client.post(MEDIA, data=data, files={"file": ("intro take 2.mp3", content)})


@pytest.fixture
def intro(owner: TestClient) -> dict[str, Any]:
    response = upload(owner)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_media_upload_names_it_after_the_file(intro: dict[str, Any]) -> None:
    assert intro["name"] == "intro take 2" and intro["media_type"] == "audio/mpeg"


def test_media_upload_keeps_given_name_and_note(owner: TestClient) -> None:
    created = upload(owner, name="Intro", note="Second take").json()
    assert (created["name"], created["note"]) == ("Intro", "Second take")


def test_media_upload_wrong_type_returns_problem_naming_file(owner: TestClient) -> None:
    response = upload(owner, sample.PNG)
    problem = response.json()
    assert response.status_code == 422 and problem["type"].endswith("unsupported-file")
    assert problem["errors"][0]["location"] == ["body", "file"]


def test_media_upload_over_limit_returns_file_too_large(
    app_settings: AppSettings, mailer: RecordingMailer, sign_up: Callable[..., TestClient]
) -> None:
    small = app_settings.model_copy(update={"max_media_bytes": 32})
    client = TestClient(create_app(small, mailer=mailer, passwords=fast_passwords()))
    client.headers.update(sign_up("owner@example.com").headers)
    client.post("/channels", json=FUNNY_MEMES)
    client.post("/channels/funny-memes/episodes", json={"title": "Pilot"})
    response = upload(client)
    assert response.status_code == 422 and response.json()["type"].endswith("file-too-large")


def test_media_upload_unknown_kind_returns_unprocessable(owner: TestClient) -> None:
    assert upload(owner, kind="trailer").status_code == 422


def test_media_upload_to_missing_episode_returns_not_found(owner: TestClient) -> None:
    response = owner.post(
        "/channels/funny-memes/episodes/9/media",
        data={"kind": "score"},
        files={"file": ("a.mp3", sample.MP3_TAGGED)},
    )
    assert response.status_code == 404


def test_media_upload_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "intro-1"}
    first = owner.post(
        MEDIA, data={"kind": "score"}, files={"file": ("a.mp3", sample.MP3_TAGGED)}, headers=headers
    )
    replay = owner.post(
        MEDIA, data={"kind": "score"}, files={"file": ("a.mp3", sample.MP3_TAGGED)}, headers=headers
    )
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]


def test_media_upload_same_key_other_file_returns_unprocessable(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "intro-1"}
    owner.post(
        MEDIA, data={"kind": "score"}, files={"file": ("a.mp3", sample.MP3_TAGGED)}, headers=headers
    )
    other = owner.post(
        MEDIA, data={"kind": "score"}, files={"file": ("a.mp3", sample.MP3_FRAME)}, headers=headers
    )
    assert other.status_code == 422


def test_media_list_filters_by_kind_and_paginates(owner: TestClient, intro: dict[str, Any]) -> None:
    upload(owner, kind="score")
    upload(owner, kind="score", name="Theme B")
    first = owner.get(MEDIA, params={"kind": "score", "limit": 1}).json()
    second = owner.get(MEDIA, params={"kind": "score", "cursor": first["next_cursor"]}).json()
    assert [item["name"] for item in second["items"]] == ["Theme B"]


def test_media_get_returns_etag(owner: TestClient, intro: dict[str, Any]) -> None:
    assert owner.get(f"{MEDIA}/{intro['id']}").headers["etag"] == '"1"'


def test_media_get_unknown_returns_not_found(owner: TestClient) -> None:
    response = owner.get(f"{MEDIA}/{uuid4()}")
    assert response.status_code == 404 and response.json()["detail"].startswith("Media")


def test_media_download_returns_content_with_type(owner: TestClient, intro: dict[str, Any]) -> None:
    response = owner.get(f"{MEDIA}/{intro['id']}/content")
    assert response.content == sample.MP3_TAGGED
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_media_download_range_returns_partial_content(
    owner: TestClient, intro: dict[str, Any]
) -> None:
    response = owner.get(f"{MEDIA}/{intro['id']}/content", headers={"Range": "bytes=0-2"})
    assert response.status_code == 206 and response.content == b"ID3"


def test_media_download_missing_file_returns_not_found(
    owner: TestClient, intro: dict[str, Any], app_settings: AppSettings
) -> None:
    for stored in (app_settings.assets_dir / "funny-memes").iterdir():
        stored.unlink()
    assert owner.get(f"{MEDIA}/{intro['id']}/content").status_code == 404


def test_media_rename_returns_next_etag(owner: TestClient, intro: dict[str, Any]) -> None:
    response = owner.put(
        f"{MEDIA}/{intro['id']}", json={"name": "Intro"}, headers={"If-Match": '"1"'}
    )
    assert response.headers["etag"] == '"2"' and response.json()["name"] == "Intro"


def test_media_rename_stale_etag_returns_precondition_failed(
    owner: TestClient, intro: dict[str, Any]
) -> None:
    response = owner.put(
        f"{MEDIA}/{intro['id']}", json={"name": "Intro"}, headers={"If-Match": '"7"'}
    )
    assert response.status_code == 412


def test_media_remove_returns_no_content(owner: TestClient, intro: dict[str, Any]) -> None:
    assert owner.delete(f"{MEDIA}/{intro['id']}").status_code == 204
    assert owner.get(MEDIA).json()["items"] == []


def test_media_upload_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert upload(viewer).status_code == 403


def test_media_upload_with_token_blocked_by_gate_stores_nothing(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "media.create")
    response = upload(agent(app, new_token(owner)))
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"
    assert not (app_settings.assets_dir / "funny-memes").exists()


def test_media_goes_with_its_episode(owner: TestClient, intro: dict[str, Any]) -> None:
    owner.delete("/channels/funny-memes")
    owner.post("/channels", json=FUNNY_MEMES)
    owner.post("/channels/funny-memes/episodes", json={"title": "Pilot"})
    assert owner.get(MEDIA).json()["items"] == []


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_list_media_returns_kind_and_size(mcp: TestClient, intro: dict[str, Any]) -> None:
    listed = tool(mcp, "list_media", {"slug": "funny-memes", "number": 1})
    item = listed["structuredContent"]["items"][0]
    assert item["kind"] == "voice_over" and item["size_bytes"] == len(sample.MP3_TAGGED)


def test_mcp_list_media_with_kind_lists_only_that_kind(
    mcp: TestClient, owner: TestClient, intro: dict[str, Any]
) -> None:
    assert upload(owner, kind="score").status_code == 201
    arguments = {"slug": "funny-memes", "number": 1, "kind": "score"}
    listed = tool(mcp, "list_media", arguments)["structuredContent"]["items"]
    assert [item["kind"] for item in listed] == ["score"]


@pytest.mark.parametrize("limit", [0, 201])
def test_mcp_list_media_limit_out_of_range_is_refused(mcp: TestClient, limit: int) -> None:
    result = tool(mcp, "list_media", {"slug": "funny-memes", "number": 1, "limit": limit})
    assert result["isError"]


def test_mcp_remove_media_returns_what_it_removed(mcp: TestClient, intro: dict[str, Any]) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "media_id": intro["id"]}
    removed = tool(mcp, "remove_media", arguments)["structuredContent"]
    assert removed == {"kind": "media", "id": intro["id"]}


def test_mcp_remove_unknown_media_says_how_to_fix_it(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "media_id": str(uuid4())}
    result = tool(mcp, "remove_media", arguments)
    assert result["isError"] and "call list_media" in result["content"][0]["text"]
