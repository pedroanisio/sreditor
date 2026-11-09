"""Dated snapshots of a post's numbers, CSV import and the channel comparison (project.metrics)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, operation_specs, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1"
DELIVERY = f"{EPISODE}/delivery"
SNAPSHOTS = f"{DELIVERY}/posts/tiktok/snapshots"
RETENTION = f"{DELIVERY}/posts/tiktok/retention-csv"
COMPARE = "/channels/funny-memes/post-metrics"
SECOND_DELIVERY = "/channels/funny-memes/episodes/2/delivery"
SECOND_SNAPSHOTS = f"{SECOND_DELIVERY}/posts/tiktok/snapshots"
CSV = "second,percent\n0,100\n1,90\n2,75\n"
FIRST_DAY = "2026-11-01"
SECOND_DAY = "2026-11-02"


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    for title in ("Pilot", "Second"):
        assert (
            client.post("/channels/funny-memes/episodes", json={"title": title}).status_code == 201
        )
    cuts = {"cuts": [{"name": "main"}], "posts": [{"platform": "tiktok", "cut": "main"}]}
    assert client.put(DELIVERY, json=cuts).status_code == 200
    return client


def numbers(**values: Any) -> dict[str, Any]:
    return {"captured_on": FIRST_DAY, **values}


# Posts of two cuts on one account (versions A and B of a test)


def add_second_cut_post(client: TestClient) -> None:
    cuts = {
        "cuts": [{"name": "main"}, {"name": "alt"}],
        "posts": [{"platform": "tiktok", "cut": "main"}],
    }
    assert client.put(DELIVERY, json=cuts).status_code == 200
    assert client.put(f"{DELIVERY}/posts/tiktok", json={"cut": "alt"}).status_code == 200


def test_snapshot_record_for_each_cut_keeps_both_posts_numbers(owner: TestClient) -> None:
    add_second_cut_post(owner)
    owner.post(SNAPSHOTS, params={"cut": "main"}, json=numbers(views=10))
    owner.post(SNAPSHOTS, params={"cut": "alt"}, json=numbers(views=99))
    first = owner.get(SNAPSHOTS, params={"cut": "main"}).json()["items"]
    second = owner.get(SNAPSHOTS, params={"cut": "alt"}).json()["items"]
    assert [(item["cut"], item["views"]) for item in [*first, *second]] == [
        ("main", 10),
        ("alt", 99),
    ]


def test_snapshot_record_without_cut_when_platform_has_two_posts_is_refused_naming_cuts(
    owner: TestClient,
) -> None:
    add_second_cut_post(owner)
    response = owner.post(SNAPSHOTS, json=numbers(views=1))
    assert response.status_code == 422 and "alt, main" in response.json()["detail"]


def test_snapshot_record_for_cut_that_was_not_posted_is_not_found(owner: TestClient) -> None:
    response = owner.post(SNAPSHOTS, params={"cut": "ghost"}, json=numbers(views=1))
    assert response.status_code == 404


def test_snapshot_record_without_cut_uses_the_platforms_only_post(owner: TestClient) -> None:
    response = owner.post(SNAPSHOTS, json=numbers(views=3))
    assert response.json()["cut"] == "main"


def test_compare_posts_lists_each_cut_of_a_platform_as_its_own_post(owner: TestClient) -> None:
    add_second_cut_post(owner)
    owner.post(SNAPSHOTS, params={"cut": "alt"}, json=numbers(views=99))
    rows = owner.get(COMPARE).json()["items"]
    assert [row["cut"] for row in rows if row["number"] == 1] == ["alt", "main"]


def test_mcp_record_post_metrics_for_cut_stores_it_under_that_cut(
    owner: TestClient, mcp: TestClient
) -> None:
    add_second_cut_post(owner)
    snapshot = {"platform": "tiktok", "cut": "alt", "views": 7}
    result = tool(mcp, "record_post_metrics", args(snapshot=snapshot))
    assert result["structuredContent"]["cut"] == "alt"


def test_mcp_record_post_metrics_without_cut_names_the_cuts_to_choose_from(
    owner: TestClient, mcp: TestClient
) -> None:
    add_second_cut_post(owner)
    result = tool(mcp, "record_post_metrics", args(snapshot={"platform": "tiktok", "views": 7}))
    assert result["isError"] and "alt, main" in result["content"][0]["text"]


# Snapshots


def test_snapshot_record_returns_created_with_numbers(owner: TestClient) -> None:
    response = owner.post(SNAPSHOTS, json=numbers(views=120, likes=7, avg_watch_s=3.5))
    body = response.json()
    assert response.status_code == 201, response.text
    assert (body["views"], body["likes"], body["reach"], body["avg_watch_s"]) == (120, 7, None, 3.5)


def test_snapshot_record_without_day_uses_today(owner: TestClient) -> None:
    body = owner.post(SNAPSHOTS, json={"views": 1}).json()
    assert body["captured_on"] == owner.get(SNAPSHOTS).json()["items"][0]["captured_on"]


def test_snapshot_record_same_day_merges_and_keeps_omitted_numbers(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json=numbers(views=100, likes=5))
    merged = owner.post(SNAPSHOTS, json=numbers(views=150, shares=2)).json()
    assert (merged["views"], merged["likes"], merged["shares"]) == (150, 5, 2)


def test_snapshot_record_zero_is_a_real_value(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json=numbers(views=100))
    assert owner.post(SNAPSHOTS, json=numbers(views=0)).json()["views"] == 0


def test_snapshot_record_negative_number_is_refused(owner: TestClient) -> None:
    assert owner.post(SNAPSHOTS, json=numbers(views=-1)).status_code == 422


def test_snapshot_record_retention_is_sorted_by_second(owner: TestClient) -> None:
    retention = [{"second": 2, "percent": 50}, {"second": 0, "percent": 100}]
    body = owner.post(SNAPSHOTS, json=numbers(retention=retention)).json()
    assert [point["second"] for point in body["retention"]] == [0, 2]


def test_snapshot_record_repeated_retention_second_is_refused(owner: TestClient) -> None:
    retention = [{"second": 1, "percent": 50}, {"second": 1, "percent": 40}]
    assert owner.post(SNAPSHOTS, json=numbers(retention=retention)).status_code == 422


def test_snapshot_record_on_platform_without_post_returns_not_found(owner: TestClient) -> None:
    response = owner.post(f"{DELIVERY}/posts/youtube/snapshots", json=numbers(views=1))
    assert response.status_code == 404 and "record_post" in response.json()["detail"]


def test_snapshot_record_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "snap-1"}
    first = owner.post(SNAPSHOTS, json=numbers(views=1), headers=headers)
    again = owner.post(SNAPSHOTS, json=numbers(views=1), headers=headers)
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()


def test_snapshot_list_is_newest_day_first(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json={"captured_on": FIRST_DAY, "views": 1})
    owner.post(SNAPSHOTS, json={"captured_on": SECOND_DAY, "views": 2})
    days = [item["captured_on"] for item in owner.get(SNAPSHOTS).json()["items"]]
    assert days == [SECOND_DAY, FIRST_DAY]


def test_snapshot_list_pages_with_cursor(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json={"captured_on": FIRST_DAY, "views": 1})
    owner.post(SNAPSHOTS, json={"captured_on": SECOND_DAY, "views": 2})
    first = owner.get(SNAPSHOTS, params={"limit": 1}).json()
    second = owner.get(SNAPSHOTS, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["captured_on"] == FIRST_DAY and second["next_cursor"] is None


def test_snapshot_list_with_broken_cursor_returns_bad_request(owner: TestClient) -> None:
    assert owner.get(SNAPSHOTS, params={"cursor": "bm9wZQ"}).status_code == 400


def test_snapshot_record_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(SNAPSHOTS, json=numbers(views=1)).status_code == 403
    assert viewer.get(SNAPSHOTS).status_code == 200


# CSV import


def test_retention_csv_import_stores_points_for_the_day(owner: TestClient) -> None:
    response = owner.post(RETENTION, json={"csv": CSV, "captured_on": FIRST_DAY})
    assert response.status_code == 201, response.text
    assert [point["percent"] for point in response.json()["retention"]] == [100, 90, 75]


def test_retention_csv_import_keeps_the_days_other_numbers(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json=numbers(views=10))
    body = owner.post(RETENTION, json={"csv": CSV, "captured_on": FIRST_DAY}).json()
    assert body["views"] == 10 and len(body["retention"]) == 3


def test_retention_csv_import_replaces_earlier_retention_of_the_day(owner: TestClient) -> None:
    owner.post(RETENTION, json={"csv": CSV, "captured_on": FIRST_DAY})
    body = owner.post(RETENTION, json={"csv": "second,percent\n0,50\n", "captured_on": FIRST_DAY})
    assert len(body.json()["retention"]) == 1


def test_retention_csv_import_bad_row_returns_problem_with_location(owner: TestClient) -> None:
    response = owner.post(RETENTION, json={"csv": "second,percent\n0,x\n"})
    error = response.json()["errors"][0]
    assert response.status_code == 422
    assert error["location"] == ["csv", 2, "percent"] and "must be a number" in error["message"]


def test_retention_csv_import_bad_header_returns_unprocessable(owner: TestClient) -> None:
    assert owner.post(RETENTION, json={"csv": "a,b,c\n1,2,3"}).status_code == 422


def test_retention_csv_import_text_over_limit_returns_unprocessable(owner: TestClient) -> None:
    assert owner.post(RETENTION, json={"csv": "x" * 200_001}).status_code == 422


# Comparison


def test_compare_ranks_posts_by_the_chosen_number(owner: TestClient) -> None:
    owner.put(
        SECOND_DELIVERY,
        json={"cuts": [{"name": "teaser"}], "posts": [{"platform": "tiktok", "cut": "teaser"}]},
    )
    owner.post(SNAPSHOTS, json=numbers(views=10, likes=90))
    owner.post(
        SECOND_SNAPSHOTS,
        json=numbers(views=500, likes=1),
    )
    by_views = [row["number"] for row in owner.get(COMPARE).json()["items"]]
    by_likes = [
        row["number"] for row in owner.get(COMPARE, params={"sort_by": "likes"}).json()["items"]
    ]
    assert (by_views, by_likes) == ([2, 1], [1, 2])


def test_compare_row_carries_latest_snapshot_and_counts(owner: TestClient) -> None:
    owner.post(SNAPSHOTS, json={"captured_on": FIRST_DAY, "views": 1})
    owner.post(SNAPSHOTS, json={"captured_on": SECOND_DAY, "views": 9})
    row = owner.get(COMPARE).json()["items"][0]
    assert (row["latest"]["views"], row["snapshots"], row["cut"]) == (9, 2, "main")


def test_compare_post_without_numbers_comes_last_and_has_no_latest(owner: TestClient) -> None:
    owner.put(
        SECOND_DELIVERY,
        json={"cuts": [{"name": "teaser"}], "posts": [{"platform": "tiktok", "cut": "teaser"}]},
    )
    owner.post(SECOND_SNAPSHOTS, json=numbers(views=3))
    rows = owner.get(COMPARE).json()["items"]
    assert [row["number"] for row in rows] == [2, 1] and rows[1]["latest"] is None


def test_compare_pages_with_limit_and_cursor(owner: TestClient) -> None:
    owner.put(
        SECOND_DELIVERY,
        json={"cuts": [{"name": "teaser"}], "posts": [{"platform": "tiktok", "cut": "teaser"}]},
    )
    first = owner.get(COMPARE, params={"limit": 1}).json()
    second = owner.get(COMPARE, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert len(first["items"]) == len(second["items"]) == 1 and second["next_cursor"] is None


def test_compare_with_unknown_metric_is_invalid(owner: TestClient) -> None:
    assert owner.get(COMPARE, params={"sort_by": "fame"}).status_code == 422


def test_compare_with_broken_cursor_returns_bad_request(owner: TestClient) -> None:
    assert owner.get(COMPARE, params={"cursor": "bm9wZQ"}).status_code == 400


def test_compare_of_unknown_channel_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/nope/post-metrics").status_code == 404


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def args(**extra: Any) -> dict[str, Any]:
    return {"slug": "funny-memes", "number": 1, **extra}


def test_mcp_record_post_metrics_returns_snapshot(mcp: TestClient) -> None:
    snapshot = {"platform": "tiktok", "captured_on": FIRST_DAY, "views": 40}
    result = tool(mcp, "record_post_metrics", args(snapshot=snapshot))
    assert result["structuredContent"]["views"] == 40


def test_mcp_record_post_metrics_without_post_tells_to_record_it(mcp: TestClient) -> None:
    snapshot = {"platform": "youtube", "views": 1}
    result = tool(mcp, "record_post_metrics", args(snapshot=snapshot))
    assert result["isError"] and "record_post" in result["content"][0]["text"]


def test_mcp_record_post_metrics_same_key_replays_first_result(mcp: TestClient) -> None:
    snapshot = {"platform": "tiktok", "captured_on": FIRST_DAY, "views": 40}
    first = tool(mcp, "record_post_metrics", args(snapshot=snapshot, idempotency_key="k1"))
    again = tool(mcp, "record_post_metrics", args(snapshot=snapshot, idempotency_key="k1"))
    assert first["structuredContent"]["id"] == again["structuredContent"]["id"]


def test_mcp_import_retention_csv_returns_points(mcp: TestClient) -> None:
    source = {"platform": "tiktok", "csv": CSV, "captured_on": FIRST_DAY}
    result = tool(mcp, "import_retention_csv", args(source=source))
    assert len(result["structuredContent"]["retention"]) == 3


def test_mcp_import_retention_csv_bad_file_names_rows_and_shape(mcp: TestClient) -> None:
    source = {"platform": "tiktok", "csv": "second,percent\n0,oops\n"}
    result = tool(mcp, "import_retention_csv", args(source=source))
    text = result["content"][0]["text"]
    assert result["isError"] and "Row 2" in text and "second,percent" in text


def test_mcp_compare_posts_returns_a_page(mcp: TestClient) -> None:
    tool(mcp, "record_post_metrics", args(snapshot={"platform": "tiktok", "views": 5}))
    result = tool(mcp, "compare_posts", {"slug": "funny-memes", "sort_by": "views"})
    page = result["structuredContent"]
    assert page["items"][0]["latest"]["views"] == 5 and page["next_cursor"] is None


def test_mcp_metrics_tools_declare_all_four_annotations(app: FastAPI) -> None:
    tools = list(operation_specs(app).values())
    wanted = {"record_post_metrics", "import_retention_csv", "compare_posts"}
    found = [item for item in tools if item["name"] in wanted]
    keys = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    assert len(found) == len(wanted) and all(keys <= item["annotations"].keys() for item in found)


def test_mcp_record_post_metrics_blocked_by_gate_returns_tool_error(
    app: FastAPI, owner: TestClient, app_settings: Any
) -> None:
    block(app_settings, "/funny-memes", "post_metrics.write")
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        snapshot = {"platform": "tiktok", "views": 1}
        assert tool(client, "record_post_metrics", args(snapshot=snapshot))["isError"]
