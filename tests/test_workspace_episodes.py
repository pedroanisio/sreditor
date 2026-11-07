"""Episode pages: list, create, edit and delete, with the roles and errors around them."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"
EPISODES = f"{CHANNEL}/episodes"
PILOT = {"title": "Pilot", "summary": "Why Mondays hurt."}


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    client.get("/app")  # shows and clears the note the creation left
    return client


@pytest.fixture
def pilot(owner: TestClient) -> None:
    created = submit(owner, EPISODES, PILOT)
    assert created.status_code == 303, created.text
    owner.get(created.headers["location"])  # clears the note


def test_channel_menu_lists_episodes(owner: TestClient) -> None:
    assert f'href="{EPISODES}"' in owner.get(CHANNEL).text


def test_episodes_without_episodes_shows_empty_state_and_form(owner: TestClient) -> None:
    page = owner.get(EPISODES).text
    assert "No episodes yet" in page and 'id="new-episode"' in page


def test_episode_create_opens_it_with_note(owner: TestClient) -> None:
    response = submit(owner, EPISODES, PILOT)
    assert response.headers["location"] == f"{EPISODES}/1"
    page = flash_after(owner, response)
    assert "Episode created." in page and "#1 · Pilot" in page


def test_episode_create_blank_title_shows_error_next_to_field(owner: TestClient) -> None:
    response = submit(owner, EPISODES, {"title": " "})
    assert response.status_code == 422 and 'id="title-error"' in response.text


def test_episodes_list_shows_episode_with_status(owner: TestClient, pilot: None) -> None:
    page = owner.get(EPISODES).text
    assert "#1 · Pilot" in page and "Idea" in page


def test_episodes_list_portuguese_translates_status(owner: TestClient, pilot: None) -> None:
    page = owner.get(f"{EPISODES}?lang=pt-BR").text
    assert "Episódios" in page and "Ideia" in page


def test_episodes_board_puts_episode_in_its_stage_column(owner: TestClient, pilot: None) -> None:
    page = owner.get(EPISODES).text
    idea = page.split('data-stage="idea"')[1].split("</section>")[0]
    assert "#1 · Pilot" in idea and 'data-stage="published"' in page


def test_episodes_board_card_is_movable_for_writer(owner: TestClient, pilot: None) -> None:
    page = owner.get(EPISODES).text
    assert "data-board-card" in page and f'action="{EPISODES}/1/move"' in page


def test_episodes_board_for_viewer_has_no_move_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, pilot: None
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(EPISODES).text
    assert "#1 · Pilot" in page and "data-board-card" not in page and "/move" not in page


def test_episodes_list_view_shows_badges_instead_of_board(owner: TestClient, pilot: None) -> None:
    page = owner.get(f"{EPISODES}?view=list").text
    assert 'class="board"' not in page and 'aria-current="page">List<' in page


def test_episode_move_changes_stage_and_returns_to_board(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1/move", {"status": "script"})
    assert response.headers["location"] == EPISODES
    page = flash_after(owner, response)
    script = page.split('data-stage="script"')[1].split("</section>")[0]
    assert "Episode moved." in page and "#1 · Pilot" in script


def test_episode_move_keeps_title_and_summary(owner: TestClient, pilot: None) -> None:
    submit(owner, f"{EPISODES}/1/move", {"status": "research"})
    page = owner.get(f"{EPISODES}/1").text
    assert 'value="Pilot"' in page and "Why Mondays hurt." in page


def test_episode_move_unknown_stage_answers_422(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1/move", {"status": "launched"})
    assert response.status_code == 422 and "Choose one of the stages" in response.text


def test_episode_move_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, pilot: None
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, f"{EPISODES}/1/move", {"status": "script"}).status_code == 403


def test_episode_move_unknown_episode_answers_404(owner: TestClient) -> None:
    assert submit(owner, f"{EPISODES}/9/move", {"status": "script"}).status_code == 404


def test_episodes_board_portuguese_names_the_switch(owner: TestClient, pilot: None) -> None:
    page = owner.get(f"{EPISODES}?lang=pt-BR").text
    assert ">Quadro</a>" in page and ">Lista</a>" in page and "Mover" in page


def test_episode_save_changes_status_with_note(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1", {**PILOT, "status": "script", "revision": "1"})
    page = flash_after(owner, response)
    assert "Changes saved." in page and 'value="script" selected' in page


def test_episode_page_new_episode_is_at_idea_stage(owner: TestClient, pilot: None) -> None:
    page = owner.get(f"{EPISODES}/1").text
    current = page.split('aria-current="step"')[1].split("</li>")[0]
    assert 'class="steps"' in page and "<span>Idea</span>" in current and "is-done" not in page


def test_episode_page_marks_stages_before_status_done(owner: TestClient, pilot: None) -> None:
    submit(owner, f"{EPISODES}/1", {**PILOT, "status": "script", "revision": "1"})
    page = owner.get(f"{EPISODES}/1").text
    assert page.count('class="is-done"') == 2 and page.count('class="is-next"') == 2


def test_episode_page_without_parts_shows_no_tab_counts(owner: TestClient, pilot: None) -> None:
    assert "tab-count" not in owner.get(f"{EPISODES}/1").text


def test_episode_page_portuguese_names_the_stages(owner: TestClient, pilot: None) -> None:
    page = owner.get(f"{EPISODES}/1?lang=pt-BR").text
    assert 'aria-label="Etapa de produção"' in page and "<span>Ideia</span>" in page


def test_episode_save_stale_revision_shows_conflict(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1", {**PILOT, "status": "idea", "revision": "4"})
    assert response.status_code == 409 and "Someone changed this episode" in response.text


def test_episode_save_unknown_status_shows_error(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1", {**PILOT, "status": "viral"})
    assert response.status_code == 422 and 'id="status-error"' in response.text


def test_episode_missing_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{EPISODES}/7").status_code == 404


def test_episode_delete_returns_to_list_with_note(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1/delete", {"revision": "1"})
    page = flash_after(owner, response)
    assert "Episode deleted." in page and "No episodes yet" in page


def test_episode_delete_stale_revision_shows_conflict(owner: TestClient, pilot: None) -> None:
    response = submit(owner, f"{EPISODES}/1/delete", {"revision": "3"})
    assert response.status_code == 409


def test_episode_page_for_viewer_hides_form_and_delete(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, pilot: None
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(f"{EPISODES}/1").text
    assert 'name="title"' not in page and "Delete episode" not in page


def test_episode_create_by_viewer_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    assert submit(viewer, EPISODES, PILOT).status_code == 403


def test_episode_delete_by_contributor_is_forbidden(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, pilot: None
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    assert submit(contributor, f"{EPISODES}/1/delete", {"revision": "1"}).status_code == 403


def test_overview_counts_episodes(owner: TestClient, pilot: None) -> None:
    page = owner.get(CHANNEL).text
    assert "<dt>Episodes</dt><dd>1</dd>" in page
