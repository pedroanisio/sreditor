"""Claims: the figures and facts an episode states, tied to their sources (rule project.claims)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.claims import ClaimInput, ClaimPlace, PlaceKind, warning_text
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, stmt, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1"
CLAIMS = f"{EPISODE}/claims"
SOURCES = f"{EPISODE}/sources"
PITCHES = "/channels/funny-memes/pitches"
ZERO_ID = "00000000-0000-0000-0000-000000000000"
STUDY = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": "Most people dread Mondays.",
    "accessed_on": "2026-11-01",
}
BETS = {**STUDY, "url": "https://example.com/bets", "title": "Bets report"}
QUOTE = "R$ 62,5 bilhões das bets"


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def source(owner: TestClient) -> str:
    """The id of a source of episode 1."""
    return str(owner.post(SOURCES, json=STUDY).json()["id"])


def claim_body(*sources: tuple[str, bool], quote: str = QUOTE) -> dict[str, Any]:
    """A claim as the API takes it: its quote, a scene and the (source, is_primary) pairs."""
    return {
        "quote": quote,
        "place": {"kind": "scene", "ref": "INT. NEWSROOM"},
        "sources": [{"source_id": item, "is_primary": primary} for item, primary in sources],
    }


@pytest.fixture
def claim(owner: TestClient, source: str) -> dict[str, Any]:
    response = owner.post(CLAIMS, json=claim_body((source, True)))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def claim_path(claim: dict[str, Any]) -> str:
    return f"{CLAIMS}/{claim['id']}"


# Models


def test_warning_text_without_claims_is_none() -> None:
    assert warning_text(0) is None


def test_warning_text_counts_claims_in_the_singular_and_plural() -> None:
    assert warning_text(1) is not None and warning_text(1).startswith("1 claim has")
    assert warning_text(3) is not None and warning_text(3).startswith("3 claims have")


def test_claim_input_repeating_a_source_is_refused() -> None:
    source_id = uuid4()
    backing = {"source_id": source_id, "is_primary": True}
    with pytest.raises(ValueError, match="once"):
        ClaimInput.model_validate(
            {
                "quote": "Q",
                "place": ClaimPlace(kind=PlaceKind.TEXT, ref="intro"),
                "sources": [backing, backing],
            }
        )


# API


def test_claim_add_starts_unchecked_with_the_source_as_it_is(
    claim: dict[str, Any],
) -> None:
    backing = claim["sources"][0]
    assert (claim["has_checked_primary"], backing["is_checked"], backing["title"]) == (
        False,
        False,
        "Mondays study",
    )


def test_claim_add_with_idempotency_key_replays(owner: TestClient, source: str) -> None:
    headers = {"Idempotency-Key": "claim-1"}
    first = owner.post(CLAIMS, json=claim_body((source, True)), headers=headers)
    again = owner.post(CLAIMS, json=claim_body((source, True)), headers=headers)
    assert again.json()["id"] == first.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true"


def test_claim_add_unknown_source_returns_unprocessable_naming_it(owner: TestClient) -> None:
    response = owner.post(CLAIMS, json=claim_body((ZERO_ID, True)))
    assert response.status_code == 422 and ZERO_ID in response.json()["detail"]


def test_claim_add_without_quote_returns_unprocessable(owner: TestClient, source: str) -> None:
    assert owner.post(CLAIMS, json=claim_body((source, True), quote=" ")).status_code == 422


def test_claim_add_in_missing_episode_returns_not_found(owner: TestClient) -> None:
    missing = "/channels/funny-memes/episodes/9/claims"
    assert owner.post(missing, json=claim_body()).status_code == 404


def test_claim_get_returns_etag_and_unknown_claim_names_the_fix(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    assert owner.get(claim_path(claim)).headers["etag"] == '"1"'
    missing = owner.get(f"{CLAIMS}/{uuid4()}")
    assert missing.status_code == 404 and "list_claims" in missing.json()["detail"]


def test_claim_list_paginates_in_the_order_added(owner: TestClient, source: str) -> None:
    for quote in ("First", "Second"):
        owner.post(CLAIMS, json=claim_body((source, True), quote=quote))
    first = owner.get(CLAIMS, params={"limit": 1}).json()
    second = owner.get(CLAIMS, params={"cursor": first["next_cursor"]}).json()
    assert [item["quote"] for item in first["items"] + second["items"]] == ["First", "Second"]
    assert second["next_cursor"] is None


def test_claim_unchecked_lists_a_claim_without_a_checked_primary_source(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    warnings = owner.get(f"{CLAIMS}/unchecked").json()
    assert warnings["count"] == 1 and warnings["claims"][0]["quote"] == QUOTE
    assert "no checked primary source" in warnings["message"]


def checked(owner: TestClient, claim: dict[str, Any], source: str, **headers: str) -> Any:
    return owner.put(f"{claim_path(claim)}/sources/{source}/check", json={}, headers=headers)


def test_claim_check_primary_source_clears_the_warning_and_names_who_checked(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    response = checked(owner, claim, source)
    backing = response.json()["sources"][0]
    assert (
        response.json()["has_checked_primary"] is True and backing["checked_by"]["type"] == "user"
    )
    assert backing["checked_at"] is not None
    assert owner.get(f"{CLAIMS}/unchecked").json()["count"] == 0


def test_claim_check_secondary_source_keeps_the_warning(owner: TestClient, source: str) -> None:
    other = str(owner.post(SOURCES, json=BETS).json()["id"])
    made = owner.post(CLAIMS, json=claim_body((source, True), (other, False))).json()
    checked(owner, made, other)
    assert owner.get(f"{CLAIMS}/unchecked").json()["claims"][0]["has_primary"] is True


def test_claim_check_taken_back_warns_again(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    checked(owner, claim, source)
    path = f"{claim_path(claim)}/sources/{source}/check"
    owner.put(path, json={"is_checked": False})
    assert owner.get(f"{CLAIMS}/unchecked").json()["count"] == 1


def test_claim_check_stale_etag_returns_precondition_failed(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    checked(owner, claim, source)
    assert checked(owner, claim, source, **{"If-Match": '"1"'}).status_code == 412


def test_claim_check_source_that_does_not_back_it_returns_not_found(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    assert checked(owner, claim, str(uuid4())).status_code == 404


def test_claim_put_keeps_the_check_of_a_source_that_stays(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    checked(owner, claim, source)
    other = str(owner.post(SOURCES, json=BETS).json()["id"])
    body = claim_body((source, True), (other, False), quote="Changed")
    response = owner.put(claim_path(claim), json=body, headers={"If-Match": '"2"'})
    assert response.json()["sources"][0]["is_checked"] is True
    assert (response.json()["quote"], response.headers["etag"]) == ("Changed", '"3"')


def test_claim_put_stale_etag_returns_precondition_failed(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    response = owner.put(
        claim_path(claim), json=claim_body((source, True)), headers={"If-Match": '"9"'}
    )
    assert response.status_code == 412


def test_claim_delete_removes_it_and_leaves_the_source(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    assert owner.delete(claim_path(claim)).status_code == 204
    assert (
        owner.get(claim_path(claim)).status_code == 404
        and len(owner.get(SOURCES).json()["items"]) == 1
    )


def test_claim_loses_its_backing_when_the_source_is_removed(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    owner.delete(f"{SOURCES}/{source}")
    assert owner.get(claim_path(claim)).json()["sources"] == []
    assert owner.get(f"{CLAIMS}/unchecked").json()["claims"][0]["has_primary"] is False


# Sources of the episode's pitches


@pytest.fixture
def pitch_source(owner: TestClient) -> dict[str, str]:
    """A pitch with one source, used by episode 1."""
    pitch = owner.post(PITCHES, json={"title": "Mondays"}).json()["id"]
    source = owner.post(f"{PITCHES}/{pitch}/sources", json=BETS).json()["id"]
    owner.put(f"{EPISODE}/pitches", json={"pitch_ids": [pitch]})
    return {"pitch": pitch, "source": source}


def test_claim_may_cite_a_source_of_the_episodes_pitch(
    owner: TestClient, pitch_source: dict[str, str]
) -> None:
    response = owner.post(CLAIMS, json=claim_body((pitch_source["source"], True)))
    backing = response.json()["sources"][0]
    assert (backing["source_kind"], backing["pitch_id"]) == ("pitch", pitch_source["pitch"])


def test_claim_citing_a_source_of_a_pitch_the_episode_does_not_use_is_refused(
    owner: TestClient,
) -> None:
    pitch = owner.post(PITCHES, json={"title": "Unused"}).json()["id"]
    source = owner.post(f"{PITCHES}/{pitch}/sources", json=BETS).json()["id"]
    assert owner.post(CLAIMS, json=claim_body((source, True))).status_code == 422


def test_claim_backing_follows_the_pitch_source_live(
    owner: TestClient, pitch_source: dict[str, str]
) -> None:
    made = owner.post(CLAIMS, json=claim_body((pitch_source["source"], True))).json()
    path = f"{PITCHES}/{pitch_source['pitch']}/sources/{pitch_source['source']}"
    owner.put(path, json={**BETS, "title": "Renamed"})
    assert owner.get(claim_path(made)).json()["sources"][0]["title"] == "Renamed"


def test_claim_backing_is_hidden_while_its_pitch_is_dropped_from_the_episode(
    owner: TestClient, pitch_source: dict[str, str]
) -> None:
    made = owner.post(CLAIMS, json=claim_body((pitch_source["source"], True))).json()
    owner.put(f"{EPISODE}/pitches", json={"pitch_ids": []})
    assert owner.get(claim_path(made)).json()["sources"] == []


# The warning when the episode moves to published, which never blocks it


def publish(owner: TestClient) -> Any:
    body = {"title": "Pilot", "summary": "", "status": "published"}
    return owner.put(EPISODE, json=body)


def test_episode_published_with_unchecked_claims_succeeds_with_a_warning_header(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    response = publish(owner)
    assert response.status_code == 200 and response.json()["status"] == "published"
    assert response.headers["warning"].startswith('199 sreditor "1 claim has no checked primary')


def test_episode_published_with_every_claim_checked_has_no_warning(
    owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    checked(owner, claim, source)
    assert "warning" not in publish(owner).headers


def test_episode_moved_to_another_status_has_no_claims_warning(
    owner: TestClient, claim: dict[str, Any]
) -> None:
    response = owner.put(EPISODE, json={"title": "Pilot", "summary": "", "status": "script"})
    assert "warning" not in response.headers


# Roles, scopes and the gate


def test_claim_add_as_viewer_returns_forbidden(
    owner: TestClient,
    source: str,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(CLAIMS, json=claim_body((source, True))).status_code == 403


def test_claim_list_as_viewer_is_allowed(
    owner: TestClient,
    claim: dict[str, Any],
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.get(CLAIMS).status_code == 200


def test_claim_check_with_token_records_the_token_as_who_checked(
    app: FastAPI, owner: TestClient, claim: dict[str, Any], source: str
) -> None:
    token = new_token(owner)
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    response = checked(agent(app, token), claim, source)
    assert response.json()["sources"][0]["checked_by"] == {"type": "token", "id": token_id}


def test_claim_add_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, app_settings: AppSettings, owner: TestClient, source: str
) -> None:
    block(app_settings, "/", "claim.create")
    response = agent(app, new_token(owner)).post(CLAIMS, json=claim_body((source, True)))
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


def test_claim_add_with_token_allowed_by_gate_is_recorded(
    app: FastAPI, app_settings: AppSettings, owner: TestClient, source: str
) -> None:
    response = agent(app, new_token(owner)).post(CLAIMS, json=claim_body((source, True)))
    events = stmt(app_settings, "/funny-memes", "events", "--json", "{}")["result"]["events"]
    assert response.status_code == 201
    assert [(event["phase"], event["decision"]) for event in events] == [
        ("BEFORE", "ALLOW"),
        ("AFTER", "ACCEPT"),
    ]


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def add_claim_tool(mcp: TestClient, source: str, **extra: Any) -> dict[str, Any]:
    arguments = {
        "slug": "funny-memes",
        "number": 1,
        "claim": claim_body((source, True)),
        **extra,
    }
    return tool(mcp, "add_claim", arguments)


def test_mcp_add_claim_returns_it_with_the_episodes_unchecked_claims(
    mcp: TestClient, source: str
) -> None:
    result = add_claim_tool(mcp, source)["structuredContent"]
    assert result["claim"]["quote"] == QUOTE and result["warnings"]["count"] == 1


def test_mcp_add_claim_with_idempotency_key_replays_the_first_result(
    mcp: TestClient, source: str
) -> None:
    first = add_claim_tool(mcp, source, idempotency_key="claim-key")["structuredContent"]
    again = add_claim_tool(mcp, source, idempotency_key="claim-key")["structuredContent"]
    assert again["claim"]["id"] == first["claim"]["id"]
    listed = tool(mcp, "list_claims", {"slug": "funny-memes", "number": 1})["structuredContent"]
    assert len(listed["claims"]["items"]) == 1


def test_mcp_add_claim_unknown_source_names_get_research(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "claim": claim_body((ZERO_ID, True))}
    result = tool(mcp, "add_claim", arguments)
    assert result["isError"] and "get_research" in result["content"][0]["text"]


def test_mcp_check_claim_source_clears_the_warning(mcp: TestClient, source: str) -> None:
    made = add_claim_tool(mcp, source)["structuredContent"]["claim"]
    arguments = {
        "slug": "funny-memes",
        "claim_id": made["id"],
        "mark": {"source_id": source},
        "expected_revision": made["revision"],
    }
    result = tool(mcp, "check_claim_source", arguments)["structuredContent"]
    assert result["claim"]["has_checked_primary"] and result["warnings"]["count"] == 0


def test_mcp_check_claim_source_not_backing_names_list_claims(mcp: TestClient, source: str) -> None:
    made = add_claim_tool(mcp, source)["structuredContent"]["claim"]
    arguments = {"slug": "funny-memes", "claim_id": made["id"], "mark": {"source_id": ZERO_ID}}
    result = tool(mcp, "check_claim_source", arguments)
    assert result["isError"] and "call list_claims" in result["content"][0]["text"]


def test_mcp_update_claim_replaces_its_quote(mcp: TestClient, source: str) -> None:
    made = add_claim_tool(mcp, source)["structuredContent"]["claim"]
    arguments = {
        "slug": "funny-memes",
        "claim_id": made["id"],
        "claim": claim_body((source, True), quote="Fixed"),
        "expected_revision": 1,
    }
    assert tool(mcp, "update_claim", arguments)["structuredContent"]["claim"]["quote"] == "Fixed"


def test_mcp_remove_claim_reports_what_it_removed(mcp: TestClient, source: str) -> None:
    made = add_claim_tool(mcp, source)["structuredContent"]["claim"]
    removed = tool(mcp, "remove_claim", {"slug": "funny-memes", "claim_id": made["id"]})
    assert removed["structuredContent"]["removed"] == {"kind": "claim", "id": made["id"]}
    assert removed["structuredContent"]["warnings"]["count"] == 0


def test_mcp_remove_unknown_claim_names_list_claims(mcp: TestClient) -> None:
    result = tool(mcp, "remove_claim", {"slug": "funny-memes", "claim_id": ZERO_ID})
    assert result["isError"] and "call list_claims" in result["content"][0]["text"]


def test_mcp_update_episode_to_published_repeats_the_claims_warning(
    mcp: TestClient, source: str
) -> None:
    add_claim_tool(mcp, source)
    arguments = {
        "slug": "funny-memes",
        "number": 1,
        "episode": {"title": "Pilot", "status": "published"},
    }
    result = tool(mcp, "update_episode", arguments)["structuredContent"]
    assert "no checked primary source" in result["claims_warning"]
