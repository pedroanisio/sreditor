"""Exports and books through get_file_link: one call for an episode snapshot (rules
project.export and agentic.mcp.files)."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import file_operations
from sreditor.export_formats import MEDIA_TYPES, ExportFormat
from tests import media_samples as sample
from tests import test_exports
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_exports import BASE, EPISODE, written
from tests.test_file_links import error_text, services_of

SLUG = "funny-memes"
PDF_SIGNATURE = b"%PDF-"


# The export tests already build an owner with a channel, and an episode with every part filled.
owner = test_exports.owner
filled = test_exports.filled


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    """An agent with a token that holds every scope."""
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


@pytest.fixture
def visitor(app: FastAPI) -> TestClient:
    """A client with no credentials at all: what an agent's sandbox has."""
    return TestClient(app, base_url=PUBLIC_URL)


def export_target(scope: str, **fields: Any) -> dict[str, Any]:
    return {"source": "export", "export": {"scope": scope, **fields}}


def export_link(mcp: TestClient, scope: str, inline: bool = False, **fields: Any) -> dict[str, Any]:
    arguments = {"slug": SLUG, "target": export_target(scope, **fields), "inline": inline}
    result = tool(mcp, "get_file_link", arguments)
    assert not result.get("isError"), result
    offer: dict[str, Any] = result["structuredContent"]
    return offer


def opened(visitor: TestClient, offer: dict[str, Any]) -> Any:
    response = visitor.get(offer["url"])
    assert response.status_code == 200, response.text
    return response


def sheet_names(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("xl/workbook.xml").decode()


# An episode's export


def test_export_link_episode_json_downloads_the_whole_episode(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", number=1)
    assert offer["url"].startswith(f"{PUBLIC_URL}/files/")
    assert offer["media_type"] == MEDIA_TYPES[ExportFormat.JSON]
    response = opened(visitor, offer)
    found = response.json()
    assert found["format_version"] == "1"
    assert found["research_notes"] == "Lead notes"
    assert found["pitch"]["notes"] == "Pitch notes"
    assert found["screenplay"]["text"]


def test_export_link_episode_download_is_an_attachment_named_like_the_api_export(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", number=1)
    today = datetime.now(UTC).date().isoformat()
    disposition = opened(visitor, offer).headers["content-disposition"]
    assert disposition == f'attachment; filename="{offer["filename"]}"'
    assert offer["filename"] == f"funny-memes-ep1-{today}.json"


def test_export_link_is_built_when_opened_not_when_issued(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", number=1)
    assert offer["size_bytes"] is None
    written(filled.put(EPISODE + "/research", json={"text": "Changed after the link"}))
    assert opened(visitor, offer).json()["research_notes"] == "Changed after the link"


def test_export_link_stores_the_request_and_never_the_file(
    app: FastAPI,
    filled: TestClient,
    mcp: TestClient,
) -> None:
    export_link(mcp, "episode", number=1)
    with services_of(app).file_links.database.connect() as connection:
        row = connection.execute("SELECT kind, reference FROM file_links").fetchone()
    assert row["kind"] == "export_file"
    assert json.loads(row["reference"])["scope"] == "episode"
    assert "Lead notes" not in row["reference"]


def test_export_link_inline_carries_the_json_and_its_size(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    offer = export_link(mcp, "episode", inline=True, number=1)
    content = base64.b64decode(offer["content_base64"])
    assert json.loads(content)["research_notes"] == "Lead notes"
    assert offer["size_bytes"] == len(content)


def test_export_link_without_inline_has_no_content(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    assert export_link(mcp, "episode", number=1)["content_base64"] is None


def test_export_link_inline_over_the_limit_only_has_the_link(
    filled: TestClient,
    mcp: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_operations, "INLINE_MAX_BYTES", 100)
    offer = export_link(mcp, "episode", inline=True, number=1)
    assert offer["content_base64"] is None and offer["size_bytes"] > 100


def test_export_link_never_holds_a_waiver(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    waiver = {"signer": "Secret Signer", "signed_on": "2026-01-02", "covers": "Her face"}
    sent = filled.post(EPISODE + "/waivers", data=waiver, files={"file": ("w.pdf", sample.PDF)})
    assert sent.status_code == 201, sent.text
    offer = export_link(mcp, "episode_book", inline=True, number=1)
    assert "Secret Signer" not in base64.b64decode(offer["content_base64"]).decode()


def test_export_link_episode_xlsx_has_a_sheet_per_table(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", format="xlsx", number=1)
    assert offer["media_type"] == MEDIA_TYPES[ExportFormat.XLSX]
    assert 'name="Sources"' in sheet_names(opened(visitor, offer).content)


def test_export_link_language_names_xlsx_sheets_in_portuguese(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", format="xlsx", language="pt-BR", number=1)
    assert 'name="Fontes"' in sheet_names(opened(visitor, offer).content)


def test_export_link_channel_json_summarizes_the_channel(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    found = opened(visitor, export_link(mcp, "channel")).json()
    assert found["channel"]["slug"] == SLUG
    assert [row["episode"]["number"] for row in found["episodes"]] == [1]


# Books


@pytest.mark.parametrize("export_format", list(ExportFormat))
def test_export_link_episode_book_downloads_in_each_format(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
    export_format: ExportFormat,
) -> None:
    offer = export_link(mcp, "episode_book", format=export_format.value, number=1)
    response = opened(visitor, offer)
    assert response.headers["content-type"].startswith(MEDIA_TYPES[export_format])
    assert (
        response.headers["content-disposition"].endswith(f'-2026-{export_format.value}"') is False
    )
    if export_format is ExportFormat.PDF:
        assert response.content.startswith(PDF_SIGNATURE)
    elif export_format is ExportFormat.XLSX:
        assert "xl/workbook.xml" in zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    else:
        assert response.json()["kind"] == "episode"


def test_export_link_pitch_book_holds_the_pitch(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    pitch = filled.get(BASE + "/pitches").json()["items"][0]
    offer = export_link(mcp, "pitch_book", pitch_id=pitch["id"])
    assert opened(visitor, offer).json()["title"] == "Why Mondays hurt"


@pytest.mark.parametrize(
    ("scope", "kind"),
    [("library_book", "library"), ("channel_book", "channel"), ("rights_book", "rights")],
)
def test_export_link_sectioned_book_json_is_that_book(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
    scope: str,
    kind: str,
) -> None:
    offer = export_link(mcp, scope)
    assert opened(visitor, offer).json()["kind"] == kind
    assert offer["filename"].startswith(f"funny-memes-{kind}-book-")


def test_export_link_book_sections_keep_only_the_ones_asked(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "channel_book", options={"sections": ["episodes"]})
    types = {part["type"] for part in opened(visitor, offer).json()["parts"]}
    assert types == {"episode"}


def test_export_link_book_status_filter_leaves_other_episodes_out(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    options = {"sections": ["episodes"], "episodes": {"statuses": ["published"]}}
    offer = export_link(mcp, "channel_book", options=options)
    assert opened(visitor, offer).json()["parts"] == []


def test_export_link_book_number_range_keeps_the_episodes_in_it(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    options = {"sections": ["episodes"], "episodes": {"first": 1, "last": 1}}
    offer = export_link(mcp, "channel_book", options=options)
    assert len(opened(visitor, offer).json()["parts"]) == 1


# Refusals


def refused(mcp: TestClient, scope: str, **fields: Any) -> str:
    arguments = {"slug": SLUG, "target": export_target(scope, **fields)}
    return error_text(tool(mcp, "get_file_link", arguments))


@pytest.mark.parametrize("scope", ["episode", "episode_book"])
def test_export_link_unknown_episode_says_how_to_find_the_number(
    filled: TestClient,
    mcp: TestClient,
    scope: str,
) -> None:
    assert "call list_episodes" in refused(mcp, scope, number=9)


def test_export_link_unknown_pitch_says_how_to_find_the_id(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    assert "list_pitches" in refused(mcp, "pitch_book", pitch_id=str(uuid4()))


def test_export_link_without_number_names_the_field(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    assert "needs number" in refused(mcp, "episode")


def test_export_link_without_pitch_id_names_the_field(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    assert "needs pitch_id" in refused(mcp, "pitch_book")


def test_export_link_source_without_export_names_the_field(mcp: TestClient) -> None:
    result = tool(mcp, "get_file_link", {"slug": SLUG, "target": {"source": "export"}})
    assert "needs export" in error_text(result)


def test_export_link_foreign_section_names_what_the_book_offers(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    message = refused(mcp, "rights_book", options={"sections": ["characters"]})
    assert "third_party, credits" in message


def test_export_link_options_on_an_episode_are_refused(
    filled: TestClient,
    mcp: TestClient,
) -> None:
    message = refused(mcp, "episode", number=1, options={"sections": ["episodes"]})
    assert "takes no options" in message


def test_export_link_without_read_scope_is_refused(
    app: FastAPI,
    filled: TestClient,
) -> None:
    with agent(app, new_token(filled, ["content:write"]), PUBLIC_URL) as client:
        assert "channels:read" in refused(client, "episode", number=1)


def test_export_link_for_a_channel_the_token_cannot_see_is_not_found(
    app: FastAPI,
    filled: TestClient,
) -> None:
    with agent(app, new_token(filled), PUBLIC_URL) as client:
        arguments = {"slug": "nope", "target": export_target("channel")}
        assert "list_channels shows" in error_text(tool(client, "get_file_link", arguments))


def test_export_link_for_an_episode_deleted_before_opening_is_not_found_and_stays_usable(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode", number=1)
    assert filled.delete(EPISODE).status_code == 204
    assert visitor.get(offer["url"]).status_code == 404
    assert visitor.get(offer["url"]).status_code == 404


# Single use


def test_export_link_second_use_is_gone(
    filled: TestClient,
    mcp: TestClient,
    visitor: TestClient,
) -> None:
    offer = export_link(mcp, "episode_book", number=1)
    opened(visitor, offer)
    assert visitor.get(offer["url"]).status_code == 410


def test_export_link_after_token_revoked_is_gone(
    app: FastAPI,
    filled: TestClient,
    visitor: TestClient,
) -> None:
    secret = new_token(filled)
    with agent(app, secret, PUBLIC_URL) as client:
        offer = export_link(client, "channel")
    token = next(item for item in filled.get("/me/tokens").json()["items"])
    assert filled.delete(f"/me/tokens/{token['id']}").status_code == 204
    assert visitor.get(offer["url"]).status_code == 410
