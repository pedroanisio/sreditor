from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from sreditor.api import create_app
from sreditor.settings import AppSettings
from sreditor.store import StmtClient, StoreError

SHA = "b" * 64
FUNNY_MEMES: dict[str, Any] = {
    "slug": "funny-memes",
    "name": "Funny Memes",
    "brand": {"avatar": {"path": "fm/avatar.png", "media_type": "image/png", "sha256": SHA}},
    "accounts": [{"platform": "tiktok", "handle": "@funnymemes", "description": "Memes."}],
}


@pytest.fixture
def settings(app_settings: AppSettings) -> AppSettings:
    return app_settings


@pytest.fixture
def api(sign_up: Callable[..., TestClient]) -> TestClient:
    """Signed in as the owner of the channels it creates."""
    return sign_up("owner@example.com", "Owner")


@pytest.fixture
def channel(api: TestClient) -> dict[str, Any]:
    response = api.post("/channels", json=FUNNY_MEMES)
    assert response.status_code == 201, response.text
    return response.json()


def stmt(settings: AppSettings, scope: str, *args: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "STMT_DB": settings.store.db,
        "STMT_DOMAIN": settings.store.domain,
        "STMT_SCOPE": scope,
    }
    done = subprocess.run(
        [settings.store.stmt, *args], check=False, env=env, capture_output=True, text=True
    )
    return json.loads(done.stdout)


CHANNEL_URL = "/channels/funny-memes"
EDITORIAL_URL = f"{CHANNEL_URL}/editorial"
TIKTOK_ACCOUNT_URL = f"{CHANNEL_URL}/accounts/tiktok"
YOUTUBE_ACCOUNT_URL = f"{CHANNEL_URL}/accounts/youtube"
TIKTOK_BRAND_AVATAR_PATH = "fm/tiktok/avatar.png"


def write_guideline(api: TestClient, body: dict[str, Any]) -> Response:
    return api.post(EDITORIAL_URL, json=body)


def retire_guideline(api: TestClient, statement_id: str, **params: str) -> Response:
    return api.delete(f"{EDITORIAL_URL}/{statement_id}", params=params)


def read_editorial(api: TestClient, **params: str) -> dict[str, Any]:
    result: dict[str, Any] = api.get(EDITORIAL_URL, params=params).json()
    return result


@pytest.fixture
def youtube_account(api: TestClient, channel: dict[str, Any]) -> dict[str, Any]:
    """A long-described YouTube account added to the channel."""
    youtube = {"platform": "youtube", "handle": "@fm", "description": "Long " * 100}
    response = api.put(YOUTUBE_ACCOUNT_URL, json=youtube)
    assert response.status_code == 200, response.text
    return youtube


@pytest.fixture
def tiktok_brand(api: TestClient, channel: dict[str, Any]) -> None:
    """The TikTok account overrides the channel avatar."""
    avatar = FUNNY_MEMES["brand"]["avatar"]
    tiktok_avatar = {**avatar, "path": TIKTOK_BRAND_AVATAR_PATH}
    account = {**FUNNY_MEMES["accounts"][0], "brand": {"avatar": tiktok_avatar}}
    api.put(TIKTOK_ACCOUNT_URL, json=account)


@pytest.fixture
def house_statements(settings: AppSettings) -> None:
    """House-level editorial statements: one overridable purpose, one other rule, one unknown kind."""
    house = {
        "statement": "Punch up, never down.",
        "owner": {"type": "user", "id": "tester"},
        "category": "editorial.purpose",
        "override_key": "editorial.purpose",
    }
    stmt(settings, "/", "create", "house.purpose", "--json", json.dumps(house))
    stmt(
        settings,
        "/",
        "create",
        "house.rule",
        "--json",
        json.dumps({**house, "category": "other", "override_key": None}),
    )
    unknown_kind = {**house, "category": "editorial.unknown", "override_key": None}
    stmt(settings, "/", "create", "house.unknown", "--json", json.dumps(unknown_kind))


@pytest.fixture
def editorial_api(api: TestClient, channel: dict[str, Any], house_statements: None) -> TestClient:
    """Signed in as the owner of a channel that inherits the house statements."""
    return api


@pytest.fixture
def guidelines(editorial_api: TestClient) -> dict[str, Any]:
    """Channel guidelines of every kind plus a TikTok purpose; returns the pillar."""
    write_guideline(editorial_api, {"kind": "purpose", "text": "Make everyday frustrations funny."})
    write_guideline(editorial_api, {"kind": "purpose", "text": "Make Mondays funny."})
    write_guideline(editorial_api, {"kind": "voice", "text": "Deadpan."})
    write_guideline(editorial_api, {"kind": "audience", "text": "Office workers."})
    pillar: dict[str, Any] = write_guideline(
        editorial_api, {"kind": "pillar", "text": "Office life."}
    ).json()
    write_guideline(editorial_api, {"kind": "avoid", "text": "Never mock appearance."})
    write_guideline(
        editorial_api, {"kind": "purpose", "text": "Fast laughs.", "platform": "tiktok"}
    )
    return pillar


def test_health_without_session_returns_ok(app: FastAPI) -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/channels"), ("post", "/channels"), ("get", CHANNEL_URL)],
)
def test_channels_without_session_return_unauthorized(app: FastAPI, method: str, path: str) -> None:
    anonymous = TestClient(app)
    response = anonymous.request(method, path, json=FUNNY_MEMES if method == "post" else None)
    assert response.status_code == 401


def test_channel_create_valid_body_returns_draft_in_portuguese(channel: dict[str, Any]) -> None:
    assert channel["status"] == "draft" and channel["language"] == "pt-BR"


def test_channel_list_after_create_returns_the_channel(
    api: TestClient, channel: dict[str, Any]
) -> None:
    assert api.get("/channels").json() == {"items": [channel], "next_cursor": None}


def test_channel_get_existing_slug_returns_the_channel(
    api: TestClient, channel: dict[str, Any]
) -> None:
    assert api.get(CHANNEL_URL).json() == channel


def test_channel_create_duplicate_slug_returns_conflict(
    api: TestClient, channel: dict[str, Any]
) -> None:
    assert api.post("/channels", json=FUNNY_MEMES).status_code == 409


def test_channel_update_status_returns_updated_channel(
    api: TestClient, channel: dict[str, Any]
) -> None:
    updated = {**channel, "status": "active"}
    assert api.put(CHANNEL_URL, json=updated).json()["status"] == "active"


def test_channel_update_changed_slug_returns_unprocessable(
    api: TestClient, channel: dict[str, Any]
) -> None:
    renamed = {**channel, "status": "active", "slug": "other"}
    assert api.put(CHANNEL_URL, json=renamed).status_code == 422


def test_channel_update_unknown_slug_returns_not_found(
    api: TestClient, channel: dict[str, Any]
) -> None:
    missing = {**channel, "status": "active", "slug": "nope"}
    assert api.put("/channels/nope", json=missing).status_code == 404


def test_channel_delete_existing_returns_no_content(
    api: TestClient, channel: dict[str, Any]
) -> None:
    assert api.delete(CHANNEL_URL).status_code == 204


def test_channel_get_after_delete_returns_not_found(
    api: TestClient, channel: dict[str, Any]
) -> None:
    api.delete(CHANNEL_URL)
    assert api.get(CHANNEL_URL).status_code == 404


def test_channel_delete_twice_returns_not_found(api: TestClient, channel: dict[str, Any]) -> None:
    api.delete(CHANNEL_URL)
    assert api.delete(CHANNEL_URL).status_code == 404


def test_channel_create_registers_stmt_scopes_for_its_accounts(
    settings: AppSettings, channel: dict[str, Any]
) -> None:
    assert stmt(settings, "/funny-memes/tiktok", "list")["ok"] is True
    assert stmt(settings, "/funny-memes/youtube", "list")["error"]["code"] == "NOT_FOUND"


def test_channel_create_invalid_body_returns_unprocessable(api: TestClient) -> None:
    assert api.post("/channels", json={"slug": "Bad Slug", "name": "x"}).status_code == 422


def test_account_put_new_platform_adds_it_to_the_channel(
    api: TestClient, channel: dict[str, Any]
) -> None:
    youtube = {"platform": "youtube", "handle": "@fm", "description": "Long " * 100}
    response = api.put(YOUTUBE_ACCOUNT_URL, json=youtube)
    assert response.status_code == 200
    assert {account["platform"] for account in response.json()["accounts"]} == {
        "tiktok",
        "youtube",
    }


def test_account_put_new_platform_registers_stmt_scope(
    settings: AppSettings, youtube_account: dict[str, Any]
) -> None:
    assert stmt(settings, "/funny-memes/youtube", "list")["ok"] is True


def test_account_put_existing_platform_replaces_it(
    api: TestClient, youtube_account: dict[str, Any]
) -> None:
    replaced = {"platform": "tiktok", "handle": "@new"}
    accounts = api.put(TIKTOK_ACCOUNT_URL, json=replaced).json()["accounts"]
    assert [account["handle"] for account in accounts if account["platform"] == "tiktok"] == [
        "@new"
    ]


def test_account_put_too_long_description_returns_unprocessable(
    api: TestClient, channel: dict[str, Any]
) -> None:
    too_long = {"platform": "tiktok", "handle": "@a", "description": "x" * 81}
    assert api.put(TIKTOK_ACCOUNT_URL, json=too_long).status_code == 422


def test_account_put_unknown_platform_returns_unprocessable(
    api: TestClient, channel: dict[str, Any]
) -> None:
    replaced = {"platform": "tiktok", "handle": "@new"}
    assert api.put(f"{CHANNEL_URL}/accounts/x", json=replaced).status_code == 422


def test_account_put_unknown_channel_returns_not_found(
    api: TestClient, channel: dict[str, Any]
) -> None:
    replaced = {"platform": "tiktok", "handle": "@new"}
    assert api.put("/channels/nope/accounts/tiktok", json=replaced).status_code == 404


def test_account_delete_existing_returns_the_remaining_accounts(
    api: TestClient, youtube_account: dict[str, Any]
) -> None:
    remaining = api.delete(YOUTUBE_ACCOUNT_URL).json()["accounts"]
    assert [account["platform"] for account in remaining] == ["tiktok"]


def test_account_delete_twice_returns_not_found(
    api: TestClient, youtube_account: dict[str, Any]
) -> None:
    api.delete(YOUTUBE_ACCOUNT_URL)
    assert api.delete(YOUTUBE_ACCOUNT_URL).status_code == 404


def test_brand_without_platform_returns_channel_avatar(api: TestClient, tiktok_brand: None) -> None:
    avatar = FUNNY_MEMES["brand"]["avatar"]
    assert api.get(f"{CHANNEL_URL}/brand").json()["avatar"]["path"] == avatar["path"]


def test_brand_with_platform_returns_platform_avatar(api: TestClient, tiktok_brand: None) -> None:
    tiktok = api.get(f"{CHANNEL_URL}/brand", params={"platform": "tiktok"}).json()
    assert tiktok["avatar"]["path"] == TIKTOK_BRAND_AVATAR_PATH


def test_brand_unknown_platform_returns_unprocessable(
    api: TestClient, channel: dict[str, Any]
) -> None:
    assert api.get(f"{CHANNEL_URL}/brand", params={"platform": "x"}).status_code == 422


def test_editorial_get_with_house_purpose_returns_house_layer(editorial_api: TestClient) -> None:
    editorial = read_editorial(editorial_api)
    assert editorial["purpose"]["id"] == "house.purpose"
    assert editorial["purpose"]["scope"] == "/"


def test_editorial_write_purpose_returns_created_statement(editorial_api: TestClient) -> None:
    purpose = write_guideline(
        editorial_api, {"kind": "purpose", "text": "Make everyday frustrations funny."}
    )
    assert purpose.status_code == 201
    assert purpose.json() == {
        "id": "funny-memes.purpose",
        "kind": "purpose",
        "text": "Make everyday frustrations funny.",
        "scope": "/funny-memes",
        "revision": 1,
        "citation": None,
    }


def test_editorial_write_purpose_twice_increments_revision(editorial_api: TestClient) -> None:
    write_guideline(editorial_api, {"kind": "purpose", "text": "Make everyday frustrations funny."})
    rewritten = write_guideline(editorial_api, {"kind": "purpose", "text": "Make Mondays funny."})
    assert rewritten.json()["revision"] == 2


def test_editorial_write_platform_purpose_returns_platform_scoped_id(
    editorial_api: TestClient,
) -> None:
    tiktok_purpose = write_guideline(
        editorial_api, {"kind": "purpose", "text": "Fast laughs.", "platform": "tiktok"}
    ).json()
    assert tiktok_purpose["id"] == "funny-memes.tiktok.purpose"


def test_editorial_write_for_platform_without_account_returns_unprocessable(
    editorial_api: TestClient,
) -> None:
    body = {"kind": "pillar", "text": "x", "platform": "youtube"}
    assert write_guideline(editorial_api, body).status_code == 422


def test_editorial_write_empty_text_returns_unprocessable(editorial_api: TestClient) -> None:
    assert write_guideline(editorial_api, {"kind": "pillar", "text": ""}).status_code == 422


def test_editorial_get_after_rewrite_returns_latest_purpose(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    assert read_editorial(editorial_api)["purpose"]["text"] == "Make Mondays funny."


def test_editorial_get_returns_voice_and_audience(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    channel_view = read_editorial(editorial_api)
    assert (channel_view["voice"]["text"], channel_view["audience"]["text"]) == (
        "Deadpan.",
        "Office workers.",
    )


def test_editorial_get_returns_pillars(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    pillars = read_editorial(editorial_api)["pillars"]
    assert [pillar["text"] for pillar in pillars] == ["Office life."]


def test_editorial_get_returns_avoid_list(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    avoid = read_editorial(editorial_api)["avoid"]
    assert [item["text"] for item in avoid] == ["Never mock appearance."]


def test_editorial_get_with_platform_returns_platform_purpose(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    tiktok_view = read_editorial(editorial_api, platform="tiktok")
    assert tiktok_view["purpose"]["text"] == "Fast laughs."


def test_editorial_get_with_platform_inherits_channel_pillars(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    tiktok_view = read_editorial(editorial_api, platform="tiktok")
    assert tiktok_view["pillars"] == read_editorial(editorial_api)["pillars"]


def test_editorial_delete_with_stale_if_match_returns_412_and_current_etag(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    stale = f'"{guidelines["revision"] + 1}"'
    url = f"{EDITORIAL_URL}/{guidelines['id']}"
    response = editorial_api.delete(url, headers={"If-Match": stale})
    assert response.status_code == 412
    assert response.headers["ETag"] == f'"{guidelines["revision"]}"'
    assert read_editorial(editorial_api)["pillars"] != []


def test_editorial_delete_with_current_if_match_returns_no_content(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    current = f'"{guidelines["revision"]}"'
    url = f"{EDITORIAL_URL}/{guidelines['id']}"
    assert editorial_api.delete(url, headers={"If-Match": current}).status_code == 204


def test_editorial_delete_channel_statement_returns_no_content(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    assert retire_guideline(editorial_api, guidelines["id"]).status_code == 204


def test_editorial_delete_channel_statement_twice_returns_no_content(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    retire_guideline(editorial_api, guidelines["id"])
    assert retire_guideline(editorial_api, guidelines["id"]).status_code == 204


def test_editorial_get_after_pillar_delete_returns_no_pillars(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    retire_guideline(editorial_api, guidelines["id"])
    assert read_editorial(editorial_api)["pillars"] == []


@pytest.mark.parametrize("house_id", ["house.purpose", "house.rule"])
def test_editorial_delete_house_statement_returns_not_found(
    editorial_api: TestClient, house_id: str
) -> None:
    assert retire_guideline(editorial_api, house_id).status_code == 404


def test_editorial_delete_platform_statement_without_platform_returns_not_found(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    assert retire_guideline(editorial_api, "funny-memes.tiktok.purpose").status_code == 404


def test_editorial_delete_platform_statement_with_platform_returns_no_content(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    response = retire_guideline(editorial_api, "funny-memes.tiktok.purpose", platform="tiktok")
    assert response.status_code == 204


def test_editorial_get_with_platform_after_platform_purpose_delete_returns_channel_purpose(
    editorial_api: TestClient, guidelines: dict[str, Any]
) -> None:
    retire_guideline(editorial_api, "funny-memes.tiktok.purpose", platform="tiktok")
    tiktok_view = read_editorial(editorial_api, platform="tiktok")
    assert tiktok_view["purpose"]["text"] == "Make Mondays funny."


def test_editorial_write_voice_records_user_as_author(
    api: TestClient, settings: AppSettings, channel: dict[str, Any]
) -> None:
    written = write_guideline(api, {"kind": "voice", "text": "Dry."}).json()
    head = stmt(settings, "/funny-memes", "get", written["id"])["result"]
    assert head["owner"] == {"type": "user", "id": api.user_id}  # type: ignore[attr-defined]


def test_editorial_write_with_store_conflict_returns_conflict(
    api: TestClient, channel: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def conflict(*args: object, **kwargs: object) -> None:
        raise StoreError("taken", "CONFLICT")

    monkeypatch.setattr(StmtClient, "write", conflict)
    response = write_guideline(api, {"kind": "voice", "text": "x"})
    assert response.status_code == 409


def test_editorial_get_with_unreachable_store_scope_returns_bad_gateway(
    api: TestClient,
    settings: AppSettings,
    channel: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = api.app.state.services  # type: ignore[attr-defined]
    broken = settings.store.model_copy(update={"domain": "wrong"})
    monkeypatch.setattr(services.editorials.client, "settings", broken)
    response = api.get(EDITORIAL_URL)
    assert response.status_code == 502
    assert "NOT_FOUND" in response.json()["detail"]


# Interactive API docs load a CDN, so they exist only in development (web.security.headers).


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_api_docs_pages_outside_development_are_not_found(app: FastAPI, path: str) -> None:
    assert TestClient(app).get(path).status_code == 404


def test_api_openapi_schema_outside_development_is_served(app: FastAPI) -> None:
    assert TestClient(app).get("/openapi.json").status_code == 200


def test_api_docs_page_in_development_is_served(app_settings: AppSettings) -> None:
    development = create_app(app_settings.model_copy(update={"has_dev_pages": True}))
    assert TestClient(development).get("/docs").status_code == 200
