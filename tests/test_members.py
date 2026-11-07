from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from sreditor.members import PERMISSIONS, Permission, Role, allows
from tests.conftest import RecordingMailer

CHANNEL = {"slug": "funny-memes", "name": "Funny Memes"}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    api = sign_up("owner@example.com", "Owner")
    assert api.post("/channels", json=CHANNEL).status_code == 201
    return api


@pytest.fixture
def join(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> Callable[[str, Role], TestClient]:
    """Sign up a user and add them to the channel with a role, through an invitation."""

    def make(email: str, role: Role) -> TestClient:
        response = owner.post(
            "/channels/funny-memes/invitations", json={"email": email, "role": role}
        )
        assert response.status_code == 201, response.text
        token = mailer.token_for(email)
        member = sign_up(email)
        accepted = member.post("/invitations/accept", json={"token": token})
        assert accepted.json() == {"channel": "funny-memes", "role": role.value}
        return member

    return make


def user_id_of(api: TestClient) -> str:
    return api.user_id  # type: ignore[attr-defined]


def member_path(api: TestClient) -> str:
    return f"/channels/funny-memes/members/{user_id_of(api)}"


def invite_as(member: TestClient, role: str, email: str = "new@example.com") -> Response:
    return member.post("/channels/funny-memes/invitations", json={"email": email, "role": role})


@pytest.fixture
def editor(join: Callable[[str, Role], TestClient]) -> TestClient:
    return join("editor@example.com", Role.EDITOR)


@pytest.fixture
def viewer(join: Callable[[str, Role], TestClient]) -> TestClient:
    return join("viewer@example.com", Role.VIEWER)


@pytest.fixture
def other_editor(join: Callable[[str, Role], TestClient]) -> TestClient:
    return join("editor2@example.com", Role.EDITOR)


@pytest.fixture
def other_user(sign_up: Callable[..., TestClient]) -> TestClient:
    return sign_up("someone@example.com")


@pytest.fixture
def invitation_token(owner: TestClient, mailer: RecordingMailer) -> str:
    """Token of a viewer invitation sent with a mixed-case email address."""
    response = invite_as(owner, "viewer", "Invitee@Example.com")
    assert response.status_code == 201, response.text
    return mailer.token_for("invitee@example.com")


@pytest.fixture
def invitee(sign_up: Callable[..., TestClient]) -> TestClient:
    return sign_up("invitee@example.com")


@pytest.fixture
def admin(owner: TestClient, sign_up: Callable[..., TestClient], app: FastAPI) -> TestClient:
    admin_client = sign_up("admin@example.com")
    with app.state.services.users.database.connect() as connection:
        connection.execute("UPDATE users SET is_admin = 1 WHERE email = ?", ("admin@example.com",))
    return admin_client


def test_permissions_owner_role_has_every_permission() -> None:
    assert PERMISSIONS[Role.OWNER] == frozenset(Permission)


def test_permissions_ranked_roles_are_strict_subsets_of_the_next() -> None:
    assert (
        PERMISSIONS[Role.VIEWER]
        < PERMISSIONS[Role.CONTRIBUTOR]
        < PERMISSIONS[Role.EDITOR]
        < PERMISSIONS[Role.OWNER]
    )


def test_allows_without_role_denies_read() -> None:
    assert not allows(None, Permission.READ)


def test_allows_editor_denies_manage_owners() -> None:
    assert not allows(Role.EDITOR, Permission.MANAGE_OWNERS)


def test_channel_create_makes_creator_owner_in_session_profile(owner: TestClient) -> None:
    assert owner.get("/me").json()["channels"] == {"funny-memes": "owner"}


def test_channel_create_lists_creator_as_only_member(owner: TestClient) -> None:
    members = owner.get("/channels/funny-memes/members").json()["items"]
    assert [(member["email"], member["role"], member["added_by"]) for member in members] == [
        ("owner@example.com", "owner", None)
    ]


def test_channel_list_as_non_member_returns_empty(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    stranger = sign_up("stranger@example.com")
    assert stranger.get("/channels").json()["items"] == []


@pytest.mark.parametrize("path", ["/channels/funny-memes", "/channels/funny-memes/members"])
def test_channel_routes_as_non_member_return_not_found(
    owner: TestClient, sign_up: Callable[..., TestClient], path: str
) -> None:
    stranger = sign_up("stranger@example.com")
    assert stranger.get(path).status_code == 404


READING_ROLES = [Role.VIEWER, Role.CONTRIBUTOR, Role.EDITOR]
GUIDELINE = {"kind": "pillar", "text": "Office life."}
TIKTOK_ACCOUNT = {"platform": "tiktok", "handle": "@fm"}


@pytest.mark.parametrize("role", READING_ROLES)
def test_channel_get_as_member_returns_ok(
    join: Callable[[str, Role], TestClient], role: Role
) -> None:
    reader = join(f"{role.value}@example.com", role)
    assert reader.get("/channels/funny-memes").status_code == 200


@pytest.mark.parametrize("role", READING_ROLES)
def test_editorial_get_as_member_returns_ok(
    join: Callable[[str, Role], TestClient], role: Role
) -> None:
    reader = join(f"{role.value}@example.com", role)
    assert reader.get("/channels/funny-memes/editorial").status_code == 200


@pytest.mark.parametrize("role", READING_ROLES)
def test_channel_list_as_member_returns_the_channel(
    join: Callable[[str, Role], TestClient], role: Role
) -> None:
    reader = join(f"{role.value}@example.com", role)
    assert [channel["slug"] for channel in reader.get("/channels").json()["items"]] == [
        "funny-memes"
    ]


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR])
def test_editorial_write_as_viewer_or_contributor_returns_forbidden(
    join: Callable[[str, Role], TestClient], role: Role
) -> None:
    weak_member = join(f"{role.value}@example.com", role)
    response = weak_member.post("/channels/funny-memes/editorial", json=GUIDELINE)
    assert response.status_code == 403


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR])
def test_account_put_as_viewer_or_contributor_returns_forbidden(
    join: Callable[[str, Role], TestClient], role: Role
) -> None:
    weak_member = join(f"{role.value}@example.com", role)
    response = weak_member.put("/channels/funny-memes/accounts/tiktok", json=TIKTOK_ACCOUNT)
    assert response.status_code == 403


def test_editorial_write_as_editor_returns_created(editor: TestClient) -> None:
    assert editor.post("/channels/funny-memes/editorial", json=GUIDELINE).status_code == 201


def test_account_put_as_editor_returns_ok(editor: TestClient) -> None:
    response = editor.put("/channels/funny-memes/accounts/tiktok", json=TIKTOK_ACCOUNT)
    assert response.status_code == 200


def test_channel_delete_as_editor_returns_forbidden(editor: TestClient) -> None:
    assert editor.delete("/channels/funny-memes").status_code == 403


def test_invitation_create_by_editor_for_contributor_returns_created(editor: TestClient) -> None:
    assert invite_as(editor, "contributor").status_code == 201


@pytest.mark.parametrize("role", ["editor", "owner"])
def test_invitation_create_by_editor_for_same_or_higher_role_returns_forbidden(
    editor: TestClient, role: str
) -> None:
    assert invite_as(editor, role).status_code == 403


def test_member_role_change_by_editor_to_lower_role_returns_ok(
    editor: TestClient, viewer: TestClient
) -> None:
    response = editor.patch(member_path(viewer), json={"role": "contributor"})
    assert response.status_code == 200


def test_member_role_change_by_editor_to_editor_returns_forbidden(
    editor: TestClient, viewer: TestClient
) -> None:
    assert editor.patch(member_path(viewer), json={"role": "editor"}).status_code == 403


def test_member_remove_by_editor_of_another_editor_returns_forbidden(
    editor: TestClient, other_editor: TestClient
) -> None:
    assert editor.delete(member_path(other_editor)).status_code == 403


def test_member_remove_by_editor_of_viewer_returns_no_content(
    editor: TestClient, viewer: TestClient
) -> None:
    assert editor.delete(member_path(viewer)).status_code == 204


def test_channel_get_after_removal_by_editor_returns_not_found(
    editor: TestClient, viewer: TestClient
) -> None:
    editor.delete(member_path(viewer))
    assert viewer.get("/channels/funny-memes").status_code == 404


def test_member_role_change_by_owner_to_owner_returns_ok(
    owner: TestClient, editor: TestClient
) -> None:
    assert owner.patch(member_path(editor), json={"role": "owner"}).status_code == 200


def test_invitation_create_by_removed_member_returns_not_found(
    editor: TestClient, viewer: TestClient
) -> None:
    editor.delete(member_path(viewer))
    assert invite_as(viewer, "viewer", "x@example.com").status_code == 404


def test_member_role_change_of_last_owner_returns_conflict(owner: TestClient) -> None:
    assert owner.patch(member_path(owner), json={"role": "editor"}).status_code == 409


def test_member_remove_of_last_owner_returns_conflict(owner: TestClient) -> None:
    assert owner.delete(member_path(owner)).status_code == 409


def test_member_role_change_of_owner_with_second_owner_returns_ok(
    owner: TestClient, join: Callable[[str, Role], TestClient]
) -> None:
    join("second@example.com", Role.OWNER)
    assert owner.patch(member_path(owner), json={"role": "editor"}).status_code == 200


def test_member_remove_of_owner_by_second_owner_returns_no_content(
    owner: TestClient, join: Callable[[str, Role], TestClient]
) -> None:
    second = join("second@example.com", Role.OWNER)
    owner.patch(member_path(owner), json={"role": "editor"})
    assert second.delete(member_path(owner)).status_code == 204


def test_member_leave_returns_no_content(viewer: TestClient) -> None:
    assert viewer.delete(member_path(viewer)).status_code == 204


def test_member_leave_removes_channel_from_session_profile(viewer: TestClient) -> None:
    viewer.delete(member_path(viewer))
    assert viewer.get("/me").json()["channels"] == {}


UNKNOWN_MEMBER_PATH = "/channels/funny-memes/members/00000000-0000-0000-0000-000000000000"


def test_member_role_change_unknown_member_returns_not_found(owner: TestClient) -> None:
    assert owner.patch(UNKNOWN_MEMBER_PATH, json={"role": "viewer"}).status_code == 404


def test_member_remove_unknown_member_returns_not_found(owner: TestClient) -> None:
    assert owner.delete(UNKNOWN_MEMBER_PATH).status_code == 404


def test_invitation_accept_by_other_user_returns_forbidden(
    other_user: TestClient, invitation_token: str
) -> None:
    response = other_user.post("/invitations/accept", json={"token": invitation_token})
    assert response.status_code == 403


def test_invitation_accept_unknown_token_returns_not_found(
    other_user: TestClient, invitation_token: str
) -> None:
    assert other_user.post("/invitations/accept", json={"token": "nope"}).status_code == 404


def test_invitation_accept_by_invitee_returns_ok(
    invitee: TestClient, invitation_token: str
) -> None:
    response = invitee.post("/invitations/accept", json={"token": invitation_token})
    assert response.status_code == 200


def test_invitation_accept_twice_returns_not_found(
    invitee: TestClient, invitation_token: str
) -> None:
    invitee.post("/invitations/accept", json={"token": invitation_token})
    response = invitee.post("/invitations/accept", json={"token": invitation_token})
    assert response.status_code == 404


def test_invitation_accept_by_existing_member_returns_conflict(
    owner: TestClient, mailer: RecordingMailer, invitee: TestClient, invitation_token: str
) -> None:
    invitee.post("/invitations/accept", json={"token": invitation_token})
    invite_as(owner, "editor", "invitee@example.com")
    again = mailer.token_for("invitee@example.com")
    assert invitee.post("/invitations/accept", json={"token": again}).status_code == 409


def test_channel_delete_removes_memberships_from_session_profile(
    owner: TestClient, viewer: TestClient
) -> None:
    owner.delete("/channels/funny-memes")
    assert viewer.get("/me").json()["channels"] == {}


def test_channel_list_as_admin_returns_every_channel(admin: TestClient) -> None:
    assert [channel["slug"] for channel in admin.get("/channels").json()["items"]] == [
        "funny-memes"
    ]


def test_editorial_write_as_admin_returns_created(admin: TestClient) -> None:
    guideline: dict[str, Any] = {"kind": "voice", "text": "Dry."}
    assert admin.post("/channels/funny-memes/editorial", json=guideline).status_code == 201


def agent_of(app: FastAPI, member: TestClient, scopes: list[str]) -> TestClient:
    """A client using a fresh API token of the member with the given scopes."""
    created = member.post("/me/tokens", json={"name": "bot", "scopes": scopes})
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    return TestClient(
        app, base_url="https://testserver", headers={"Authorization": f"Bearer {secret}"}
    )


def test_invitation_accept_with_api_token_returns_forbidden(
    app: FastAPI, invitee: TestClient, invitation_token: str
) -> None:
    bot = agent_of(app, invitee, ["channels:read", "members:write"])
    response = bot.post("/invitations/accept", json={"token": invitation_token})
    assert response.status_code == 403


def test_member_get_returns_role_as_etag(owner: TestClient, viewer: TestClient) -> None:
    response = owner.get(member_path(viewer))
    assert response.status_code == 200 and response.headers["ETag"] == '"viewer"'


def test_member_get_unknown_member_returns_not_found(
    owner: TestClient, other_user: TestClient
) -> None:
    assert owner.get(member_path(other_user)).status_code == 404


def test_member_role_change_with_matching_if_match_returns_ok(
    owner: TestClient, viewer: TestClient
) -> None:
    response = owner.patch(
        member_path(viewer), json={"role": "contributor"}, headers={"If-Match": '"viewer"'}
    )
    assert response.status_code == 200


def test_member_role_change_with_stale_if_match_returns_precondition_failed(
    owner: TestClient, viewer: TestClient
) -> None:
    response = owner.patch(
        member_path(viewer), json={"role": "contributor"}, headers={"If-Match": '"editor"'}
    )
    assert response.status_code == 412 and response.headers["ETag"] == '"viewer"'


def test_member_role_change_with_foreign_if_match_returns_precondition_failed(
    owner: TestClient, viewer: TestClient
) -> None:
    response = owner.patch(
        member_path(viewer), json={"role": "contributor"}, headers={"If-Match": '"7"'}
    )
    assert response.status_code == 412


def test_member_remove_with_stale_if_match_keeps_member(
    owner: TestClient, viewer: TestClient
) -> None:
    response = owner.delete(member_path(viewer), headers={"If-Match": 'W/"contributor"'})
    assert response.status_code == 412 and owner.get(member_path(viewer)).status_code == 200


def test_member_remove_with_any_if_match_returns_no_content(
    owner: TestClient, viewer: TestClient
) -> None:
    assert owner.delete(member_path(viewer), headers={"If-Match": "*"}).status_code == 204


def test_member_role_change_after_concurrent_change_is_refused(
    app: FastAPI, owner: TestClient, viewer: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = app.state.services.members
    monkeypatch.setattr(members, "set_role", lambda slug, user_id, role, current: False)
    response = owner.patch(member_path(viewer), json={"role": "contributor"})
    assert response.status_code == 412


def test_member_remove_after_concurrent_change_is_refused(
    app: FastAPI, owner: TestClient, viewer: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = app.state.services.members
    monkeypatch.setattr(members, "remove", lambda slug, user_id, current: False)
    assert owner.delete(member_path(viewer)).status_code == 412


@pytest.mark.parametrize("path", ["/channels", "/me"])
def test_channel_listing_with_token_without_channels_read_returns_forbidden(
    app: FastAPI, owner: TestClient, path: str
) -> None:
    bot = agent_of(app, owner, ["content:write"])
    response = bot.get(path)
    assert response.status_code == 403 and "channels:read" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/channels", "/me"])
def test_channel_listing_with_channels_read_token_returns_ok(
    app: FastAPI, owner: TestClient, path: str
) -> None:
    assert agent_of(app, owner, ["channels:read"]).get(path).status_code == 200
