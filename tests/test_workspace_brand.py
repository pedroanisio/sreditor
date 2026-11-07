from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from PIL import Image

from sreditor.store import StoreError
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"
TIKTOK = {"platform": "tiktok", "handle": "memes", "url": "", "description": "", "status": "active"}


def png(width: int = 8, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "#ff0066").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    assert submit(client, f"{CHANNEL}/accounts", {**TIKTOK, "revision": ""}).status_code == 303
    client.get("/app")
    return client


def upload(client: TestClient, slot: str, content: bytes, platform: str = "") -> Response:
    files = {"file": ("picture.png", content, "image/png")}
    return submit(client, f"{CHANNEL}/brand/files/{slot}", {"platform": platform}, files=files)


# Accounts


def test_accounts_lists_account_with_its_form(owner: TestClient) -> None:
    page = owner.get(f"{CHANNEL}/accounts").text
    assert "TikTok" in page and 'id="tiktok-description"' in page and ">0/80<" in page


def test_accounts_description_over_limit_answers_422_with_count(owner: TestClient) -> None:
    data = {**TIKTOK, "description": "x" * 81, "revision": ""}
    response = submit(owner, f"{CHANNEL}/accounts", data)
    assert response.status_code == 422
    assert "TikTok allows 80 characters; this has 81." in response.text


def test_accounts_unknown_platform_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts", {**TIKTOK, "platform": "myspace"})
    assert response.status_code == 422 and 'id="new-platform-error"' in response.text


def test_accounts_unknown_status_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts", {**TIKTOK, "status": "gone"})
    assert response.status_code == 422 and 'id="tiktok-status-error"' in response.text


def test_accounts_invalid_url_answers_422_next_to_field(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts", {**TIKTOK, "url": "not a url"})
    assert response.status_code == 422 and 'id="tiktok-url-error"' in response.text


def test_accounts_stale_revision_answers_409(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts", {**TIKTOK, "revision": "1"})
    assert response.status_code == 409


def test_accounts_edit_keeps_brand_overrides(owner: TestClient) -> None:
    upload(owner, "avatar", png(), platform="tiktok")
    submit(owner, f"{CHANNEL}/accounts", {**TIKTOK, "handle": "memes2"})
    page = owner.get(f"{CHANNEL}/brand?platform=tiktok").text
    assert "@memes2" in owner.get(f"{CHANNEL}/accounts").text and '.png" target="_blank"' in page


def test_accounts_remove_account_says_removed(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts/tiktok/remove", {"revision": ""})
    assert "Removed." in flash_after(owner, response)
    assert "No accounts yet" in owner.get(f"{CHANNEL}/accounts").text


def test_accounts_remove_missing_account_answers_404(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts/x/remove", {"revision": ""})
    assert response.status_code == 404 and "Reload the page" in response.text


def test_accounts_remove_stale_revision_answers_409(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/accounts/tiktok/remove", {"revision": "1"})
    assert response.status_code == 409


def test_accounts_viewer_sees_no_forms(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    viewer = join(app, mailer, owner, "vic@example.com", "viewer")
    page = viewer.get(f"{CHANNEL}/accounts").text
    assert "TikTok" in page and 'id="tiktok-handle"' not in page
    assert submit(viewer, f"{CHANNEL}/accounts", TIKTOK).status_code == 403


# Brand files


def test_brand_upload_avatar_stores_and_serves_it(owner: TestClient) -> None:
    response = upload(owner, "avatar", png())
    assert "File uploaded." in flash_after(owner, response)
    page = owner.get(f"{CHANNEL}/brand").text
    src = page.split('class="preview" src="')[1].split('"')[0]
    served = owner.get(src)
    assert served.content == png() and served.headers["content-type"] == "image/png"
    assert "immutable" in served.headers["cache-control"]
    assert served.headers["x-content-type-options"] == "nosniff"


def test_brand_upload_font_is_served_as_attachment(owner: TestClient) -> None:
    font = b"wOF2" + b"\x00" * 60
    files = {"file": ("brand.woff2", font, "font/woff2")}
    assert submit(owner, f"{CHANNEL}/brand/files/font", {}, files=files).status_code == 303
    page = owner.get(f"{CHANNEL}/brand").text
    href = page.split(f'<a href="{CHANNEL}/files/')[1].split('"')[0]
    href = f"{CHANNEL}/files/{href}"
    assert owner.get(href).headers["content-disposition"].startswith("attachment")


def test_brand_upload_wide_avatar_answers_422_with_size(owner: TestClient) -> None:
    response = upload(owner, "avatar", png(16, 8))
    assert response.status_code == 422 and "16 × 8" in response.text


def test_brand_upload_without_file_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/brand/files/logo", {"platform": ""})
    assert response.status_code == 422 and "Choose a file." in response.text


def test_brand_upload_over_rate_limit_answers_429(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    assert upload(owner, "logo", png()).status_code == 429


def test_brand_upload_over_rate_limit_carries_retry_after(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    assert int(upload(owner, "logo", png()).headers["Retry-After"]) > 0


def test_brand_upload_on_platform_shows_on_its_tab_only(owner: TestClient) -> None:
    upload(owner, "logo", png(), platform="tiktok")
    assert "preview" in owner.get(f"{CHANNEL}/brand?platform=tiktok").text
    assert 'class="preview"' not in owner.get(f"{CHANNEL}/brand").text


def test_brand_platform_tab_shows_channel_file_as_inherited(owner: TestClient) -> None:
    upload(owner, "logo", png())
    assert "From the channel" in owner.get(f"{CHANNEL}/brand?platform=tiktok").text


def test_brand_unknown_account_tab_answers_404(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/brand?platform=x").status_code == 404


def test_brand_upload_for_missing_account_answers_404(owner: TestClient) -> None:
    assert upload(owner, "logo", png(), platform="x").status_code == 404


def test_brand_remove_file_empties_slot(owner: TestClient) -> None:
    upload(owner, "logo", png())
    response = submit(owner, f"{CHANNEL}/brand/files/logo/remove", {"platform": ""})
    assert "Removed." in flash_after(owner, response)
    assert 'class="preview"' not in owner.get(f"{CHANNEL}/brand").text


def test_brand_file_not_in_kit_answers_404(owner: TestClient) -> None:
    upload(owner, "logo", png())
    submit(owner, f"{CHANNEL}/brand/files/logo/remove", {"platform": ""})
    assert owner.get(f"{CHANNEL}/files/{'0' * 64}.png").status_code == 404


def test_brand_file_missing_on_disk_answers_404(owner: TestClient, app: FastAPI) -> None:
    upload(owner, "logo", png())
    for stored in (app.state.services.assets.root / "funny-memes").iterdir():
        stored.unlink()
    page = owner.get(f"{CHANNEL}/brand").text
    src = page.split('class="preview" src="')[1].split('"')[0]
    assert owner.get(src).status_code == 404


def test_brand_file_other_member_channel_answers_404(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    upload(owner, "logo", png())
    src = owner.get(f"{CHANNEL}/brand").text.split('class="preview" src="')[1].split('"')[0]
    stranger = member(app, mailer, "bob@example.com")
    assert stranger.get(src).status_code == 404


# Brand colors


def test_brand_color_added_shows_as_chip(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/brand/colors", {"name": "Pink", "hex": "#ff0066"})
    assert "Changes saved." in flash_after(owner, response)
    assert 'fill="#ff0066"' in owner.get(f"{CHANNEL}/brand").text


def test_brand_color_bad_hex_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/brand/colors", {"name": "Pink", "hex": "pink"})
    assert response.status_code == 422 and 'id="color-hex-error"' in response.text


def test_brand_color_stale_revision_answers_409(owner: TestClient) -> None:
    data = {"name": "Pink", "hex": "#ff0066", "revision": "1"}
    assert submit(owner, f"{CHANNEL}/brand/colors", data).status_code == 409


def test_brand_color_removed_is_gone(owner: TestClient) -> None:
    submit(owner, f"{CHANNEL}/brand/colors", {"name": "Pink", "hex": "#ff0066"})
    submit(owner, f"{CHANNEL}/brand/colors/remove", {"name": "pink"})
    assert 'fill="#ff0066"' not in owner.get(f"{CHANNEL}/brand").text


def test_brand_platform_without_colors_shows_channel_colors(owner: TestClient) -> None:
    submit(owner, f"{CHANNEL}/brand/colors", {"name": "Pink", "hex": "#ff0066"})
    page = owner.get(f"{CHANNEL}/brand?platform=tiktok").text
    assert "uses the channel" in page and 'fill="#ff0066"' in page


# Editorial


def test_editorial_purpose_saved_shows_as_set_here(owner: TestClient) -> None:
    data = {"kind": "purpose", "text": "Make people laugh.", "platform": ""}
    response = submit(owner, f"{CHANNEL}/editorial", data)
    page = flash_after(owner, response)
    assert "Make people laugh." in page and "Set here" in page


def test_editorial_platform_tab_shows_channel_guideline_as_inherited(owner: TestClient) -> None:
    submit(owner, f"{CHANNEL}/editorial", {"kind": "voice", "text": "Dry wit.", "platform": ""})
    page = owner.get(f"{CHANNEL}/editorial?platform=tiktok").text
    assert "Dry wit." in page and "From the channel" in page


def test_editorial_house_rule_shows_its_origin(owner: TestClient, app: FastAPI) -> None:
    services = app.state.services
    document = {
        "statement": "Never punch down.",
        "owner": {"type": "service", "id": "test"},
        "category": "editorial.avoid",
        "priority": 1,
    }
    services.stmt.write("house.avoid.down", document, services.stmt.settings.scope, None)
    page = owner.get(f"{CHANNEL}/editorial").text
    assert "Never punch down." in page and "House rule" in page


def test_editorial_unknown_kind_answers_422(owner: TestClient) -> None:
    data = {"kind": "mood", "text": "x", "platform": ""}
    assert submit(owner, f"{CHANNEL}/editorial", data).status_code == 422


def test_editorial_empty_text_answers_422_next_to_field(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/editorial", {"kind": "pillar", "text": " "})
    assert response.status_code == 422 and 'id="pillar-text-error"' in response.text


def test_editorial_store_failure_answers_502(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise StoreError("down")

    monkeypatch.setattr(app.state.services.editorials, "write", fail)
    response = submit(owner, f"{CHANNEL}/editorial", {"kind": "pillar", "text": "Cats"})
    assert response.status_code == 502 and "could not be saved" in response.text


def test_editorial_retire_removes_guideline(owner: TestClient) -> None:
    submit(owner, f"{CHANNEL}/editorial", {"kind": "pillar", "text": "Cats", "platform": "tiktok"})
    page = owner.get(f"{CHANNEL}/editorial?platform=tiktok").text
    statement_id = page.split('name="statement_id" value="')[1].split('"')[0]
    data = {"statement_id": statement_id, "platform": "tiktok"}
    response = submit(owner, f"{CHANNEL}/editorial/retire", data)
    assert "Cats" not in flash_after(owner, response).split("<main")[1]


def test_editorial_retire_unknown_answers_409(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/editorial/retire", {"statement_id": "nope"})
    assert response.status_code == 409 and "already retired" in response.text


def test_editorial_retire_store_failure_answers_502(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise StoreError("down")

    monkeypatch.setattr(app.state.services.editorials, "retire", fail)
    response = submit(owner, f"{CHANNEL}/editorial/retire", {"statement_id": "x"})
    assert response.status_code == 502


def test_editorial_unknown_account_tab_answers_404(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/editorial?platform=x").status_code == 404


def test_editorial_viewer_cannot_write(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    viewer = join(app, mailer, owner, "vic@example.com", "viewer")
    assert 'id="purpose-text"' not in viewer.get(f"{CHANNEL}/editorial").text
    response = submit(viewer, f"{CHANNEL}/editorial", {"kind": "pillar", "text": "Cats"})
    assert response.status_code == 403
