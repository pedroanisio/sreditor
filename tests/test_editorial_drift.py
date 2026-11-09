"""Editorial guidelines that cite a stmt statement warn when it drifts (project.editorial.drift).

Each test builds its own statement in a temporary store, so none depends on the real rules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.store import Statement, StmtClient, StoreError
from tests.conftest import RecordingMailer
from tests.test_api import EDITORIAL_URL, FUNNY_MEMES
from tests.workspace_helpers import flash_after, member, new_channel, submit

SOURCE_ID = "house.first-line"
OLD_TEXT = "The first line lands at 0.5 s."
NEW_TEXT = "The first line lands at 0.25 s."
PAGE = "/app/channels/funny-memes/editorial"


def source_document(text: str, is_enabled: bool = True) -> dict[str, Any]:
    return {
        "statement": text,
        "owner": {"type": "user", "id": "pedro"},
        "category": "other",
        "enabled": is_enabled,
    }


def house_statement(app: FastAPI, text: str, is_enabled: bool = True) -> int:
    """Create the cited statement, or revise it; returns its revision."""
    client: StmtClient = app.state.services.stmt
    current = client.get(SOURCE_ID, "/")
    revision = current.revision if current else None
    return client.write(SOURCE_ID, source_document(text, is_enabled), "/", revision)


def break_reading(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make stmt fail when the cited statement is read, as when it is down."""
    client: StmtClient = app.state.services.stmt
    original = client.get

    def get(statement_id: str, scope: str) -> Statement | None:
        if statement_id == SOURCE_ID:
            raise StoreError("stmt is down")
        return original(statement_id, scope)

    monkeypatch.setattr(client, "get", get)


@pytest.fixture
def api(app: FastAPI, sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    house_statement(app, OLD_TEXT)
    return client


def cite(api: TestClient, text: str = OLD_TEXT, kind: str = "voice") -> dict[str, Any]:
    body = {"kind": kind, "text": text, "cited_statement_id": SOURCE_ID}
    response = api.post(EDITORIAL_URL, json=body)
    assert response.status_code == 201, response.text
    written: dict[str, Any] = response.json()
    return written


def warnings_of(api: TestClient) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = api.get(EDITORIAL_URL).json()["warnings"]
    return found


def test_editorial_guideline_matching_cited_text_has_no_warning(api: TestClient) -> None:
    cite(api)
    assert warnings_of(api) == []


def test_editorial_guideline_records_the_revision_it_follows(api: TestClient) -> None:
    assert cite(api)["citation"] == {"statement_id": SOURCE_ID, "revision": 1}


def test_editorial_changed_statement_warns_with_both_revisions_and_texts(
    api: TestClient, app: FastAPI
) -> None:
    written = cite(api)
    house_statement(app, NEW_TEXT)
    (warning,) = warnings_of(api)
    assert warning == {
        "guideline_id": written["id"],
        "kind": "changed",
        "statement_id": SOURCE_ID,
        "copy_revision": 1,
        "statement_revision": 2,
        "copy_text": OLD_TEXT,
        "statement_text": NEW_TEXT,
        "detail": f"Statement {SOURCE_ID} is at revision 2, this copy follows revision 1.",
    }


def test_editorial_changed_statement_with_same_text_has_no_warning(
    api: TestClient, app: FastAPI
) -> None:
    cite(api)
    house_statement(app, OLD_TEXT)
    assert warnings_of(api) == []


def test_editorial_retired_statement_warns_that_it_is_missing(
    api: TestClient, app: FastAPI
) -> None:
    cite(api)
    house_statement(app, OLD_TEXT, is_enabled=False)
    (warning,) = warnings_of(api)
    assert warning["kind"] == "missing" and warning["statement_text"] is None


def test_editorial_unreadable_stmt_warns_without_failing_the_read(
    api: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    cite(api)
    break_reading(app, monkeypatch)
    response = api.get(EDITORIAL_URL)
    assert response.status_code == 200
    assert [item["kind"] for item in response.json()["warnings"]] == ["unavailable"]


def test_editorial_citing_unknown_statement_answers_422(api: TestClient) -> None:
    body = {"kind": "voice", "text": "x", "cited_statement_id": "nobody.home"}
    response = api.post(EDITORIAL_URL, json=body)
    assert response.status_code == 422 and "nobody.home" in response.json()["detail"]


def test_editorial_take_cited_text_revises_the_copy_to_the_statement(
    api: TestClient, app: FastAPI
) -> None:
    written = cite(api)
    house_statement(app, NEW_TEXT)
    response = api.post(f"{EDITORIAL_URL}/{written['id']}/take-cited-text")
    assert response.status_code == 200
    assert response.json()["text"] == NEW_TEXT and response.json()["revision"] == 2
    editorial = api.get(EDITORIAL_URL).json()
    assert editorial["voice"]["citation"]["revision"] == 2 and editorial["warnings"] == []


def test_editorial_take_cited_text_without_citation_answers_409(api: TestClient) -> None:
    plain = api.post(EDITORIAL_URL, json={"kind": "voice", "text": "Dry."}).json()
    response = api.post(f"{EDITORIAL_URL}/{plain['id']}/take-cited-text")
    assert response.status_code == 409


def test_editorial_take_cited_text_of_missing_statement_answers_422(
    api: TestClient, app: FastAPI
) -> None:
    written = cite(api)
    house_statement(app, OLD_TEXT, is_enabled=False)
    response = api.post(f"{EDITORIAL_URL}/{written['id']}/take-cited-text")
    assert response.status_code == 422


def test_editorial_take_cited_text_with_stale_revision_answers_412(
    api: TestClient, app: FastAPI
) -> None:
    written = cite(api)
    house_statement(app, NEW_TEXT)
    url = f"{EDITORIAL_URL}/{written['id']}/take-cited-text"
    assert api.post(url, headers={"If-Match": '"9"'}).status_code == 412


# The page


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    house_statement(app, OLD_TEXT)
    return client


def cite_on_page(owner: TestClient, text: str = OLD_TEXT) -> None:
    data = {"kind": "voice", "text": text, "platform": "", "cited_statement_id": SOURCE_ID}
    assert submit(owner, PAGE, data).status_code == 303


def test_editorial_page_changed_statement_shows_both_texts_and_the_action(
    owner: TestClient, app: FastAPI
) -> None:
    cite_on_page(owner)
    house_statement(app, NEW_TEXT)
    page = owner.get(PAGE).text
    assert OLD_TEXT in page and NEW_TEXT in page
    assert "this copy follows revision 1" in page and "Use the statement&#39;s text" in page


def test_editorial_page_take_action_adopts_the_statement_text(
    owner: TestClient, app: FastAPI
) -> None:
    cite_on_page(owner)
    house_statement(app, NEW_TEXT)
    guideline_id = owner.get(PAGE).text.split('name="statement_id" value="')[1].split('"')[0]
    response = submit(owner, f"{PAGE}/take-cited-text", {"statement_id": guideline_id})
    page = flash_after(owner, response)
    assert NEW_TEXT in page and "The statement changed." not in page


def test_editorial_page_unreadable_stmt_still_renders_with_a_warning(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    cite_on_page(owner)
    break_reading(app, monkeypatch)
    response = owner.get(PAGE)
    assert response.status_code == 200 and "cannot be read now" in response.text


def test_editorial_page_missing_statement_warns_and_hides_the_action(
    owner: TestClient, app: FastAPI
) -> None:
    cite_on_page(owner)
    house_statement(app, OLD_TEXT, is_enabled=False)
    page = owner.get(PAGE).text
    assert "The statement is gone." in page and "take-cited-text" not in page


def test_editorial_page_citing_unknown_statement_answers_422_next_to_field(
    owner: TestClient,
) -> None:
    data = {"kind": "voice", "text": "x", "cited_statement_id": "nobody.home"}
    response = submit(owner, PAGE, data)
    assert response.status_code == 422 and 'id="voice-cited-error"' in response.text


def test_editorial_page_portuguese_translates_the_warning(owner: TestClient, app: FastAPI) -> None:
    cite_on_page(owner)
    house_statement(app, NEW_TEXT)
    page = owner.get(f"{PAGE}?lang=pt-BR").text
    assert "A declaração mudou." in page and "Usar o texto da declaração" in page
