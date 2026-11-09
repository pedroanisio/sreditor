"""The episode overview, its readiness list and the channel's readiness (rule project.readiness)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import readiness
from sreditor.delivery import DeliveryWarningKind
from sreditor.episode_facts import EpisodeFacts
from sreditor.overview_operations import DEFAULT_SECTIONS, DIGESTS, Section
from sreditor.readiness import Area, ReadinessItem, ReadinessSource
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, new_token, operation_specs, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined
from tests.test_mcp_contract import registered
from tests.workspace_helpers import join, member, new_channel, submit

SLUG = "funny-memes"
CHANNEL = f"/channels/{SLUG}"
EPISODE = f"{CHANNEL}/episodes/1"
OVERVIEW = f"{EPISODE}/overview"
PAGE = f"/app/channels/{SLUG}/episodes/1"
SOURCE = {
    "url": "https://example.com/mondays",
    "title": "Mondays study",
    "claim": "Most people dread Mondays.",
}
CLAIM = {
    "quote": "Most people dread Mondays.",
    "place": {"kind": "scene", "ref": "INT. NEWSROOM"},
    "sources": [],
}
CLIP = {
    "kind": "video",
    "title": "Mystery clip",
    "url": "https://example.com/clip",
    "creator": "Someone",
    "rights": "unknown",
}
NO_HOOK_SCRIPT = {
    "duration_s": 5,
    "blocks": [{"start_s": 0, "end_s": 5, "function": "development", "speech": "Oi."}],
}
SCREENPLAY = "INT. OFFICE - DAY\n\nREX\nGood morning.\n"


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


def overview(client: TestClient, query: str = "") -> dict[str, Any]:
    response = client.get(f"{OVERVIEW}{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def codes(body: dict[str, Any]) -> set[str]:
    return {gap["code"] for gap in body["readiness"]["items"]}


def load_every_source(owner: TestClient) -> None:
    """Give the episode one gap for each readiness source."""
    source = owner.post(f"{EPISODE}/sources", json=SOURCE).json()
    claim = {**CLAIM, "sources": [{"source_id": source["id"], "is_primary": False}]}
    assert owner.post(f"{EPISODE}/claims", json=claim).status_code == 201
    clip = owner.post(f"{CHANNEL}/external-media", json=CLIP).json()
    assert owner.post(f"{EPISODE}/external-media", json={"media_id": clip["id"]}).status_code == 201
    package = {
        "cuts": [{"name": "main"}],
        "posts": [{"platform": "tiktok", "cut": "main"}],
    }
    assert owner.put(f"{EPISODE}/delivery", json=package).status_code == 200
    assert owner.put(f"{EPISODE}/short-script", json=NO_HOOK_SCRIPT).status_code == 200
    assert owner.put(f"{EPISODE}/screenplay", json={"text": SCREENPLAY}).status_code == 200
    in_production(owner)


def in_production(owner: TestClient) -> None:
    body = {"title": "Pilot", "summary": "", "status": "production"}
    assert owner.put(EPISODE, json=body).status_code == 200


# The sections


def test_overview_default_includes_the_compact_set_and_leaves_the_rest_null(
    owner: TestClient,
) -> None:
    body = overview(owner)
    assert set(body["sections"]) == {section.value for section in DEFAULT_SECTIONS}
    for section in Section:
        assert (body[section.value] is not None) == (section in DEFAULT_SECTIONS), section


def test_overview_include_names_exactly_the_sections_to_return(owner: TestClient) -> None:
    body = overview(owner, "?include=context&include=delivery&include=pitches&include=credits")
    assert body["sections"] == ["pitches", "credits", "context", "delivery"]
    assert body["research"] is None and body["readiness"] is None
    assert body["delivery"]["read_with"] == "get_delivery"


def test_overview_every_section_has_a_digest_builder() -> None:
    assert set(DIGESTS) == set(Section)


def test_overview_unknown_section_answers_422(owner: TestClient) -> None:
    assert owner.get(f"{OVERVIEW}?include=waivers").status_code == 422


def test_overview_unknown_episode_answers_404(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/episodes/9/overview").status_code == 404


def test_overview_carries_the_revision_of_each_part(owner: TestClient) -> None:
    notes = owner.put(f"{EPISODE}/research", json={"text": "# Notes\n" + "x" * 300})
    screenplay = owner.put(f"{EPISODE}/screenplay", json={"text": SCREENPLAY})
    script = owner.put(f"{EPISODE}/short-script", json=NO_HOOK_SCRIPT)
    body = overview(owner, "?include=research&include=screenplay&include=short_script")
    assert body["research"]["revision"] == notes.json()["revision"]
    assert body["screenplay"]["revision"] == screenplay.json()["revision"]
    assert body["short_script"]["revision"] == script.json()["revision"]


def test_overview_episode_revision_matches_the_etag(owner: TestClient) -> None:
    assert f'"{overview(owner)["revision"]}"' == owner.get(EPISODE).headers["ETag"]


def test_overview_research_digest_cuts_the_notes_to_an_excerpt(owner: TestClient) -> None:
    owner.put(f"{EPISODE}/research", json={"text": "y" * 1000})
    digest = overview(owner, "?include=research")["research"]
    assert digest["notes_characters"] == 1000 and len(digest["notes_excerpt"]) < 1000


def test_overview_screenplay_digest_names_characters_and_places(owner: TestClient) -> None:
    owner.put(f"{EPISODE}/screenplay", json={"text": SCREENPLAY})
    digest = overview(owner, "?include=screenplay")["screenplay"]
    assert digest["characters"] == ["REX"] and digest["scene_count"] == 1


def test_overview_never_includes_waivers(owner: TestClient) -> None:
    assert "waiver" not in owner.get(f"{OVERVIEW}?include=media&include=readiness").text.lower()


# Readiness


def test_readiness_each_source_reports_its_gap_with_a_fix_tool(owner: TestClient) -> None:
    load_every_source(owner)
    body = overview(owner)
    assert {
        "claims_unchecked",
        "rights_unknown",
        "delivery_no_video",
        "delivery_no_link",
        "short_script_no_hook",
        "library_character_missing",
        "srstudio_no_style",
    } <= codes(body)


def test_readiness_items_name_area_severity_and_fix(owner: TestClient) -> None:
    load_every_source(owner)
    gaps = {gap["code"]: gap for gap in overview(owner)["readiness"]["items"]}
    assert gaps["claims_unchecked"]["fix_tool"] == "check_claim_source"
    assert gaps["claims_unchecked"]["severity"] == "warning"
    assert gaps["srstudio_no_style"]["severity"] == "blocker"
    assert gaps["srstudio_no_style"]["fix_tool"] == "set_episode_style"
    assert gaps["delivery_no_link"]["fix_tool"] == "record_post"
    assert all(gap["fix_hint"] and gap["message"] for gap in gaps.values())


def test_readiness_srstudio_gap_before_production_only_warns(owner: TestClient) -> None:
    gaps = {gap["code"]: gap for gap in overview(owner)["readiness"]["items"]}
    assert gaps["srstudio_no_style"]["severity"] == "warning"


def test_readiness_srstudio_gap_in_production_blocks(owner: TestClient) -> None:
    in_production(owner)
    gaps = {gap["code"]: gap for gap in overview(owner)["readiness"]["items"]}
    assert gaps["srstudio_no_style"]["severity"] == "blocker"


def test_readiness_lists_blockers_before_warnings(owner: TestClient) -> None:
    load_every_source(owner)
    report = overview(owner)["readiness"]
    severities = [gap["severity"] for gap in report["items"]]
    assert severities == sorted(severities, key=lambda value: value != "blocker")
    assert report["blockers"] == severities.count("blocker") > 0
    assert report["warnings"] == severities.count("warning") > 0


def test_readiness_every_reported_code_is_a_declared_kind(owner: TestClient) -> None:
    load_every_source(owner)
    declared = {kind.code for source in readiness.READINESS_SOURCES for kind in source.kinds}
    assert codes(overview(owner)) <= declared


def test_readiness_every_fix_tool_is_a_registered_tool(mcp: TestClient) -> None:
    fixes = {kind.fix.tool for source in readiness.READINESS_SOURCES for kind in source.kinds}
    assert fixes - set(registered(mcp)) == set()


def test_overview_every_read_with_is_a_registered_tool(mcp: TestClient, owner: TestClient) -> None:
    every = "".join(f"&include={section.value}" for section in Section)
    body = overview(owner, f"?{every[1:]}")
    named = {
        part["read_with"]
        for part in body.values()
        if isinstance(part, dict) and "read_with" in part
    }
    assert named and named - set(registered(mcp)) == set()


def test_readiness_every_source_kind_has_a_distinct_code() -> None:
    every = [kind.code for source in readiness.READINESS_SOURCES for kind in source.kinds]
    assert len(every) == len(set(every))


def test_readiness_a_new_source_is_one_registry_entry(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = readiness.kind(
        "extra_gap", Area.CLAIMS, readiness.Fix(tool="get_episode", hint="Look at it.")
    )

    def check(facts: EpisodeFacts) -> list[ReadinessItem]:
        return [readiness.item(extra, "Something else is missing.")]

    sources = (*readiness.READINESS_SOURCES, ReadinessSource(Area.CLAIMS, check, (extra,)))
    monkeypatch.setattr(readiness, "READINESS_SOURCES", sources)
    assert "extra_gap" in codes(overview(owner))


def test_readiness_viewer_skips_srstudio_and_says_so(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    report = overview(viewer)["readiness"]
    assert report["skipped_areas"] == ["srstudio"]
    assert not any(gap["severity"] == "blocker" for gap in report["items"])


def test_readiness_token_without_channels_write_skips_srstudio(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        report = overview(reader)["readiness"]
    assert report["skipped_areas"] == ["srstudio"]


def test_overview_non_member_answers_404(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    stranger = sign_up("stranger@example.com")
    assert stranger.get(OVERVIEW).status_code == 404
    assert stranger.get(f"{CHANNEL}/readiness").status_code == 404


# The channel's readiness


def test_channel_readiness_pages_through_the_episodes_by_number(owner: TestClient) -> None:
    for title in ("Two", "Three"):
        owner.post(f"{CHANNEL}/episodes", json={"title": title})
    first = owner.get(f"{CHANNEL}/readiness?limit=2").json()
    second = owner.get(f"{CHANNEL}/readiness?limit=2&cursor={first['next_cursor']}").json()
    assert [row["number"] for row in first["items"]] == [1, 2]
    assert [row["number"] for row in second["items"]] == [3] and second["next_cursor"] is None


def test_channel_readiness_counts_match_the_episode_overview(owner: TestClient) -> None:
    load_every_source(owner)
    report = overview(owner)["readiness"]
    row = owner.get(f"{CHANNEL}/readiness").json()["items"][0]
    assert (row["blockers"], row["warnings"], row["title"], row["status"]) == (
        report["blockers"],
        report["warnings"],
        "Pilot",
        "production",
    )


def test_channel_readiness_invalid_cursor_answers_400(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/readiness?cursor=!!").status_code == 400


# MCP


def structured(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    done = tool(client, name, arguments)
    assert not done.get("isError"), done
    content: dict[str, Any] = done["structuredContent"]
    return content


def test_mcp_get_episode_overview_returns_sections_and_readiness(
    mcp: TestClient, owner: TestClient
) -> None:
    load_every_source(owner)
    body = structured(mcp, "get_episode_overview", {"slug": SLUG, "number": 1})
    assert body["episode"]["title"] == "Pilot" and "claims_unchecked" in codes(body)
    assert body["context"] is None


def test_mcp_get_episode_overview_include_picks_sections(mcp: TestClient) -> None:
    body = structured(
        mcp, "get_episode_overview", {"slug": SLUG, "number": 1, "include": ["context"]}
    )
    assert body["sections"] == ["context"] and body["readiness"] is None


def test_mcp_get_episode_overview_is_read_only_with_an_output_schema(app: FastAPI) -> None:
    described = operation_specs(app)["get_episode_overview"]
    assert described["annotations"]["readOnlyHint"] is True
    assert described["annotations"]["destructiveHint"] is False
    assert described["annotations"]["openWorldHint"] is False
    assert described["outputSchema"]["properties"]["readiness"]


def test_mcp_get_episode_overview_unknown_episode_names_list_episodes(mcp: TestClient) -> None:
    refused = tool(mcp, "get_episode_overview", {"slug": SLUG, "number": 9})
    assert refused["isError"] and "list_episodes" in refused["content"][0]["text"]


def test_mcp_list_episodes_with_readiness_counts_each_episode(
    mcp: TestClient, owner: TestClient
) -> None:
    load_every_source(owner)
    page = structured(mcp, "list_episodes", {"slug": SLUG, "readiness": True})
    expected = overview(owner)["readiness"]
    assert page["readiness"] == [
        {
            "number": 1,
            "title": "Pilot",
            "status": "production",
            "blockers": expected["blockers"],
            "warnings": expected["warnings"],
        }
    ]


def test_mcp_list_episodes_without_readiness_leaves_it_out(mcp: TestClient) -> None:
    page = structured(mcp, "list_episodes", {"slug": SLUG})
    assert page["readiness"] is None and len(page["items"]) == 1


# The page


@pytest.fixture
def pages(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    """A signed-in owner on the pages, with a channel and its first episode."""
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    assert submit(client, f"/app/channels/{SLUG}/episodes", {"title": "Pilot"}).status_code == 303
    return client


def test_episode_page_lists_gaps_with_links_to_the_pages_that_fix_them(pages: TestClient) -> None:
    in_production(pages)
    page = pages.get(PAGE).text
    assert 'id="readiness"' in page and "Blocks export and render" in page
    assert f'href="{PAGE}/srstudio"' in page


def test_episode_page_portuguese_translates_the_readiness_list(pages: TestClient) -> None:
    in_production(pages)
    page = pages.get(f"{PAGE}?lang=pt-BR").text
    assert "Prontidão" in page and "Bloqueia a exportação e a renderização" in page


def test_episode_page_viewer_without_gaps_says_nothing_holds_it_back(
    app: FastAPI, mailer: RecordingMailer, pages: TestClient
) -> None:
    viewer = join(app, mailer, pages, "viewer@example.com", "viewer")
    assert "Nothing holds this episode back." in viewer.get(PAGE).text


# Reviews and post text in the readiness list


def test_readiness_episode_in_production_without_review_names_add_review(owner: TestClient) -> None:
    in_production(owner)
    gaps = {gap["code"]: gap for gap in overview(owner)["readiness"]["items"]}
    assert gaps["review_missing"]["fix_tool"] == "add_review"


def test_readiness_episode_before_production_has_no_review_gap(owner: TestClient) -> None:
    assert "review_missing" not in codes(overview(owner))


def test_readiness_review_asking_for_changes_is_a_gap(owner: TestClient) -> None:
    in_production(owner)
    review = {"verdict": "changes_requested", "notes": "Cut the intro."}
    assert owner.post(f"{EPISODE}/reviews", json=review).status_code == 201
    assert "review_not_approved" in codes(overview(owner))


def test_readiness_approved_review_leaves_no_review_gap(owner: TestClient) -> None:
    in_production(owner)
    assert owner.post(f"{EPISODE}/reviews", json={"verdict": "approved"}).status_code == 201
    assert not {"review_missing", "review_not_approved"} & codes(overview(owner))


def test_readiness_every_delivery_warning_has_a_kind() -> None:
    assert set(readiness.DELIVERY_KINDS) == set(DeliveryWarningKind)
