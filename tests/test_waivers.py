"""An episode's waivers: storage, who may see them, API, gate and MCP (rule project.waivers)."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.assets import PDF, AssetStore, UnsupportedFile, sniff
from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.members import Permission, Role, allows
from sreditor.settings import AppSettings
from sreditor.waivers import NewWaiver, WaiverInput, WaiverStore
from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.test_agentic import ALL_SCOPES, PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

WAIVERS = "/channels/funny-memes/episodes/1/waivers"
WITH_WAIVERS = [*ALL_SCOPES, "waivers:read"]


# Formats and roles


def test_sniff_pdf_names_its_format() -> None:
    assert sniff(sample.PDF) == PDF


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.VIEWER, False), (Role.CONTRIBUTOR, False), (Role.EDITOR, True), (Role.OWNER, True)],
)
def test_allows_read_waivers_only_owners_and_editors(role: Role, expected: bool) -> None:
    assert allows(role, Permission.READ_WAIVERS) is expected


# Store


@pytest.fixture
def waiver_store(tmp_path: Path) -> WaiverStore:
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
    return WaiverStore(database, AssetStore(tmp_path / "assets", 1024), 1024)


def ana_waiver() -> NewWaiver:
    return NewWaiver(channel="funny-memes", number=1, data=WaiverInput(signer="Ana Souza"))


def test_waiver_add_stores_pdf_by_hash(waiver_store: WaiverStore) -> None:
    added = waiver_store.add(ana_waiver(), io.BytesIO(sample.PDF))
    stored = waiver_store.assets.path_of(added.stored())
    assert stored.read_bytes() == sample.PDF and stored.name == f"{added.sha256}.pdf"


def test_waiver_add_audio_is_unsupported(waiver_store: WaiverStore) -> None:
    with pytest.raises(UnsupportedFile, match="PDF, JPG, PNG"):
        waiver_store.add(ana_waiver(), io.BytesIO(sample.WAV))


def test_waiver_replace_stale_revision_raises_changed(waiver_store: WaiverStore) -> None:
    added = waiver_store.add(ana_waiver(), io.BytesIO(sample.PDF))
    with pytest.raises(PartChanged):
        waiver_store.replace(added, WaiverInput(signer="Ana"), 4)


def test_waiver_delete_stale_revision_raises_changed(waiver_store: WaiverStore) -> None:
    added = waiver_store.add(ana_waiver(), io.BytesIO(sample.PDF))
    with pytest.raises(PartChanged):
        waiver_store.delete(added, 4)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def upload(client: TestClient, content: bytes = sample.PDF, **fields: str) -> Any:
    data = {"signer": "Ana Souza", **fields}
    return client.post(WAIVERS, data=data, files={"file": ("ana.pdf", content)})


@pytest.fixture
def signed(owner: TestClient) -> dict[str, Any]:
    response = upload(owner, covers="Image and voice", signed_on="2026-11-01")
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_waiver_upload_keeps_signer_date_and_type(signed: dict[str, Any]) -> None:
    assert (signed["signer"], signed["signed_on"], signed["media_type"]) == (
        "Ana Souza",
        "2026-11-01",
        "application/pdf",
    )


def test_waiver_upload_wrong_type_returns_problem_naming_file(owner: TestClient) -> None:
    response = upload(owner, sample.MP4)
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "file"]


def test_waiver_upload_without_signer_returns_unprocessable(owner: TestClient) -> None:
    assert upload(owner, signer="  ").status_code == 422


def test_waiver_list_paginates(owner: TestClient, signed: dict[str, Any]) -> None:
    upload(owner, signer="Bruno")
    first = owner.get(WAIVERS, params={"limit": 1}).json()
    second = owner.get(WAIVERS, params={"cursor": first["next_cursor"]}).json()
    assert [item["signer"] for item in second["items"]] == ["Bruno"]


def test_waiver_get_unknown_returns_not_found(owner: TestClient) -> None:
    response = owner.get(f"{WAIVERS}/{uuid4()}")
    assert response.status_code == 404 and response.json()["detail"].startswith("Waiver")


def test_waiver_download_is_an_attachment(owner: TestClient, signed: dict[str, Any]) -> None:
    response = owner.get(f"{WAIVERS}/{signed['id']}/content")
    assert response.content == sample.PDF
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["cache-control"] == "private, no-store"


def test_waiver_download_missing_file_returns_not_found(
    owner: TestClient, signed: dict[str, Any], app_settings: AppSettings
) -> None:
    for stored in (app_settings.assets_dir / "funny-memes").iterdir():
        stored.unlink()
    assert owner.get(f"{WAIVERS}/{signed['id']}/content").status_code == 404


def test_waiver_update_returns_next_etag(owner: TestClient, signed: dict[str, Any]) -> None:
    response = owner.put(
        f"{WAIVERS}/{signed['id']}", json={"signer": "Ana S."}, headers={"If-Match": '"1"'}
    )
    assert response.headers["etag"] == '"2"' and response.json()["signed_on"] is None


def test_waiver_get_returns_etag(owner: TestClient, signed: dict[str, Any]) -> None:
    assert owner.get(f"{WAIVERS}/{signed['id']}").headers["etag"] == '"1"'


def test_waiver_remove_returns_no_content(owner: TestClient, signed: dict[str, Any]) -> None:
    assert owner.delete(f"{WAIVERS}/{signed['id']}", headers={"If-Match": '"1"'}).status_code == 204
    assert owner.get(WAIVERS).json()["items"] == []


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_waiver_list_by_role_without_waivers_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer, role: str
) -> None:
    member = joined(owner, sign_up, mailer, role)
    assert member.get(WAIVERS).status_code == 403


def test_waiver_list_by_editor_is_allowed(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    editor = joined(owner, sign_up, mailer, "editor")
    assert editor.get(WAIVERS).status_code == 200


def test_waiver_list_with_token_without_scope_is_forbidden(app: FastAPI, owner: TestClient) -> None:
    response = agent(app, new_token(owner, ALL_SCOPES)).get(WAIVERS)
    assert response.status_code == 403 and "waivers:read" in response.json()["detail"]


def test_waiver_upload_with_read_scope_only_is_forbidden(app: FastAPI, owner: TestClient) -> None:
    response = upload(agent(app, new_token(owner, ["channels:read", "waivers:read"])))
    assert response.status_code == 403 and "editorial:write" in response.json()["detail"]


def test_waiver_upload_with_token_blocked_by_gate_stores_nothing(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "waiver.create")
    response = upload(agent(app, new_token(owner, WITH_WAIVERS)))
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"
    assert not (app_settings.assets_dir / "funny-memes").exists()


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner, WITH_WAIVERS), PUBLIC_URL) as client:
        yield client


def test_mcp_list_waivers_returns_signer(mcp: TestClient, signed: dict[str, Any]) -> None:
    listed = tool(mcp, "list_waivers", {"slug": "funny-memes", "number": 1})
    assert listed["structuredContent"]["items"][0]["signer"] == "Ana Souza"


def test_mcp_list_waivers_without_scope_is_a_tool_error(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ALL_SCOPES), PUBLIC_URL) as client:
        result = tool(client, "list_waivers", {"slug": "funny-memes", "number": 1})
    assert result["isError"] and "waivers:read" in result["content"][0]["text"]
