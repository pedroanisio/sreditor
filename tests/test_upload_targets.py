"""Upload links for third-party copies and brand files, and the brand file API (SRED-11).

Rules agentic.mcp.files and project.brand: one create_upload_link, a target per place, and the
same file rules and permissions as the API calls the links stand for.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from sreditor.api import create_app
from sreditor.file_links import LINK_TTL
from sreditor.members import Role
from sreditor.settings import AppSettings
from tests import media_samples as sample
from tests import test_file_links as links
from tests.conftest import PASSWORD, RecordingMailer, fast_passwords
from tests.test_agentic import PUBLIC_URL, agent, new_token, operation_specs, tool
from tests.test_file_links import error_text

SLUG = "funny-memes"
BRAND = f"/channels/{SLUG}/brand"
LIBRARY = f"/channels/{SLUG}/external-media"
WOFF2 = b"wOF2" + b"\x00" * 60
IMAGE_SLOTS = ["avatar", "logo", "banner", "watermark", "thumbnail_template"]
STING_SLOTS = ["intro", "outro"]

# The file link tests already build an owner with a channel, a clock a test can move and an app.
owner = links.owner
moment = links.moment
app = links.app
mcp = links.mcp
visitor = links.visitor


def png(width: int = 8, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "#ff0066").save(buffer, "PNG")
    return buffer.getvalue()


def link_for(mcp: TestClient, upload: dict[str, Any]) -> dict[str, Any]:
    result = tool(mcp, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert not result.get("isError"), result
    offer: dict[str, Any] = result["structuredContent"]
    return offer


def brand_link(mcp: TestClient, slot: str, **fields: Any) -> dict[str, Any]:
    return link_for(mcp, {"target": "brand_file", "slot": slot, **fields})


def new_item(owner: TestClient, kind: str = "document", title: str = "Paper") -> dict[str, Any]:
    body = {"kind": kind, "title": title, "url": "https://example.com/p", "creator": "Ada"}
    created: dict[str, Any] = owner.post(LIBRARY, json=body).json()
    return created


def copy_link(mcp: TestClient, item: dict[str, Any]) -> dict[str, Any]:
    return link_for(mcp, {"target": "external_copy", "item_id": item["id"]})


def set_font(owner: TestClient, role: str = "heading") -> None:
    patch = {"fonts": {role: {"family": "Bebas Neue", "weight": 700}}}
    assert owner.patch(BRAND, json=patch).status_code == 200


def add_account(owner: TestClient) -> None:
    account = {"platform": "tiktok", "handle": "fm"}
    assert owner.put(f"/channels/{SLUG}/accounts/tiktok", json=account).status_code == 200


def kit(owner: TestClient, platform: str | None = None) -> dict[str, Any]:
    query = {} if platform is None else {"platform": platform}
    found: dict[str, Any] = owner.get(BRAND, params=query).json()
    return found


# Episode media keeps its arguments


def test_upload_link_original_arguments_without_target_still_work(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    offer = link_for(mcp, {"number": 1, "kind": "voice_over", "name": "Take 3"})
    assert offer["accepted_types"] == ["audio/mpeg", "audio/wav", "audio/mp4"]
    assert visitor.put(offer["url"], content=sample.MP3_TAGGED).status_code == 201
    assert [
        item["name"] for item in owner.get(f"/channels/{SLUG}/episodes/1/media").json()["items"]
    ] == ["Take 3"]


def test_upload_link_episode_media_target_is_the_same_as_no_target(
    mcp: TestClient, visitor: TestClient
) -> None:
    upload = {"target": "episode_media", "number": 1, "kind": "cover", "name": "Cover"}
    offer = link_for(mcp, upload)
    assert offer["accepted_types"] == ["image/png", "image/jpeg", "image/webp"]
    assert visitor.put(offer["url"], content=png()).status_code == 201


def test_upload_link_unknown_target_is_refused_naming_the_field(mcp: TestClient) -> None:
    result = tool(
        mcp, "create_upload_link", {"slug": SLUG, "upload": {"target": "waiver", "number": 1}}
    )
    assert result["isError"] and "target" in str(result["content"])


# Third-party copies


def test_upload_link_external_copy_states_the_types_of_the_items_kind(
    mcp: TestClient, owner: TestClient
) -> None:
    offer = copy_link(mcp, new_item(owner))
    assert offer["accepted_types"] == ["application/pdf"]
    assert offer["method"] == "PUT" and offer["max_bytes"] == 95 * 1024 * 1024


def test_upload_external_copy_sets_the_items_copy_like_the_api(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner)
    sent = visitor.put(copy_link(mcp, item)["url"], content=sample.PDF)
    assert sent.status_code == 201 and sent.json()["id"] == item["id"]
    assert sent.headers["etag"] == '"2"'
    stored = owner.get(f"{LIBRARY}/{item['id']}").json()
    assert stored["copy_file"]["media_type"] == "application/pdf"
    assert owner.get(f"{LIBRARY}/{item['id']}/copy/content").content == sample.PDF


def test_upload_external_copy_of_a_shader_takes_utf8_text(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner, "shader", "Glow")
    offer = copy_link(mcp, item)
    assert "text/plain" in offer["accepted_types"] and "application/zip" in offer["accepted_types"]
    assert visitor.put(offer["url"], content=sample.SHADER).status_code == 201


def test_upload_external_copy_of_wrong_content_is_refused_and_the_link_stays(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner)
    url = copy_link(mcp, item)["url"]
    refused = visitor.put(url, content=sample.PNG)
    assert refused.status_code == 422 and refused.json()["type"].endswith("unsupported-file")
    assert owner.get(f"{LIBRARY}/{item['id']}").json()["copy_file"] is None
    assert visitor.put(url, content=sample.PDF).status_code == 201


def test_upload_external_copy_named_as_pdf_but_holding_video_is_refused(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = copy_link(mcp, new_item(owner))["url"]
    assert visitor.put(url, content=sample.MP4).status_code == 422


def test_upload_link_external_copy_of_unknown_item_names_the_listing_tool(
    mcp: TestClient,
) -> None:
    upload = {"target": "external_copy", "item_id": "00000000-0000-0000-0000-000000000001"}
    result = tool(mcp, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert "list_external_media" in error_text(result)


def test_upload_link_external_copy_without_content_scope_names_it(
    app: FastAPI, owner: TestClient
) -> None:
    item = new_item(owner)
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as client:
        upload = {"target": "external_copy", "item_id": item["id"]}
        result = tool(client, "create_upload_link", {"slug": SLUG, "upload": upload})
    assert "content:write" in error_text(result)


def test_upload_external_copy_second_use_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = copy_link(mcp, new_item(owner))["url"]
    visitor.put(url, content=sample.PDF)
    assert visitor.put(url, content=sample.PDF).status_code == 410


def test_upload_external_copy_after_fifteen_minutes_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient, moment: list[datetime]
) -> None:
    item = new_item(owner)
    url = copy_link(mcp, item)["url"]
    moment[0] += LINK_TTL + timedelta(seconds=1)
    assert visitor.put(url, content=sample.PDF).status_code == 410
    assert owner.get(f"{LIBRARY}/{item['id']}").json()["copy_file"] is None


def test_upload_external_copy_of_item_deleted_meanwhile_is_not_found(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner)
    url = copy_link(mcp, item)["url"]
    assert owner.delete(f"{LIBRARY}/{item['id']}").status_code == 204
    assert visitor.put(url, content=sample.PDF).status_code == 404


def test_upload_link_replayed_with_the_same_key_is_marked_and_issues_a_new_link(
    mcp: TestClient, owner: TestClient
) -> None:
    arguments = {
        "slug": SLUG,
        "upload": {"target": "external_copy", "item_id": new_item(owner)["id"]},
        "idempotency_key": "k1",
    }
    first = tool(mcp, "create_upload_link", arguments)
    again = tool(mcp, "create_upload_link", arguments)
    assert again["_meta"]["sreditor/replayed"] is True
    assert first["structuredContent"]["url"] != again["structuredContent"]["url"]


# Remove a copy


def test_remove_external_media_copy_keeps_the_item_and_the_stored_file(
    mcp: TestClient, owner: TestClient, visitor: TestClient, app_settings: AppSettings
) -> None:
    item = new_item(owner)
    visitor.put(copy_link(mcp, item)["url"], content=sample.PDF)
    stored = next(app_settings.assets_dir.rglob("*.pdf"))
    result = tool(mcp, "remove_external_media_copy", {"slug": SLUG, "media_id": item["id"]})
    assert result["structuredContent"]["copy_file"] is None
    assert owner.get(f"{LIBRARY}/{item['id']}").json()["title"] == "Paper"
    assert stored.read_bytes() == sample.PDF


def test_remove_external_media_copy_leaves_another_item_sharing_the_file_intact(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    first = new_item(owner, title="First")
    second = new_item(owner, title="Second")
    for item in (first, second):
        visitor.put(copy_link(mcp, item)["url"], content=sample.PDF)
    tool(mcp, "remove_external_media_copy", {"slug": SLUG, "media_id": first["id"]})
    assert owner.get(f"{LIBRARY}/{second['id']}/copy/content").content == sample.PDF


def test_remove_external_media_copy_of_an_item_in_use_keeps_its_use(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner)
    visitor.put(copy_link(mcp, item)["url"], content=sample.PDF)
    use = {"media_id": item["id"], "scene": "1", "pages": "2"}
    assert owner.post(f"/channels/{SLUG}/episodes/1/external-media", json=use).status_code == 201
    tool(mcp, "remove_external_media_copy", {"slug": SLUG, "media_id": item["id"]})
    uses = owner.get(f"/channels/{SLUG}/episodes/1/external-media").json()["items"]
    assert [entry["media_id"] for entry in uses] == [item["id"]]


def test_remove_external_media_copy_with_stale_revision_is_refused(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    item = new_item(owner)
    visitor.put(copy_link(mcp, item)["url"], content=sample.PDF)
    arguments = {"slug": SLUG, "media_id": item["id"], "expected_revision": 1}
    assert "revision" in error_text(tool(mcp, "remove_external_media_copy", arguments))


# Brand files through links


@pytest.mark.parametrize("slot", IMAGE_SLOTS)
def test_upload_brand_image_slot_fills_the_kit(
    slot: str, mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    offer = brand_link(mcp, slot)
    assert offer["accepted_types"] == ["image/png", "image/jpeg", "image/webp"]
    sent = visitor.put(offer["url"], content=png())
    assert sent.status_code == 201 and sent.headers["etag"] == '"2"'
    found = kit(owner)
    stored = found["avatar"] if slot == "avatar" else found["assets"][slot]
    assert stored["media_type"] == "image/png" and (stored["width"], stored["height"]) == (8, 8)


@pytest.mark.parametrize("slot", STING_SLOTS)
def test_upload_brand_sting_slot_takes_mp4_and_webm_only(
    slot: str, mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    offer = brand_link(mcp, slot)
    assert offer["accepted_types"] == ["video/mp4", "video/webm"]
    assert visitor.put(offer["url"], content=sample.MP4).status_code == 201
    assert kit(owner)["assets"][slot]["media_type"] == "video/mp4"


def test_upload_brand_font_goes_to_the_role_and_keeps_family_and_weight(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    set_font(owner, "caption")
    offer = brand_link(mcp, "font", role="caption")
    assert offer["accepted_types"] == ["font/woff2", "font/ttf", "font/otf"]
    assert visitor.put(offer["url"], content=WOFF2).status_code == 201
    font = kit(owner)["fonts"]["caption"]
    assert (font["family"], font["weight"], font["file"]["media_type"]) == (
        "Bebas Neue",
        700,
        "font/woff2",
    )


def test_upload_link_brand_font_before_its_family_names_update_brand(mcp: TestClient) -> None:
    result = tool(
        mcp,
        "create_upload_link",
        {"slug": SLUG, "upload": {"target": "brand_file", "slot": "font", "role": "heading"}},
    )
    assert "update_brand" in error_text(result) and "heading" in error_text(result)


def test_upload_link_brand_font_without_role_names_the_field(mcp: TestClient) -> None:
    result = tool(
        mcp,
        "create_upload_link",
        {"slug": SLUG, "upload": {"target": "brand_file", "slot": "font"}},
    )
    assert "role" in error_text(result)


def test_upload_link_brand_logo_with_a_role_is_refused(mcp: TestClient) -> None:
    result = tool(
        mcp,
        "create_upload_link",
        {"slug": SLUG, "upload": {"target": "brand_file", "slot": "logo", "role": "body"}},
    )
    assert "Only a font file takes a role" in error_text(result)


def test_upload_brand_logo_holding_a_video_is_refused_by_content_and_the_link_stays(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = brand_link(mcp, "logo")["url"]
    refused = visitor.put(url, content=sample.MP4)
    assert refused.status_code == 422 and refused.json()["type"].endswith("unsupported-file")
    assert refused.json()["errors"][0]["location"] == ["body", "file"]
    assert "logo" not in kit(owner)["assets"]
    assert visitor.put(url, content=png()).status_code == 201


def test_upload_brand_avatar_that_is_not_square_is_refused(
    mcp: TestClient, visitor: TestClient
) -> None:
    refused = visitor.put(brand_link(mcp, "avatar")["url"], content=png(16, 8))
    assert refused.status_code == 422 and "square" in refused.json()["detail"]


def test_upload_brand_file_over_the_limit_is_refused_and_the_offer_states_it(
    app_settings: AppSettings, mailer: RecordingMailer, owner: TestClient, moment: list[datetime]
) -> None:
    small = create_app(
        app_settings.model_copy(update={"max_upload_bytes": 16}),
        mailer=mailer,
        passwords=fast_passwords(),
        clock=lambda: moment[0],
    )
    bare = TestClient(small, base_url=PUBLIC_URL)
    with agent(small, new_token(owner), PUBLIC_URL) as client:
        offer = brand_link(client, "intro")
        refused = bare.put(offer["url"], content=sample.MP4)
    assert offer["max_bytes"] == 16
    assert refused.status_code == 422 and refused.json()["type"].endswith("file-too-large")


def test_upload_brand_file_second_use_is_gone_and_changes_nothing(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = brand_link(mcp, "logo")["url"]
    visitor.put(url, content=png())
    again = visitor.put(url, content=png(9, 9))
    assert again.status_code == 410 and kit(owner)["assets"]["logo"]["width"] == 8


def test_upload_brand_file_after_fifteen_minutes_is_gone(
    mcp: TestClient, owner: TestClient, visitor: TestClient, moment: list[datetime]
) -> None:
    url = brand_link(mcp, "logo")["url"]
    moment[0] += LINK_TTL + timedelta(seconds=1)
    assert visitor.put(url, content=png()).status_code == 410
    assert "logo" not in kit(owner)["assets"]


def test_upload_brand_file_into_an_account_changes_only_its_overrides(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    add_account(owner)
    assert (
        visitor.put(brand_link(mcp, "logo", platform="tiktok")["url"], content=png()).status_code
        == 201
    )
    assert "logo" not in owner.get(f"/channels/{SLUG}").json()["brand"]["assets"]
    assert "logo" in kit(owner, "tiktok")["assets"]


def test_upload_link_brand_for_a_platform_without_an_account_is_refused(mcp: TestClient) -> None:
    result = tool(
        mcp,
        "create_upload_link",
        {"slug": SLUG, "upload": {"target": "brand_file", "slot": "logo", "platform": "youtube"}},
    )
    assert "youtube" in error_text(result)


def test_upload_link_brand_without_channels_write_names_the_scope(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read", "content:write"]), PUBLIC_URL) as client:
        result = tool(
            client,
            "create_upload_link",
            {"slug": SLUG, "upload": {"target": "brand_file", "slot": "logo"}},
        )
    assert "channels:write" in error_text(result)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR])
def test_upload_link_brand_for_a_role_that_cannot_change_the_channel_is_refused(
    role: Role, app: FastAPI, owner: TestClient, mailer: RecordingMailer
) -> None:
    email = f"{role.value}@example.com"
    owner.post(f"/channels/{SLUG}/invitations", json={"email": email, "role": role})
    token = mailer.token_for(email)
    guest = TestClient(app, base_url=PUBLIC_URL)
    guest.post("/auth/register", json={"email": email, "display_name": "G", "password": PASSWORD})
    guest.post("/auth/verify", json={"token": mailer.token_for(email)})
    secret = guest.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
    guest.headers["Authorization"] = f"Bearer {secret}"
    guest.post("/invitations/accept", json={"token": token})
    with agent(app, new_token(guest), PUBLIC_URL) as client:
        result = tool(
            client,
            "create_upload_link",
            {"slug": SLUG, "upload": {"target": "brand_file", "slot": "logo"}},
        )
    assert role.value in error_text(result)


def test_upload_brand_file_after_token_revoked_is_gone_and_changes_nothing(
    mcp: TestClient, owner: TestClient, visitor: TestClient
) -> None:
    url = brand_link(mcp, "logo")["url"]
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    assert owner.delete(f"/me/tokens/{token_id}").status_code == 204
    assert visitor.put(url, content=png()).status_code == 410
    assert "logo" not in kit(owner)["assets"]


def test_waivers_are_not_a_target_of_any_link(mcp: TestClient) -> None:
    refused = tool(
        mcp, "get_file_link", {"slug": SLUG, "target": {"source": "waiver", "number": 1}}
    )
    assert refused["isError"]


# The brand file API


def put_file(
    client: TestClient,
    slot: str,
    content: bytes,
    headers: dict[str, str] | None = None,
    **query: str,
) -> Any:
    return client.put(
        f"{BRAND}/files/{slot}",
        files={"file": ("upload.bin", content)},
        params=query,
        headers=headers or {},
    )


@pytest.mark.parametrize("slot", IMAGE_SLOTS)
def test_api_put_brand_image_returns_the_kit_and_its_etag(slot: str, owner: TestClient) -> None:
    response = put_file(owner, slot, png())
    body = response.json()
    stored = body["avatar"] if slot == "avatar" else body["assets"][slot]
    assert response.status_code == 200 and stored["media_type"] == "image/png"
    assert response.headers["etag"] == '"2"'


@pytest.mark.parametrize("slot", STING_SLOTS)
def test_api_put_brand_sting_takes_video(slot: str, owner: TestClient) -> None:
    response = put_file(owner, slot, sample.MP4)
    assert response.json()["assets"][slot]["media_type"] == "video/mp4"


def test_api_put_brand_font_file_with_role_keeps_the_family(owner: TestClient) -> None:
    set_font(owner)
    font = put_file(owner, "font", WOFF2, role="heading").json()["fonts"]["heading"]
    assert font["family"] == "Bebas Neue" and font["file"]["media_type"] == "font/woff2"


def test_api_put_brand_font_without_role_is_a_422_naming_the_field(owner: TestClient) -> None:
    response = put_file(owner, "font", WOFF2)
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["query", "role"]


def test_api_put_brand_font_before_its_family_is_a_422_naming_role(owner: TestClient) -> None:
    response = put_file(owner, "font", WOFF2, role="body")
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["query", "role"]


def test_api_put_brand_logo_with_wrong_content_is_a_422_naming_file(owner: TestClient) -> None:
    response = put_file(owner, "logo", sample.MP4)
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "file"]


def test_api_put_brand_unknown_slot_is_a_422(owner: TestClient) -> None:
    assert put_file(owner, "poster", png()).status_code == 422


def test_api_put_brand_file_on_account_changes_only_its_overrides(owner: TestClient) -> None:
    add_account(owner)
    assert put_file(owner, "logo", png(), platform="tiktok").status_code == 200
    assert "logo" not in kit(owner)["assets"] and "logo" in kit(owner, "tiktok")["assets"]


def test_api_put_brand_file_on_missing_account_is_a_422(owner: TestClient) -> None:
    assert put_file(owner, "logo", png(), platform="youtube").status_code == 422


def test_api_put_brand_file_with_stale_if_match_is_a_412_and_changes_nothing(
    owner: TestClient,
) -> None:
    response = put_file(owner, "logo", png(), headers={"If-Match": '"99"'})
    assert response.status_code == 412 and "logo" not in kit(owner)["assets"]


def test_api_put_brand_file_with_current_if_match_succeeds(owner: TestClient) -> None:
    current = owner.get(BRAND).headers["etag"]
    assert put_file(owner, "logo", png(), headers={"If-Match": current}).status_code == 200


def test_api_delete_brand_file_empties_the_slot_and_returns_a_new_etag(
    owner: TestClient,
) -> None:
    put_file(owner, "logo", png())
    response = owner.delete(f"{BRAND}/files/logo")
    assert response.status_code == 200 and "logo" not in response.json()["assets"]
    assert response.headers["etag"] == '"3"'


def test_api_delete_brand_font_file_keeps_the_family(owner: TestClient) -> None:
    set_font(owner)
    put_file(owner, "font", WOFF2, role="heading")
    font = owner.delete(f"{BRAND}/files/font", params={"role": "heading"}).json()["fonts"][
        "heading"
    ]
    assert font["family"] == "Bebas Neue" and font["file"] is None


def test_api_delete_brand_file_with_stale_if_match_is_a_412(owner: TestClient) -> None:
    put_file(owner, "logo", png())
    response = owner.delete(f"{BRAND}/files/logo", headers={"If-Match": '"1"'})
    assert response.status_code == 412 and "logo" in kit(owner)["assets"]


def test_api_put_brand_file_without_channels_write_is_a_403(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as client:
        response = put_file(client, "logo", png())
    assert response.status_code == 403 and "channels:write" in response.json()["detail"]


def test_api_brand_file_operations_have_ids_and_descriptions(owner: TestClient) -> None:
    paths = owner.get("/openapi.json").json()["paths"]["/channels/{slug}/brand/files/{slot}"]
    assert paths["put"]["operationId"] == "upload_brand_file"
    assert paths["delete"]["operationId"] == "remove_brand_file"
    assert paths["put"]["description"] and paths["delete"]["description"]


# Clearing a slot through MCP


def update_brand(mcp: TestClient, brand: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = tool(mcp, "update_brand", {"slug": SLUG, "brand": brand, **extra})
    assert not result.get("isError"), result
    found: dict[str, Any] = result["structuredContent"]
    return found


def test_update_brand_clear_files_empties_the_named_slots(
    mcp: TestClient, owner: TestClient
) -> None:
    put_file(owner, "logo", png())
    put_file(owner, "intro", sample.MP4)
    done = update_brand(mcp, {"clear_files": [{"slot": "logo"}]})
    assert "logo" not in done["brand"]["assets"] and "intro" in done["brand"]["assets"]


def test_update_brand_clear_files_clears_a_font_file_not_its_family(
    mcp: TestClient, owner: TestClient
) -> None:
    set_font(owner)
    put_file(owner, "font", WOFF2, role="heading")
    done = update_brand(mcp, {"clear_files": [{"slot": "font", "role": "heading"}]})
    assert done["brand"]["fonts"]["heading"]["family"] == "Bebas Neue"
    assert done["brand"]["fonts"]["heading"]["file"] is None


def test_update_brand_clear_files_of_a_font_role_without_a_font_changes_nothing(
    mcp: TestClient, owner: TestClient
) -> None:
    done = update_brand(mcp, {"clear_files": [{"slot": "font", "role": "body"}]})
    assert done["brand"]["fonts"] == {}


def test_update_brand_clear_files_together_with_colors_is_one_change(
    mcp: TestClient, owner: TestClient
) -> None:
    put_file(owner, "avatar", png())
    colors = [{"name": "Pink", "hex": "#ff0066", "role": "primary"}]
    done = update_brand(mcp, {"colors": colors, "clear_files": [{"slot": "avatar"}]})
    assert done["brand"]["avatar"] is None and done["brand"]["colors"][0]["name"] == "Pink"
    assert done["revision"] == 3


def test_update_brand_clear_files_on_account_shows_the_channels_file_again(
    mcp: TestClient, owner: TestClient
) -> None:
    add_account(owner)
    put_file(owner, "logo", png(8, 8))
    put_file(owner, "logo", png(9, 9), platform="tiktok")
    done = update_brand(mcp, {"clear_files": [{"slot": "logo"}]}, platform="tiktok")
    assert done["brand"]["assets"]["logo"]["width"] == 8


def test_update_brand_clear_files_with_a_font_slot_and_no_role_names_the_field(
    mcp: TestClient,
) -> None:
    result = tool(mcp, "update_brand", {"slug": SLUG, "brand": {"clear_files": [{"slot": "font"}]}})
    assert "role" in str(result["content"])


def test_update_brand_with_nothing_to_change_mentions_clear_files(mcp: TestClient) -> None:
    result = tool(mcp, "update_brand", {"slug": SLUG, "brand": {}})
    assert "clear_files" in error_text(result)


def test_update_brand_clearing_a_slot_keeps_the_file_other_kits_still_use(
    mcp: TestClient, owner: TestClient, app_settings: AppSettings
) -> None:
    add_account(owner)
    put_file(owner, "logo", png())
    put_file(owner, "logo", png(), platform="tiktok")
    update_brand(mcp, {"clear_files": [{"slot": "logo"}]})
    shared = list(Path(app_settings.assets_dir).rglob("*.png"))
    assert len(shared) == 1
    offer = tool(
        mcp,
        "get_file_link",
        {
            "slug": SLUG,
            "target": {"source": "brand_file", "brand_slot": "logo", "platform": "tiktok"},
        },
    )
    assert offer["structuredContent"]["media_type"] == "image/png"


def test_update_brand_description_points_to_create_upload_link(app: FastAPI) -> None:
    listed = list(operation_specs(app).values())
    brand = next(item for item in listed if item["name"] == "update_brand")
    assert "create_upload_link" in brand["description"]
    assert "cannot upload" in brand["description"]
