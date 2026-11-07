"""MCP discovery, idempotency and renames (rules agentic.mcp.discovery, .idempotency, .renames)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.deps import RoleMissing, ScopeMissing
from sreditor.mcp_aliases import ALIAS_DAYS, ToolRename
from sreditor.mcp_idempotency import REPLAYED_META
from sreditor.mcp_server import INSTRUCTIONS, build, error_message
from sreditor.members import Permission, Role, minimum_role
from sreditor.tokens import Scope
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, mcp_call, new_token, tool
from tests.test_api import FUNNY_MEMES

SLUG = "funny-memes"
# A tool name starts with one of these verbs; other snake_case words (idempotency_key) are not tools.
TOOL_VERBS = (
    "add|apply|cancel|check|classify|compare|create|estimate|generate|get|import|invite|list|"
    "put|record|remove|request|retire|save|set|unlink|update|use|write"
)
TOOL_NAME = re.compile(rf"\b(?:{TOOL_VERBS})(?:_[a-z]+)+\b")
# Registered but deliberately left out of INSTRUCTIONS.
UNMENTIONED = {"set_episode_pitch"}  # the one-pitch form of set_episode_pitches
TODAY = date(2026, 11, 7)
RENAME = ToolRename(old_name="get_notes", new_name="get_research", renamed_on=TODAY)


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"/channels/{SLUG}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def registered(client: TestClient) -> dict[str, dict[str, Any]]:
    listed = mcp_call(client, "tools/list", {})["result"]["tools"]
    return {item["name"]: item for item in listed}


# Discovery: the instructions


def test_instructions_name_only_registered_tools(mcp: TestClient) -> None:
    named = set(TOOL_NAME.findall(INSTRUCTIONS))
    assert named - set(registered(mcp)) == set()


def test_instructions_mention_every_registered_tool_but_the_unmentioned(mcp: TestClient) -> None:
    named = set(TOOL_NAME.findall(INSTRUCTIONS))
    assert set(registered(mcp)) - named == UNMENTIONED


def test_instructions_unmentioned_tools_are_registered(mcp: TestClient) -> None:
    assert UNMENTIONED <= set(registered(mcp))


def test_instructions_never_mention_the_removed_api_upload() -> None:
    assert "upload_media" not in INSTRUCTIONS


def test_tool_descriptions_do_not_send_media_through_the_api(mcp: TestClient) -> None:
    assert "through the API" not in registered(mcp)["list_media"]["description"]


# Discovery: refusals that name the fix


def reconnect_message(scopes: list[str], owner: TestClient, app: FastAPI) -> str:
    with agent(app, new_token(owner, scopes), PUBLIC_URL) as reader:
        refused = tool(reader, "create_episode", {"slug": SLUG, "episode": {"title": "Two"}})
    assert refused["isError"]
    text: str = refused["content"][0]["text"]
    return text


def test_mcp_token_without_scope_names_the_scope_and_how_to_get_it(
    app: FastAPI, owner: TestClient
) -> None:
    message = reconnect_message(["channels:read"], owner, app)
    assert "lacks the content:write scope" in message and "Reconnect" in message


def test_mcp_create_channel_without_scope_names_the_scope(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        refused = tool(reader, "create_channel", {"channel": {**FUNNY_MEMES, "slug": "other"}})
    assert "channels:write scope" in refused["content"][0]["text"]


def test_mcp_viewer_role_names_the_role_needed_and_who_grants_it(
    app: FastAPI,
    owner: TestClient,
    mailer: RecordingMailer,
    sign_up: Callable[..., TestClient],
) -> None:
    viewer = sign_up("viewer@example.com", "Viewer")
    invite = {"email": "viewer@example.com", "role": "viewer"}
    assert owner.post(f"/channels/{SLUG}/invitations", json=invite).status_code == 201
    accepted = viewer.post("/invitations/accept", json={"token": mailer.token_for(invite["email"])})
    assert accepted.status_code == 200, accepted.text
    viewer_token = new_token(viewer)
    with agent(app, viewer_token, PUBLIC_URL) as client:
        refused = tool(client, "create_episode", {"slug": SLUG, "episode": {"title": "Two"}})
    message = refused["content"][0]["text"]
    assert f"Your role on channel {SLUG} is viewer" in message
    assert "contributor role" in message and "channel owner" in message


def test_minimum_role_of_owner_permission_is_owner() -> None:
    assert minimum_role(Permission.DELETE_CHANNEL) is Role.OWNER


def test_minimum_role_of_content_write_is_contributor() -> None:
    assert minimum_role(Permission.WRITE_CONTENT) is Role.CONTRIBUTOR


def test_error_message_for_missing_role_without_membership_says_none() -> None:
    error = RoleMissing(SLUG, None, Permission.READ)
    assert "is none" in error_message(error)


def test_error_message_for_missing_scope_keeps_the_api_detail_apart() -> None:
    error = ScopeMissing(Scope.CONTENT_WRITE)
    assert error.detail == "This token lacks the content:write scope"


def test_api_scope_refusal_keeps_its_detail(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    response = reader.post(f"/channels/{SLUG}/episodes", json={"title": "Two"})
    assert response.status_code == 403
    assert response.json()["detail"] == "This token lacks the content:write scope"


# Idempotency: every tool that creates, uploads or spends takes a key


def keyed_calls(mcp: TestClient) -> dict[str, dict[str, Any]]:
    """Arguments for each keyed tool that can be called on a fresh channel with one episode."""
    pitch = tool(mcp, "create_pitch", {"slug": SLUG, "pitch": {"title": "Seed"}})
    item = tool(
        mcp,
        "add_external_media",
        {
            "slug": SLUG,
            "item": {
                "kind": "video",
                "title": "Mars",
                "url": "https://svs.gsfc.nasa.gov/mars",
                "creator": "NASA",
                "rights": "public_domain",
            },
        },
    )
    source = {"url": "https://example.com/a", "title": "A", "claim": "A fact."}
    return {
        "create_channel": {"channel": {**FUNNY_MEMES, "slug": "other", "accounts": []}},
        "create_episode": {"slug": SLUG, "episode": {"title": "Second"}},
        "write_guideline": {
            "slug": SLUG,
            "guideline": {"kind": "pillar", "text": "Everyday life."},
        },
        "add_source": {"slug": SLUG, "number": 1, "source": source},
        "save_library_entry": {
            "slug": SLUG,
            "kind": "character",
            "entry": {"name": "Ana", "description": "Host."},
        },
        "invite_member": {"slug": SLUG, "invite": {"email": "new@example.com", "role": "viewer"}},
        "create_pitch": {"slug": SLUG, "pitch": {"title": "Mondays"}},
        "add_pitch_source": {
            "slug": SLUG,
            "pitch_id": pitch["structuredContent"]["id"],
            "source": source,
        },
        "add_external_media": {
            "slug": SLUG,
            "item": {
                "kind": "image",
                "title": "Photo",
                "url": "https://example.com/p",
                "creator": "Ana",
                "rights": "own",
            },
        },
        "use_external_media": {
            "slug": SLUG,
            "number": 1,
            "use": {
                "media_id": item["structuredContent"]["id"],
                "start_seconds": 1,
                "end_seconds": 4,
            },
        },
        "create_upload_link": {
            "slug": SLUG,
            "upload": {"number": 1, "kind": "voice_over", "name": "take"},
        },
    }


KEYED_TOOLS = [
    "create_channel",
    "create_episode",
    "write_guideline",
    "add_source",
    "save_library_entry",
    "invite_member",
    "create_pitch",
    "add_pitch_source",
    "add_external_media",
    "use_external_media",
    "create_upload_link",
]


@pytest.mark.parametrize("name", KEYED_TOOLS)
def test_mcp_keyed_tool_declares_an_optional_idempotency_key(mcp: TestClient, name: str) -> None:
    schema = registered(mcp)[name]["inputSchema"]
    assert "idempotency_key" in schema["properties"]
    assert "idempotency_key" not in schema.get("required", [])


@pytest.mark.parametrize("name", KEYED_TOOLS)
def test_mcp_keyed_tool_replay_returns_the_first_result_marked_replayed(
    mcp: TestClient, name: str
) -> None:
    arguments = {**keyed_calls(mcp)[name], "idempotency_key": f"{name}-1"}
    first = tool(mcp, name, arguments)
    again = tool(mcp, name, arguments)
    assert not first.get("isError"), first
    assert first["_meta"] == {REPLAYED_META: False}
    assert again["_meta"] == {REPLAYED_META: True}
    assert again["structuredContent"] == first["structuredContent"]


@pytest.mark.parametrize("name", KEYED_TOOLS)
def test_mcp_keyed_tool_without_key_carries_no_replay_mark(mcp: TestClient, name: str) -> None:
    result = tool(mcp, name, keyed_calls(mcp)[name])
    assert not result.get("isError"), result
    assert not result.get("_meta")


def test_mcp_replayed_create_episode_makes_one_episode(mcp: TestClient) -> None:
    arguments = {**keyed_calls(mcp)["create_episode"], "idempotency_key": "k"}
    tool(mcp, "create_episode", arguments)
    tool(mcp, "create_episode", arguments)
    listed = tool(mcp, "list_episodes", {"slug": SLUG})["structuredContent"]["items"]
    assert len(listed) == 2


def test_mcp_invite_member_replay_sends_one_email(mcp: TestClient, mailer: RecordingMailer) -> None:
    arguments = {**keyed_calls(mcp)["invite_member"], "idempotency_key": "invite-1"}
    tool(mcp, "invite_member", arguments)
    sent = len(mailer.sent)
    tool(mcp, "invite_member", arguments)
    assert len(mailer.sent) == sent


def test_mcp_key_reused_with_other_arguments_is_a_tool_error(mcp: TestClient) -> None:
    calls = keyed_calls(mcp)
    tool(mcp, "create_pitch", {**calls["create_pitch"], "idempotency_key": "k"})
    other = {"slug": SLUG, "pitch": {"title": "Other"}, "idempotency_key": "k"}
    refused = tool(mcp, "create_pitch", other)
    assert refused["isError"] and "already used" in refused["content"][0]["text"]


def test_mcp_key_reused_by_another_tool_is_a_tool_error(mcp: TestClient) -> None:
    calls = keyed_calls(mcp)
    tool(mcp, "create_pitch", {**calls["create_pitch"], "idempotency_key": "k"})
    refused = tool(mcp, "create_episode", {**calls["create_episode"], "idempotency_key": "k"})
    assert refused["isError"]


def test_mcp_keys_are_not_shared_between_users(
    app: FastAPI, owner: TestClient, mcp: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    other_user = sign_up("second@example.com", "Second")
    assert (
        other_user.post(
            "/channels", json={**FUNNY_MEMES, "slug": "theirs", "accounts": []}
        ).status_code
        == 201
    )
    arguments = {"episode": {"title": "Same"}, "idempotency_key": "shared"}
    mine = tool(mcp, "create_episode", {"slug": SLUG, **arguments})
    # The app's lifespan already runs for `mcp`, so this client must not enter it again.
    theirs_client = agent(app, new_token(other_user), PUBLIC_URL)
    theirs = tool(theirs_client, "create_episode", {"slug": "theirs", **arguments})
    assert mine["_meta"] == {REPLAYED_META: False}
    assert theirs["_meta"] == {REPLAYED_META: False}


def test_mcp_failed_keyed_call_releases_the_key(mcp: TestClient) -> None:
    arguments = {"slug": "nope", "episode": {"title": "Pilot"}, "idempotency_key": "k"}
    assert tool(mcp, "create_episode", arguments)["isError"]
    fixed = tool(mcp, "create_episode", {**arguments, "slug": SLUG})
    assert not fixed.get("isError")


def test_mcp_request_render_description_says_it_is_idempotent(mcp: TestClient) -> None:
    description = registered(mcp)["request_render"]["description"]
    assert "idempotency_key" not in registered(mcp)["request_render"]["inputSchema"]["properties"]
    assert "idempotent" in description


def test_mcp_every_tool_that_creates_takes_a_key_or_is_listed(mcp: TestClient) -> None:
    creating = {
        name
        for name, item in registered(mcp).items()
        if item["annotations"]["idempotentHint"] is False
    }
    keyed = {
        name
        for name, item in registered(mcp).items()
        if "idempotency_key" in item["inputSchema"]["properties"]
    }
    assert creating - keyed == {"get_file_link"}  # a link issued again is a fresh, usable link


# Renames: aliases


def build_names(app: FastAPI, today: date, renames: tuple[ToolRename, ...] = (RENAME,)) -> set[str]:
    server = build(app.state.services, renames, lambda: today)
    return set(server.added)


def test_build_registers_the_old_name_while_the_alias_is_active(app: FastAPI) -> None:
    assert "get_notes" in build_names(app, TODAY + timedelta(days=ALIAS_DAYS))


def test_build_drops_the_old_name_after_the_end_date(app: FastAPI) -> None:
    assert "get_notes" not in build_names(app, TODAY + timedelta(days=ALIAS_DAYS + 1))


def test_build_without_renames_registers_no_alias(app: FastAPI) -> None:
    server = build(app.state.services)
    assert server.alias_names == []


def test_alias_ends_ninety_days_after_the_rename() -> None:
    assert RENAME.ends_on == date(2027, 2, 5)


def test_alias_description_names_the_new_tool_and_the_end_date() -> None:
    description = RENAME.description()
    assert "get_research" in description and "2027-02-05" in description


def test_alias_answers_with_the_same_arguments_as_the_new_tool(
    app: FastAPI, owner: TestClient
) -> None:
    server = build(app.state.services, (RENAME,), lambda: TODAY)
    assert server.alias_names == ["get_notes"]
    # The alias shares the function, so its schema and result are the new tool's.
    assert server.added["get_notes"].function is server.added["get_research"].function


def test_instructions_never_mention_an_alias() -> None:
    assert RENAME.old_name not in INSTRUCTIONS


def test_alias_listing_shows_the_alias_description_and_title(app: FastAPI) -> None:
    server = build(app.state.services, (RENAME,), lambda: TODAY)
    listed = {item.name: item for item in asyncio.run(server.list_tools())}
    alias = listed["get_notes"]
    assert alias.description == RENAME.description()
    assert alias.input_schema == listed["get_research"].input_schema
    assert alias.annotations is not None and alias.annotations.title.endswith("(old name)")
