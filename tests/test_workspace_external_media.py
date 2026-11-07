"""The third-party media pages: the library, one item with its copy, and the episode tab."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.web.workspace.external_media import TimeUnreadable, clock_of, seconds_of
from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

LIBRARY = "/app/channels/funny-memes/external-media"
EPISODE = "/app/channels/funny-memes/episodes/1"
USES = f"{EPISODE}/external-media"
NASA = {
    "kind": "video",
    "title": "Mars landing animation",
    "url": "https://svs.gsfc.nasa.gov/mars",
    "creator": "NASA",
    "rights": "public_domain",
}
PAPER = {
    "kind": "document",
    "title": "Mondays and mood",
    "url": "https://example.com/paper",
    "rights": "citation",
}


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def added(client: TestClient, fields: dict[str, str]) -> str:
    """The address of a new item's page."""
    created = submit(client, LIBRARY, fields)
    location: str = created.headers["location"]
    client.get(location)
    return location


@pytest.fixture
def nasa(owner: TestClient) -> str:
    return added(owner, NASA)


def media_id(item: str) -> str:
    return item.rsplit("/", 1)[1]


# Times


def test_seconds_of_minutes_and_hours_reads_seconds() -> None:
    assert [seconds_of(text, "start") for text in ("75", "1:15", "0:01:15", " ")] == [
        75,
        75,
        75,
        None,
    ]


def test_seconds_of_words_raises_unreadable() -> None:
    with pytest.raises(TimeUnreadable):
        seconds_of("a minute", "start")


def test_clock_of_long_time_shows_hours() -> None:
    assert (clock_of(75), clock_of(3675), clock_of(None)) == ("1:15", "1:01:15", "")


# Library


def test_channel_menu_links_to_third_party_media(owner: TestClient) -> None:
    assert f'href="{LIBRARY}"' in owner.get("/app/channels/funny-memes").text


def test_library_without_items_shows_empty_state(owner: TestClient) -> None:
    assert "No third-party media yet" in owner.get(LIBRARY).text


def test_item_add_opens_it_with_note(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, LIBRARY, NASA))
    assert "Added to third-party media." in page and "Mars landing animation" in page


def test_item_add_bad_link_reopens_dialog_with_error(owner: TestClient) -> None:
    response = submit(owner, LIBRARY, {**NASA, "url": "mars"})
    assert response.status_code == 422 and 'id="new-url-error"' in response.text
    assert 'id="new-item" aria-labelledby="new-item-title" data-open' in response.text


def test_library_filtered_by_kind_hides_others(owner: TestClient, nasa: str) -> None:
    added(owner, PAPER)
    page = owner.get(LIBRARY, params={"kind": "document"}).text
    assert "Mondays and mood" in page and "Mars landing animation" not in page


def test_item_save_changes_credit_with_note(owner: TestClient, nasa: str) -> None:
    fields = {**NASA, "attribution": "NASA/JPL-Caltech", "revision": "1"}
    page = flash_after(owner, submit(owner, nasa, fields))
    assert "Changes saved." in page and "NASA/JPL-Caltech" in page


def test_item_save_stale_revision_shows_conflict(owner: TestClient, nasa: str) -> None:
    submit(owner, nasa, {**NASA, "title": "First", "revision": "1"})
    response = submit(owner, nasa, {**NASA, "title": "Mine", "revision": "1"})
    assert response.status_code == 409 and "changed or removed" in response.text


def test_item_page_of_unknown_or_malformed_id_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{LIBRARY}/{uuid4()}").status_code == 404
    assert owner.get(f"{LIBRARY}/nope").status_code == 404


def test_item_page_links_back_to_the_library(owner: TestClient, nasa: str) -> None:
    assert f'<p class="back-link"><a href="{LIBRARY}">' in owner.get(nasa).text


# Copy


def upload(client: TestClient, item: str, content: bytes, revision: str = "1") -> object:
    files = {"file": ("copy.bin", content, "application/octet-stream")}
    return submit(client, f"{item}/copy", {"revision": revision}, files=files)


def test_copy_upload_plays_inline_on_the_page(owner: TestClient) -> None:
    photo = added(owner, {**NASA, "kind": "image"})
    page = flash_after(owner, upload(owner, photo, sample.PNG))  # type: ignore[arg-type]
    assert "File uploaded." in page and f'src="{photo}/copy"' in page
    assert owner.get(f"{photo}/copy").content == sample.PNG


def test_copy_pdf_downloads_as_attachment(owner: TestClient) -> None:
    paper = added(owner, PAPER)
    upload(owner, paper, sample.PDF)
    response = owner.get(f"{paper}/copy")
    assert response.headers["content-disposition"].startswith("attachment")


def test_copy_of_wrong_type_shows_error_in_dialog(owner: TestClient, nasa: str) -> None:
    response = upload(owner, nasa, sample.PDF)
    assert response.status_code == 422  # type: ignore[attr-defined]
    assert 'id="upload-copy" aria-labelledby="upload-copy-title" data-open' in response.text  # type: ignore[attr-defined]


def test_copy_without_file_shows_error(owner: TestClient, nasa: str) -> None:
    assert submit(owner, f"{nasa}/copy", {"revision": "1"}).status_code == 422


def test_copy_remove_leaves_none(owner: TestClient) -> None:
    paper = added(owner, PAPER)
    upload(owner, paper, sample.PDF)
    page = flash_after(owner, submit(owner, f"{paper}/copy/remove", {"revision": "2"}))
    assert "No copy kept." in page
    assert owner.get(f"{paper}/copy").status_code == 404


def test_copy_for_non_member_answers_not_found(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    paper = added(owner, PAPER)
    upload(owner, paper, sample.PDF)
    stranger = member(app, mailer, "zed@example.com")
    assert stranger.get(f"{paper}/copy").status_code == 404


# The episode tab


def use(client: TestClient, item: str, **fields: str) -> object:
    return submit(client, USES, {"media_id": media_id(item), **fields})


def test_episode_tabs_link_to_third_party(owner: TestClient) -> None:
    assert f'href="{USES}"' in owner.get(EPISODE).text


def test_episode_tab_without_library_points_to_it(owner: TestClient) -> None:
    assert "Add third-party media first" in owner.get(USES).text


def test_use_add_shows_it_and_its_credit(owner: TestClient, nasa: str) -> None:
    page = flash_after(owner, use(owner, nasa, start="0:05", end="0:12", scene="Intro"))  # type: ignore[arg-type]
    assert "Use recorded." in page and "0:05–0:12" in page and "Intro" in page
    assert "Mars landing animation — NASA" in page
    assert "Check the rights before publishing" not in page


def test_use_of_citation_without_reason_warns(owner: TestClient) -> None:
    paper = added(owner, PAPER)
    page = flash_after(owner, use(owner, paper, pages="3-4"))  # type: ignore[arg-type]
    assert "Check the rights before publishing" in page and "say why the use is allowed" in page


def test_use_unreadable_time_shows_error(owner: TestClient, nasa: str) -> None:
    response = use(owner, nasa, start="soon")
    assert response.status_code == 422 and 'id="use-start-error"' in response.text  # type: ignore[attr-defined]


def test_use_range_on_document_shows_error(owner: TestClient) -> None:
    paper = added(owner, PAPER)
    response = use(owner, paper, start="0:05")
    assert (
        response.status_code == 422 and "Only video and audio take a time range." in response.text
    )  # type: ignore[attr-defined]


def test_use_unknown_item_shows_error(owner: TestClient, nasa: str) -> None:
    response = submit(owner, USES, {"media_id": str(uuid4())})
    assert response.status_code == 422 and 'id="media_id-error"' in response.text


def test_use_remove_then_item_delete_succeeds(owner: TestClient, nasa: str) -> None:
    page = flash_after(owner, use(owner, nasa))  # type: ignore[arg-type]
    use_id = page.split('name="use_id" value="')[1].split('"')[0]
    refused = submit(owner, f"{nasa}/delete", {"revision": "1"})
    assert refused.status_code == 409 and "Episodes still use this item." in refused.text
    removed = flash_after(
        owner, submit(owner, f"{USES}/remove", {"use_id": use_id, "revision": "1"})
    )
    assert "Removed." in removed and "uses no third-party media yet" in removed
    deleted = flash_after(owner, submit(owner, f"{nasa}/delete", {"revision": "1"}))
    assert "Removed from third-party media." in deleted


def test_use_remove_malformed_id_shows_conflict(owner: TestClient) -> None:
    assert submit(owner, f"{USES}/remove", {"use_id": "nope"}).status_code == 409


def test_item_page_lists_the_episodes_using_it(owner: TestClient, nasa: str) -> None:
    use(owner, nasa)
    assert f'href="{USES}"' in owner.get(nasa).text


# Roles


def test_library_for_viewer_has_no_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, nasa: str
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(nasa).text
    assert "Mars landing animation" in page and 'id="edit-item"' not in page
    assert submit(viewer, LIBRARY, NASA).status_code == 403


def test_item_delete_by_contributor_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, nasa: str
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    assert 'id="danger"' not in contributor.get(nasa).text
    assert submit(contributor, f"{nasa}/delete", {"revision": "1"}).status_code == 403


def test_library_in_portuguese_says_midia_de_terceiros(owner: TestClient) -> None:
    assert "Mídia de terceiros" in owner.get(LIBRARY, params={"lang": "pt-BR"}).text
