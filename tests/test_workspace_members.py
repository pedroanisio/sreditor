from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, RecordingMailer
from tests.workspace_helpers import browser, flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"
MEMBERS = f"{CHANNEL}/members"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    client.get("/app")
    return client


def user_id(app: FastAPI, email: str) -> str:
    found = app.state.services.users.credentials(email)
    assert found is not None
    return str(found[0].id)


def invitation_id(owner: TestClient) -> str:
    page = owner.get(MEMBERS).text
    return page.split(f"{CHANNEL}/invitations/")[1].split("/")[0]


# Members


def test_members_lists_owner_as_you(owner: TestClient) -> None:
    page = owner.get(MEMBERS).text
    assert "Ana (you)" in page and "ana@example.com" in page


def test_members_invite_sends_email_and_lists_it_pending(
    owner: TestClient, mailer: RecordingMailer
) -> None:
    response = submit(owner, f"{MEMBERS}/invite", {"email": "new@example.com", "role": "editor"})
    page = flash_after(owner, response)
    assert "Invitation sent." in page and "new@example.com" in page
    assert mailer.sent[-1][0] == "new@example.com"


def test_members_invite_in_portuguese_sends_portuguese_email(
    owner: TestClient, mailer: RecordingMailer
) -> None:
    submit(owner, f"{MEMBERS}/invite?lang=pt-BR", {"email": "new@example.com", "role": "viewer"})
    assert "convid" in mailer.sent[-1][1]


def test_members_invite_bad_email_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{MEMBERS}/invite", {"email": "nope", "role": "viewer"})
    assert response.status_code == 422 and 'id="invite-email-error"' in response.text


def test_members_invite_unknown_role_answers_422(owner: TestClient) -> None:
    response = submit(owner, f"{MEMBERS}/invite", {"email": "a@example.com", "role": "king"})
    assert response.status_code == 422 and 'id="invite-role-error"' in response.text


def test_members_invite_existing_member_answers_409(owner: TestClient) -> None:
    response = submit(owner, f"{MEMBERS}/invite", {"email": "ANA@example.com", "role": "viewer"})
    assert response.status_code == 409 and "already a member" in response.text


def test_members_invite_over_rate_limit_answers_429(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    response = submit(owner, f"{MEMBERS}/invite", {"email": "a@example.com", "role": "viewer"})
    assert response.status_code == 429


def test_members_editor_cannot_offer_owner_role(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    editor = join(app, mailer, owner, "eve@example.com", "editor")
    assert 'value="owner"' not in editor.get(MEMBERS).text
    response = submit(editor, f"{MEMBERS}/invite", {"email": "a@example.com", "role": "owner"})
    assert response.status_code == 422


def test_members_viewer_sees_no_invite_form(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    viewer = join(app, mailer, owner, "vic@example.com", "viewer")
    assert 'id="invite-email"' not in viewer.get(MEMBERS).text
    response = submit(viewer, f"{MEMBERS}/invite", {"email": "a@example.com", "role": "viewer"})
    assert response.status_code == 403


def test_members_change_role_saves_it(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    join(app, mailer, owner, "vic@example.com", "viewer")
    target = user_id(app, "vic@example.com")
    response = submit(owner, f"{MEMBERS}/{target}/role", {"role": "editor"})
    assert "Changes saved." in flash_after(owner, response)
    assert app.state.services.members.role("funny-memes", target).value == "editor"


def test_members_last_owner_demoting_self_answers_409(owner: TestClient, app: FastAPI) -> None:
    target = user_id(app, "ana@example.com")
    response = submit(owner, f"{MEMBERS}/{target}/role", {"role": "viewer"})
    assert response.status_code == 409 and "at least one owner" in response.text


def test_members_change_to_unknown_role_answers_422(owner: TestClient, app: FastAPI) -> None:
    target = user_id(app, "ana@example.com")
    assert submit(owner, f"{MEMBERS}/{target}/role", {"role": "king"}).status_code == 422


def test_members_editor_changing_owner_answers_403(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    editor = join(app, mailer, owner, "eve@example.com", "editor")
    target = user_id(app, "ana@example.com")
    assert submit(editor, f"{MEMBERS}/{target}/role", {"role": "viewer"}).status_code == 403
    assert submit(editor, f"{MEMBERS}/{target}/remove").status_code == 403


def test_members_unknown_or_malformed_member_answers_404(owner: TestClient) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    assert submit(owner, f"{MEMBERS}/{unknown}/role", {"role": "viewer"}).status_code == 404
    assert submit(owner, f"{MEMBERS}/not-a-uuid/remove").status_code == 404


def test_members_remove_member_says_removed(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    join(app, mailer, owner, "vic@example.com", "viewer")
    response = submit(owner, f"{MEMBERS}/{user_id(app, 'vic@example.com')}/remove")
    assert "Removed." in flash_after(owner, response)
    assert "vic@example.com" not in owner.get(MEMBERS).text


def test_members_viewer_leaving_goes_to_channel_list(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    viewer = join(app, mailer, owner, "vic@example.com", "viewer")
    response = submit(viewer, f"{MEMBERS}/{user_id(app, 'vic@example.com')}/remove")
    assert response.headers["location"] == "/app"
    assert "You left the channel." in flash_after(viewer, response)


def test_members_last_owner_leaving_answers_409(owner: TestClient, app: FastAPI) -> None:
    response = submit(owner, f"{MEMBERS}/{user_id(app, 'ana@example.com')}/remove")
    assert response.status_code == 409 and "at least one owner" in response.text


def test_members_revoke_invitation_says_revoked(owner: TestClient) -> None:
    submit(owner, f"{MEMBERS}/invite", {"email": "new@example.com", "role": "viewer"})
    response = submit(owner, f"{CHANNEL}/invitations/{invitation_id(owner)}/revoke")
    page = flash_after(owner, response)
    assert "Revoked." in page and "No pending invitations." in page


def test_members_revoke_unknown_invitation_answers_404(owner: TestClient) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    assert submit(owner, f"{CHANNEL}/invitations/{unknown}/revoke").status_code == 404


def test_members_editor_revoking_owner_invitation_answers_403(
    owner: TestClient, app: FastAPI, mailer: RecordingMailer
) -> None:
    submit(owner, f"{MEMBERS}/invite", {"email": "new@example.com", "role": "owner"})
    pending = invitation_id(owner)
    editor = join(app, mailer, owner, "eve@example.com", "editor")
    assert submit(editor, f"{CHANNEL}/invitations/{pending}/revoke").status_code == 403


def test_members_revoke_already_gone_answers_409(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(owner, f"{MEMBERS}/invite", {"email": "new@example.com", "role": "viewer"})
    pending = invitation_id(owner)
    monkeypatch.setattr(app.state.services.members, "revoke_invitation", lambda *args: False)
    response = submit(owner, f"{CHANNEL}/invitations/{pending}/revoke")
    assert response.status_code == 409 and "no longer pending" in response.text


# Account


def test_account_rename_shows_new_name(owner: TestClient) -> None:
    response = submit(owner, "/app/account/profile", {"display_name": "Ana Maria"})
    assert "Ana Maria" in flash_after(owner, response)


def test_account_rename_blank_answers_422(owner: TestClient) -> None:
    response = submit(owner, "/app/account/profile", {"display_name": "  "})
    assert response.status_code == 422 and 'id="display_name-error"' in response.text


def test_account_password_wrong_current_answers_422(owner: TestClient) -> None:
    data = {"current_password": "wrong", "password": "another long password"}
    response = submit(owner, "/app/account/password", data)
    assert response.status_code == 422 and "Current password is wrong" in response.text


def test_account_password_short_new_answers_422(owner: TestClient) -> None:
    data = {"current_password": PASSWORD, "password": "short"}
    response = submit(owner, "/app/account/password", data)
    assert response.status_code == 422 and 'id="password-error"' in response.text


def test_account_password_over_rate_limit_answers_429(
    owner: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.services.limiter, "allow", lambda key: False)
    data = {"current_password": PASSWORD, "password": "another long password"}
    assert submit(owner, "/app/account/password", data).status_code == 429


def test_account_password_changed_keeps_this_session_only(owner: TestClient, app: FastAPI) -> None:
    other = browser(app)
    assert (
        submit(other, "/login", {"email": "ana@example.com", "password": PASSWORD}).status_code
        == 303
    )
    data = {"current_password": PASSWORD, "password": "another long password"}
    response = submit(owner, "/app/account/password", data)
    assert "Password changed." in flash_after(owner, response)
    assert other.get("/app").status_code == 303


# API tokens


def test_tokens_create_shows_secret_once_uncached(owner: TestClient) -> None:
    data = {"name": "bot", "scopes": ["channels:read"], "expires_in_days": "90"}
    response = submit(owner, "/app/account/tokens", data)
    assert 'value="sre_' in response.text and response.headers["cache-control"] == "no-store"
    assert 'value="sre_' not in owner.get("/app/account/tokens").text


def test_tokens_create_without_scope_answers_422(owner: TestClient) -> None:
    response = submit(owner, "/app/account/tokens", {"name": "bot", "expires_in_days": "90"})
    assert response.status_code == 422 and 'id="scopes-error"' in response.text


def test_tokens_create_bad_days_answers_422(owner: TestClient) -> None:
    data = {"name": "bot", "scopes": ["channels:read"], "expires_in_days": "forever"}
    response = submit(owner, "/app/account/tokens", data)
    assert response.status_code == 422 and 'id="expires_in_days-error"' in response.text


def test_tokens_create_blank_name_answers_422(owner: TestClient) -> None:
    data = {"name": " ", "scopes": ["channels:read"], "expires_in_days": "90"}
    response = submit(owner, "/app/account/tokens", data)
    assert response.status_code == 422 and 'id="name-error"' in response.text


def test_tokens_revoke_marks_it_revoked(owner: TestClient, app: FastAPI) -> None:
    submit(
        owner,
        "/app/account/tokens",
        {"name": "bot", "scopes": ["channels:read"], "expires_in_days": "30"},
    )
    owner_id = user_id(app, "ana@example.com")
    token = app.state.services.tokens.list(UUID(owner_id))[0]
    response = submit(owner, f"/app/account/tokens/{token.id}/revoke")
    assert "Revoked." in flash_after(owner, response)


def test_tokens_revoke_unknown_or_malformed_answers_404(owner: TestClient) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    assert submit(owner, f"/app/account/tokens/{unknown}/revoke").status_code == 404
    assert submit(owner, "/app/account/tokens/nope/revoke").status_code == 404
