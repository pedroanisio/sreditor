"""One social account belongs to one channel at most."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.channels import normalized_handle
from sreditor.database import Database
from tests.conftest import RecordingMailer
from tests.test_api import FUNNY_MEMES
from tests.workspace_helpers import member, new_channel, submit

OTHER = {**FUNNY_MEMES, "slug": "other", "name": "Other"}


@pytest.fixture
def api(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


def test_normalized_handle_ignores_at_sign_and_case() -> None:
    assert normalized_handle(" @FunnyMemes ") == "funnymemes"


def test_channel_create_with_taken_account_returns_conflict(api: TestClient) -> None:
    response = api.post("/channels", json=OTHER)
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The tiktok account @funnymemes already belongs to another channel"
    )


def test_channel_create_with_taken_account_leaves_no_channel(api: TestClient) -> None:
    api.post("/channels", json=OTHER)
    assert api.get("/channels/other").status_code == 404


def test_account_put_same_handle_other_case_returns_conflict(api: TestClient) -> None:
    api.post("/channels", json={**OTHER, "accounts": []})
    account = {"platform": "tiktok", "handle": "FunnyMemes"}
    response = api.put("/channels/other/accounts/tiktok", json=account)
    assert response.status_code == 409 and response.json()["type"].endswith("account-taken")


def test_account_put_same_handle_other_platform_succeeds(api: TestClient) -> None:
    api.post("/channels", json={**OTHER, "accounts": []})
    account = {"platform": "instagram", "handle": "@funnymemes"}
    assert api.put("/channels/other/accounts/instagram", json=account).status_code == 200


def test_account_handle_is_free_after_removal(api: TestClient) -> None:
    api.delete("/channels/funny-memes/accounts/tiktok")
    assert api.post("/channels", json=OTHER).status_code == 201


def test_account_handle_is_free_after_channel_deletion(api: TestClient) -> None:
    api.delete("/channels/funny-memes")
    assert api.post("/channels", json=OTHER).status_code == 201


def test_account_put_on_own_channel_keeps_its_handle(api: TestClient) -> None:
    account: dict[str, Any] = {**FUNNY_MEMES["accounts"][0], "description": "New."}
    assert api.put("/channels/funny-memes/accounts/tiktok", json=account).status_code == 200


def test_accounts_page_taken_handle_shows_error_next_to_field(
    app: FastAPI, mailer: RecordingMailer, api: TestClient
) -> None:
    client = member(app, mailer, "bia@example.com")
    new_channel(client, slug="bia", name="Bia")
    response = submit(
        client, "/app/channels/bia/accounts", {"platform": "tiktok", "handle": "@FunnyMemes"}
    )
    assert response.status_code == 409
    assert 'id="new-handle-error"' in response.text
    assert "already belongs to another channel" in response.text


def old_database(path: Path, documents: dict[str, dict[str, Any]]) -> None:
    """A database from before account_handles, holding these channel documents."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE channels (slug TEXT PRIMARY KEY, document TEXT NOT NULL,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        for slug, document in documents.items():
            connection.execute(
                "INSERT INTO channels VALUES (?, ?, '2026-01-01', '2026-01-01')",
                (slug, json.dumps(document)),
            )


def test_database_from_before_handles_gives_shared_account_to_first_slug(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    old_database(path, {"b-channel": FUNNY_MEMES, "a-channel": FUNNY_MEMES})
    with Database(path).connect() as connection:
        rows = connection.execute("SELECT channel_slug FROM account_handles").fetchall()
    assert [row["channel_slug"] for row in rows] == ["a-channel"]
