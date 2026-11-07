"""The render workers page, for instance admins (rule project.render.workers)."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, member, submit

PAGE = "/app/admin/render-workers"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def admin(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "admin@example.com")
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin@example.com'")
    return client


def registered_id(app: FastAPI) -> str:
    """The id of the only registered worker."""
    return str(app.state.services.renders.workers()[0].id)


def test_workers_page_linked_in_admin_sidebar(admin: TestClient) -> None:
    assert f'href="{PAGE}"' in admin.get("/app").text


def test_workers_page_without_workers_says_none(admin: TestClient) -> None:
    assert "No workers yet." in admin.get(PAGE).text


def test_worker_register_shows_secret_once_uncached(admin: TestClient) -> None:
    response = submit(admin, PAGE, {"name": "gpu-1"})
    assert re.search(r'value="srw_[^"]+"', response.text)
    assert response.headers["cache-control"] == "no-store"
    page = admin.get(PAGE).text
    assert "gpu-1" in page and "Never connected" in page and "srw_" in page
    assert not re.search(r'value="srw_[^"]+"', page)


def test_worker_register_blank_name_shows_error(admin: TestClient) -> None:
    response = submit(admin, PAGE, {"name": "  "})
    assert response.status_code == 422 and 'aria-invalid="true"' in response.text


def test_worker_revoke_marks_revoked(app: FastAPI, admin: TestClient) -> None:
    submit(admin, PAGE, {"name": "gpu-1"})
    page = flash_after(admin, submit(admin, f"{PAGE}/{registered_id(app)}/revoke", {}))
    assert "Revoked." in page and "Revoke worker" not in page


def test_worker_revoke_unknown_answers_not_found(admin: TestClient) -> None:
    assert submit(admin, f"{PAGE}/{UNKNOWN_ID}/revoke", {}).status_code == 404


def test_workers_page_refuses_non_admin(app: FastAPI, mailer: RecordingMailer) -> None:
    client = member(app, mailer, "ana@example.com")
    assert f'href="{PAGE}"' not in client.get("/app").text
    assert client.get(PAGE).status_code == 403
    assert submit(client, PAGE, {"name": "gpu-1"}).status_code == 403
