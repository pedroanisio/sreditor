"""Helpers for the workspace page tests: a browser-like client signed in through the pages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from sreditor.web.forms import CSRF_COOKIE
from tests.conftest import PASSWORD, RecordingMailer

ORIGIN = "https://sreditor.test"


def browser(app: FastAPI) -> TestClient:
    """A same-origin client that does not follow redirects, as a browser posting forms."""
    return TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}, follow_redirects=False)


def submit(
    client: TestClient,
    path: str,
    data: Mapping[str, str | Sequence[str]] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> Response:
    """Post a form with the CSRF token the client's cookie holds, as a page would."""
    if CSRF_COOKIE not in client.cookies:
        client.get("/login")
    form: dict[str, str | Sequence[str]] = {
        "csrf_token": client.cookies[CSRF_COOKIE],
        **(data or {}),
    }
    return client.post(path, data=form, files=files)


def autosave(client: TestClient, path: str, data: Mapping[str, str]) -> Response:
    """Post a text part's form as workspace.js does while people type."""
    form = {"csrf_token": client.cookies[CSRF_COOKIE], **data}
    return client.post(path, data=form, headers={"Sreditor-Autosave": "1"})


def member(app: FastAPI, mailer: RecordingMailer, email: str, name: str = "Ana") -> TestClient:
    """Register, verify and sign in through the JSON API, then browse with the session cookie."""
    client = browser(app)
    client.post("/auth/register", json={"email": email, "display_name": name, "password": PASSWORD})
    client.post("/auth/verify", json={"token": mailer.token_for(email)})
    response = submit(client, "/login", {"email": email, "password": PASSWORD})
    assert response.status_code == 303, response.text
    return client


def new_channel(client: TestClient, slug: str = "funny-memes", name: str = "Funny Memes") -> None:
    """Create a channel through its page."""
    response = submit(client, "/app/channels/new", {"name": name, "slug": slug, "language": "en"})
    assert response.status_code == 303, response.text


def join(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient, email: str, role: str
) -> TestClient:
    """Invite someone through the members page and have them accept as a signed-in member."""
    sent = submit(owner, "/app/channels/funny-memes/members/invite", {"email": email, "role": role})
    assert sent.status_code == 303, sent.text
    token = mailer.token_for(email)
    client = member(app, mailer, email, name=email.split("@")[0])
    accepted = client.post("/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    return client


def flash_after(client: TestClient, response: Response) -> str:
    """The page a redirect leads to, as the browser would open it next."""
    assert response.status_code == 303, response.text
    return client.get(response.headers["location"]).text
