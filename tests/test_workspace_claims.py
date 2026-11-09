"""The episode's claims page: add, edit, check and remove a claim, and the unchecked warning."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import (
    approve_episode,
    flash_after,
    join,
    member,
    new_channel,
    submit,
)

EPISODE = "/app/channels/funny-memes/episodes/1"
CLAIMS = f"{EPISODE}/claims"
SOURCES = f"{EPISODE}/sources"
STUDY = {
    "url": "https://example.com/bets",
    "title": "Bets report",
    "claim": "Bets moved a lot of money.",
    "accessed_on": "2026-11-01",
}
QUOTE = "R$ 62,5 bilhões das bets"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


@pytest.fixture
def source(owner: TestClient) -> str:
    """The id of a source of the episode, as the research page's removal form carries it."""
    owner.get(submit(owner, SOURCES, STUDY).headers["location"])
    page = owner.get(f"{EPISODE}/research").text
    return page.split('name="source_id" value="')[1].split('"')[0]


def new_claim_form(source: str, quote: str = QUOTE) -> dict[str, str | list[str]]:
    return {
        "quote": quote,
        "place_kind": "scene",
        "place_ref": "INT. NEWSROOM",
        "source": [source],
        "primary": [source],
    }


@pytest.fixture
def claim(owner: TestClient, source: str) -> str:
    """The id of a claim of the episode, as its forms carry it."""
    owner.get(submit(owner, CLAIMS, new_claim_form(source)).headers["location"])
    page = owner.get(CLAIMS).text
    return page.split('name="claim_id" value="')[1].split('"')[0]


def test_episode_menu_links_to_the_claims_page(owner: TestClient) -> None:
    assert f'href="{CLAIMS}"' in owner.get(EPISODE).text


def test_claims_page_without_claims_shows_the_empty_state(owner: TestClient) -> None:
    assert "No claims yet." in owner.get(CLAIMS).text


def test_claims_page_without_sources_asks_for_sources_first(owner: TestClient) -> None:
    assert "Add sources to the episode or to one of its pitches first." in owner.get(CLAIMS).text


def test_claim_add_lists_it_with_the_unchecked_warning(owner: TestClient, source: str) -> None:
    page = flash_after(owner, submit(owner, CLAIMS, new_claim_form(source)))
    assert "Claim added." in page and QUOTE in page and "Bets report" in page
    assert "1 claim has no checked primary source." in page


def test_claim_add_without_quote_shows_error_next_to_the_field(
    owner: TestClient, source: str
) -> None:
    response = submit(owner, CLAIMS, new_claim_form(source, quote=" "))
    assert response.status_code == 422 and 'id="new-claim-quote-error"' in response.text


def test_claim_add_without_place_shows_error_on_the_place_field(
    owner: TestClient, source: str
) -> None:
    form = {**new_claim_form(source), "place_ref": ""}
    assert 'id="new-claim-place-error"' in submit(owner, CLAIMS, form).text


def test_claim_add_with_unknown_source_shows_error(owner: TestClient, source: str) -> None:
    form = new_claim_form(str(uuid4()))
    response = submit(owner, CLAIMS, form)
    assert response.status_code == 422 and "no longer there" in response.text


def test_claim_check_clears_the_warning_and_shows_who_checked(
    owner: TestClient, claim: str, source: str
) -> None:
    fields = {"claim_id": claim, "source_id": source, "is_checked": "1", "revision": "1"}
    page = flash_after(owner, submit(owner, f"{CLAIMS}/check", fields))
    assert "Checked" in page and "no checked primary source." not in page and "Ana" in page


def test_claim_check_taken_back_shows_the_warning_again(
    owner: TestClient, claim: str, source: str
) -> None:
    fields = {"claim_id": claim, "source_id": source, "is_checked": "1", "revision": "1"}
    submit(owner, f"{CLAIMS}/check", fields)
    undo = {"claim_id": claim, "source_id": source, "is_checked": "", "revision": "2"}
    page = flash_after(owner, submit(owner, f"{CLAIMS}/check", undo))
    assert "1 claim has no checked primary source." in page


def test_claim_check_stale_revision_shows_conflict(
    owner: TestClient, claim: str, source: str
) -> None:
    fields = {"claim_id": claim, "source_id": source, "is_checked": "1", "revision": "7"}
    assert submit(owner, f"{CLAIMS}/check", fields).status_code == 409


def test_claim_check_malformed_ids_show_conflict(owner: TestClient, claim: str) -> None:
    fields = {"claim_id": claim, "source_id": "nope", "is_checked": "1", "revision": "1"}
    assert submit(owner, f"{CLAIMS}/check", fields).status_code == 409
    assert submit(owner, f"{CLAIMS}/check", {"claim_id": "nope"}).status_code == 409


def test_claim_save_changes_the_quote(owner: TestClient, claim: str, source: str) -> None:
    form = {**new_claim_form(source, quote="New wording"), "claim_id": claim, "revision": "1"}
    page = flash_after(owner, submit(owner, f"{CLAIMS}/save", form))
    assert "Changes saved." in page and "New wording" in page


def test_claim_save_stale_revision_shows_conflict_and_keeps_text(
    owner: TestClient, claim: str, source: str
) -> None:
    form = {**new_claim_form(source, quote="Mine"), "claim_id": claim, "revision": "9"}
    response = submit(owner, f"{CLAIMS}/save", form)
    assert response.status_code == 409 and "Mine" in response.text


def test_claim_save_invalid_quote_shows_error(owner: TestClient, claim: str, source: str) -> None:
    form = {**new_claim_form(source, quote=""), "claim_id": claim, "revision": "1"}
    assert submit(owner, f"{CLAIMS}/save", form).status_code == 422


def test_claim_save_unknown_claim_shows_conflict(owner: TestClient, source: str) -> None:
    form = {**new_claim_form(source), "claim_id": str(uuid4()), "revision": "1"}
    assert submit(owner, f"{CLAIMS}/save", form).status_code == 409


def test_claim_remove_drops_it(owner: TestClient, claim: str) -> None:
    fields = {"claim_id": claim, "revision": "1"}
    page = flash_after(owner, submit(owner, f"{CLAIMS}/delete", fields))
    assert "Removed." in page and "No claims yet." in page


def test_claim_remove_stale_revision_shows_conflict(owner: TestClient, claim: str) -> None:
    fields = {"claim_id": claim, "revision": "9"}
    assert submit(owner, f"{CLAIMS}/delete", fields).status_code == 409


def test_claim_remove_malformed_id_shows_conflict(owner: TestClient) -> None:
    assert submit(owner, f"{CLAIMS}/delete", {"claim_id": "nope"}).status_code == 409


def test_episode_page_warns_about_unchecked_claims_with_a_link(
    owner: TestClient, claim: str
) -> None:
    page = owner.get(EPISODE).text
    assert "Claims without a checked primary source: 1." in page and f'href="{CLAIMS}"' in page


def test_episode_published_with_unchecked_claims_saves_and_repeats_the_warning(
    owner: TestClient, claim: str
) -> None:
    approve_episode(owner, EPISODE)
    form = {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    response = submit(owner, EPISODE, form)
    page = flash_after(owner, response)
    assert "some claims still have no checked primary source" in page
    assert 'value="published" selected' in page


def test_episode_moved_to_published_from_the_board_repeats_the_warning(
    owner: TestClient, claim: str
) -> None:
    approve_episode(owner, EPISODE)
    response = submit(owner, f"{EPISODE}/move", {"status": "published"})
    assert "some claims still have no checked primary source" in flash_after(owner, response)


def test_episode_published_without_claims_says_it_was_saved_as_usual(owner: TestClient) -> None:
    approve_episode(owner, EPISODE)
    form = {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    assert "Changes saved." in flash_after(owner, submit(owner, EPISODE, form))


def test_claims_page_as_viewer_hides_the_forms(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, claim: str
) -> None:
    viewer = join(app, mailer, owner, "viewer@example.com", "viewer")
    page = viewer.get(CLAIMS).text
    assert QUOTE in page and "Add a claim" not in page and "Mark as checked" not in page


def test_claim_add_as_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, source: str
) -> None:
    viewer = join(app, mailer, owner, "viewer@example.com", "viewer")
    assert submit(viewer, CLAIMS, new_claim_form(source)).status_code == 403


def test_claims_page_of_missing_episode_answers_not_found(owner: TestClient) -> None:
    assert owner.get("/app/channels/funny-memes/episodes/9/claims").status_code == 404
