"""Connecting an app with OAuth in a real browser, in each language (rules agentic.oauth, ui.e2e.playwright)."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import Page, Playwright, Route, expect

from e2e.conftest import Evidence, Server
from e2e.test_flows import PASSWORD, heading, open_page, sign_in
from e2e.test_workspace import EMAIL, registered
from sreditor.i18n import LOCALES, Translator

CLIENT_NAME = "E2E"
# Nothing listens here: the browser's request is caught by page.route and answered in the test.
CALLBACK = "http://127.0.0.1:9/callback"
STATE = "e2e-state"
VERIFIER_BYTES = 48  # 64 characters once encoded, inside the 43-128 PKCE allows


@pytest.fixture(params=LOCALES)
def translate(request: pytest.FixtureRequest) -> Translator:
    return Translator(request.param)


def challenge_for(verifier: str) -> str:
    """The PKCE S256 challenge: base64url of the verifier's SHA-256, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def register_client(playwright: Playwright, server: Server) -> str:
    """Register a public client the way a connector does, and return its id."""
    api = playwright.request.new_context()
    body = {"redirect_uris": [CALLBACK], "client_name": CLIENT_NAME}
    response = api.post(f"{server.url}/oauth/register", data=body)
    assert response.status == 201
    client_id = str(response.json()["client_id"])
    api.dispose()
    return client_id


def authorize_path(client_id: str, verifier: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        "state": STATE,
    }
    return f"/oauth/authorize?{urlencode(query)}"


def catch_callback(page: Page) -> list[str]:
    """Answer the client's callback in the browser and keep each URL it was called with."""
    calls: list[str] = []

    def answer(route: Route) -> None:
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="text/plain", body="connected")

    page.route(f"{CALLBACK}**", answer)
    return calls


def callback_query(calls: list[str]) -> dict[str, str]:
    """The query of the latest callback, one value per name."""
    assert calls, "the browser never came back to the client"
    return {name: values[0] for name, values in parse_qs(urlsplit(calls[-1]).query).items()}


def exchange(
    playwright: Playwright, server: Server, client_id: str, code: str, verifier: str
) -> dict[str, str]:
    """Trade the code for a token, as the client does after the redirect."""
    api = playwright.request.new_context()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    response = api.post(f"{server.url}/oauth/token", form=form)
    assert response.ok, response.text()
    token = {name: str(value) for name, value in response.json().items()}
    api.dispose()
    return token


def consent(page: Page, translate: Translator) -> None:
    heading(page, translate("Connect %(name)s", name=CLIENT_NAME))


def test_oauth_in_browser_connects_an_app_and_can_be_denied(
    page: Page, playwright: Playwright, server: Server, translate: Translator, evidence: Evidence
) -> None:
    registered(playwright, server)
    client_id = register_client(playwright, server)
    verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    calls = catch_callback(page)
    # The language is picked first; the authorize link then sends the visitor to sign in.
    open_page(page, server, "/login", translate)
    page.goto(f"{server.url}{authorize_path(client_id, verifier)}")
    sign_in(page, translate, EMAIL, PASSWORD)
    consent(page, translate)
    evidence.capture("oauth-consent")

    for box in page.locator("input[name='scopes']").all():
        box.uncheck()
    page.get_by_role("button", name=translate("Allow")).click()
    expect(page.locator("#scopes-error")).to_have_text(translate("Choose at least one permission."))
    evidence.capture("oauth-no-permission")

    page.locator("input[name='scopes']").first.check()
    page.get_by_role("button", name=translate("Allow")).click()
    expect(page.get_by_text("connected")).to_be_visible()
    allowed = callback_query(calls)
    assert allowed["state"] == STATE
    assert allowed["iss"] == server.url
    token = exchange(playwright, server, client_id, allowed["code"], verifier)
    assert token["token_type"] == "Bearer"
    assert token["access_token"].startswith("sre_")

    open_page(page, server, "/app/account/tokens", translate)
    expect(page.get_by_text(f"{CLIENT_NAME} (OAuth)", exact=True)).to_be_visible()
    evidence.capture("oauth-token-listed")

    page.goto(f"{server.url}{authorize_path(client_id, verifier)}")
    consent(page, translate)
    page.get_by_role("button", name=translate("Deny")).click()
    expect(page.get_by_text("connected")).to_be_visible()
    denied = callback_query(calls)
    assert denied["error"] == "access_denied"
    assert denied["state"] == STATE
