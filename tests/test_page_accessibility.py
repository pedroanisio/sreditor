"""Page markup that keyboard and screen-reader users depend on, and what the browser may keep."""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.workspace_helpers import join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    return client


def htmx_config(page: str) -> dict[str, object]:
    """The htmx settings the page's meta tag declares."""
    found = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", page)
    assert found is not None
    config: dict[str, object] = json.loads(found.group(1))
    return config


def test_pages_htmx_history_cache_is_off(owner: TestClient) -> None:
    # A token's secret or a waiver must never be copied into localStorage.
    assert htmx_config(owner.get("/app").text)["historyCacheSize"] == 0


def test_pages_first_link_skips_to_main(owner: TestClient) -> None:
    page = owner.get(CHANNEL).text
    body = page.split("<body", 1)[1]
    first_link = re.search(r"<a [^>]*>", body)
    assert first_link is not None and 'href="#main"' in first_link.group(0)
    assert 'id="main"' in page


def test_pages_carry_translated_request_error_messages(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = member(app, mailer, "ana@example.com")
    page = client.get("/app?lang=pt-BR").text
    assert 'data-error-network="Não foi possível falar com o sreditor.' in page
    assert '<template id="toast-error">' in page


def test_overview_checklist_says_done_or_to_do_in_words(owner: TestClient) -> None:
    page = owner.get(CHANNEL).text
    assert '<span class="visually-hidden">To do: </span>' in page


def test_members_remove_button_names_the_member(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    join(app, mailer, owner, "vi@example.com", "viewer")
    page = owner.get(f"{CHANNEL}/members").text
    assert re.search(r'<summary class="[^"]*" aria-label="Remove: [^"]+">', page)


def test_short_script_row_controls_name_their_block(owner: TestClient) -> None:
    submit(owner, f"{CHANNEL}/episodes", {"title": "Pilot"})
    page = owner.get(f"{CHANNEL}/episodes/1/short-script").text
    assert "Speech, block 1" in page and "Remove block, block 1" in page
