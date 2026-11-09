"""Agent-ready API: tokens, problems, pagination, idempotency, ETags, the stmt gate and MCP."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter

from sreditor.api import create_app
from sreditor.database import Database, utc_now
from sreditor.idempotency import Idempotency
from sreditor.idempotency_store import IdempotencyStore, KeyInProgress, StoredResponse
from sreditor.mcp_catalog import OPERATION_TOOL
from sreditor.mcp_groups import result_type
from sreditor.mcp_server import Effect, record_operations, tool_annotations
from sreditor.pagination import MAX_LIMIT, decode_cursor, encode_cursor
from sreditor.problems import ProblemError
from sreditor.security import RateLimiter
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer, fast_passwords
from tests.test_api import FUNNY_MEMES

ALL_SCOPES = [
    "channels:read",
    "channels:write",
    "channels:delete",
    "editorial:write",
    "members:write",
    "content:write",
]
PURPOSE = {"kind": "purpose", "text": "Make people laugh at everyday life."}
PUBLIC_URL = "https://sreditor.test"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


def new_token(client: TestClient, scopes: list[str] | None = None, **extra: Any) -> str:
    body = {"name": "agent", "scopes": scopes or ALL_SCOPES, **extra}
    response = client.post("/me/tokens", json=body)
    assert response.status_code == 201, response.text
    secret: str = response.json()["secret"]
    return secret


def agent(app: FastAPI, secret: str, base_url: str = "https://testserver") -> TestClient:
    return TestClient(app, base_url=base_url, headers={"Authorization": f"Bearer {secret}"})


def stmt(settings: AppSettings, scope: str, *args: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "STMT_DB": settings.store.db,
        "STMT_DOMAIN": settings.store.domain,
        "STMT_SCOPE": scope,
    }
    done = subprocess.run(
        [settings.store.stmt, *args], check=False, env=env, capture_output=True, text=True
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def block(settings: AppSettings, scope: str, operation: str) -> None:
    document = {
        "statement": f"Agents may not {operation} here.",
        "owner": {"type": "team", "id": "editors"},
        "category": "agents",
        "priority": 4,
        "conditionals": ["has(ctx.token)"],
        "rule": {
            "effect": "BLOCK",
            "trigger": {"kind": "EVENT", "phase": "BEFORE", "operation": operation},
        },
    }
    created = stmt(settings, scope, "create", "agents.block", "--json", json.dumps(document))
    assert created["ok"], created


# Tokens


def test_token_create_returns_secret_once(owner: TestClient) -> None:
    created = owner.post("/me/tokens", json={"name": "bot", "scopes": ["channels:read"]}).json()
    assert created["secret"].startswith("sre_")
    listed = owner.get("/me/tokens").json()
    assert [token["id"] for token in listed["items"]] == [created["token"]["id"]]
    assert "secret" not in listed["items"][0]


def test_token_authenticates_agent_reads(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert reader.get("/channels/funny-memes").status_code == 200


def test_token_without_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    response = reader.post("/channels/funny-memes/editorial", json=PURPOSE)
    assert response.status_code == 403
    assert response.json()["detail"] == "This token lacks the editorial:write scope"


def test_token_never_exceeds_user_role(
    app: FastAPI, owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = sign_up("viewer@example.com")
    invite = {"email": "viewer@example.com", "role": "viewer"}
    owner.post("/channels/funny-memes/invitations", json=invite)
    token = mailer.token_for("viewer@example.com")
    assert viewer.post("/invitations/accept", json={"token": token}).status_code == 200
    bot = agent(app, new_token(viewer))
    response = bot.post("/channels/funny-memes/editorial", json=PURPOSE)
    assert response.status_code == 403
    assert response.json()["detail"].startswith("Your role cannot")


def test_token_create_without_scopes_is_rejected(owner: TestClient) -> None:
    response = owner.post("/me/tokens", json={"name": "bot", "scopes": []})
    assert response.status_code == 422


def test_token_cannot_manage_tokens(app: FastAPI, owner: TestClient) -> None:
    bot = agent(app, new_token(owner))
    response = bot.post("/me/tokens", json={"name": "child", "scopes": ["channels:read"]})
    assert response.status_code == 403
    assert response.json()["detail"] == "This needs a signed-in session"


def test_token_cannot_change_profile(app: FastAPI, owner: TestClient) -> None:
    bot = agent(app, new_token(owner))
    assert bot.patch("/me", json={"display_name": "Bot"}).status_code == 403


def test_token_revoked_stops_working(app: FastAPI, owner: TestClient) -> None:
    secret = new_token(owner)
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    assert owner.delete(f"/me/tokens/{token_id}").status_code == 204
    assert agent(app, secret).get("/channels").status_code == 401


def test_token_revoke_unknown_returns_404(owner: TestClient) -> None:
    response = owner.delete("/me/tokens/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_token_unknown_secret_returns_401(app: FastAPI) -> None:
    response = agent(app, "sre_not-a-real-token").get("/channels")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_expired_returns_401(
    app_settings: AppSettings, owner: TestClient, mailer: RecordingMailer
) -> None:
    secret = new_token(owner, expires_in_days=1)
    later = create_app(
        app_settings,
        mailer=mailer,
        passwords=fast_passwords(),
        clock=lambda: utc_now() + timedelta(days=2),
    )
    assert agent(later, secret).get("/channels").status_code == 401


def test_token_of_disabled_user_returns_401(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    secret = new_token(owner)
    database = Database(app_settings.channels_db, utc_now)
    with database.connect() as connection:
        connection.execute("UPDATE users SET status = 'disabled'")
    assert agent(app, secret).get("/channels").status_code == 401


def test_token_list_paginates_newest_first(owner: TestClient) -> None:
    for name in ("one", "two", "three"):
        owner.post("/me/tokens", json={"name": name, "scopes": ["channels:read"]})
    first = owner.get("/me/tokens", params={"limit": 2}).json()
    second = owner.get("/me/tokens", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    names = [token["name"] for token in first["items"] + second["items"]]
    assert sorted(names) == ["one", "three", "two"] and second["next_cursor"] is None


# Audit and the stmt gate


def test_gate_allows_agent_write_and_records_token_as_author(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    bot = agent(app, new_token(owner))
    response = bot.post("/channels/funny-memes/editorial", json=PURPOSE)
    assert response.status_code == 201, response.text
    statement = stmt(app_settings, "/funny-memes", "get", response.json()["id"])
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    assert statement["result"]["owner"] == {"type": "token", "id": token_id}
    events = stmt(app_settings, "/funny-memes", "events", "--json", "{}")
    journal = [
        (event["phase"], event["decision"], event["root"]) for event in events["result"]["events"]
    ]
    gate_id = journal[0][2]
    assert journal == [("BEFORE", "ALLOW", gate_id), ("AFTER", "ACCEPT", gate_id)]


def test_gate_block_refuses_agent_write(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/funny-memes", "editorial.write")
    bot = agent(app, new_token(owner))
    response = bot.post("/channels/funny-memes/editorial", json=PURPOSE)
    assert response.status_code == 403
    problem = response.json()
    assert problem["type"] == "urn:sreditor:problem:gate-refused"
    assert problem["decision"] == "BLOCK"


def test_gate_does_not_apply_to_session_writes(
    app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/funny-memes", "editorial.write")
    assert owner.post("/channels/funny-memes/editorial", json=PURPOSE).status_code == 201


def test_gate_block_refuses_agent_channel_create(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/", "channel.create")
    bot = agent(app, new_token(owner))
    response = bot.post("/channels", json={**FUNNY_MEMES, "slug": "other", "accounts": []})
    assert response.status_code == 403


def test_agent_creates_channel_with_write_scope(app: FastAPI, owner: TestClient) -> None:
    bot = agent(app, new_token(owner, ["channels:write"]))
    assert (
        bot.post("/channels", json={**FUNNY_MEMES, "slug": "other", "accounts": []}).status_code
        == 201
    )


def test_agent_channel_create_without_scope_is_refused(app: FastAPI, owner: TestClient) -> None:
    bot = agent(app, new_token(owner, ["channels:read"]))
    assert (
        bot.post("/channels", json={**FUNNY_MEMES, "slug": "other", "accounts": []}).status_code
        == 403
    )


@pytest.fixture
def bot(app: FastAPI, owner: TestClient) -> TestClient:
    return agent(app, new_token(owner))


def test_agent_with_all_scopes_puts_account_returns_200(bot: TestClient) -> None:
    account = {"platform": "youtube", "handle": "@funnymemes"}
    assert bot.put("/channels/funny-memes/accounts/youtube", json=account).status_code == 200


def test_agent_with_all_scopes_deletes_missing_guideline_returns_404(bot: TestClient) -> None:
    assert bot.delete("/channels/funny-memes/editorial/missing").status_code == 404


def test_agent_with_all_scopes_deletes_channel_returns_204(bot: TestClient) -> None:
    assert bot.delete("/channels/funny-memes").status_code == 204


# Problem details


def test_problem_has_rfc9457_members(owner: TestClient) -> None:
    response = owner.get("/channels/nope")
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Channel nope not found",
        "instance": "/channels/nope",
    }


def test_problem_validation_lists_each_field(owner: TestClient) -> None:
    response = owner.post("/channels", json={"slug": "Bad Slug"})
    problem = response.json()
    assert response.status_code == 422 and problem["detail"] == "Request validation failed"
    assert {tuple(error["location"]) for error in problem["errors"]} >= {("body", "name")}


def test_problem_rate_limit_carries_retry_after(app: FastAPI) -> None:
    client = TestClient(app)
    body = {"email": "x@example.com", "password": "wrong"}
    responses = [client.post("/auth/login", json=body) for _ in range(11)]
    assert responses[-1].status_code == 429
    assert responses[-1].headers["retry-after"] == "60"


def test_problem_error_keeps_extensions() -> None:
    error = ProblemError(409, "busy", "busy", headers={"Retry-After": "1"}, hint="wait")
    assert error.type == "urn:sreditor:problem:busy" and error.extensions == {"hint": "wait"}


# Pagination


def test_channels_paginate_by_slug(owner: TestClient) -> None:
    for slug in ("b-channel", "c-channel"):
        owner.post("/channels", json={**FUNNY_MEMES, "slug": slug, "accounts": []})
    first = owner.get("/channels", params={"limit": 2}).json()
    rest = owner.get("/channels", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    slugs = [c["slug"] for c in first["items"] + rest["items"]]
    assert slugs == ["b-channel", "c-channel", "funny-memes"] and rest["next_cursor"] is None


def test_pagination_limit_above_maximum_is_rejected(owner: TestClient) -> None:
    assert owner.get("/channels", params={"limit": 201}).status_code == 422


def test_pagination_invalid_cursor_returns_400(owner: TestClient) -> None:
    response = owner.get("/channels", params={"cursor": "!!!"})
    assert response.status_code == 400
    assert response.json()["type"] == "urn:sreditor:problem:invalid-cursor"


def test_pagination_cursor_round_trips() -> None:
    assert decode_cursor(encode_cursor("funny-memes")) == "funny-memes"


def test_member_list_within_limit_returns_page_without_next_cursor(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    sign_up("viewer@example.com")
    owner.post(
        "/channels/funny-memes/invitations", json={"email": "viewer@example.com", "role": "viewer"}
    )
    page = owner.get("/channels/funny-memes/members", params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["next_cursor"] is None


# Idempotency


def test_idempotency_replays_stored_response(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "create-other"}
    body = {**FUNNY_MEMES, "slug": "other", "accounts": []}
    first = owner.post("/channels", json=body, headers=headers)
    again = owner.post("/channels", json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"


def test_idempotency_key_of_one_token_never_replays_for_another(
    app: FastAPI, owner: TestClient
) -> None:
    headers = {"Idempotency-Key": "pillar-1"}
    pillar = {"kind": "pillar", "text": "Office life."}
    writer = agent(app, new_token(owner))
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert writer.post("/channels/funny-memes/editorial", json=pillar, headers=headers).is_success
    refused = reader.post("/channels/funny-memes/editorial", json=pillar, headers=headers)
    assert refused.status_code == 403 and "idempotent-replayed" not in refused.headers


def test_idempotency_key_with_other_body_returns_422(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "create-other"}
    owner.post("/channels", json={**FUNNY_MEMES, "slug": "other", "accounts": []}, headers=headers)
    reused = owner.post(
        "/channels", json={**FUNNY_MEMES, "slug": "third", "accounts": []}, headers=headers
    )
    assert reused.status_code == 422
    assert reused.json()["type"] == "urn:sreditor:problem:idempotency-key-reused"


def test_idempotency_failed_request_releases_key(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "dupe"}
    assert owner.post("/channels", json=FUNNY_MEMES, headers=headers).status_code == 409
    assert owner.post("/channels", json=FUNNY_MEMES, headers=headers).status_code == 409


def test_idempotency_guideline_retry_adds_one_pillar(owner: TestClient) -> None:
    pillar = {"kind": "pillar", "text": "Office life."}
    headers = {"Idempotency-Key": "pillar-1"}
    first = owner.post("/channels/funny-memes/editorial", json=pillar, headers=headers)
    again = owner.post("/channels/funny-memes/editorial", json=pillar, headers=headers)
    assert first.json()["id"] == again.json()["id"]
    assert len(owner.get("/channels/funny-memes/editorial").json()["pillars"]) == 1


def test_idempotency_repeated_invitation_returns_stored_response_marked_replayed(
    owner: TestClient,
) -> None:
    invite = {"email": "new@example.com", "role": "viewer"}
    headers = {"Idempotency-Key": "invite-1"}
    first = owner.post("/channels/funny-memes/invitations", json=invite, headers=headers)
    again = owner.post("/channels/funny-memes/invitations", json=invite, headers=headers)
    assert first.json() == again.json() and again.headers["idempotent-replayed"] == "true"


@pytest.fixture
def keys(app_settings: AppSettings) -> Iterator[IdempotencyStore]:
    yield IdempotencyStore(Database(app_settings.channels_db, utc_now))


def test_idempotency_store_with_running_key_refuses_it(
    owner: TestClient, keys: IdempotencyStore
) -> None:
    user = UUID(owner.user_id)  # type: ignore[attr-defined]
    assert keys.begin(user, "busy", "hash") is None
    with pytest.raises(KeyInProgress):
        keys.begin(user, "busy", "hash")


def test_idempotency_running_key_returns_409(owner: TestClient, keys: IdempotencyStore) -> None:
    user = UUID(owner.user_id)  # type: ignore[attr-defined]
    assert keys.begin(user, "busy", "hash") is None
    retry = Idempotency(store=keys, user_id=user, key="busy", request_hash="hash")
    with pytest.raises(ProblemError) as raised:
        retry.run(BaseModel, 201)
    assert raised.value.status == 409 and raised.value.headers == {"Retry-After": "1"}


def test_idempotency_key_expires_after_a_day(owner: TestClient, app_settings: AppSettings) -> None:
    user = UUID(owner.user_id)  # type: ignore[attr-defined]
    now = utc_now()
    today = IdempotencyStore(Database(app_settings.channels_db, lambda: now))
    assert today.begin(user, "key", "hash") is None
    today.complete(user, "key", StoredResponse(201, {"ok": True}))
    assert today.begin(user, "key", "hash") == StoredResponse(201, {"ok": True})
    tomorrow = IdempotencyStore(
        Database(app_settings.channels_db, lambda: now + timedelta(hours=25))
    )
    assert tomorrow.begin(user, "key", "other") is None


# ETag and If-Match


def test_etag_on_read_names_current_revision(owner: TestClient) -> None:
    assert owner.get("/channels/funny-memes").headers["etag"] == '"1"'


def test_etag_after_matching_if_match_update_names_next_revision(owner: TestClient) -> None:
    read = owner.get("/channels/funny-memes")
    changed = owner.put(
        "/channels/funny-memes", json=FUNNY_MEMES, headers={"If-Match": read.headers["etag"]}
    )
    assert changed.status_code == 200 and changed.headers["etag"] == '"2"'


def test_if_match_stale_returns_412_with_current_etag(owner: TestClient) -> None:
    owner.put("/channels/funny-memes", json=FUNNY_MEMES)
    stale = owner.put("/channels/funny-memes", json=FUNNY_MEMES, headers={"If-Match": '"1"'})
    assert stale.status_code == 412 and stale.headers["etag"] == '"2"'
    assert stale.json()["type"] == "urn:sreditor:problem:stale-revision"


def test_if_match_weak_and_star_are_accepted(owner: TestClient) -> None:
    weak = owner.put("/channels/funny-memes", json=FUNNY_MEMES, headers={"If-Match": 'W/"1"'})
    star = owner.put("/channels/funny-memes", json=FUNNY_MEMES, headers={"If-Match": "*"})
    assert weak.status_code == star.status_code == 200


def test_if_match_malformed_returns_412(owner: TestClient) -> None:
    response = owner.put("/channels/funny-memes", json=FUNNY_MEMES, headers={"If-Match": "abc"})
    assert response.status_code == 412


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/channels/funny-memes/accounts/youtube", {"platform": "youtube", "handle": "@fm"}),
        ("DELETE", "/channels/funny-memes/accounts/tiktok", None),
        ("DELETE", "/channels/funny-memes", None),
    ],
)
def test_if_match_stale_on_guarded_route_returns_412(
    owner: TestClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    response = owner.request(method, path, json=body, headers={"If-Match": '"9"'})
    assert response.status_code == 412


def test_if_match_current_revision_on_channel_delete_returns_204(owner: TestClient) -> None:
    response = owner.delete("/channels/funny-memes", headers={"If-Match": '"1"'})
    assert response.status_code == 204


# OpenAPI


@pytest.fixture
def operations(app: FastAPI) -> list[dict[str, Any]]:
    schema = TestClient(app).get("/openapi.json").json()
    return [operation for path in schema["paths"].values() for operation in path.values()]


def test_openapi_operation_ids_are_unique(operations: list[dict[str, Any]]) -> None:
    ids = [operation["operationId"] for operation in operations]
    assert len(ids) == len(set(ids))


def test_openapi_operation_ids_are_snake_case(operations: list[dict[str, Any]]) -> None:
    ids = [operation["operationId"] for operation in operations]
    assert all(op_id.isidentifier() and op_id.islower() for op_id in ids)


def test_openapi_operations_have_summary_and_description(
    operations: list[dict[str, Any]],
) -> None:
    assert all(op.get("summary") and op.get("description") for op in operations), [
        op["operationId"] for op in operations if not op.get("description")
    ]


# MCP


def mcp_call(client: TestClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=MCP_HEADERS,
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a served tool by its own name, as a client does."""
    result: dict[str, Any] = mcp_call(client, "tools/call", {"name": name, "arguments": arguments})[
        "result"
    ]
    return result


def operation_specs(app: FastAPI) -> dict[str, dict[str, Any]]:
    """Each operation's description, hints and result schema, as it had them as a tool."""
    described: dict[str, dict[str, Any]] = {}
    for name, added in record_operations(app.state.services).items():
        hints = added.annotations
        described[name] = {
            "name": name,
            "description": inspect.cleandoc(added.function.__doc__ or ""),
            "annotations": hints.model_dump(by_alias=True, exclude_none=True) if hints else {},
            "outputSchema": TypeAdapter(result_type(added)).json_schema(),
        }
    return described


def tool(client: TestClient, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one operation through the tool that groups it; its result comes back unwrapped."""
    request = {"operation": operation, **arguments}
    result = call_tool(client, OPERATION_TOOL[operation], {"request": request})
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        result["structuredContent"] = structured["result"]
    return result


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_without_token_returns_401(app: FastAPI) -> None:
    with TestClient(app, base_url=PUBLIC_URL) as client:
        response = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_mcp_with_session_token_returns_401(app: FastAPI, owner: TestClient) -> None:
    with TestClient(app, base_url=PUBLIC_URL, headers=dict(owner.headers)) as client:
        assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 401


def test_mcp_tools_list_includes_main_operations(mcp: TestClient) -> None:
    tools = {item["name"] for item in mcp_call(mcp, "tools/list", {})["result"]["tools"]}
    assert {"read_channel", "manage_channel", "read_episode", "edit_episode"} <= tools


def test_mcp_list_channels_returns_visible_channels(mcp: TestClient) -> None:
    listed = tool(mcp, "list_channels", {})["structuredContent"]
    assert [item["slug"] for item in listed["items"]] == ["funny-memes"]


def test_mcp_get_channel_returns_channel_with_revision(mcp: TestClient) -> None:
    view = tool(mcp, "get_channel", {"slug": "funny-memes"})["structuredContent"]
    assert view["revision"] == 1


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_brand", {"slug": "funny-memes", "platform": "tiktok"}),
        ("list_members", {"slug": "funny-memes"}),
    ],
)
def test_mcp_read_tool_on_existing_channel_reports_no_error(
    mcp: TestClient, name: str, arguments: dict[str, Any]
) -> None:
    assert not tool(mcp, name, arguments).get("isError")


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1])
def test_mcp_list_with_limit_out_of_range_is_refused(mcp: TestClient, limit: int) -> None:
    assert tool(mcp, "list_channels", {"limit": limit})["isError"]


def test_mcp_list_members_pages_with_cursor(mcp: TestClient) -> None:
    first = tool(mcp, "list_members", {"slug": "funny-memes", "limit": 1})["structuredContent"]
    assert len(first["items"]) == 1
    assert first["next_cursor"] is None  # the owner is the only member


@pytest.fixture
def written_statement_id(mcp: TestClient) -> str:
    written = tool(mcp, "write_guideline", {"slug": "funny-memes", "guideline": PURPOSE})
    statement_id: str = written["structuredContent"]["id"]
    return statement_id


def test_mcp_written_guideline_is_the_editorial_purpose(
    mcp: TestClient, written_statement_id: str
) -> None:
    editorial = tool(mcp, "get_editorial", {"slug": "funny-memes"})["structuredContent"]
    assert editorial["purpose"]["id"] == written_statement_id


def test_mcp_get_editorial_warns_when_the_cited_statement_changed(
    mcp: TestClient, app: FastAPI
) -> None:
    client = app.state.services.stmt
    house = {"statement": "Cut at 0.5 s.", "owner": {"type": "user", "id": "x"}, "category": "o"}
    client.write("house.cut", house, "/", None)
    guideline = {**PURPOSE, "text": "Cut at 0.5 s.", "cited_statement_id": "house.cut"}
    tool(mcp, "write_guideline", {"slug": "funny-memes", "guideline": guideline})
    client.write("house.cut", {**house, "statement": "Cut at 0.25 s."}, "/", 1)
    editorial = tool(mcp, "get_editorial", {"slug": "funny-memes"})["structuredContent"]
    (warning,) = editorial["warnings"]
    assert warning["kind"] == "changed" and warning["statement_revision"] == 2


def test_mcp_retire_guideline_reports_no_error(mcp: TestClient, written_statement_id: str) -> None:
    arguments = {"slug": "funny-memes", "statement_id": written_statement_id}
    assert not tool(mcp, "retire_guideline", arguments).get("isError")


def test_mcp_retire_guideline_with_stale_revision_says_how_to_reread(
    mcp: TestClient, written_statement_id: str
) -> None:
    arguments = {"slug": "funny-memes", "statement_id": written_statement_id}
    refused = tool(mcp, "retire_guideline", {**arguments, "expected_revision": 99})
    assert refused["isError"]
    assert "get_editorial" in refused["content"][0]["text"]


def test_mcp_put_account_with_expected_revision_bumps_channel_revision(mcp: TestClient) -> None:
    account = {"platform": "youtube", "handle": "@fm"}
    moved = tool(
        mcp, "put_account", {"slug": "funny-memes", "account": account, "expected_revision": 1}
    )
    assert moved["structuredContent"]["revision"] == 2


def test_mcp_create_channel_returns_the_new_channel(mcp: TestClient) -> None:
    created = tool(
        mcp, "create_channel", {"channel": {**FUNNY_MEMES, "slug": "other", "accounts": []}}
    )
    assert created["structuredContent"]["slug"] == "other"


REFUSED_CALLS: dict[str, dict[str, Any]] = {
    "get_channel": {"slug": "nope"},
    "create_channel": {"channel": FUNNY_MEMES},
    "put_account": {
        "slug": "funny-memes",
        "account": {"platform": "youtube", "handle": "@fm"},
        "expected_revision": 9,
    },
    "retire_guideline": {"slug": "funny-memes", "statement_id": "missing"},
    "get_editorial": {"slug": "funny-memes", "platform": "youtube"},
    "list_channels": {"cursor": "!!!"},
}


@pytest.mark.parametrize(("name", "arguments"), REFUSED_CALLS.items())
def test_mcp_refused_call_is_reported_as_tool_error(
    mcp: TestClient, name: str, arguments: dict[str, Any]
) -> None:
    assert tool(mcp, name, arguments)["isError"]


@pytest.mark.parametrize(
    ("name", "cause", "fix"),
    [
        ("get_channel", "Channel nope not found", "list_channels"),
        ("create_channel", "Channel funny-memes already exists", "choose another slug"),
        ("retire_guideline", "Guideline missing not found", "get_editorial"),
    ],
)
def test_mcp_refused_call_message_names_the_cause_and_the_fix(
    mcp: TestClient, name: str, cause: str, fix: str
) -> None:
    message = tool(mcp, name, REFUSED_CALLS[name])["content"][0]["text"]
    assert cause in message
    assert fix in message


def test_mcp_put_account_with_stale_revision_message_names_current_revision(
    mcp: TestClient,
) -> None:
    message = tool(mcp, "put_account", REFUSED_CALLS["put_account"])["content"][0]["text"]
    assert "revision 1" in message
    assert "get_channel" in message


def test_gate_block_refuses_agent_invitation(
    app: FastAPI, app_settings: AppSettings, owner: TestClient, mailer: RecordingMailer
) -> None:
    block(app_settings, "/funny-memes", "member.invite")
    bot = agent(app, new_token(owner))
    invite = {"email": "viewer@example.com", "role": "viewer"}
    response = bot.post("/channels/funny-memes/invitations", json=invite)
    assert response.status_code == 403
    assert response.json()["decision"] == "BLOCK"


def test_gate_block_refuses_agent_role_change(
    app: FastAPI,
    app_settings: AppSettings,
    owner: TestClient,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    viewer = sign_up("viewer@example.com")
    owner.post(
        "/channels/funny-memes/invitations", json={"email": "viewer@example.com", "role": "viewer"}
    )
    viewer.post("/invitations/accept", json={"token": mailer.token_for("viewer@example.com")})
    viewer_id = viewer.get("/me").json()["user"]["id"]
    block(app_settings, "/funny-memes", "member.role")
    bot = agent(app, new_token(owner))
    path = f"/channels/funny-memes/members/{viewer_id}"
    assert bot.patch(path, json={"role": "contributor"}).status_code == 403
    roles = {
        member["user_id"]: member["role"]
        for member in owner.get("/channels/funny-memes/members").json()["items"]
    }
    assert roles[viewer_id] == "viewer"


# Per-token rate limits


def limit_agents_to(app: FastAPI, requests: int) -> None:
    app.state.services.agent_limiter = RateLimiter(requests, 60.0)


def test_agent_over_its_limit_gets_429_with_retry_after(app: FastAPI, owner: TestClient) -> None:
    limit_agents_to(app, 1)
    bot = agent(app, new_token(owner))
    assert bot.get("/channels").status_code == 200
    response = bot.get("/channels")
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_agent_limit_counts_each_token_separately(app: FastAPI, owner: TestClient) -> None:
    limit_agents_to(app, 1)
    first, second = agent(app, new_token(owner)), agent(app, new_token(owner))
    first.get("/channels")
    assert second.get("/channels").status_code == 200


def test_agent_limit_leaves_sessions_alone(app: FastAPI, owner: TestClient) -> None:
    limit_agents_to(app, 1)
    assert [owner.get("/channels").status_code for _ in range(3)] == [200, 200, 200]


def test_mcp_over_the_token_limit_returns_429_problem(app: FastAPI, mcp: TestClient) -> None:
    limit_agents_to(app, 1)
    mcp_call(mcp, "tools/list", {})
    response = mcp.post("/mcp", json={}, headers=MCP_HEADERS)
    assert response.status_code == 429
    assert response.headers["content-type"] == "application/problem+json"
    assert int(response.headers["retry-after"]) >= 1


# Tool annotations (rule agentic.mcp.annotations)

CHANNEL_TOOL_EFFECTS = {
    "list_channels": Effect.READ,
    "get_channel": Effect.READ,
    "create_channel": Effect.REPLACE,
    "put_account": Effect.REPLACE,
    "get_brand": Effect.READ,
    "get_editorial": Effect.READ,
    "write_guideline": Effect.CREATE,
    "retire_guideline": Effect.REMOVE,
    "list_members": Effect.READ,
}
ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
OPEN_WORLD_TOOLS = {"voice_over"}  # it calls ElevenLabs


@pytest.mark.parametrize(
    ("effect", "hints"),
    [
        (Effect.READ, (True, False, True, False)),
        (Effect.CREATE, (False, False, False, False)),
        (Effect.REPLACE, (False, False, True, False)),
        (Effect.REMOVE, (False, True, True, False)),
    ],
)
def test_tool_annotations_effect_sets_every_hint(
    effect: Effect, hints: tuple[bool, bool, bool, bool]
) -> None:
    annotations = tool_annotations("A tool", effect).model_dump(by_alias=True)
    assert tuple(annotations[hint] for hint in ANNOTATION_HINTS) == hints
    assert annotations["title"] == "A tool"


def test_mcp_channel_tools_declare_title_and_every_hint(app: FastAPI) -> None:
    listed = operation_specs(app)
    for name, effect in CHANNEL_TOOL_EFFECTS.items():
        expected = tool_annotations(listed[name]["annotations"]["title"], effect)
        assert listed[name]["annotations"] == expected.model_dump(by_alias=True, exclude_none=True)


def test_mcp_every_tool_declares_title_and_every_hint(mcp: TestClient) -> None:
    for listed in mcp_call(mcp, "tools/list", {})["result"]["tools"]:
        annotations = listed.get("annotations", {})
        missing = [hint for hint in ("title", *ANNOTATION_HINTS) if hint not in annotations]
        assert not missing, f"{listed['name']} lacks {missing}"
        is_open_world = listed["name"] in OPEN_WORLD_TOOLS
        assert annotations["openWorldHint"] is is_open_world, listed["name"]
