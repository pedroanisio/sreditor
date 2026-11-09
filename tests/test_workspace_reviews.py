"""The episode's reviews page: add, list and delete reviews, the latest verdict, and translation."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
REVIEWS = f"{EPISODE}/reviews"
MEDIA = f"{EPISODE}/media"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def review_form(**fields: str) -> dict[str, str]:
    return {"verdict": "approved", "notes": "Looks **good**.", "media_id": "", "seen": "", **fields}


def test_episode_menu_links_to_the_reviews_page(owner: TestClient) -> None:
    assert f'href="{REVIEWS}"' in owner.get(EPISODE).text


def test_reviews_page_without_reviews_shows_the_empty_state_and_the_warning(
    owner: TestClient,
) -> None:
    page = owner.get(REVIEWS).text
    assert "No reviews yet." in page and "The episode has no review yet." in page


def test_review_add_lists_it_with_its_verdict_reviewer_and_formatted_notes(
    owner: TestClient,
) -> None:
    page = flash_after(owner, submit(owner, REVIEWS, review_form()))
    assert "Review added." in page and "Approved" in page and "Ana" in page
    assert "<strong>good</strong>" in page and "The episode has no review yet." not in page


def test_review_add_with_ranges_and_a_file_lists_them_with_the_files_hash(
    owner: TestClient,
) -> None:
    video = {"file": ("m.mp4", sample.MP4, "video/mp4")}
    submit(owner, f"{MEDIA}/upload/final-videos", {"name": "master"}, files=video)
    page = owner.get(REVIEWS).text
    media_id = re.search(r'<option value="([0-9a-f-]{36})"', page)[1]  # type: ignore[index]
    form = review_form(media_id=media_id, seen="0 - 3.5\n10 to 12")
    shown = flash_after(owner, submit(owner, REVIEWS, form))
    assert "0–3.5 s" in shown and "10–12 s" in shown and "SHA-256" in shown


def test_review_add_with_a_malformed_range_shows_error_next_to_the_field(
    owner: TestClient,
) -> None:
    response = submit(owner, REVIEWS, review_form(seen="soon"))
    assert response.status_code == 422 and 'id="review-seen-error"' in response.text


def test_review_add_with_ranges_but_no_file_shows_error_next_to_the_field(
    owner: TestClient,
) -> None:
    response = submit(owner, REVIEWS, review_form(seen="0 - 3"))
    assert response.status_code == 422 and 'id="review-seen-error"' in response.text


def test_review_add_with_an_unknown_file_shows_error_next_to_the_field(owner: TestClient) -> None:
    response = submit(owner, REVIEWS, review_form(media_id="00000000-0000-0000-0000-000000000000"))
    assert response.status_code == 422 and 'id="review-media-error"' in response.text


def test_review_add_with_an_unknown_verdict_shows_error_next_to_the_field(
    owner: TestClient,
) -> None:
    response = submit(owner, REVIEWS, review_form(verdict="meh"))
    assert response.status_code == 422 and 'id="review-verdict-error"' in response.text


def test_review_with_changes_requested_shows_the_warning_that_never_blocks(
    owner: TestClient,
) -> None:
    page = flash_after(owner, submit(owner, REVIEWS, review_form(verdict="changes_requested")))
    assert "The latest review is not an approval." in page


def test_review_delete_removes_it(owner: TestClient) -> None:
    submit(owner, REVIEWS, review_form())
    review_id = owner.get(REVIEWS).text.split('name="review_id" value="')[1].split('"')[0]
    page = flash_after(owner, submit(owner, f"{REVIEWS}/delete", {"review_id": review_id}))
    assert "Removed." in page and "No reviews yet." in page


def test_review_delete_of_an_unknown_review_shows_conflict(owner: TestClient) -> None:
    response = submit(owner, f"{REVIEWS}/delete", {"review_id": "nope"})
    assert response.status_code == 409


def test_episode_page_shows_the_latest_verdict(owner: TestClient) -> None:
    submit(owner, REVIEWS, review_form(verdict="rejected"))
    page = owner.get(EPISODE).text
    assert "Rejected" in page and "Latest review on" in page


def test_episode_page_without_a_review_says_so(owner: TestClient) -> None:
    assert "No review yet." in owner.get(EPISODE).text


def test_episode_published_without_a_review_saves_and_says_it_lacks_an_approval(
    owner: TestClient,
) -> None:
    form = {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    page = flash_after(owner, submit(owner, EPISODE, form))
    assert "has no approving review" in page and 'value="published" selected' in page


def test_episode_published_with_an_approval_says_changes_saved(owner: TestClient) -> None:
    submit(owner, REVIEWS, review_form())
    form = {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    assert "Changes saved." in flash_after(owner, submit(owner, EPISODE, form))


def test_reviews_page_in_portuguese_is_translated(owner: TestClient) -> None:
    submit(owner, REVIEWS, review_form())
    page = owner.get(f"{REVIEWS}?lang=pt-BR").text
    assert "Pareceres" in page and "Aprovada" in page and "Adicionar parecer" in page


def test_reviews_page_for_contributor_hides_the_form_and_refuses_posts(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    contributor = join(app, mailer, owner, "bia@example.com", "contributor")
    assert "Add a review" not in contributor.get(REVIEWS).text
    assert submit(contributor, REVIEWS, review_form()).status_code == 403


def test_reviews_page_hides_delete_from_an_editor_who_did_not_write_the_review(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, REVIEWS, review_form())
    editor = join(app, mailer, owner, "bia@example.com", "editor")
    assert 'name="review_id"' not in editor.get(REVIEWS).text


def test_review_delete_by_an_editor_who_did_not_write_it_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, REVIEWS, review_form())
    review_id = owner.get(REVIEWS).text.split('name="review_id" value="')[1].split('"')[0]
    editor = join(app, mailer, owner, "bia@example.com", "editor")
    assert submit(editor, f"{REVIEWS}/delete", {"review_id": review_id}).status_code == 403


def test_media_page_shows_the_sha256_of_each_file(owner: TestClient) -> None:
    files = {"file": ("f.png", sample.PNG, "image/png")}
    submit(owner, f"{MEDIA}/upload/stills", {"name": "frame"}, files=files)
    page = owner.get(MEDIA).text
    assert "SHA-256" in page and "frame" in page and "Stills" in page
