"""Exports of an episode and of a channel as JSON, Excel and PDF (rule project.export)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.export_operations import every_item
from sreditor.i18n import Translator
from sreditor.pagination import Page
from sreditor.web.export_files import (
    CELL_LIMIT,
    FONTS,
    AppFontsOnly,
    ExportFormat,
    Sheet,
    file_name,
    workbook,
)
from tests.test_agentic import agent, new_token
from tests.test_api import FUNNY_MEMES

BASE = "/channels/funny-memes"
EPISODE = BASE + "/episodes/1"
EPISODE_EXPORT = EPISODE + "/export"
CHANNEL_EXPORT = BASE + "/export"
FORMULA = '=HYPERLINK("https://evil.example","click")'
STUDY = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": FORMULA,
    "accessed_on": "2026-11-01",
}
NASA = {
    "kind": "video",
    "title": "Mars landing animation",
    "url": "https://svs.gsfc.nasa.gov/mars",
    "creator": "NASA",
    "rights": "public_domain",
}
SCRIPT = {
    "duration_s": 5,
    "voice": {"voice_id": "v1", "model": "v3", "language": "pt-BR"},
    "blocks": [
        {"start_s": 0, "end_s": 2, "function": "hook", "speech": "Oi."},
        {"start_s": 2, "end_s": 5, "function": "punch", "speech": "Tchau."},
    ],
}
COMEDY = {
    "dimensions": {
        "genres": {"state": "present", "items": [{"value": "comedy", "role": "primary"}]},
        "humor_devices": {"state": "present", "items": [{"value": "deadpan", "role": "primary"}]},
    }
}
PILOT = "INT. KITCHEN - DAY\n\nANA\nIs it Monday again?\n"


def written(response: Any) -> dict[str, Any]:
    """The JSON of a write that must have worked."""
    assert response.status_code in (200, 201), response.text
    found: dict[str, Any] = response.json()
    return found


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    written(client.post("/channels", json=FUNNY_MEMES))
    written(client.post(BASE + "/episodes", json={"title": "Pilot", "summary": "Mondays."}))
    return client


@pytest.fixture
def filled(owner: TestClient) -> TestClient:
    """An episode with every part an export carries, and a waiver it must leave out."""
    written(owner.put(EPISODE + "/research", json={"text": "Lead notes"}))
    written(owner.post(EPISODE + "/sources", json=STUDY))
    pitch = written(owner.post(BASE + "/pitches", json={"title": "Why Mondays hurt"}))
    written(owner.put(f"{BASE}/pitches/{pitch['id']}/research", json={"text": "Pitch notes"}))
    written(owner.post(f"{BASE}/pitches/{pitch['id']}/sources", json={**STUDY, "title": "Poll"}))
    written(owner.put(EPISODE + "/pitch", json={"pitch_id": pitch["id"]}))
    written(owner.put(EPISODE + "/screenplay", json={"text": PILOT}))
    written(owner.put(EPISODE + "/short-script", json=SCRIPT))
    written(owner.put(EPISODE + "/classification", json=COMEDY))
    nasa = written(owner.post(BASE + "/external-media", json=NASA))
    written(owner.post(EPISODE + "/external-media", json={"media_id": nasa["id"]}))
    return owner


def downloaded(client: TestClient, path: str, export_format: str) -> bytes:
    """An export's bytes, after checking it came as an attachment."""
    response = client.get(path, params={"format": export_format})
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    return response.content


def sheet_files(content: bytes) -> dict[str, str]:
    """Each part of an .xlsx, as text."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist()}


# The API


def test_export_episode_json_holds_every_part(filled: TestClient) -> None:
    found = filled.get(EPISODE_EXPORT, params={"format": "json"}).json()
    assert found["format_version"] == "1"
    assert found["research_notes"] == "Lead notes"
    assert found["sources"][0]["title"] == "Mondays study"
    assert found["pitch"]["notes"] == "Pitch notes"
    assert found["pitch"]["sources"][0]["title"] == "Poll"
    assert found["screenplay"]["text"] == PILOT
    assert found["short_script"]["blocks"][1]["speech"] == "Tchau."
    assert found["classification"]["classification"]["dimensions"]["genres"]["state"] == "present"
    assert found["third_party"][0]["item"]["creator"] == "NASA"


def test_export_episode_json_never_mentions_waivers(filled: TestClient) -> None:
    assert "waiver" not in filled.get(EPISODE_EXPORT, params={"format": "json"}).text


def test_export_episode_without_short_script_is_null(owner: TestClient) -> None:
    found = owner.get(EPISODE_EXPORT, params={"format": "json"}).json()
    assert (found["short_script"], found["pitch"]) == (None, None)


def test_export_episode_file_name_names_channel_episode_and_day(owner: TestClient) -> None:
    response = owner.get(EPISODE_EXPORT, params={"format": "pdf"})
    assert response.headers["content-type"] == "application/pdf"
    assert "funny-memes-ep1-" in response.headers["content-disposition"]


def test_export_episode_pdf_is_a_pdf(filled: TestClient) -> None:
    assert downloaded(filled, EPISODE_EXPORT, "pdf").startswith(b"%PDF-")


def test_export_episode_pdf_in_portuguese_is_a_pdf(filled: TestClient) -> None:
    response = filled.get(EPISODE_EXPORT, params={"format": "pdf", "lang": "pt-BR"})
    assert response.content.startswith(b"%PDF-")


def test_export_episode_xlsx_has_one_sheet_per_table(filled: TestClient) -> None:
    book = sheet_files(downloaded(filled, EPISODE_EXPORT, "xlsx"))["xl/workbook.xml"]
    for name in ("Episode", "Classification", "Sources", "Third-party"):
        assert f'name="{name}"' in book


def test_export_episode_xlsx_keeps_formula_text_as_text(filled: TestClient) -> None:
    parts = sheet_files(downloaded(filled, EPISODE_EXPORT, "xlsx"))
    assert "HYPERLINK" in parts["xl/sharedStrings.xml"]
    assert not any("<f>" in text for name, text in parts.items() if name.startswith("xl/work"))
    assert not any("hyperlink" in name for name in parts)


def test_export_episode_xlsx_in_portuguese_names_sheets_in_portuguese(
    filled: TestClient,
) -> None:
    response = filled.get(EPISODE_EXPORT, params={"format": "xlsx", "lang": "pt-BR"})
    assert 'name="Fontes"' in sheet_files(response.content)["xl/workbook.xml"]


def test_export_unknown_format_returns_422(owner: TestClient) -> None:
    assert owner.get(EPISODE_EXPORT, params={"format": "docx"}).status_code == 422


def test_export_missing_episode_returns_404(owner: TestClient) -> None:
    assert owner.get(BASE + "/episodes/9/export", params={"format": "json"}).status_code == 404


def test_export_channel_json_lists_episodes_with_classification(filled: TestClient) -> None:
    found = filled.get(CHANNEL_EXPORT, params={"format": "json"}).json()
    assert found["channel"] == {"slug": "funny-memes", "name": FUNNY_MEMES["name"]}
    assert found["episodes"][0]["episode"]["title"] == "Pilot"
    assert found["observed_profile"]["sample_size"] == 1


def test_export_channel_xlsx_has_episodes_and_profile(filled: TestClient) -> None:
    book = sheet_files(downloaded(filled, CHANNEL_EXPORT, "xlsx"))["xl/workbook.xml"]
    assert 'name="Episodes"' in book and 'name="Profile"' in book


def test_export_channel_pdf_is_a_pdf(filled: TestClient) -> None:
    assert downloaded(filled, CHANNEL_EXPORT, "pdf").startswith(b"%PDF-")


def test_export_by_non_member_returns_404(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    stranger = sign_up("stranger@example.com", "Stranger")
    assert stranger.get(CHANNEL_EXPORT, params={"format": "json"}).status_code == 404


def test_export_by_token_with_read_scope_works(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert reader.get(EPISODE_EXPORT, params={"format": "json"}).status_code == 200


def test_export_by_token_without_read_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    writer = agent(app, new_token(owner, ["editorial:write"]))
    assert writer.get(EPISODE_EXPORT, params={"format": "json"}).status_code == 403


# The files


def test_file_name_unsafe_characters_become_dashes() -> None:
    day = datetime(2026, 11, 7, tzinfo=UTC)
    assert file_name("Fun/../Memes ep1", day, ExportFormat.PDF) == "fun-memes-ep1-2026-11-07.pdf"


def test_file_name_without_safe_characters_is_export() -> None:
    day = datetime(2026, 11, 7, tzinfo=UTC)
    assert file_name("???", day, ExportFormat.JSON) == "export-2026-11-07.json"


def test_workbook_long_text_is_cut_to_cell_limit() -> None:
    content = workbook([Sheet(name="Long", headers=["Text"], rows=[["x" * (CELL_LIMIT + 5)]])])
    assert "x" * (CELL_LIMIT + 1) not in sheet_files(content)["xl/sharedStrings.xml"]


def test_app_fonts_only_refuses_web_address() -> None:
    with pytest.raises(ValueError, match="only the app's fonts"):
        AppFontsOnly().fetch("https://example.com/tracker.png")


def test_app_fonts_only_refuses_file_outside_fonts() -> None:
    with pytest.raises(ValueError, match="only the app's fonts"):
        AppFontsOnly().fetch((FONTS / ".." / ".." / "routes.py").resolve().as_uri())


def test_every_item_follows_cursors_to_the_last_page() -> None:
    pages = {None: Page(items=[1, 2], next_cursor="b"), "b": Page(items=[3], next_cursor=None)}
    assert every_item(lambda cursor: pages[cursor]) == [1, 2, 3]


def test_translator_used_for_sheets_is_the_request_locale() -> None:
    assert Translator("pt-BR")("Sources") == "Fontes"


# The pages


def test_episode_page_header_offers_each_export_format(owner: TestClient) -> None:
    page = owner.get("/app/channels/funny-memes/episodes/1").text
    for export_format in ("pdf", "xlsx", "json"):
        assert f'href="{EPISODE_EXPORT}?format={export_format}" download' in page


def test_channel_overview_offers_channel_export(owner: TestClient) -> None:
    page = owner.get("/app/channels/funny-memes").text
    assert f'href="{CHANNEL_EXPORT}?format=pdf" download' in page
