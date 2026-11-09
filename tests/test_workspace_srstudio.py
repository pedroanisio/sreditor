"""The srstudio sheet page of a library entry (rules project.srstudio.bible, project.srstudio.props)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.deps import Services
from sreditor.library import LibraryKind
from sreditor.srstudio import ExportedSheet
from sreditor.srstudio_operations import all_entries
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

LIBRARY = "/app/channels/funny-memes/library"
EPISODES = "/app/channels/funny-memes/episodes"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    return client


def first_entry(owner: TestClient, kind_path: str, name: str) -> str:
    """Add an entry and return its sheet address."""
    owner.get(submit(owner, f"{LIBRARY}/{kind_path}", {"name": name}).headers["location"])
    after_name = owner.get(LIBRARY).text.split(f"<strong>{name}</strong>")[1]
    entry = after_name.split(f'href="{LIBRARY}/{kind_path}/')[1].split("/srstudio")[0]
    return f"{LIBRARY}/{kind_path}/{entry}/srstudio"


@pytest.fixture
def rex(owner: TestClient) -> str:
    return first_entry(owner, "characters", "Rex")


def character_form(**changes: str | list[str]) -> dict[str, str | list[str]]:
    """A character sheet form with a slug, the blank fact row and the blank open-question row."""
    form: dict[str, str | list[str]] = {
        "revision": "0",
        "slug": "rex",
        "world": "okayville",
        "status": "placeholder",
        "character_kind": "animal",
        "rig": "toon-dog",
        "rig_version": "1",
        "look_palette": "brown",
        "voice_speed": "1.0",
        "voice_use_speaker_boost": "yes",
        "comic_role": "",
        "never": "shouts\n\nlies",
        "open_question": "",
        "open_proposal": "",
    }
    form.update(changes)
    return form


def test_library_entry_links_to_its_srstudio_sheet(owner: TestClient, rex: str) -> None:
    assert f'href="{rex}"' in owner.get(LIBRARY).text


def test_srstudio_sheet_page_shows_proposed_slug(owner: TestClient, rex: str) -> None:
    page = owner.get(rex).text
    assert 'value="rex"' in page and "Who they are" in page


def test_srstudio_sheet_unknown_kind_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{LIBRARY}/vehicles/x/srstudio").status_code == 404


def test_srstudio_sheet_malformed_id_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{LIBRARY}/characters/nope/srstudio").status_code == 404


def test_srstudio_sheet_missing_entry_answers_not_found(owner: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert owner.get(f"{LIBRARY}/characters/{missing}/srstudio").status_code == 404


def test_srstudio_sheet_save_keeps_fields_and_drops_blank_rows(owner: TestClient, rex: str) -> None:
    page = flash_after(owner, submit(owner, rex, character_form()))
    assert "brown" in page and 'value="toon-dog"' in page
    assert "shouts\nlies" in page and 'id="open_question-1"' not in page


def test_srstudio_sheet_save_keeps_open_question(owner: TestClient, rex: str) -> None:
    form = character_form(open_question=["Has a sister?", ""], open_proposal=["No", ""])
    page = flash_after(owner, submit(owner, rex, form))
    assert 'value="Has a sister?"' in page and 'id="open_question-1"' in page


def test_srstudio_sheet_bad_slug_shows_error_next_to_it(owner: TestClient, rex: str) -> None:
    response = submit(owner, rex, character_form(slug="Rex Dog"))
    assert response.status_code == 422 and 'id="slug-error"' in response.text


def test_srstudio_sheet_speed_out_of_range_shows_error(owner: TestClient, rex: str) -> None:
    response = submit(owner, rex, character_form(voice_speed="2"))
    assert response.status_code == 422 and 'id="voice_speed-error"' in response.text


def test_srstudio_sheet_taken_slug_shows_conflict(owner: TestClient, rex: str) -> None:
    submit(owner, rex, character_form())
    other = first_entry(owner, "characters", "Max")
    response = submit(owner, other, character_form())
    assert response.status_code == 409 and "already uses this slug" in response.text


def test_srstudio_sheet_stale_revision_shows_conflict(owner: TestClient, rex: str) -> None:
    submit(owner, rex, character_form())
    response = submit(owner, rex, character_form(look_palette="grey"))
    assert response.status_code == 409 and "Reload the page" in response.text


def test_srstudio_sheet_without_published_episode_explains_facts(
    owner: TestClient, rex: str
) -> None:
    assert "Publish an episode to cite it here." in owner.get(rex).text


def test_srstudio_sheet_fact_citing_unpublished_episode_is_refused(
    owner: TestClient, rex: str
) -> None:
    owner.get(submit(owner, EPISODES, {"title": "Pilot"}).headers["location"])
    form = character_form(
        canon_fact="Barks at mail",
        canon_field="identity",
        canon_episode="1",
        canon_kind="seen",
        canon_quote="woof",
    )
    response = submit(owner, rex, form)
    assert response.status_code == 422 and "published episode" in response.text


def test_srstudio_sheet_unknown_toon_rig_is_refused(owner: TestClient, rex: str) -> None:
    response = submit(owner, rex, character_form(rig="toon-host"))
    assert response.status_code == 422 and "not in srstudio&#39;s cast" in response.text


def test_srstudio_sheet_style_saves_palette(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    form = {
        "revision": "0",
        "slug": "flat",
        "style": "toon",
        "palette_ink": "#111111",
        "palette_white": "#ffffff",
        "palette_shade": "#888888",
        "palette_background": "#eeeeee",
    }
    assert 'value="#888888"' in flash_after(owner, submit(owner, style, form))


STYLE_FORM = {
    "revision": "0",
    "slug": "flat",
    "style": "toon",
    "fonts_heading_family": "Inter",
    "fonts_heading_weight": "700",
    "fonts_body_family": "",
    "fonts_body_weight": "",
    "cut_rhythm_shortest_shot_s": "0.4",
    "cut_rhythm_average_shot_s": "1.5",
    "min_text_px": "48",
    "safe_zone_top": "10",
    "safe_zone_right": "5",
    "safe_zone_bottom": "20",
    "safe_zone_left": "5",
}


def test_srstudio_sheet_style_saves_fonts_rhythm_text_and_safe_zone(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    page = flash_after(owner, submit(owner, style, STYLE_FORM))
    assert 'value="Inter"' in page and 'value="0.4"' in page and 'value="48"' in page
    assert (
        '<option value="700" selected>' in page
        and 'name="safe_zone_bottom" type="number" value="20.0"' in page
    )


def test_srstudio_sheet_style_empty_data_fields_save_as_nothing(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    blank = {key: "" for key in STYLE_FORM} | {"revision": "0", "slug": "flat", "style": "toon"}
    page = flash_after(owner, submit(owner, style, blank))
    assert 'name="min_text_px" type="number" value=""' in page


def test_srstudio_sheet_style_inverted_rhythm_shows_error_next_to_the_field(
    owner: TestClient,
) -> None:
    style = first_entry(owner, "styles", "Flat")
    form = STYLE_FORM | {"cut_rhythm_shortest_shot_s": "3"}
    response = submit(owner, style, form)
    assert response.status_code == 422 and 'id="cut_rhythm_average_shot_s-error"' in response.text
    assert 'value="3"' in response.text


def test_srstudio_sheet_style_half_filled_safe_zone_names_the_missing_edge(
    owner: TestClient,
) -> None:
    style = first_entry(owner, "styles", "Flat")
    response = submit(owner, style, STYLE_FORM | {"safe_zone_left": ""})
    assert response.status_code == 422 and 'id="safe_zone_left-error"' in response.text


def test_srstudio_sheet_style_font_without_weight_names_the_weight(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    response = submit(owner, style, STYLE_FORM | {"fonts_heading_weight": ""})
    assert response.status_code == 422 and 'id="fonts_heading_weight-error"' in response.text


def test_srstudio_sheet_style_text_that_is_not_a_number_shows_error(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    response = submit(owner, style, STYLE_FORM | {"min_text_px": "big"})
    assert response.status_code == 422 and 'id="min_text_px-error"' in response.text


def test_srstudio_sheet_style_page_in_portuguese_names_the_new_panels(owner: TestClient) -> None:
    style = first_entry(owner, "styles", "Flat")
    page = owner.get(style, params={"lang": "pt-BR"}).text
    assert (
        "Ritmo de corte" in page and "Tamanho do texto e zona segura" in page and "Fontes" in page
    )


def test_srstudio_sheet_prop_saves_rig(owner: TestClient) -> None:
    prop = first_entry(owner, "props", "Laptop")
    form = {"revision": "0", "slug": "laptop", "rig": "laptop"}
    assert "Changes saved." in flash_after(owner, submit(owner, prop, form))


def test_srstudio_sheet_viewer_sees_it_without_save_button(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, rex: str
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(rex)
    assert page.status_code == 200 and "Save sheet" not in page.text


def test_srstudio_sheet_save_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, rex: str
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, rex, character_form()).status_code == 403


def lock_sheets(app: FastAPI) -> None:
    """Fix the characters' slugs as an srstudio export that carried them does."""
    services: Services = app.state.services
    entries = all_entries(services, "funny-memes", LibraryKind.CHARACTER)
    sheets = services.srstudio.sheets(entries)
    exported = [ExportedSheet(entry=entry, sheet=sheets[entry.id]) for entry in entries]
    services.srstudio.mark_exported([], exported)


def test_srstudio_sheet_after_export_shows_slug_fixed(
    app: FastAPI, owner: TestClient, rex: str
) -> None:
    lock_sheets(app)
    page = owner.get(rex).text
    assert 'value="rex" required readonly' in page and "Fixed since the first" in page


def test_srstudio_sheet_after_export_refuses_new_slug(
    app: FastAPI, owner: TestClient, rex: str
) -> None:
    lock_sheets(app)
    response = submit(owner, rex, character_form(revision="1", slug="rexy"))
    assert response.status_code == 409 and "can no longer change" in response.text


def test_srstudio_sheet_after_export_saves_other_fields(
    app: FastAPI, owner: TestClient, rex: str
) -> None:
    lock_sheets(app)
    page = flash_after(owner, submit(owner, rex, character_form(revision="1", look_palette="grey")))
    assert "Changes saved." in page and "grey" in page
