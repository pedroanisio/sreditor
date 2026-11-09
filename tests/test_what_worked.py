"""What worked: posts grouped by classification, hook or length (project.metrics.what-worked)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_agentic import PUBLIC_URL, agent, new_token, operation_specs, tool
from tests.test_api import FUNNY_MEMES

CHANNEL = "/channels/funny-memes"
WORKED = f"{CHANNEL}/post-metrics/what-worked"
PAGE = "/app/channels/funny-memes/metrics/what-worked"
ACCOUNTS = [
    {"platform": "tiktok", "handle": "@funnymemes"},
    {"platform": "instagram", "handle": "@funnymemes"},
]
HASH = "a" * 64


@dataclass(frozen=True)
class Spec:
    """One episode's classification, hook, video length, publish day and views."""

    genre: str
    reaction: str
    hook_end: float
    seconds: float
    published: str
    views: int


SPECS = [
    Spec("documentary", "curiosity", 1.5, 12, "2026-10-01", 100),
    Spec("documentary", "curiosity", 2.5, 20, "2026-10-05", 200),
    Spec("documentary", "surprise", 2.5, 20, "2026-10-09", 300),
    Spec("drama", "surprise", 4.0, 45, "2026-10-13", 50),
]


def classification(genre: str) -> dict[str, Any]:
    item = {"value": genre, "role": "primary", "confidence": None}
    return {"dimensions": {"genres": {"state": "present", "items": [item]}}}


def short_script(spec: Spec) -> dict[str, Any]:
    return {
        "duration_s": 15,
        "blocks": [
            {
                "start_s": 0,
                "end_s": spec.hook_end,
                "function": "hook",
                "speech": "Oi.",
                "impact": {"reaction": spec.reaction, "intensity": 3},
            },
            {"start_s": spec.hook_end, "end_s": 15, "function": "punch", "speech": "Tchau."},
        ],
    }


def delivery(spec: Spec, platform: str = "tiktok") -> dict[str, Any]:
    video = {"location": "drive://x", "sha256": HASH, "duration_seconds": spec.seconds}
    return {
        "cuts": [{"name": "main", "external_video": video}],
        "posts": [
            {"platform": platform, "cut": "main", "published_at": f"{spec.published}T10:00:00Z"}
        ],
    }


def put(client: TestClient, path: str, body: dict[str, Any], status: int = 200) -> None:
    response = client.put(path, json=body, headers={"If-Match": '"0"'})
    assert response.status_code == status, response.text


def numbers(
    client: TestClient, number: int, day: str, views: int, platform: str = "tiktok"
) -> None:
    path = f"{CHANNEL}/episodes/{number}/delivery/posts/{platform}/snapshots"
    response = client.post(path, json={"captured_on": day, "views": views})
    assert response.status_code == 201, response.text


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    channel = {**FUNNY_MEMES, "accounts": ACCOUNTS}
    assert client.post("/channels", json=channel).status_code == 201
    for number, spec in enumerate(SPECS, start=1):
        episode = f"{CHANNEL}/episodes"
        assert client.post(episode, json={"title": f"Episode {number}"}).status_code == 201
        base = f"{episode}/{number}"
        put(client, f"{base}/classification", classification(spec.genre))
        put(client, f"{base}/short-script", short_script(spec))
        put(client, f"{base}/delivery", delivery(spec))
        numbers(client, number, spec.published, spec.views)
    return client


def groups(client: TestClient, **params: str) -> dict[str, dict[str, Any]]:
    response = client.get(WORKED, params=params)
    assert response.status_code == 200, response.text
    return {group["key"]: group for group in response.json()["groups"]}


def excluded(client: TestClient, **params: str) -> dict[str, int]:
    found: dict[str, int] = client.get(WORKED, params=params).json()["excluded"]
    return found


GENRES = {"group_by": "classification", "classification_dimension": "genres"}


# Grouping


def test_what_worked_by_classification_groups_by_primary_value(owner: TestClient) -> None:
    found = groups(owner, **GENRES)
    assert (found["documentary"]["posts"], found["documentary"]["median"]) == (3, 200)
    assert (found["documentary"]["mean"], found["documentary"]["episodes"]) == (200, [1, 2, 3])
    assert (found["drama"]["posts"], found["drama"]["median"]) == (1, 50)


def test_what_worked_orders_groups_by_median_best_first(owner: TestClient) -> None:
    keys = [group["key"] for group in owner.get(WORKED, params=GENRES).json()["groups"]]
    assert keys == ["documentary", "drama"]


def test_what_worked_by_hook_reaction_groups_by_the_hook_blocks_reaction(owner: TestClient) -> None:
    found = groups(owner, group_by="hook_reaction")
    assert (found["curiosity"]["median"], found["surprise"]["median"]) == (150, 175)


def test_what_worked_by_hook_length_groups_into_bands(owner: TestClient) -> None:
    found = groups(owner, group_by="hook_length")
    assert {key: group["posts"] for key, group in found.items()} == {
        "up_to_2s": 1,
        "2_to_3s": 2,
        "over_3s": 1,
    }


def test_what_worked_by_video_length_groups_into_bands(owner: TestClient) -> None:
    found = groups(owner, group_by="video_length")
    assert {key: group["episodes"] for key, group in found.items()} == {
        "up_to_15s": [1],
        "15_to_30s": [2, 3],
        "30_to_60s": [4],
    }


def test_what_worked_by_other_metric_uses_that_number(owner: TestClient) -> None:
    numbers(owner, 1, "2026-10-02", 100)
    owner.post(
        f"{CHANNEL}/episodes/1/delivery/posts/tiktok/snapshots",
        json={"captured_on": "2026-10-02", "likes": 9},
    )
    found = groups(owner, group_by="video_length", metric="likes")
    assert found["up_to_15s"]["median"] == 9 and "15_to_30s" not in found


# Snapshots


def test_what_worked_latest_reads_the_newest_snapshot(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-20", 500)
    assert groups(owner, **GENRES)["drama"]["median"] == 500


def test_what_worked_fixed_age_reads_the_snapshot_near_that_age(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-20", 500)
    numbers(owner, 4, "2026-10-30", 900)
    assert groups(owner, age="day_7", **GENRES)["drama"]["median"] == 500


def test_what_worked_fixed_age_leaves_out_posts_without_a_snapshot_then(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-20", 500)
    assert excluded(owner, age="day_7", **GENRES)["without_numbers"] == 3


def test_what_worked_fixed_age_ignores_later_snapshots(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-14", 70)
    numbers(owner, 4, "2026-11-01", 900)
    assert groups(owner, age="day_1", **GENRES)["drama"]["median"] == 70


def test_what_worked_fixed_age_tolerates_two_days(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-22", 80)
    assert groups(owner, age="day_7", **GENRES)["drama"]["median"] == 80


def test_what_worked_fixed_age_beyond_tolerance_has_no_numbers(owner: TestClient) -> None:
    numbers(owner, 4, "2026-10-23", 80)
    assert "drama" not in groups(owner, age="day_7", **GENRES)


# Exclusions


def test_what_worked_proposal_is_left_out_and_counted(app: FastAPI, owner: TestClient) -> None:
    with agent(app, new_token(owner), PUBLIC_URL) as bot:
        proposal = classification("comedy")
        proposal["dimensions"]["genres"]["items"][0]["confidence"] = 0.9
        proposal["method"] = "ai"
        proposal["dimensions"]["humor_devices"] = {
            "state": "present",
            "items": [{"value": "deadpan", "role": "primary", "confidence": 0.8}],
        }
        response = bot.put(
            f"{CHANNEL}/episodes/4/classification", json=proposal, headers={"If-Match": '"1"'}
        )
        assert response.status_code == 200, response.text
    found = groups(owner, **GENRES)
    assert "drama" not in found and "comedy" not in found
    assert excluded(owner, **GENRES)["proposals"] == 1


def test_what_worked_post_without_publish_time_is_counted_apart(owner: TestClient) -> None:
    body = delivery(SPECS[3])
    body["posts"][0].pop("published_at")
    response = owner.put(f"{CHANNEL}/episodes/4/delivery", json=body, headers={"If-Match": '"1"'})
    assert response.status_code == 200, response.text
    assert excluded(owner, **GENRES)["without_publish_time"] == 1
    assert "drama" not in groups(owner, **GENRES)


def test_what_worked_unclassified_episode_is_counted_without_value(owner: TestClient) -> None:
    assert owner.post(f"{CHANNEL}/episodes", json={"title": "Fifth"}).status_code == 201
    put(owner, f"{CHANNEL}/episodes/5/delivery", delivery(SPECS[0]))
    numbers(owner, 5, "2026-10-01", 7)
    assert excluded(owner, **GENRES)["without_value"] == 1


def test_what_worked_post_without_numbers_is_counted_without_numbers(owner: TestClient) -> None:
    assert owner.post(f"{CHANNEL}/episodes", json={"title": "Fifth"}).status_code == 201
    base = f"{CHANNEL}/episodes/5"
    put(owner, f"{base}/classification", classification("drama"))
    put(owner, f"{base}/delivery", delivery(SPECS[3]))
    assert excluded(owner, **GENRES)["without_numbers"] == 1


# Small groups


def test_what_worked_group_under_three_posts_is_flagged_too_small(owner: TestClient) -> None:
    found = groups(owner, **GENRES)
    assert (found["documentary"]["is_too_small"], found["drama"]["is_too_small"]) == (False, True)


def test_what_worked_reply_names_the_smallest_readable_group(owner: TestClient) -> None:
    assert owner.get(WORKED, params=GENRES).json()["min_group_posts"] == 3


# Filters


def test_what_worked_platform_filter_without_posts_there_returns_no_groups(
    owner: TestClient,
) -> None:
    assert groups(owner, platform="youtube", **GENRES) == {}


def test_what_worked_platform_filter_selects_the_platforms_posts(owner: TestClient) -> None:
    body = delivery(SPECS[3], "instagram")
    body["cuts"].append({"name": "alt"})
    body["posts"].append(
        {"platform": "tiktok", "cut": "alt", "published_at": "2026-10-13T10:00:00Z"}
    )
    put_revision = owner.put(
        f"{CHANNEL}/episodes/4/delivery", json=body, headers={"If-Match": '"1"'}
    )
    assert put_revision.status_code == 200, put_revision.text
    numbers(owner, 4, "2026-10-14", 999, "instagram")
    found = groups(owner, platform="instagram", **GENRES)
    assert list(found) == ["drama"] and found["drama"]["median"] == 999


def test_what_worked_date_range_keeps_posts_published_inside_it(owner: TestClient) -> None:
    found = groups(owner, published_from="2026-10-05", published_to="2026-10-09", **GENRES)
    assert found["documentary"]["episodes"] == [2, 3] and "drama" not in found


def test_what_worked_inverted_date_range_is_refused(owner: TestClient) -> None:
    response = owner.get(
        WORKED, params={**GENRES, "published_from": "2026-11-01", "published_to": "2026-10-01"}
    )
    assert response.status_code == 422 and "published_from" in response.json()["detail"]


# Shape and errors


def test_what_worked_classification_without_dimension_names_the_field(owner: TestClient) -> None:
    response = owner.get(WORKED, params={"group_by": "classification"})
    assert response.status_code == 422 and "classification_dimension" in response.json()["detail"]


def test_what_worked_dimension_with_another_grouping_is_refused(owner: TestClient) -> None:
    params = {"group_by": "hook_length", "classification_dimension": "genres"}
    assert owner.get(WORKED, params=params).status_code == 422


def test_what_worked_unknown_group_is_invalid(owner: TestClient) -> None:
    assert owner.get(WORKED, params={"group_by": "mood"}).status_code == 422


def test_what_worked_of_unknown_channel_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/nope/post-metrics/what-worked", params=GENRES).status_code == 404


def test_what_worked_reply_echoes_the_query(owner: TestClient) -> None:
    body = owner.get(WORKED, params={**GENRES, "metric": "likes", "age": "day_3"}).json()
    assert (body["group_by"], body["metric"], body["age"]) == ("classification", "likes", "day_3")


def test_what_worked_operation_id_is_stable(owner: TestClient) -> None:
    paths = owner.get("/openapi.json").json()["paths"]
    assert paths["/channels/{slug}/post-metrics/what-worked"]["get"]["operationId"] == "what_worked"


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_what_worked_returns_groups_as_structured_content(mcp: TestClient) -> None:
    query = {"group_by": "classification", "classification_dimension": "genres"}
    result = tool(mcp, "what_worked", {"slug": "funny-memes", "query": query})
    groups_found = {group["key"]: group for group in result["structuredContent"]["groups"]}
    assert groups_found["documentary"]["median"] == 200


def test_mcp_what_worked_without_dimension_returns_tool_error_naming_the_fix(
    mcp: TestClient,
) -> None:
    result = tool(
        mcp, "what_worked", {"slug": "funny-memes", "query": {"group_by": "classification"}}
    )
    assert result["isError"] and "get_classification_vocabulary" in result["content"][0]["text"]


def test_mcp_what_worked_is_read_only_and_declares_an_output_schema(app: FastAPI) -> None:
    listed = list(operation_specs(app).values())
    (found,) = [item for item in listed if item["name"] == "what_worked"]
    assert (
        found["annotations"]["readOnlyHint"] is True
        and found["annotations"]["openWorldHint"] is False
    )
    assert "groups" in found["outputSchema"]["properties"]


# Page


def test_page_lists_groups_with_numbers_and_a_small_group_warning(owner: TestClient) -> None:
    page = owner.get(PAGE).text
    assert "Posts 3 · Median 200 · Mean 200" in page and "Too few posts to read" in page


def test_page_with_chosen_grouping_shows_that_grouping(owner: TestClient) -> None:
    page = owner.get(PAGE, params={"by": "hook_length"}).text
    assert "2 to 3 s" in page and "Up to 2 s" in page


def test_page_in_portuguese_is_translated(owner: TestClient) -> None:
    page = owner.get(PAGE, params={"lang": "pt-BR"}).text
    assert "Poucas publicações para concluir" in page


def test_page_for_a_viewer_without_channel_is_not_found(sign_up: Callable[..., TestClient]) -> None:
    stranger = sign_up("stranger@example.com", "Stranger")
    assert stranger.get(PAGE).status_code == 404
