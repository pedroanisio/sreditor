"""Books: ready-made exports of a pitch, an episode, the library, the channel and its rights,
as JSON, Excel and PDF with the channel's brand (rule project.export)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from sreditor.web import books as books_module
from sreditor.web.books import ChannelFilesOnly
from sreditor.web.export_files import FONTS
from sreditor.web.forms import CSRF_COOKIE
from tests.test_agentic import agent, new_token
from tests.test_exports import NASA, filled, owner, written  # noqa: F401  # shared fixtures
from tests.workspace_helpers import ORIGIN

BASE = "/channels/funny-memes"
LIBRARY_BOOK = BASE + "/books/library"
CHANNEL_BOOK = BASE + "/books/channel"
RIGHTS_BOOK = BASE + "/books/rights"
EPISODE_BOOK = BASE + "/episodes/1/book"
PDF_SIGNATURE = b"%PDF-"
IMAGE_OBJECT = b"/Subtype /Image"


def png(color: str = "#ff0066") -> bytes:
    """A small real PNG of one color."""
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, "PNG")
    return buffer.getvalue()


def book(client: TestClient, path: str, **params: Any) -> Any:
    """A book downloaded as an attachment: parsed JSON, or the raw bytes of another format."""
    response = client.get(path, params={"format": "json", **params})
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    return response.json() if params.get("format", "json") == "json" else response.content


def csrf(client: TestClient) -> str:
    """The CSRF token a page would send, after a page set its cookie."""
    if CSRF_COOKIE not in client.cookies:
        client.get("/login")
    return str(client.cookies[CSRF_COOKIE])


def sheet_names(content: bytes) -> str:
    """The workbook part that lists the sheets."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("xl/workbook.xml").decode()


def sheet_text(content: bytes) -> str:
    """Every shared string of a workbook."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("xl/sharedStrings.xml").decode()


@pytest.fixture
def library(filled: TestClient) -> TestClient:  # noqa: F811  # pytest fixture injection
    """A library with a character, a place, a style and a prop, a picture of the character
    linked to it, a relationship and a brand logo."""
    ana = written(filled.post(BASE + "/characters", json={"name": "Ana", "description": "Host"}))
    bia = written(filled.post(BASE + "/characters", json={"name": "Bia"}))
    written(filled.post(BASE + "/locations", json={"name": "Kitchen"}))
    written(filled.post(BASE + "/styles", json={"name": "Flat"}))
    written(filled.post(BASE + "/props", json={"name": "Laptop"}))
    sheet = {"slug": "ana", "sheet": {"kind": "character", "status": "canon", "rig": "toon-dog"}}
    written(filled.put(f"{BASE}/characters/{ana['id']}/srstudio", json=sheet))
    photo = {**NASA, "kind": "image", "title": "Ana portrait", "attribution": "Photo by Rui"}
    item = written(filled.post(BASE + "/external-media", json=photo))
    files = {"file": ("ana.png", png(), "image/png")}
    written(filled.put(f"{BASE}/external-media/{item['id']}/copy", files=files))
    link = {"to": {"kind": "external_media", "id": item["id"]}, "question": "who"}
    written(filled.post(f"{BASE}/items/character/{ana['id']}/links", json=link))
    pair = {"first": ana["id"], "second": bia["id"], "relation": "Sisters"}
    written(filled.post(BASE + "/relationships", json=pair))
    logo = {"file": ("logo.png", png("#0066ff"), "image/png")}
    filled.headers["origin"] = ORIGIN
    response = filled.post(
        "/app/channels/funny-memes/brand/files/logo",
        data={"platform": "", "csrf_token": csrf(filled)},
        files=logo,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return filled


# Pitch and episode books


def test_pitch_book_json_holds_research_and_episodes(filled: TestClient) -> None:  # noqa: F811
    pitch = filled.get(BASE + "/pitches").json()["items"][0]
    found = book(filled, f"{BASE}/pitches/{pitch['id']}/book")
    part = found["parts"][0]
    assert (found["kind"], found["title"]) == ("pitch", "Why Mondays hurt")
    assert (part["notes"], part["sources"][0]["title"], part["episodes"]) == (
        "Pitch notes",
        "Poll",
        [1],
    )


def test_pitch_book_pdf_is_a_pdf(filled: TestClient) -> None:  # noqa: F811
    pitch = filled.get(BASE + "/pitches").json()["items"][0]
    content = book(filled, f"{BASE}/pitches/{pitch['id']}/book", format="pdf")
    assert content.startswith(PDF_SIGNATURE)


def test_pitch_book_pdf_markdown_notes_print_formatted(
    filled: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pitch = filled.get(BASE + "/pitches").json()["items"][0]
    research = f"{BASE}/pitches/{pitch['id']}/research"
    written(filled.put(research, json={"text": "## Lead\n\n**Strike** vote", "revision": 1}))
    printed: list[str] = []

    def recording_html(**arguments: Any) -> Any:
        printed.append(arguments["string"])
        return books_module.HTML.__wrapped__(**arguments)

    recording_html.__wrapped__ = books_module.HTML  # type: ignore[attr-defined]
    monkeypatch.setattr(books_module, "HTML", recording_html)
    book(filled, f"{BASE}/pitches/{pitch['id']}/book", format="pdf")
    assert "<h2>Lead</h2>" in printed[0] and "<strong>Strike</strong> vote" in printed[0]


def test_episode_book_json_adds_context_and_cast(filled: TestClient) -> None:  # noqa: F811
    part = book(filled, EPISODE_BOOK)["parts"][0]
    assert part["type"] == "episode"
    assert part["export"]["screenplay"]["text"].startswith("INT. KITCHEN")
    assert part["cast"]["characters"][0]["name"] == "ANA"
    assert "what" in part["context"]


def test_episode_book_pdf_is_a_pdf(filled: TestClient) -> None:  # noqa: F811
    assert book(filled, EPISODE_BOOK, format="pdf").startswith(PDF_SIGNATURE)


def test_episode_book_unknown_episode_is_not_found(owner: TestClient) -> None:  # noqa: F811
    assert owner.get(BASE + "/episodes/9/book", params={"format": "json"}).status_code == 404


# The library book


def test_library_book_json_holds_each_kind_and_relationships(library: TestClient) -> None:
    found = book(library, LIBRARY_BOOK)
    types = [part["type"] for part in found["parts"]]
    kinds = [part["entry"]["kind"] for part in found["parts"] if part["type"] == "library_entry"]
    assert kinds == ["character", "character", "location", "style", "prop"]
    assert types[-1] == "relationships"
    assert found["parts"][-1]["relationships"][0]["first"] == "Ana"


def test_library_book_json_names_linked_pictures_without_paths(library: TestClient) -> None:
    ana = book(library, LIBRARY_BOOK)["parts"][0]
    assert ana["sheet"]["slug"] == "ana"
    assert ana["images"][0]["title"] == "Ana portrait"
    assert "stored" not in ana["images"][0]


def test_library_book_json_leaves_the_logo_path_out(library: TestClient) -> None:
    assert "logo" not in book(library, LIBRARY_BOOK)["brand"]


def test_library_book_section_keeps_only_that_section(library: TestClient) -> None:
    found = book(library, LIBRARY_BOOK, section="props")
    assert [part["entry"]["name"] for part in found["parts"]] == ["Laptop"]


def test_library_book_pdf_draws_logo_and_picture(library: TestClient) -> None:
    content = book(library, LIBRARY_BOOK, format="pdf")
    assert content.startswith(PDF_SIGNATURE)
    assert content.count(IMAGE_OBJECT) >= 2


def test_library_book_xlsx_lists_the_style_data(library: TestClient) -> None:
    style = library.get(BASE + "/styles").json()["items"][0]
    sheet = {
        "kind": "style",
        "style": "toon",
        "fonts": {"heading": {"family": "Inter", "weight": 700}},
        "cut_rhythm": {"shortest_shot_s": 0.4, "average_shot_s": 1.5},
        "min_text_px": 48,
        "safe_zone": {"top": 10, "right": 5, "bottom": 20, "left": 5},
    }
    written(
        library.put(f"{BASE}/styles/{style['id']}/srstudio", json={"slug": "flat", "sheet": sheet})
    )
    text = sheet_text(book(library, LIBRARY_BOOK, format="xlsx"))
    assert "Heading: Inter 700" in text and "Shortest shot 0.4 s, average 1.5 s" in text
    assert "48 px" in text and "10% / 5% / 20% / 5%" in text


def test_library_book_xlsx_in_portuguese_translates_the_style_labels(library: TestClient) -> None:
    style = library.get(BASE + "/styles").json()["items"][0]
    sheet = {"kind": "style", "cut_rhythm": {"shortest_shot_s": 1, "average_shot_s": 2}}
    written(
        library.put(f"{BASE}/styles/{style['id']}/srstudio", json={"slug": "flat", "sheet": sheet})
    )
    text = sheet_text(book(library, LIBRARY_BOOK, format="xlsx", lang="pt-BR"))
    assert "Ritmo de corte" in text and "Plano mais curto 1 s, médio 2 s" in text


def test_library_book_json_holds_the_style_data(library: TestClient) -> None:
    style = library.get(BASE + "/styles").json()["items"][0]
    sheet = {"kind": "style", "min_text_px": 36}
    written(
        library.put(f"{BASE}/styles/{style['id']}/srstudio", json={"slug": "flat", "sheet": sheet})
    )
    parts = book(library, LIBRARY_BOOK, section="styles")["parts"]
    assert parts[0]["sheet"]["sheet"]["min_text_px"] == 36


def test_library_book_xlsx_has_a_sheet_per_kind(library: TestClient) -> None:
    names = sheet_names(book(library, LIBRARY_BOOK, format="xlsx"))
    for name in ("Characters", "Places", "Visual styles", "Props", "Relationships"):
        assert f'name="{name}"' in names


def test_library_book_xlsx_in_portuguese_names_sheets_in_portuguese(library: TestClient) -> None:
    names = sheet_names(book(library, LIBRARY_BOOK, format="xlsx", lang="pt-BR"))
    assert 'name="Personagens"' in names


def test_library_book_empty_xlsx_says_it_is_empty(owner: TestClient) -> None:  # noqa: F811
    text = sheet_text(book(owner, LIBRARY_BOOK, format="xlsx"))
    assert "This book has nothing yet." in text


def test_library_book_empty_pdf_is_a_pdf(owner: TestClient) -> None:  # noqa: F811
    assert book(owner, LIBRARY_BOOK, format="pdf").startswith(PDF_SIGNATURE)


# The channel book


def test_channel_book_json_holds_every_section_in_order(library: TestClient) -> None:
    types = [part["type"] for part in book(library, CHANNEL_BOOK)["parts"]]
    assert types[:4] == ["editorial", "profile", "pitch", "episode"]
    assert types[-1] == "third_party"


def test_channel_book_json_never_mentions_waivers(library: TestClient) -> None:
    assert "waiver" not in library.get(CHANNEL_BOOK, params={"format": "json"}).text


def test_channel_book_status_filter_leaves_other_episodes_out(library: TestClient) -> None:
    found = book(library, CHANNEL_BOOK, section="episodes", status="published")
    assert found["parts"] == []


def test_channel_book_range_keeps_episodes_inside_it(library: TestClient) -> None:
    found = book(library, CHANNEL_BOOK, section="episodes", **{"from": 1, "to": 1})
    assert [part["export"]["episode"]["number"] for part in found["parts"]] == [1]


def test_channel_book_empty_range_keeps_every_episode(library: TestClient) -> None:
    found = book(library, CHANNEL_BOOK, section="episodes", **{"from": "", "to": ""})
    assert [part["export"]["episode"]["number"] for part in found["parts"]] == [1]


def test_channel_book_range_ending_before_start_is_refused(owner: TestClient) -> None:  # noqa: F811
    response = owner.get(CHANNEL_BOOK, params={"format": "json", "from": 3, "to": 1})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["query", "to"]


def test_channel_book_foreign_section_is_refused(owner: TestClient) -> None:  # noqa: F811
    response = owner.get(CHANNEL_BOOK, params={"format": "json", "section": "credits"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["query", "section"]


def test_channel_book_pdf_renders_every_part(library: TestClient) -> None:
    assert book(library, CHANNEL_BOOK, format="pdf").startswith(PDF_SIGNATURE)


def test_channel_book_xlsx_holds_editorial_episodes_and_sources(library: TestClient) -> None:
    names = sheet_names(book(library, CHANNEL_BOOK, format="xlsx"))
    for name in ("Pitches", "Episodes", "Sources", "Third-party", "Profile"):
        assert f'name="{name}"' in names


# The rights book


def test_rights_book_json_holds_items_and_credits(filled: TestClient) -> None:  # noqa: F811
    parts = book(filled, RIGHTS_BOOK)["parts"]
    assert (parts[0]["type"], parts[0]["episodes"]) == ("third_party", [1])
    assert parts[-1]["type"] == "credits"
    assert parts[-1]["credits"]["credits"][0]["title"] == "Mars landing animation"


def test_rights_book_xlsx_holds_rights_and_credits(filled: TestClient) -> None:  # noqa: F811
    names = sheet_names(book(filled, RIGHTS_BOOK, format="xlsx"))
    assert 'name="Third-party"' in names and 'name="Credits"' in names


def test_rights_book_pdf_is_a_pdf(filled: TestClient) -> None:  # noqa: F811
    assert book(filled, RIGHTS_BOOK, format="pdf").startswith(PDF_SIGNATURE)


def test_rights_book_file_name_names_channel_and_book(filled: TestClient) -> None:  # noqa: F811
    response = filled.get(RIGHTS_BOOK, params={"format": "pdf"})
    assert "funny-memes-rights-book-" in response.headers["content-disposition"]


# Tokens


def test_books_with_read_token_download(app: FastAPI, owner: TestClient) -> None:  # noqa: F811
    token = new_token(owner, scopes=["channels:read"])
    response = agent(app, token).get(LIBRARY_BOOK, params={"format": "json"})
    assert response.status_code == 200


# The PDF renderer's reach


def test_channel_files_only_refuses_a_web_address() -> None:
    with pytest.raises(ValueError, match="PDF books read only"):
        ChannelFilesOnly(frozenset()).fetch("https://example.com/logo.png")


def test_channel_files_only_refuses_a_file_it_was_not_given(tmp_path: Path) -> None:
    other = tmp_path / "other.png"
    other.write_bytes(png())
    with pytest.raises(ValueError, match="PDF books read only"):
        ChannelFilesOnly(frozenset()).fetch(other.as_uri())


def test_channel_files_only_reads_a_file_it_was_given(tmp_path: Path) -> None:
    mine = tmp_path / "mine.png"
    mine.write_bytes(png())
    found = ChannelFilesOnly(frozenset({mine.resolve()})).fetch(mine.as_uri())
    assert found is not None


def test_channel_files_only_reads_the_app_fonts() -> None:
    font = next(FONTS.iterdir())
    assert ChannelFilesOnly(frozenset()).fetch(font.as_uri()) is not None


# The books page and menus


def test_books_page_offers_channel_book_by_default(owner: TestClient) -> None:  # noqa: F811
    page = owner.get("/app/channels/funny-memes/books").text
    assert f'action="{CHANNEL_BOOK}"' in page
    assert 'name="status"' in page


def test_books_page_library_tab_offers_library_sections(owner: TestClient) -> None:  # noqa: F811
    page = owner.get("/app/channels/funny-memes/books", params={"book": "library"}).text
    assert f'action="{LIBRARY_BOOK}"' in page
    assert 'value="relationships"' in page
    assert 'name="status"' not in page


def test_books_page_unknown_book_falls_back_to_channel(owner: TestClient) -> None:  # noqa: F811
    page = owner.get("/app/channels/funny-memes/books", params={"book": "pitch"}).text
    assert f'action="{CHANNEL_BOOK}"' in page


def test_channel_menu_lists_books(owner: TestClient) -> None:  # noqa: F811
    assert 'href="/app/channels/funny-memes/books"' in owner.get("/app/channels/funny-memes").text


def test_pitch_page_export_menu_downloads_pitch_book(filled: TestClient) -> None:  # noqa: F811
    pitch = filled.get(BASE + "/pitches").json()["items"][0]
    page = filled.get(f"/app/channels/funny-memes/pitches/{pitch['id']}").text
    assert f'href="{BASE}/pitches/{pitch["id"]}/book?format=pdf" download' in page
