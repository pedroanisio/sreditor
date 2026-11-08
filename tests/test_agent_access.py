"""Agent access fixes: channel-limited tokens, guarded guideline writes, idempotent episode
creation, invitations over MCP and short scripts that start in the channel's language."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import Database
from sreditor.short_script import speech_language_for
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.workspace_helpers import member, new_channel, submit

OTHER = {"slug": "other-memes", "name": "Other Memes"}
PURPOSE = {"kind": "purpose", "text": "Make Mondays funny."}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    for channel in (FUNNY_MEMES, OTHER):
        assert client.post("/channels", json=channel).status_code == 201
    return client


@pytest.fixture
def limited(app: FastAPI, owner: TestClient) -> TestClient:
    """An agent whose token reaches only funny-memes."""
    return agent(app, new_token(owner, channels=["funny-memes"]))


# Channel-limited tokens


def test_token_limited_to_channel_lists_only_it(owner: TestClient) -> None:
    response = owner.post(
        "/me/tokens", json={"name": "a", "scopes": ["channels:read"], "channels": ["funny-memes"]}
    )
    assert response.json()["token"]["channels"] == ["funny-memes"]


def test_token_without_channels_reaches_all(owner: TestClient) -> None:
    response = owner.post("/me/tokens", json={"name": "a", "scopes": ["channels:read"]})
    assert response.json()["token"]["channels"] is None


def test_limited_token_reads_its_channel(limited: TestClient) -> None:
    assert limited.get("/channels/funny-memes").status_code == 200


def test_limited_token_on_other_channel_gets_not_found(limited: TestClient) -> None:
    assert limited.get("/channels/other-memes").status_code == 404


def test_limited_token_lists_only_its_channels(limited: TestClient) -> None:
    slugs = [item["slug"] for item in limited.get("/channels").json()["items"]]
    assert slugs == ["funny-memes"]


def test_limited_token_of_removed_member_lists_no_channels(
    app: FastAPI, limited: TestClient
) -> None:
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("DELETE FROM memberships")
    assert limited.get("/channels").json()["items"] == []


def test_limited_token_me_shows_only_its_channels(limited: TestClient) -> None:
    assert list(limited.get("/me").json()["channels"]) == ["funny-memes"]


def test_limited_token_cannot_create_channel(limited: TestClient) -> None:
    response = limited.post("/channels", json={"slug": "third", "name": "Third"})
    assert response.status_code == 403 and "limited to some channels" in response.json()["detail"]


def test_token_for_channel_you_are_not_in_is_refused(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    stranger = sign_up("stranger@example.com")
    body = {"name": "a", "scopes": ["channels:read"], "channels": ["funny-memes"]}
    response = stranger.post("/me/tokens", json=body)
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "channels"]


def test_admin_token_for_existing_channel_is_allowed(
    owner: TestClient, sign_up: Callable[..., TestClient], app: FastAPI
) -> None:
    admin = sign_up("admin@example.com")
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin@example.com'")
    body = {"name": "a", "scopes": ["channels:read"], "channels": ["other-memes"]}
    assert admin.post("/me/tokens", json=body).status_code == 201


def test_limited_token_mcp_hides_other_channels(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, channels=["funny-memes"]), PUBLIC_URL) as mcp:
        listed = tool(mcp, "list_channels", {})["structuredContent"]["items"]
        refused = tool(mcp, "get_channel", {"slug": "other-memes"})
    assert [item["slug"] for item in listed] == ["funny-memes"] and refused["isError"]


def test_tokens_page_limits_new_token_to_picked_channel(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    fields = {
        "name": "bot",
        "scopes": "channels:read",
        "expires_in_days": "30",
        "channels": "funny-memes",
    }
    page = submit(client, "/app/account/tokens", fields).text
    assert "Copy it now" in page and "· funny-memes" in page


def test_tokens_page_with_foreign_channel_shows_error(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    fields = {"name": "bot", "scopes": "channels:read", "expires_in_days": "30", "channels": "x"}
    response = submit(client, "/app/account/tokens", fields)
    assert response.status_code == 422 and 'id="channels-error"' in response.text


def test_old_token_rows_reach_all_channels(owner: TestClient, app: FastAPI) -> None:
    secret = new_token(owner)
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("UPDATE api_tokens SET channels = NULL")
    assert agent(app, secret).get("/channels/other-memes").status_code == 200


# Guarded guideline writes


def test_guideline_write_with_stale_revision_is_refused(owner: TestClient) -> None:
    owner.post("/channels/funny-memes/editorial", json=PURPOSE)
    response = owner.post(
        "/channels/funny-memes/editorial",
        json={**PURPOSE, "text": "Mine."},
        headers={"If-Match": '"0"'},
    )
    assert response.status_code == 412 and response.headers["etag"] == '"1"'


def test_guideline_write_with_current_revision_changes_it(owner: TestClient) -> None:
    owner.post("/channels/funny-memes/editorial", json=PURPOSE, headers={"If-Match": '"0"'})
    response = owner.post(
        "/channels/funny-memes/editorial",
        json={**PURPOSE, "text": "Mine."},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 201 and response.json()["revision"] == 2


def test_pillar_ignores_expected_revision(owner: TestClient) -> None:
    pillar = {"kind": "pillar", "text": "Office life."}
    response = owner.post(
        "/channels/funny-memes/editorial", json=pillar, headers={"If-Match": '"7"'}
    )
    assert response.status_code == 201


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_guideline_stale_revision_is_tool_error(mcp: TestClient) -> None:
    tool(mcp, "write_guideline", {"slug": "funny-memes", "guideline": PURPOSE})
    arguments = {"slug": "funny-memes", "guideline": PURPOSE, "expected_revision": 0}
    assert tool(mcp, "write_guideline", arguments)["isError"]


def create_episode(mcp: TestClient, **more: Any) -> dict[str, Any]:
    arguments = {"slug": "funny-memes", "episode": {"title": "Pilot"}, **more}
    result: dict[str, Any] = tool(mcp, "create_episode", arguments)
    return result


def test_mcp_create_episode_retried_with_key_makes_one(mcp: TestClient) -> None:
    first = create_episode(mcp, idempotency_key="pilot-1")["structuredContent"]
    again = create_episode(mcp, idempotency_key="pilot-1")["structuredContent"]
    listed = tool(mcp, "list_episodes", {"slug": "funny-memes"})["structuredContent"]["items"]
    assert first == again and len(listed) == 1


def test_mcp_create_episode_without_key_makes_another(mcp: TestClient) -> None:
    create_episode(mcp)
    assert create_episode(mcp)["structuredContent"]["episode"]["number"] == 2


def test_mcp_create_episode_key_reused_for_other_episode_is_error(mcp: TestClient) -> None:
    create_episode(mcp, idempotency_key="k")
    other = tool(
        mcp,
        "create_episode",
        {"slug": "funny-memes", "episode": {"title": "Other"}, "idempotency_key": "k"},
    )
    assert other["isError"] and "already used" in other["content"][0]["text"]


def test_mcp_invite_member_sends_email(mcp: TestClient, mailer: RecordingMailer) -> None:
    invite = {"email": "new@example.com", "role": "editor"}
    result = tool(mcp, "invite_member", {"slug": "funny-memes", "invite": invite})
    assert result["structuredContent"]["role"] == "editor"
    assert mailer.token_for("new@example.com")


def test_mcp_invite_member_without_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as mcp:
        invite = {"email": "new@example.com", "role": "viewer"}
        result = tool(mcp, "invite_member", {"slug": "funny-memes", "invite": invite})
    assert result["isError"] and "members:write" in result["content"][0]["text"]


# Short script language


def test_short_script_unwritten_speaks_channel_language(owner: TestClient) -> None:
    owner.post("/channels", json={"slug": "english", "name": "English", "language": "en"})
    owner.post("/channels/english/episodes", json={"title": "Pilot"})
    script = owner.get("/channels/english/episodes/1/short-script").json()
    assert script["voice"]["language"] == "en-US"


def test_short_script_written_without_language_speaks_channel_language(
    owner: TestClient,
) -> None:
    owner.post("/channels", json={"slug": "english", "name": "English", "language": "en-GB"})
    owner.post("/channels/english/episodes", json={"title": "Pilot"})
    written = owner.put("/channels/english/episodes/1/short-script", json={"duration_s": 20})
    assert written.json()["voice"]["language"] == "en-GB"


@pytest.mark.parametrize(
    ("channel", "speech"), [("pt-br", "pt-BR"), ("en", "en-US"), ("es-MX", "es"), ("fr", "pt-BR")]
)
def test_speech_language_for_channel_picks_closest(channel: str, speech: str) -> None:
    assert speech_language_for(channel) == speech
