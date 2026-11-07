"""The relationships pages beside the library (rule project.srstudio.bible)."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

LIBRARY = "/app/channels/funny-memes/library"
PAGE = "/app/channels/funny-memes/relationships"
MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    return client


def character(owner: TestClient, name: str) -> str:
    """Add a character to the library and return its id."""
    owner.get(submit(owner, f"{LIBRARY}/characters", {"name": name}).headers["location"])
    after_name = owner.get(LIBRARY).text.split(f"<strong>{name}</strong>")[1]
    return after_name.split(f'href="{LIBRARY}/characters/')[1].split("/")[0]


@pytest.fixture
def pair(owner: TestClient) -> tuple[str, str]:
    return character(owner, "Rex"), character(owner, "Lua")


@pytest.fixture
def neighbours(owner: TestClient, pair: tuple[str, str]) -> str:
    """Rex and Lua as neighbours; the address of their page."""
    rex, lua = pair
    created = submit(owner, PAGE, {"first": rex, "second": lua, "relation": "neighbours"})
    location: str = created.headers["location"]
    owner.get(location)
    return location


def pair_form(pair: tuple[str, str], **changes: str | list[str]) -> dict[str, str | list[str]]:
    """The pair page's form with the blank history row."""
    rex, lua = pair
    form: dict[str, str | list[str]] = {
        "revision": "1",
        "first": rex,
        "second": lua,
        "status": "proposal",
        "relation": "neighbours",
        "history_fact": "",
        "history_field": "relationship",
        "history_episode": "",
        "history_kind": "line",
        "history_quote": "",
    }
    return form | changes


def test_library_links_to_relationships(owner: TestClient) -> None:
    assert f'href="{PAGE}"' in owner.get(LIBRARY).text


def test_relationships_without_two_characters_explains_why(owner: TestClient) -> None:
    character(owner, "Rex")
    page = owner.get(PAGE).text
    assert "Add at least two characters" in page and 'data-dialog="add-relationship"' not in page


def test_relationship_add_opens_its_page_with_note(
    owner: TestClient, pair: tuple[str, str]
) -> None:
    created = submit(owner, PAGE, {"first": pair[0], "second": pair[1], "relation": "neighbours"})
    page = flash_after(owner, created)
    assert "Added to the library." in page and "Rex &amp; Lua" in page and "neighbours" in page


def test_relationships_list_links_each_pair(owner: TestClient, neighbours: str) -> None:
    page = owner.get(PAGE).text
    assert f'href="{neighbours}"' in page and "Proposal" in page


def test_relationship_add_same_character_twice_shows_error(
    owner: TestClient, pair: tuple[str, str]
) -> None:
    response = submit(owner, PAGE, {"first": pair[0], "second": pair[0]})
    assert response.status_code == 422 and 'id="second-error"' in response.text


def test_relationship_add_without_second_shows_error_in_open_dialog(
    owner: TestClient, pair: tuple[str, str]
) -> None:
    response = submit(owner, PAGE, {"first": pair[0], "second": ""})
    assert response.status_code == 422 and 'id="second-error"' in response.text
    assert re.search(r'<dialog[^>]*id="add-relationship"[^>]*open', response.text)


def test_relationship_add_unknown_character_shows_error(
    owner: TestClient, pair: tuple[str, str]
) -> None:
    response = submit(owner, PAGE, {"first": MISSING, "second": pair[1]})
    assert response.status_code == 422 and "no longer in the library" in response.text


def test_relationship_save_changes_texts_and_status(
    owner: TestClient, pair: tuple[str, str], neighbours: str
) -> None:
    form = pair_form(pair, status="canon", tension="the shared fridge")
    page = flash_after(owner, submit(owner, neighbours, form))
    assert "Changes saved." in page and "the shared fridge" in page
    assert '<option value="canon" selected' in page


def test_relationship_save_stale_revision_shows_conflict(
    owner: TestClient, pair: tuple[str, str], neighbours: str
) -> None:
    submit(owner, neighbours, pair_form(pair))
    response = submit(owner, neighbours, pair_form(pair, relation="rivals"))
    assert response.status_code == 409 and "Reload the page" in response.text


def test_relationship_history_cites_published_episode(
    owner: TestClient, pair: tuple[str, str], neighbours: str
) -> None:
    owner.post("/channels/funny-memes/episodes", json={"title": "Whose Milk"})
    owner.put(
        "/channels/funny-memes/episodes/1", json={"title": "Whose Milk", "status": "published"}
    )
    history = {
        "history_fact": ["Rex steals Lua's milk.", ""],
        "history_field": ["relationship", "relationship"],
        "history_episode": ["1", ""],
        "history_kind": ["line", "line"],
        "history_quote": ["Mine.", ""],
    }
    page = flash_after(owner, submit(owner, neighbours, pair_form(pair, **history)))
    assert "Rex steals Lua&#39;s milk." in page and "Fact 2" in page


def test_relationship_history_without_published_episode_explains_facts(
    owner: TestClient, neighbours: str
) -> None:
    assert "Publish an episode to cite it here." in owner.get(neighbours).text


def test_relationship_remove_returns_to_list(owner: TestClient, neighbours: str) -> None:
    page = flash_after(owner, submit(owner, f"{neighbours}/remove", {"revision": "1"}))
    assert "No relationships yet." in page


def test_relationship_unknown_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{PAGE}/{MISSING}").status_code == 404
    assert owner.get(f"{PAGE}/nope").status_code == 404


def test_relationship_contributor_reads_but_cannot_remove(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, neighbours: str
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    page = contributor.get(neighbours).text
    assert "Rex &amp; Lua" in page and f'action="{neighbours}/remove"' not in page
    assert submit(contributor, f"{neighbours}/remove", {"revision": "1"}).status_code == 403
