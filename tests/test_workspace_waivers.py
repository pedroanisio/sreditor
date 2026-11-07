"""The episode's waivers page: only owners and editors upload, edit, download and remove."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
WAIVERS = f"{EPISODE}/waivers"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def send(client: TestClient, content: bytes = sample.PDF, **fields: str) -> Response:
    data = {"signer": "Bruno Lima", **fields}
    return submit(client, WAIVERS, data, files={"file": ("bruno.pdf", content, "application/pdf")})


def waiver_id(client: TestClient) -> str:
    """The id of the first waiver, as its edit dialog carries it."""
    return client.get(WAIVERS).text.split('data-dialog="edit-')[1].split('"')[0]


@pytest.fixture
def signed(owner: TestClient) -> str:
    owner.get(send(owner, signed_on="2026-11-01").headers["location"])
    return waiver_id(owner)


def test_episode_page_shows_waivers_tab_to_owner(owner: TestClient) -> None:
    assert f'href="{WAIVERS}"' in owner.get(EPISODE).text


def test_waivers_page_without_waivers_shows_empty_state(owner: TestClient) -> None:
    assert "No waivers yet." in owner.get(WAIVERS).text


def test_waiver_upload_shows_note_signer_and_date(owner: TestClient) -> None:
    page = flash_after(owner, send(owner, signed_on="2026-11-01"))
    assert "File uploaded." in page and "<strong>Bruno Lima</strong>" in page
    assert "Signed on Nov 1, 2026" in page


def test_waiver_upload_returns_to_page_without_fragment(owner: TestClient) -> None:
    assert send(owner).headers["location"] == WAIVERS


def test_waiver_upload_wrong_type_shows_error_in_open_dialog(owner: TestClient) -> None:
    response = send(owner, sample.WAV)
    assert response.status_code == 422 and 'id="waiver-file-error"' in response.text


def test_waiver_upload_without_signer_shows_error(owner: TestClient) -> None:
    response = send(owner, signer="")
    assert response.status_code == 422 and 'id="waiver-signer-error"' in response.text


def test_waiver_upload_without_file_asks_for_one(owner: TestClient) -> None:
    response = submit(owner, WAIVERS, {"signer": "Bruno"})
    assert response.status_code == 422 and "Choose a file." in response.text


def test_waiver_file_downloads_with_security_headers(owner: TestClient, signed: str) -> None:
    response = owner.get(f"{WAIVERS}/{signed}/file")
    assert response.content == sample.PDF
    assert response.headers["content-disposition"].startswith("attachment")
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_waiver_download_link_skips_htmx(owner: TestClient, signed: str) -> None:
    assert f'href="{WAIVERS}/{signed}/file" download hx-boost="false"' in owner.get(WAIVERS).text


def test_waiver_file_unknown_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{WAIVERS}/nope/file").status_code == 404


def test_waiver_save_changes_signer(owner: TestClient, signed: str) -> None:
    fields = {"signer": "Bruno L.", "covers": "Voice", "signed_on": "", "revision": "1"}
    assert "<strong>Bruno L.</strong>" in flash_after(
        owner, submit(owner, f"{WAIVERS}/{signed}", fields)
    )


def test_waiver_save_stale_revision_shows_conflict(owner: TestClient, signed: str) -> None:
    submit(owner, f"{WAIVERS}/{signed}", {"signer": "One", "revision": "1"})
    response = submit(owner, f"{WAIVERS}/{signed}", {"signer": "Two", "revision": "1"})
    assert response.status_code == 409 and "Reload the page." in response.text


def test_waiver_save_bad_date_shows_error(owner: TestClient, signed: str) -> None:
    fields = {"signer": "Bruno", "signed_on": "yesterday", "revision": "1"}
    response = submit(owner, f"{WAIVERS}/{signed}", fields)
    assert response.status_code == 422 and f'id="{signed}-signed-on-error"' in response.text


def test_waiver_remove_drops_it(owner: TestClient, signed: str) -> None:
    page = flash_after(owner, submit(owner, f"{WAIVERS}/{signed}/remove", {"revision": "1"}))
    assert "Removed." in page and "No waivers yet." in page


def test_waiver_remove_malformed_id_shows_conflict(owner: TestClient) -> None:
    assert submit(owner, f"{WAIVERS}/nope/remove", {"revision": "1"}).status_code == 409


def test_waivers_hidden_from_contributor(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, signed: str
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    assert f'href="{WAIVERS}"' not in contributor.get(EPISODE).text
    assert contributor.get(WAIVERS).status_code == 403
    assert contributor.get(f"{WAIVERS}/{signed}/file").status_code == 403
    assert send(contributor).status_code == 403


def test_waivers_tab_counts_uploaded_waivers(owner: TestClient, signed: str) -> None:
    assert 'Waivers <span class="tab-count">1</span>' in owner.get(EPISODE).text


def test_waivers_page_in_portuguese_names_the_tab(owner: TestClient) -> None:
    assert "Autorizações" in owner.get(f"{WAIVERS}?lang=pt-BR").text


def test_waiver_upload_over_rate_limit_answers_429(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    assert send(owner).status_code == 429


@pytest.mark.parametrize(
    "path", [WAIVERS, f"{WAIVERS}/any", f"{WAIVERS}/any/remove"], ids=["upload", "save", "remove"]
)
def test_waiver_form_with_forged_csrf_is_refused(owner: TestClient, path: str) -> None:
    response = owner.post(path, data={"csrf_token": "forged", "signer": "Y"})
    assert response.status_code == 403 and "This form has expired" in response.text
