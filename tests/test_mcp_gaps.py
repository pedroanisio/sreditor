"""MCP tools that close the parity gaps with the API: accounts, status, profile, snapshots,
deletes, members and whoami (card SRED-15)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, new_token, operation_specs, tool
from tests.test_api import FUNNY_MEMES
from tests.test_classification_api import present
from tests.test_episodes import joined

SLUG = "funny-memes"
CHANNEL = f"/channels/{SLUG}"
MISSING_ID = "00000000-0000-0000-0000-000000000000"
NASA = {
    "kind": "video",
    "title": "Mars landing animation",
    "url": "https://svs.gsfc.nasa.gov/mars",
    "creator": "NASA",
    "rights": "public_domain",
}
CUTS = {"cuts": [{"name": "main"}], "posts": [{"platform": "tiktok", "cut": "main"}]}
READ_ONLY = ["channels:read"]


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


@pytest.fixture
def reader(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner, READ_ONLY), PUBLIC_URL) as client:
        yield client


def failure(result: dict[str, Any]) -> str:
    """The text of a tool error; fails the test when the call did not fail."""
    assert result["isError"], result
    text: str = result["content"][0]["text"]
    return text


def content(result: dict[str, Any]) -> Any:
    assert not result.get("isError"), result
    return result["structuredContent"]


# remove_account


def test_mcp_remove_account_takes_the_account_off_the_channel(mcp: TestClient) -> None:
    view = content(tool(mcp, "remove_account", {"slug": SLUG, "platform": "tiktok"}))
    assert view["channel"]["accounts"] == [] and view["revision"] == 2


def test_mcp_remove_account_with_stale_revision_names_the_current_one(mcp: TestClient) -> None:
    tool(mcp, "update_channel", {"slug": SLUG, "name": "One"})
    done = tool(mcp, "remove_account", {"slug": SLUG, "platform": "tiktok", "expected_revision": 1})
    assert "revision 2" in failure(done)


def test_mcp_remove_account_missing_platform_points_at_get_channel(mcp: TestClient) -> None:
    text = failure(tool(mcp, "remove_account", {"slug": SLUG, "platform": "youtube"}))
    assert "no youtube account" in text and "get_channel" in text


def test_mcp_remove_account_without_write_scope_names_the_scope(reader: TestClient) -> None:
    text = failure(tool(reader, "remove_account", {"slug": SLUG, "platform": "tiktok"}))
    assert "channels:write scope" in text


def test_mcp_remove_account_as_viewer_names_the_role_needed(
    app: FastAPI, owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    with agent(app, new_token(viewer), PUBLIC_URL) as client:
        text = failure(tool(client, "remove_account", {"slug": SLUG, "platform": "tiktok"}))
    assert "role" in text and "Ask a channel owner" in text


# set_channel_status


def test_mcp_set_channel_status_archives_the_channel(mcp: TestClient) -> None:
    view = content(tool(mcp, "set_channel_status", {"slug": SLUG, "status": "archived"}))
    assert view["channel"]["status"] == "archived" and view["channel"]["name"] == "Funny Memes"


def test_mcp_set_channel_status_unknown_value_is_a_tool_error(mcp: TestClient) -> None:
    assert tool(mcp, "set_channel_status", {"slug": SLUG, "status": "gone"})["isError"]


def test_mcp_set_channel_status_without_write_scope_names_the_scope(reader: TestClient) -> None:
    text = failure(tool(reader, "set_channel_status", {"slug": SLUG, "status": "paused"}))
    assert "channels:write scope" in text


def test_mcp_set_channel_status_unknown_channel_points_at_list_channels(mcp: TestClient) -> None:
    text = failure(tool(mcp, "set_channel_status", {"slug": "nope", "status": "paused"}))
    assert "list_channels" in text


# declare_channel_profile


def test_mcp_declare_channel_profile_is_read_back_by_get_channel_profile(mcp: TestClient) -> None:
    profile = {"dimensions": {"tones": present("ironic")}}
    declared = content(tool(mcp, "declare_channel_profile", {"slug": SLUG, "profile": profile}))
    seen = content(tool(mcp, "get_channel_profile", {"slug": SLUG}))["declared"]
    assert declared["revision"] == 1 and seen["classification"]["dimensions"]["tones"]["items"]


def test_mcp_declare_channel_profile_comedy_without_device_names_the_fix(mcp: TestClient) -> None:
    profile = {"dimensions": {"genres": present("comedy")}}
    text = failure(tool(mcp, "declare_channel_profile", {"slug": SLUG, "profile": profile}))
    assert "humor" in text and "declare_channel_profile" in text


def test_mcp_declare_channel_profile_with_stale_revision_names_the_current_one(
    mcp: TestClient,
) -> None:
    profile = {"dimensions": {"tones": present("ironic")}}
    tool(mcp, "declare_channel_profile", {"slug": SLUG, "profile": profile})
    arguments = {"slug": SLUG, "profile": profile, "expected_revision": 0}
    assert "revision 1" in failure(tool(mcp, "declare_channel_profile", arguments))


def test_mcp_declare_channel_profile_without_editorial_scope_names_the_scope(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read", "content:write"]), PUBLIC_URL) as client:
        text = failure(tool(client, "declare_channel_profile", {"slug": SLUG, "profile": {}}))
    assert "editorial:write scope" in text


# list_post_snapshots


@pytest.fixture
def posted(owner: TestClient) -> TestClient:
    assert owner.put(f"{CHANNEL}/episodes/1/delivery", json=CUTS).status_code == 200
    return owner


def record(mcp: TestClient, day: str, views: int) -> None:
    snapshot = {"platform": "tiktok", "captured_on": day, "views": views}
    done = tool(mcp, "record_post_metrics", {"slug": SLUG, "number": 1, "snapshot": snapshot})
    assert not done.get("isError"), done


def test_mcp_list_post_snapshots_returns_every_day_newest_first(
    mcp: TestClient, posted: TestClient
) -> None:
    record(mcp, "2026-11-01", 100)
    record(mcp, "2026-11-02", 250)
    arguments = {"slug": SLUG, "post": {"number": 1, "platform": "tiktok"}}
    found = content(tool(mcp, "list_post_snapshots", arguments))
    assert [item["views"] for item in found["items"]] == [250, 100]


def test_mcp_list_post_snapshots_pages_with_a_cursor(mcp: TestClient, posted: TestClient) -> None:
    record(mcp, "2026-11-01", 100)
    record(mcp, "2026-11-02", 250)
    arguments = {"slug": SLUG, "post": {"number": 1, "platform": "tiktok"}, "limit": 1}
    first = content(tool(mcp, "list_post_snapshots", arguments))
    second = content(
        tool(mcp, "list_post_snapshots", {**arguments, "cursor": first["next_cursor"]})
    )
    assert [item["views"] for item in second["items"]] == [100] and second["next_cursor"] is None


def test_mcp_list_post_snapshots_without_post_points_at_record_post(
    mcp: TestClient, posted: TestClient
) -> None:
    arguments = {"slug": SLUG, "post": {"number": 1, "platform": "youtube"}}
    assert "record_post" in failure(tool(mcp, "list_post_snapshots", arguments))


def test_mcp_list_post_snapshots_unknown_episode_points_at_list_episodes(mcp: TestClient) -> None:
    arguments = {"slug": SLUG, "post": {"number": 9, "platform": "tiktok"}}
    assert "list_episodes" in failure(tool(mcp, "list_post_snapshots", arguments))


def test_mcp_list_post_snapshots_without_read_scope_names_the_scope(
    app: FastAPI, owner: TestClient, posted: TestClient
) -> None:
    with agent(app, new_token(owner, ["content:write"]), PUBLIC_URL) as client:
        arguments = {"slug": SLUG, "post": {"number": 1, "platform": "tiktok"}}
        assert "channels:read scope" in failure(tool(client, "list_post_snapshots", arguments))


# delete_pitch


def make_pitch(mcp: TestClient) -> str:
    pitch = content(tool(mcp, "create_pitch", {"slug": SLUG, "pitch": {"title": "Mondays"}}))
    found: str = pitch["id"]
    return found


def test_mcp_delete_pitch_removes_it_from_the_list(mcp: TestClient) -> None:
    pitch_id = make_pitch(mcp)
    removed = content(tool(mcp, "delete_pitch", {"slug": SLUG, "pitch_id": pitch_id}))
    listed = content(tool(mcp, "list_pitches", {"slug": SLUG}))["items"]
    assert removed == {"kind": "pitch", "id": pitch_id} and listed == []


def test_mcp_delete_pitch_used_by_an_episode_names_the_calls_that_free_it(mcp: TestClient) -> None:
    pitch_id = make_pitch(mcp)
    tool(mcp, "set_episode_pitch", {"slug": SLUG, "number": 1, "pitch_id": pitch_id})
    text = failure(tool(mcp, "delete_pitch", {"slug": SLUG, "pitch_id": pitch_id}))
    assert "episodes 1" in text and "get_pitch" in text and "set_episode_pitches" in text


def test_mcp_delete_pitch_after_dropping_it_from_episodes_succeeds(mcp: TestClient) -> None:
    pitch_id = make_pitch(mcp)
    tool(mcp, "set_episode_pitch", {"slug": SLUG, "number": 1, "pitch_id": pitch_id})
    tool(mcp, "set_episode_pitches", {"slug": SLUG, "number": 1, "pitch_ids": []})
    assert not tool(mcp, "delete_pitch", {"slug": SLUG, "pitch_id": pitch_id}).get("isError")


def test_mcp_delete_unknown_pitch_points_at_list_pitches(mcp: TestClient) -> None:
    text = failure(tool(mcp, "delete_pitch", {"slug": SLUG, "pitch_id": MISSING_ID}))
    assert "list_pitches" in text


def test_mcp_delete_pitch_without_write_scope_names_the_scope(
    reader: TestClient, owner: TestClient
) -> None:
    pitch = owner.post(f"{CHANNEL}/pitches", json={"title": "Mondays"}).json()
    text = failure(tool(reader, "delete_pitch", {"slug": SLUG, "pitch_id": pitch["id"]}))
    assert "channels:write scope" in text


# delete_external_media and list_external_media_episodes


def make_item(mcp: TestClient) -> str:
    item = content(tool(mcp, "add_external_media", {"slug": SLUG, "item": NASA}))
    found: str = item["id"]
    return found


def test_mcp_delete_external_media_removes_it_from_the_library(mcp: TestClient) -> None:
    media_id = make_item(mcp)
    removed = content(tool(mcp, "delete_external_media", {"slug": SLUG, "media_id": media_id}))
    listed = content(tool(mcp, "list_external_media", {"slug": SLUG}))["items"]
    assert removed == {"kind": "external_media", "id": media_id} and listed == []


def test_mcp_delete_external_media_in_use_names_the_calls_that_free_it(mcp: TestClient) -> None:
    media_id = make_item(mcp)
    use = {"slug": SLUG, "number": 1, "use": {"media_id": media_id}}
    tool(mcp, "use_external_media", use)
    text = failure(tool(mcp, "delete_external_media", {"slug": SLUG, "media_id": media_id}))
    assert "episodes 1" in text
    assert "list_external_media_episodes" in text and "remove_external_media_use" in text


def test_mcp_delete_unknown_external_media_points_at_list_external_media(mcp: TestClient) -> None:
    text = failure(tool(mcp, "delete_external_media", {"slug": SLUG, "media_id": MISSING_ID}))
    assert "list_external_media" in text


def test_mcp_delete_external_media_without_write_scope_names_the_scope(
    reader: TestClient, owner: TestClient
) -> None:
    item = owner.post(f"{CHANNEL}/external-media", json=NASA).json()
    text = failure(tool(reader, "delete_external_media", {"slug": SLUG, "media_id": item["id"]}))
    assert "channels:write scope" in text


def test_mcp_list_external_media_episodes_returns_the_episodes_that_use_it(
    mcp: TestClient, owner: TestClient
) -> None:
    owner.post(f"{CHANNEL}/episodes", json={"title": "Second"})
    media_id = make_item(mcp)
    for number in (2, 1):
        use = {"slug": SLUG, "number": number, "use": {"media_id": media_id}}
        tool(mcp, "use_external_media", use)
    found = content(tool(mcp, "list_external_media_episodes", {"slug": SLUG, "media_id": media_id}))
    assert found["items"] == [1, 2] and found["next_cursor"] is None


def test_mcp_list_external_media_episodes_pages_with_a_cursor(
    mcp: TestClient, owner: TestClient
) -> None:
    owner.post(f"{CHANNEL}/episodes", json={"title": "Second"})
    media_id = make_item(mcp)
    for number in (1, 2):
        tool(
            mcp,
            "use_external_media",
            {"slug": SLUG, "number": number, "use": {"media_id": media_id}},
        )
    arguments = {"slug": SLUG, "media_id": media_id, "limit": 1}
    first = content(tool(mcp, "list_external_media_episodes", arguments))
    second = content(
        tool(mcp, "list_external_media_episodes", {**arguments, "cursor": first["next_cursor"]})
    )
    assert (first["items"], second["items"]) == ([1], [2])


def test_mcp_list_external_media_episodes_unknown_item_points_at_list_external_media(
    mcp: TestClient,
) -> None:
    arguments = {"slug": SLUG, "media_id": MISSING_ID}
    assert "list_external_media" in failure(tool(mcp, "list_external_media_episodes", arguments))


# change_role and remove_member


def member_id(owner: TestClient, email: str) -> str:
    members = owner.get(f"{CHANNEL}/members").json()["items"]
    return str(next(item["user_id"] for item in members if item["email"] == email))


@pytest.fixture
def invited(owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer) -> str:
    """The user id of a contributor invited by mistake."""
    joined(owner, sign_up, mailer, "contributor")
    return member_id(owner, "contributor@example.com")


def role_of(owner: TestClient, user_id: str) -> str:
    member = owner.get(f"{CHANNEL}/members/{user_id}").json()
    return str(member["role"])


def test_mcp_change_role_gives_the_member_the_new_role(
    mcp: TestClient, invited: str, owner: TestClient
) -> None:
    done = content(tool(mcp, "change_role", {"slug": SLUG, "user_id": invited, "role": "viewer"}))
    assert done == {"channel": SLUG, "user_id": invited, "role": "viewer"}
    assert role_of(owner, invited) == "viewer"


def test_mcp_change_role_with_stale_expected_role_names_the_current_one(
    mcp: TestClient, invited: str
) -> None:
    arguments = {"slug": SLUG, "user_id": invited, "role": "viewer", "expected_role": "editor"}
    text = failure(tool(mcp, "change_role", arguments))
    assert "now contributor" in text and "list_members" in text


def test_mcp_change_role_of_unknown_user_points_at_list_members(mcp: TestClient) -> None:
    text = failure(
        tool(mcp, "change_role", {"slug": SLUG, "user_id": MISSING_ID, "role": "viewer"})
    )
    assert "list_members" in text


def test_mcp_change_role_of_last_owner_names_the_fix(mcp: TestClient, owner: TestClient) -> None:
    owner_id = member_id(owner, "owner@example.com")
    text = failure(tool(mcp, "change_role", {"slug": SLUG, "user_id": owner_id, "role": "viewer"}))
    assert "at least one owner" in text and "change_role" in text


def test_mcp_change_role_without_members_scope_names_the_scope(
    app: FastAPI, owner: TestClient, invited: str
) -> None:
    with agent(app, new_token(owner, ["channels:read", "content:write"]), PUBLIC_URL) as client:
        text = failure(
            tool(client, "change_role", {"slug": SLUG, "user_id": invited, "role": "viewer"})
        )
    assert "members:write scope" in text


def test_mcp_change_role_by_editor_to_editor_names_the_role_needed(
    app: FastAPI,
    owner: TestClient,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
    invited: str,
) -> None:
    editor = joined(owner, sign_up, mailer, "editor")
    with agent(app, new_token(editor), PUBLIC_URL) as client:
        arguments = {"slug": SLUG, "user_id": invited, "role": "editor"}
        text = failure(tool(client, "change_role", arguments))
    assert "owner role" in text and "Ask a channel owner" in text


def test_mcp_remove_member_takes_the_member_off_the_channel(
    mcp: TestClient, invited: str, owner: TestClient
) -> None:
    removed = content(tool(mcp, "remove_member", {"slug": SLUG, "user_id": invited}))
    assert removed == {"kind": "member", "id": invited}
    assert owner.get(f"{CHANNEL}/members/{invited}").status_code == 404


def test_mcp_remove_member_of_last_owner_names_the_fix(mcp: TestClient, owner: TestClient) -> None:
    owner_id = member_id(owner, "owner@example.com")
    text = failure(tool(mcp, "remove_member", {"slug": SLUG, "user_id": owner_id}))
    assert "at least one owner" in text and "change_role" in text


def test_mcp_remove_unknown_member_points_at_list_members(mcp: TestClient) -> None:
    text = failure(tool(mcp, "remove_member", {"slug": SLUG, "user_id": MISSING_ID}))
    assert "list_members" in text


def test_mcp_remove_member_without_members_scope_names_the_scope(
    app: FastAPI, owner: TestClient, invited: str
) -> None:
    with agent(app, new_token(owner, ["channels:read", "content:write"]), PUBLIC_URL) as client:
        text = failure(tool(client, "remove_member", {"slug": SLUG, "user_id": invited}))
    assert "members:write scope" in text


def test_mcp_remove_member_leaving_needs_the_members_scope_too(
    app: FastAPI, owner: TestClient
) -> None:
    owner_id = member_id(owner, "owner@example.com")
    with agent(app, new_token(owner, READ_ONLY), PUBLIC_URL) as client:
        text = failure(tool(client, "remove_member", {"slug": SLUG, "user_id": owner_id}))
    assert "members:write scope" in text


def test_mcp_remove_member_with_stale_expected_role_keeps_the_member(
    mcp: TestClient, invited: str, owner: TestClient
) -> None:
    arguments = {"slug": SLUG, "user_id": invited, "expected_role": "editor"}
    assert tool(mcp, "remove_member", arguments)["isError"]
    assert role_of(owner, invited) == "contributor"


# whoami


def test_mcp_whoami_names_the_user_the_token_and_the_role_per_channel(
    mcp: TestClient, owner: TestClient
) -> None:
    me = content(tool(mcp, "whoami", {}))
    assert (me["user_id"], me["display_name"], me["email"]) == (
        owner.user_id,  # type: ignore[attr-defined]
        "Owner",
        "owner@example.com",
    )
    assert me["channels"]["items"] == [{"slug": SLUG, "name": "Funny Memes", "role": "owner"}]


def test_mcp_whoami_describes_the_token_without_its_secret(app: FastAPI, owner: TestClient) -> None:
    secret = new_token(owner, ["channels:read", "content:write"], channels=[SLUG])
    with agent(app, secret, PUBLIC_URL) as client:
        result = tool(client, "whoami", {})
    token = content(result)["token"]
    assert sorted(token["scopes"]) == ["channels:read", "content:write"]
    assert token["channels"] == [SLUG] and token["expires_at"] and secret not in str(result)


def test_mcp_whoami_lists_only_the_channels_a_limited_token_reaches(
    app: FastAPI, owner: TestClient
) -> None:
    assert (
        owner.post("/channels", json={**FUNNY_MEMES, "slug": "other", "accounts": []}).status_code
        == 201
    )
    with agent(app, new_token(owner, READ_ONLY, channels=["other"]), PUBLIC_URL) as client:
        me = content(tool(client, "whoami", {}))
    assert [item["slug"] for item in me["channels"]["items"]] == ["other"]


def test_mcp_whoami_pages_the_channels_with_a_cursor(mcp: TestClient, owner: TestClient) -> None:
    assert (
        owner.post("/channels", json={**FUNNY_MEMES, "slug": "zeta", "accounts": []}).status_code
        == 201
    )
    first = content(tool(mcp, "whoami", {"limit": 1}))["channels"]
    second = content(tool(mcp, "whoami", {"limit": 1, "cursor": first["next_cursor"]}))["channels"]
    assert [item["slug"] for item in first["items"] + second["items"]] == [SLUG, "zeta"]


def test_mcp_whoami_without_channels_scope_names_the_scope(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner, ["content:write"]), PUBLIC_URL) as client:
        assert "channels:read scope" in failure(tool(client, "whoami", {}))


def test_mcp_new_tools_declare_every_annotation(app: FastAPI) -> None:
    listed = operation_specs(app)
    removals = ("remove_account", "delete_pitch", "delete_external_media", "remove_member")
    assert all(listed[name]["annotations"]["destructiveHint"] for name in removals)
    assert listed["whoami"]["annotations"]["readOnlyHint"] is True
