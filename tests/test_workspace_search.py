"""The workspace's quick search: the dialog, its results alone and the page without JavaScript."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import member, new_channel, submit

SEARCH = "/app/search"
DIALOG = {"HX-Request": "true", "HX-Target": "search-results"}
CHANNEL = "/app/channels/funny-memes"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    submit(client, f"{CHANNEL}/episodes", {"title": "Produção do piloto"})
    submit(client, f"{CHANNEL}/pitches", {"title": "Mondays", "summary": "Why"})
    return client


def test_workspace_sidebar_offers_search_with_shortcut(owner: TestClient) -> None:
    page = owner.get(CHANNEL).text
    assert f'href="{SEARCH}?channel=funny-memes"' in page and "<kbd>Ctrl K</kbd>" in page
    assert 'id="search-query"' in page


def test_search_dialog_finds_episode_ignoring_accents(owner: TestClient) -> None:
    fragment = owner.get(SEARCH, params={"q": "producao"}, headers=DIALOG).text
    assert f'href="{CHANNEL}/episodes/1"' in fragment and "#1 · Produção do piloto" in fragment
    assert "<html" not in fragment


def test_search_dialog_finds_pitch_with_channel_name(owner: TestClient) -> None:
    fragment = owner.get(SEARCH, params={"q": "monday"}, headers=DIALOG).text
    assert f"{CHANNEL}/pitches/" in fragment and "<small>Funny Memes</small>" in fragment


def test_search_dialog_in_channel_lists_its_sections(owner: TestClient) -> None:
    fragment = owner.get(SEARCH, params={"q": "lib", "channel": "funny-memes"}, headers=DIALOG)
    assert f'href="{CHANNEL}/library"' in fragment.text and "Sections" in fragment.text


def test_search_dialog_without_text_lists_channels(owner: TestClient) -> None:
    fragment = owner.get(SEARCH, headers=DIALOG).text
    assert f'href="{CHANNEL}"' in fragment and "Episodes" not in fragment


def test_search_dialog_without_match_says_so(owner: TestClient) -> None:
    assert "Nothing matches “zzz”." in owner.get(SEARCH, params={"q": "zzz"}, headers=DIALOG).text


def test_search_skips_channels_of_others(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    stranger = member(app, mailer, "bo@example.com", "Bo")
    assert "Produção" not in stranger.get(SEARCH, params={"q": "piloto"}, headers=DIALOG).text


def test_search_in_channel_of_others_answers_404(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    stranger = member(app, mailer, "bo@example.com", "Bo")
    assert stranger.get(SEARCH, params={"channel": "funny-memes"}).status_code == 404


def test_search_page_without_javascript_shows_field_and_results(owner: TestClient) -> None:
    page = owner.get(SEARCH, params={"q": "piloto"}).text
    assert 'role="search"' in page and "Produção do piloto" in page
    assert page.count('id="search-query"') == 1


def test_search_page_in_portuguese_names_the_groups(owner: TestClient) -> None:
    page = owner.get(SEARCH, params={"q": "piloto", "lang": "pt-BR"}).text
    assert "Episódios" in page and "Buscar canais, episódios e pautas" in page


def test_search_page_without_session_goes_to_sign_in(app: FastAPI) -> None:
    response = TestClient(app).get(SEARCH, follow_redirects=False)
    assert response.status_code == 303 and "/login" in response.headers["location"]
