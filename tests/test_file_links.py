"""Signed file links and the MCP tools that issue them (rule agentic.mcp.files)."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.api import create_app
from sreditor.database import utc_now
from sreditor.deps import Services, access_for
from sreditor.file_links import LINK_TTL
from sreditor.file_operations import FileSource, FileTarget, issue_download
from sreditor.media import KIND_FORMATS, MediaInput, MediaKind, NewMedia
from sreditor.renders import NewJob, RenderOutcome, RenderPreset
from sreditor.security import hash_token
from sreditor.settings import AppSettings
from tests import media_samples as sample
from tests import test_srstudio_export as srstudio_export
from tests.conftest import RecordingMailer, fast_passwords
from tests.test_agentic import PUBLIC_URL, agent, block, mcp_call, new_token, tool
from tests.test_srstudio_export import HAS_FFMPEG

SLUG = "funny-memes"
MEDIA = f"/channels/{SLUG}/episodes/1/media"
LIBRARY = f"/channels/{SLUG}/external-media"
INLINE_LIMIT = 1024 * 1024

# The srstudio tests already build an owner with a channel and an episode, and a fully voiced one.
owner = srstudio_export.owner
voice = srstudio_export.voice
rex = srstudio_export.rex
toon_style = srstudio_export.toon_style
take_audio = srstudio_export.take_audio
voiced = srstudio_export.voiced


@pytest.fixture
def moment() -> list[datetime]:
    """The app's clock, which a test moves forward."""
    return [utc_now()]


@pytest.fixture
def app(app_settings: AppSettings, mailer: RecordingMailer, moment: list[datetime]) -> FastAPI:
    return create_app(
        app_settings, mailer=mailer, passwords=fast_passwords(), clock=lambda: moment[0]
    )


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


@pytest.fixture
def visitor(app: FastAPI) -> TestClient:
    """A client with no credentials at all: what an agent's sandbox has."""
    return TestClient(app, base_url=PUBLIC_URL)


def upload_through_api(client: TestClient, content: bytes = sample.MP3_TAGGED) -> dict[str, Any]:
    response = client.post(
        MEDIA, data={"kind": "voice_over"}, files={"file": ("intro.mp3", content)}
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def media_target(media: dict[str, Any]) -> dict[str, Any]:
    return {"source": "episode_media", "number": 1, "media_id": media["id"]}


def file_link(mcp: TestClient, target: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": target, **extra})
    assert not result.get("isError"), result
    offer: dict[str, Any] = result["structuredContent"]
    return offer


def upload_link(mcp: TestClient, **fields: Any) -> dict[str, Any]:
    upload = {"number": 1, "kind": "voice_over", "name": "Take 3", **fields}
    result = tool(mcp, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert not result.get("isError"), result
    offer: dict[str, Any] = result["structuredContent"]
    return offer


def error_text(result: dict[str, Any]) -> str:
    assert result["isError"], result
    text: str = result["content"][0]["text"]
    return text


def services_of(app: FastAPI) -> Services:
    services: Services = app.state.services
    return services


# Download links


def test_file_link_for_media_names_its_type_size_and_public_url(
    mcp: TestClient, owner: TestClient
) -> None:
    offer = file_link(mcp, media_target(upload_through_api(owner)))
    assert offer["url"].startswith(f"{PUBLIC_URL}/files/")
    assert offer["media_type"] == "audio/mpeg" and offer["size_bytes"] == len(sample.MP3_TAGGED)
    assert offer["content_base64"] is None


def test_file_link_expires_fifteen_minutes_after_it_was_issued(
    mcp: TestClient, owner: TestClient
) -> None:
    offer = file_link(mcp, media_target(upload_through_api(owner)))
    expected = datetime.now(UTC) + LINK_TTL
    assert abs(datetime.fromisoformat(offer["expires_at"]) - expected) < timedelta(seconds=30)


def test_file_link_is_stored_only_as_a_hash(
    app: FastAPI, mcp: TestClient, owner: TestClient
) -> None:
    offer = file_link(mcp, media_target(upload_through_api(owner)))
    secret = offer["url"].rsplit("/", 1)[1]
    with services_of(app).file_links.database.connect() as connection:
        stored = [
            row["link_hash"] for row in connection.execute("SELECT link_hash FROM file_links")
        ]
    assert stored == [hash_token(secret)]


def test_file_download_needs_no_credentials_and_sends_the_file(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    response = visitor.get(file_link(mcp, media_target(upload_through_api(owner)))["url"])
    assert response.status_code == 200 and response.content == sample.MP3_TAGGED
    assert response.headers["content-type"] == "audio/mpeg"


def test_file_download_sends_the_security_headers_and_is_not_cached(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    response = visitor.get(file_link(mcp, media_target(upload_through_api(owner)))["url"])
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "private, no-store"


def test_file_download_second_use_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    visitor.get(url)
    again = visitor.get(url)
    assert again.status_code == 410
    assert again.headers["content-type"] == "application/problem+json"
    assert again.json()["type"].endswith("link-gone")


def test_file_download_unknown_link_is_not_found(visitor: TestClient) -> None:
    response = visitor.get("/files/never-issued")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith("link-not-found")


def test_file_download_after_fifteen_minutes_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient, moment: list[datetime]
) -> None:
    url = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    moment[0] += LINK_TTL + timedelta(seconds=1)
    assert visitor.get(url).status_code == 410


def test_file_download_just_before_expiry_still_works(
    mcp: TestClient, owner: TestClient, visitor: TestClient, moment: list[datetime]
) -> None:
    url = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    moment[0] += LINK_TTL - timedelta(seconds=2)
    assert visitor.get(url).status_code == 200


def test_file_download_long_after_expiry_is_forgotten(
    mcp: TestClient, owner: TestClient, visitor: TestClient, moment: list[datetime]
) -> None:
    old = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    moment[0] += timedelta(days=2)
    file_link(mcp, media_target(upload_through_api(owner, sample.MP3_FRAME)))
    assert visitor.get(old).status_code == 404


def test_file_download_after_token_revoked_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    assert owner.delete(f"/me/tokens/{token_id}").status_code == 204
    assert visitor.get(url).status_code == 410


def test_file_download_with_put_is_refused_and_keeps_the_link(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = file_link(mcp, media_target(upload_through_api(owner)))["url"]
    wrong = visitor.put(url, content=sample.MP3_TAGGED)
    assert wrong.status_code == 405 and wrong.headers["allow"] == "GET"
    assert visitor.get(url).status_code == 200


def test_file_link_inline_small_file_carries_its_content(
    mcp: TestClient, owner: TestClient
) -> None:
    offer = file_link(mcp, media_target(upload_through_api(owner)), inline=True)
    assert base64.b64decode(offer["content_base64"]) == sample.MP3_TAGGED


def test_file_link_inline_file_over_one_megabyte_only_has_the_link(
    mcp: TestClient, owner: TestClient
) -> None:
    big = sample.MP3_TAGGED + b"\x00" * INLINE_LIMIT
    offer = file_link(mcp, media_target(upload_through_api(owner, big)), inline=True)
    assert offer["content_base64"] is None and offer["size_bytes"] == len(big)


def test_file_link_inline_file_of_exactly_one_megabyte_is_included(
    mcp: TestClient, owner: TestClient
) -> None:
    exact = sample.MP3_TAGGED + b"\x00" * (INLINE_LIMIT - len(sample.MP3_TAGGED))
    offer = file_link(mcp, media_target(upload_through_api(owner, exact)), inline=True)
    assert len(base64.b64decode(offer["content_base64"])) == INLINE_LIMIT


def test_file_link_unknown_media_says_how_to_find_the_id(mcp: TestClient) -> None:
    target = {"source": "episode_media", "number": 1, "media_id": str(uuid4())}
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    assert "call list_media" in error_text(result)


def test_file_link_missing_field_names_it(mcp: TestClient) -> None:
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": {"source": "episode_media"}})
    assert "needs number" in error_text(result)


def test_file_link_waiver_is_not_a_target(mcp: TestClient) -> None:
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": {"source": "waiver"}})
    assert result["isError"]


def test_file_link_waiver_id_is_not_found_as_media(
    mcp: TestClient, owner: TestClient, app_settings: AppSettings
) -> None:
    waiver = {
        "signer": "Ana",
        "signed_on": "2026-01-02",
        "covers": "Her face",
    }
    sent = owner.post(
        f"/channels/{SLUG}/episodes/1/waivers", data=waiver, files={"file": ("w.pdf", sample.PDF)}
    )
    assert sent.status_code == 201, sent.text
    target = {"source": "episode_media", "number": 1, "media_id": sent.json()["id"]}
    assert "Media" in error_text(tool(mcp, "get_file_link", {"slug": SLUG, "target": target}))


def test_file_link_unknown_channel_is_not_found(mcp: TestClient) -> None:
    result = tool(mcp, "get_file_link", {"slug": "nope", "target": {"source": "brand_file"}})
    assert "list_channels shows" in error_text(result)


def test_file_link_without_read_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["content:write"]), PUBLIC_URL) as client:
        target = media_target(upload_through_api(owner))
        result = tool(client, "get_file_link", {"slug": SLUG, "target": target})
    assert "channels:read" in error_text(result)


def test_file_link_by_session_without_token_is_refused(app: FastAPI, owner: TestClient) -> None:
    services = services_of(app)
    user = services.users.get(UUID(owner.user_id))  # type: ignore[attr-defined]
    assert user is not None
    access = access_for(services, SLUG, user, None)
    media = upload_through_api(owner)
    target = FileTarget(source=FileSource.EPISODE_MEDIA, number=1, media_id=media["id"])
    with pytest.raises(Exception, match="only to API tokens"):
        issue_download(services, access, target)


# Brand files


def test_file_link_brand_avatar_downloads_inline(
    mcp: TestClient, visitor: TestClient, app_settings: AppSettings
) -> None:
    stored = app_settings.assets_dir / "fm" / "avatar.png"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(sample.PNG)
    offer = file_link(mcp, {"source": "brand_file", "brand_slot": "avatar"})
    response = visitor.get(offer["url"])
    assert response.content == sample.PNG and response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline")


def test_file_link_brand_slot_without_file_says_how_to_look(mcp: TestClient) -> None:
    target = {"source": "brand_file", "brand_slot": "logo"}
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    assert "no logo file" in error_text(result) and "get_channel" in error_text(result)


def test_file_link_brand_without_slot_names_the_field(mcp: TestClient) -> None:
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": {"source": "brand_file"}})
    assert "needs brand_slot" in error_text(result)


def test_file_link_brand_of_missing_account_is_not_found(mcp: TestClient) -> None:
    target = {"source": "brand_file", "brand_slot": "avatar", "platform": "youtube"}
    assert "no youtube account" in error_text(
        tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    )


def test_file_link_brand_file_missing_from_storage_is_not_found(mcp: TestClient) -> None:
    target = {"source": "brand_file", "brand_slot": "avatar"}
    assert "missing from storage" in error_text(
        tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    )


# Third-party copies


def test_file_link_external_copy_downloads_its_content(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = owner.post(
        LIBRARY,
        json={
            "kind": "document",
            "title": "Paper",
            "url": "https://example.com/paper",
            "creator": "Ada",
        },
    ).json()
    owner.put(f"{LIBRARY}/{item['id']}/copy", files={"file": ("paper.pdf", sample.PDF)})
    offer = file_link(mcp, {"source": "external_media", "item_id": item["id"]})
    response = visitor.get(offer["url"])
    assert response.content == sample.PDF
    assert response.headers["content-disposition"].startswith("attachment")


def test_file_link_external_item_without_copy_is_not_found(
    mcp: TestClient, owner: TestClient
) -> None:
    item = owner.post(
        LIBRARY,
        json={"kind": "document", "title": "Paper", "url": "https://example.com/p", "creator": "A"},
    ).json()
    result = tool(
        mcp,
        "get_file_link",
        {"slug": SLUG, "target": {"source": "external_media", "item_id": item["id"]}},
    )
    assert "no uploaded copy" in error_text(result)


# Renders


def finished_render(app: FastAPI) -> UUID:
    services = services_of(app)
    stored = services.assets.save_stream(
        SLUG, KIND_FORMATS[MediaKind.RENDER], io.BytesIO(sample.MP4), 1024
    )
    media = services.media.insert(
        NewMedia(channel=SLUG, number=1, kind=MediaKind.RENDER, data=MediaInput(name="Master")),
        stored,
    )
    job = services.renders.add(
        NewJob(
            channel=SLUG,
            number=1,
            preset=RenderPreset.MASTER,
            export_sha256="a" * 64,
            export_path="x",
            engine_version="1",
            requested_by="test",
        )
    )
    return services.renders.finish(job, RenderOutcome(media_id=media.id)).id


def test_file_link_render_downloads_the_finished_video(
    app: FastAPI, mcp: TestClient, visitor: TestClient
) -> None:
    target = {"source": "render", "number": 1, "render_id": str(finished_render(app))}
    response = visitor.get(file_link(mcp, target)["url"])
    assert response.content == sample.MP4 and response.headers["content-type"] == "video/mp4"


def test_file_link_render_media_file_downloads_as_episode_media(
    app: FastAPI, mcp: TestClient, visitor: TestClient
) -> None:
    render_id = finished_render(app)
    video = services_of(app).renders.get(render_id).media_id
    target = {"source": "episode_media", "number": 1, "media_id": str(video)}
    assert visitor.get(file_link(mcp, target)["url"]).content == sample.MP4


def test_file_link_render_not_done_says_to_wait(app: FastAPI, mcp: TestClient) -> None:
    services = services_of(app)
    job = services.renders.add(
        NewJob(
            channel=SLUG,
            number=1,
            preset=RenderPreset.MASTER,
            export_sha256="b" * 64,
            export_path="x",
            engine_version="1",
            requested_by="test",
        )
    )
    target = {"source": "render", "number": 1, "render_id": str(job.id)}
    assert "call get_render" in error_text(
        tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    )


def test_file_link_unknown_render_says_how_to_find_the_id(mcp: TestClient) -> None:
    target = {"source": "render", "number": 1, "render_id": str(uuid4())}
    assert "call list_renders" in error_text(
        tool(mcp, "get_file_link", {"slug": SLUG, "target": target})
    )


# Uploads


def test_upload_link_states_method_limits_and_accepted_types(mcp: TestClient) -> None:
    offer = upload_link(mcp)
    assert offer["method"] == "PUT" and offer["url"].startswith(f"{PUBLIC_URL}/files/")
    assert offer["accepted_types"] == ["audio/mpeg", "audio/wav", "audio/mp4"]
    assert offer["max_bytes"] == 95 * 1024 * 1024


def test_upload_link_for_video_kind_accepts_video_types(mcp: TestClient) -> None:
    assert upload_link(mcp, kind="visual_effect")["accepted_types"] == ["video/mp4", "video/webm"]


def test_upload_creates_the_media_file_like_upload_media(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    offer = upload_link(mcp, name="Take 3", note="fresh")
    sent = visitor.put(offer["url"], content=sample.MP3_TAGGED)
    assert sent.status_code == 201 and sent.headers["etag"] == '"1"'
    listed = owner.get(MEDIA).json()["items"]
    assert [(item["name"], item["note"], item["kind"]) for item in listed] == [
        ("Take 3", "fresh", "voice_over")
    ]
    assert owner.get(f"{MEDIA}/{sent.json()['id']}/content").content == sample.MP3_TAGGED


def test_upload_second_use_is_gone_and_adds_nothing(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = upload_link(mcp)["url"]
    visitor.put(url, content=sample.MP3_TAGGED)
    again = visitor.put(url, content=sample.MP3_FRAME)
    assert again.status_code == 410 and len(owner.get(MEDIA).json()["items"]) == 1


def test_upload_wrong_type_is_refused_and_the_link_stays_usable(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = upload_link(mcp)["url"]
    refused = visitor.put(url, content=sample.PNG)
    assert refused.status_code == 422 and refused.json()["type"].endswith("unsupported-file")
    assert refused.json()["errors"][0]["location"] == ["body", "file"]
    assert owner.get(MEDIA).json()["items"] == []
    assert visitor.put(url, content=sample.MP3_TAGGED).status_code == 201


def test_upload_empty_body_is_refused(mcp: TestClient, visitor: TestClient) -> None:
    assert visitor.put(upload_link(mcp)["url"], content=b"").status_code == 422


def test_upload_video_as_voice_over_is_refused(mcp: TestClient, visitor: TestClient) -> None:
    assert visitor.put(upload_link(mcp)["url"], content=sample.MP4).status_code == 422


def test_upload_over_the_size_limit_is_refused_and_stores_nothing(
    app_settings: AppSettings,
    mailer: RecordingMailer,
    owner: TestClient,
    moment: list[datetime],
) -> None:
    small = create_app(
        app_settings.model_copy(update={"max_media_bytes": 32}),
        mailer=mailer,
        passwords=fast_passwords(),
        clock=lambda: moment[0],
    )
    bare = TestClient(small, base_url=PUBLIC_URL)
    with agent(small, new_token(owner), PUBLIC_URL) as client:
        offer = upload_link(client)
        refused = bare.put(offer["url"], content=sample.WAV + b"\x00" * 64)
        assert offer["max_bytes"] == 32
    assert refused.status_code == 422 and refused.json()["type"].endswith("file-too-large")
    assert not (app_settings.assets_dir / SLUG).exists() or not any(
        (app_settings.assets_dir / SLUG).iterdir()
    )


def test_upload_declared_size_over_the_limit_is_refused_before_reading(
    app_settings: AppSettings,
    mailer: RecordingMailer,
    owner: TestClient,
    moment: list[datetime],
) -> None:
    small = create_app(
        app_settings.model_copy(update={"max_media_bytes": 32}),
        mailer=mailer,
        passwords=fast_passwords(),
        clock=lambda: moment[0],
    )
    bare = TestClient(small, base_url=PUBLIC_URL)
    with agent(small, new_token(owner), PUBLIC_URL) as client:
        url = upload_link(client)["url"]
        refused = bare.put(url, content=b"x" * 64, headers={"Content-Length": "64"})
    assert refused.status_code == 422


def test_upload_render_kind_is_refused_with_the_right_tool(mcp: TestClient) -> None:
    upload = {"number": 1, "kind": "render", "name": "Final"}
    result = tool(mcp, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert "request_render" in error_text(result)


def test_upload_unknown_episode_says_how_to_find_the_number(mcp: TestClient) -> None:
    upload = {"number": 9, "kind": "score", "name": "Theme"}
    result = tool(mcp, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert "call list_episodes" in error_text(result)


def test_upload_link_without_write_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as client:
        upload = {"number": 1, "kind": "score", "name": "Theme"}
        result = tool(client, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert "content:write" in error_text(result)


def test_upload_blocked_by_the_gate_is_refused_and_the_link_stays_usable(
    app: FastAPI,
    owner: TestClient,
    mcp: TestClient,
    visitor: TestClient,
    app_settings: AppSettings,
) -> None:
    block(app_settings, "/", "media.create")
    url = upload_link(mcp)["url"]
    refused = visitor.put(url, content=sample.MP3_TAGGED)
    assert refused.status_code == 403 and refused.json()["decision"] == "BLOCK"
    assert owner.get(MEDIA).json()["items"] == []


def test_upload_after_token_revoked_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = upload_link(mcp)["url"]
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    owner.delete(f"/me/tokens/{token_id}")
    assert visitor.put(url, content=sample.MP3_TAGGED).status_code == 410
    assert owner.get(MEDIA).json()["items"] == []


def test_upload_with_get_is_refused_and_keeps_the_link(
    mcp: TestClient, visitor: TestClient
) -> None:
    url = upload_link(mcp)["url"]
    wrong = visitor.get(url)
    assert wrong.status_code == 405 and wrong.headers["allow"] == "PUT"
    assert visitor.put(url, content=sample.MP3_TAGGED).status_code == 201


def test_upload_link_same_key_returns_the_first_link(mcp: TestClient) -> None:
    upload = {"number": 1, "kind": "score", "name": "Theme"}
    arguments = {"slug": SLUG, "upload": upload, "idempotency_key": "theme-1"}
    first = tool(mcp, "create_upload_link", arguments)["structuredContent"]
    second = tool(mcp, "create_upload_link", arguments)["structuredContent"]
    assert first["url"] == second["url"]


def test_upload_link_same_key_other_arguments_is_refused(mcp: TestClient) -> None:
    first = {"number": 1, "kind": "score", "name": "Theme"}
    tool(mcp, "create_upload_link", {"slug": SLUG, "upload": first, "idempotency_key": "k"})
    other = {**first, "name": "Other"}
    result = tool(
        mcp, "create_upload_link", {"slug": SLUG, "upload": other, "idempotency_key": "k"}
    )
    assert "different request" in error_text(result)


def test_upload_link_without_key_issues_another_each_call(mcp: TestClient) -> None:
    assert upload_link(mcp)["url"] != upload_link(mcp)["url"]


# srstudio export


def test_check_srstudio_export_lists_what_is_missing(mcp: TestClient) -> None:
    result = tool(mcp, "check_srstudio_export", {"slug": SLUG, "number": 1})
    check = result["structuredContent"]
    assert check["ready"] is False and check["file_link"] is None
    assert any("visual style" in message for message in check["missing"])
    assert check["slug"] and isinstance(check["seed"], int)


def test_check_srstudio_export_by_contributor_is_refused(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as client:
        result = tool(client, "check_srstudio_export", {"slug": SLUG, "number": 1})
    assert "channels:write" in error_text(result)


def test_check_srstudio_export_unknown_episode_says_how_to_find_it(mcp: TestClient) -> None:
    result = tool(mcp, "check_srstudio_export", {"slug": SLUG, "number": 9})
    assert "call list_episodes" in error_text(result)


def test_file_link_srstudio_export_without_pieces_is_refused_on_opening_and_keeps_the_link(
    mcp: TestClient, visitor: TestClient
) -> None:
    offer = file_link(mcp, {"source": "srstudio_export", "number": 1})
    assert offer["media_type"] == "application/zip" and offer["size_bytes"] is None
    refused = visitor.get(offer["url"])
    assert refused.status_code == 409 and refused.json()["errors"]
    assert visitor.get(offer["url"]).status_code == 409


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to turn takes into WAV")
def test_check_srstudio_export_when_ready_links_the_zip(
    mcp: TestClient, voiced: srstudio_export.FakeTransport, visitor: TestClient
) -> None:
    check = tool(mcp, "check_srstudio_export", {"slug": SLUG, "number": 1})["structuredContent"]
    assert check["ready"] is True and check["missing"] == []
    response = visitor.get(check["file_link"]["url"])
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "timeline.json" in names and "episode.json" in names
    episode = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read("episode.json"))
    assert response.headers["content-disposition"].endswith(f'{episode["slug"]}.zip"')


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to turn takes into WAV")
def test_check_srstudio_export_zip_link_is_single_use(
    mcp: TestClient, voiced: srstudio_export.FakeTransport, visitor: TestClient
) -> None:
    url = tool(mcp, "check_srstudio_export", {"slug": SLUG, "number": 1})["structuredContent"][
        "file_link"
    ]["url"]
    visitor.get(url)
    assert visitor.get(url).status_code == 410


# Tool catalogue


def test_file_tools_declare_their_annotations(mcp: TestClient) -> None:
    listed = mcp_call(mcp, "tools/list", {})["result"]["tools"]
    hints = {
        item["name"]: item["annotations"]
        for item in listed
        if item["name"] in {"get_file_link", "create_upload_link", "check_srstudio_export"}
    }
    assert hints["check_srstudio_export"]["readOnlyHint"] is True
    assert hints["get_file_link"]["readOnlyHint"] is False
    assert hints["create_upload_link"]["idempotentHint"] is False
    assert all(item["openWorldHint"] is False for item in hints.values())
