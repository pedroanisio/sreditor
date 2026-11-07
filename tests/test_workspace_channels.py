from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.web.workspace.forms import platform_or_none, revision_of, slug_from
from sreditor.web.workspace.shell import FLASH_COOKIE
from tests.conftest import RecordingMailer
from tests.workspace_helpers import browser, flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    client.get("/app")  # shows and clears the note the creation left
    return client


def revision(owner: TestClient) -> str:
    """The channel's current revision, as its pages carry it."""
    page = owner.get(f"{CHANNEL}/settings").text
    return page.split('name="revision" value="')[1].split('"')[0]


def test_slug_from_accented_name_is_ascii_words() -> None:
    assert slug_from("  Café com Memes! ") == "cafe-com-memes"


def test_revision_of_blank_is_none() -> None:
    assert revision_of("") is None and revision_of("7") == 7


def test_platform_or_none_unknown_is_none() -> None:
    assert platform_or_none("myspace") is None


def test_workspace_without_session_redirects_to_sign_in(app: FastAPI) -> None:
    response = browser(app).get("/app/channels/funny-memes?x=1")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/app/channels/funny-memes%3Fx%3D1"


def test_workspace_without_session_keeps_chosen_language(app: FastAPI) -> None:
    response = browser(app).get("/app?lang=pt-BR")
    assert "sreditor_lang=pt-BR" in response.headers["set-cookie"]


def test_workspace_htmx_without_session_answers_hx_redirect(app: FastAPI) -> None:
    response = browser(app).get("/app", headers={"hx-request": "true"})
    assert response.headers["hx-redirect"] == "/login?next=/app"


def test_channel_list_without_channels_shows_empty_state(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    page = member(app, mailer, "ana@example.com").get("/app").text
    assert "No channels yet" in page and 'href="/app/channels/new"' in page


def test_channel_list_shows_channel_status_and_role(owner: TestClient) -> None:
    page = owner.get("/app").text
    assert "Funny Memes" in page and "Draft" in page and "Owner" in page


def test_channel_list_portuguese_translates_labels(owner: TestClient) -> None:
    page = owner.get("/app?lang=pt-BR").text
    assert "Seus canais" in page and "Rascunho" in page


def test_new_channel_creates_it_and_opens_accounts_with_note(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = member(app, mailer, "ana@example.com")
    response = submit(client, "/app/channels/new", {"name": "Funny Memes", "language": "en"})
    assert response.headers["location"] == f"{CHANNEL}/accounts"
    assert "Channel created. Add its accounts next." in flash_after(client, response)


def test_flash_note_shows_once(owner: TestClient) -> None:
    data = {"name": "Memes", "language": "en", "status": "draft", "revision": revision(owner)}
    response = submit(owner, f"{CHANNEL}/settings", data)
    assert "Changes saved." in flash_after(owner, response)
    assert "Changes saved." not in owner.get("/app").text


def test_flash_unknown_key_shows_nothing(owner: TestClient) -> None:
    owner.cookies.set(FLASH_COOKIE, "bogus", path="/app")
    assert 'class="alert success"' not in owner.get("/app").text


def test_new_channel_form_suggests_page_language(app: FastAPI, mailer: RecordingMailer) -> None:
    page = member(app, mailer, "ana@example.com").get("/app/channels/new?lang=pt-BR").text
    assert 'name="language" type="text" value="pt-BR"' in page


def test_new_channel_taken_slug_answers_409_next_to_field(owner: TestClient) -> None:
    response = submit(
        owner, "/app/channels/new", {"name": "X", "slug": "funny-memes", "language": "en"}
    )
    assert response.status_code == 409
    assert 'id="slug-error">This address is taken.' in response.text


def test_new_channel_invalid_language_answers_422(owner: TestClient) -> None:
    response = submit(owner, "/app/channels/new", {"name": "X", "language": "english!"})
    assert response.status_code == 422 and 'id="language-error"' in response.text


def test_new_channel_forged_csrf_is_refused(owner: TestClient) -> None:
    response = owner.post("/app/channels/new", data={"csrf_token": "forged", "name": "Y"})
    assert response.status_code == 403 and "This form has expired" in response.text


def test_overview_new_channel_lists_steps_left(owner: TestClient) -> None:
    page = owner.get(CHANNEL).text
    assert "7 steps left" in page and f'href="{CHANNEL}/accounts">Add an account' in page


def test_overview_shows_sections_menu_with_current_page(owner: TestClient) -> None:
    page = owner.get(CHANNEL).text
    assert f'<a href="{CHANNEL}" aria-current="page">' in page and "<span>Overview</span>" in page


def test_channel_page_non_member_answers_404(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    stranger = member(app, mailer, "bob@example.com")
    response = stranger.get(CHANNEL)
    assert response.status_code == 404 and "Not found" in response.text


def test_settings_saves_name_and_status(owner: TestClient) -> None:
    data = {"name": "Memes", "language": "pt-BR", "status": "active", "revision": revision(owner)}
    response = submit(owner, f"{CHANNEL}/settings", data)
    assert "Changes saved." in flash_after(owner, response)
    assert "Active" in owner.get(CHANNEL).text


def test_settings_stale_revision_answers_409(owner: TestClient) -> None:
    data = {"name": "Memes", "language": "en", "status": "draft", "revision": "1"}
    submit(owner, f"{CHANNEL}/settings", {**data, "revision": revision(owner)})
    response = submit(owner, f"{CHANNEL}/settings", data)
    assert response.status_code == 409 and "Someone changed this channel" in response.text


def test_settings_unknown_status_answers_422(owner: TestClient) -> None:
    data = {"name": "Memes", "language": "en", "status": "gone", "revision": ""}
    response = submit(owner, f"{CHANNEL}/settings", data)
    assert response.status_code == 422 and 'id="status-error"' in response.text


def test_settings_viewer_answers_403(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    viewer = join(app, mailer, owner, "vic@example.com", "viewer")
    assert viewer.get(f"{CHANNEL}/settings").status_code == 403
    overview = viewer.get(CHANNEL).text
    assert (
        f'href="{CHANNEL}/settings"' not in overview and "<span>Add an account</span>" in overview
    )


def test_delete_wrong_slug_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/delete", {"confirm_slug": "nope", "revision": ""})
    assert response.status_code == 422 and "exactly as shown" in response.text


def test_delete_stale_revision_answers_409(owner: TestClient) -> None:
    response = submit(owner, f"{CHANNEL}/delete", {"confirm_slug": "funny-memes", "revision": "99"})
    assert response.status_code == 409


def test_delete_typed_slug_deletes_channel(owner: TestClient) -> None:
    data = {"confirm_slug": "funny-memes", "revision": revision(owner)}
    response = submit(owner, f"{CHANNEL}/delete", data)
    assert response.headers["location"] == "/app"
    assert "Channel deleted." in flash_after(owner, response)
    assert owner.get(CHANNEL).status_code == 404


def test_delete_editor_answers_403(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    editor = join(app, mailer, owner, "eve@example.com", "editor")
    response = submit(editor, f"{CHANNEL}/delete", {"confirm_slug": "funny-memes"})
    assert response.status_code == 403 and "Not allowed" in response.text
