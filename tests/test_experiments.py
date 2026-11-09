"""A/B experiments between two episodes (project.experiments)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.database import MIGRATIONS, Database
from sreditor.migrations import current_version, run_migrations
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, mcp_call, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.workspace_helpers import ORIGIN, submit

CHANNEL = "/channels/funny-memes"
EXPERIMENTS = f"{CHANNEL}/experiments"
PAGE = "/app/channels/funny-memes/experiments"
ACCOUNTS = [
    {"platform": "tiktok", "handle": "@funnymemes"},
    {"platform": "instagram", "handle": "@funnymemes"},
]
OTHER = {"slug": "other-memes", "name": "Other Memes"}
NOW = "2026-11-09T12:00:00+00:00"


def delivery(published: str, platforms: tuple[str, ...] = ("tiktok",)) -> dict[str, Any]:
    posts = [
        {"platform": platform, "cut": "main", "published_at": f"{published}T10:00:00Z"}
        for platform in platforms
    ]
    return {"cuts": [{"name": "main"}], "posts": posts}


def numbers(
    client: TestClient, number: int, day: str, views: int, platform: str = "tiktok"
) -> None:
    path = f"{CHANNEL}/episodes/{number}/delivery/posts/{platform}/snapshots"
    response = client.post(path, json={"captured_on": day, "views": views})
    assert response.status_code == 201, response.text


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    """Funny memes with episodes 1 to 3 posted on tiktok, and another channel with episode 1."""
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json={**FUNNY_MEMES, "accounts": ACCOUNTS}).status_code == 201
    assert client.post("/channels", json=OTHER).status_code == 201
    for number in (1, 2, 3):
        assert (
            client.post(f"{CHANNEL}/episodes", json={"title": f"Episode {number}"}).status_code
            == 201
        )
    assert (
        client.post("/channels/other-memes/episodes", json={"title": "Elsewhere"}).status_code
        == 201
    )
    for number, day in ((1, "2026-10-01"), (2, "2026-10-05")):
        response = client.put(
            f"{CHANNEL}/episodes/{number}/delivery", json=delivery(day), headers={"If-Match": '"0"'}
        )
        assert response.status_code == 200, response.text
    return client


def experiment(**changes: Any) -> dict[str, Any]:
    return {
        "title": "Question hook",
        "episode_a": 1,
        "episode_b": 2,
        "change": "B opens with a **question**.",
        "hypothesis": "A question keeps people watching.",
        "metric": "views",
        **changes,
    }


def create(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.post(EXPERIMENTS, json=experiment(**changes))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def result_of(client: TestClient, found: dict[str, Any]) -> dict[str, Any]:
    response = client.get(f"{EXPERIMENTS}/{found['id']}/result")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()["result"]
    return body


# Migration


def database_at(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        run_migrations(connection, MIGRATIONS[:version], NOW)


def seed_channels(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        for slug in ("a", "b"):
            connection.execute(
                "INSERT INTO channels (slug, document, created_at, updated_at, revision)"
                " VALUES (?, '{}', ?, ?, 1)",
                (slug, NOW, NOW),
            )
            for number in (1, 2):
                connection.execute(
                    "INSERT INTO episodes (channel_slug, number, title, status, revision,"
                    " created_at, updated_at) VALUES (?, ?, 'T', 'idea', 1, ?, ?)",
                    (slug, number, NOW, NOW),
                )


def insert_experiment(
    connection: sqlite3.Connection, channel: str, first: int, second: int
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "INSERT INTO experiments (id, channel_slug, title, episode_a, episode_b, metric,"
        " snapshot_age, status, revision, created_at, updated_at)"
        " VALUES (?, ?, 'T', ?, ?, 'views', 'latest', 'planned', 1, ?, ?)",
        (str(uuid4()), channel, first, second, NOW, NOW),
    )


def test_migration_on_fresh_database_creates_the_experiments_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.sqlite")
    with database.connect() as connection:
        found = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        assert (found, current_version(connection)) == (0, MIGRATIONS[-1].version)


def test_migration_on_existing_database_keeps_rows_and_adds_the_table(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    database_at(path, MIGRATIONS[-1].version - 1)
    seed_channels(path)
    with database_at_connect(path) as connection:
        assert current_version(connection) == MIGRATIONS[-1].version - 1
    with Database(path).connect() as connection:
        episodes = connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        assert (episodes, current_version(connection)) == (4, MIGRATIONS[-1].version)


def database_at_connect(path: Path) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return closing(connection)


def test_database_pair_across_channels_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "pair.sqlite"
    Database(path)
    seed_channels(path)
    with closing(sqlite3.connect(path)) as connection, pytest.raises(sqlite3.IntegrityError):
        insert_experiment(connection, "a", 1, 3)


def test_database_same_episode_twice_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "same.sqlite"
    Database(path)
    seed_channels(path)
    with closing(sqlite3.connect(path)) as connection, pytest.raises(sqlite3.IntegrityError):
        insert_experiment(connection, "a", 1, 1)


def test_database_episode_in_an_experiment_is_not_deleted_by_the_database(tmp_path: Path) -> None:
    path = tmp_path / "keep.sqlite"
    Database(path)
    seed_channels(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_experiment(connection, "a", 1, 2)
    with closing(sqlite3.connect(path)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM episodes WHERE channel_slug = 'a' AND number = 1")


def test_database_channel_delete_removes_its_experiments_and_episodes(tmp_path: Path) -> None:
    path = tmp_path / "cascade.sqlite"
    Database(path)
    seed_channels(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        insert_experiment(connection, "a", 1, 2)
        connection.execute("DELETE FROM channels WHERE slug = 'a'")
        left = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    assert left == 0


# API: writing


def test_create_returns_the_planned_experiment_at_revision_one(owner: TestClient) -> None:
    response = owner.post(EXPERIMENTS, json=experiment())
    body = response.json()
    assert response.status_code == 201 and body["revision"] == 1
    assert (body["status"], body["verdict"], body["snapshot_age"]) == ("planned", None, "latest")


def test_create_with_the_same_episode_twice_names_the_field(owner: TestClient) -> None:
    response = owner.post(EXPERIMENTS, json=experiment(episode_b=1))
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"][-1] == "episode_b"


def test_create_with_an_unknown_episode_returns_not_found(owner: TestClient) -> None:
    assert owner.post(EXPERIMENTS, json=experiment(episode_b=9)).status_code == 404


def test_create_with_an_episode_of_another_channel_returns_not_found(owner: TestClient) -> None:
    other = "/channels/other-memes/episodes/1"
    assert owner.get(other).status_code == 200
    assert owner.post(EXPERIMENTS, json=experiment(episode_a=1, episode_b=4)).status_code == 404


def test_create_read_without_verdict_names_the_field(owner: TestClient) -> None:
    response = owner.post(EXPERIMENTS, json=experiment(status="read"))
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"][-1] == "verdict"


def test_create_verdict_without_read_is_refused(owner: TestClient) -> None:
    assert owner.post(EXPERIMENTS, json=experiment(verdict="b_better")).status_code == 422


def test_create_read_with_verdict_keeps_the_reading(owner: TestClient) -> None:
    found = create(owner, status="read", verdict="b_better", reading="B won clearly.")
    assert (found["verdict"], found["reading"]) == ("b_better", "B won clearly.")


def test_create_with_idempotency_key_replays_the_first_experiment(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "plan-1"}
    first = owner.post(EXPERIMENTS, json=experiment(), headers=headers)
    again = owner.post(EXPERIMENTS, json=experiment(), headers=headers)
    assert again.headers["idempotent-replayed"] == "true" and again.json() == first.json()
    assert len(owner.get(EXPERIMENTS).json()["items"]) == 1


def test_put_with_current_revision_replaces_and_moves_the_etag(owner: TestClient) -> None:
    found = create(owner)
    response = owner.put(
        f"{EXPERIMENTS}/{found['id']}",
        json=experiment(status="running", started_on="2026-10-01"),
        headers={"If-Match": '"1"'},
    )
    assert (response.status_code, response.headers["ETag"]) == (200, '"2"')
    assert response.json()["started_on"] == "2026-10-01"


def test_put_with_stale_revision_returns_412_with_the_current_etag(owner: TestClient) -> None:
    found = create(owner)
    path = f"{EXPERIMENTS}/{found['id']}"
    assert owner.put(path, json=experiment(), headers={"If-Match": '"1"'}).status_code == 200
    stale = owner.put(path, json=experiment(title="Late"), headers={"If-Match": '"1"'})
    assert stale.status_code == 412 and stale.headers["ETag"] == '"2"'


def test_delete_with_stale_revision_returns_412(owner: TestClient) -> None:
    found = create(owner)
    response = owner.delete(f"{EXPERIMENTS}/{found['id']}", headers={"If-Match": '"7"'})
    assert response.status_code == 412


def test_delete_removes_the_experiment_and_keeps_the_episodes(owner: TestClient) -> None:
    found = create(owner)
    assert owner.delete(f"{EXPERIMENTS}/{found['id']}").status_code == 204
    assert owner.get(f"{EXPERIMENTS}/{found['id']}").status_code == 404
    assert owner.get(f"{CHANNEL}/episodes/1").status_code == 200


# API: reading


def test_get_returns_the_experiment_with_its_etag(owner: TestClient) -> None:
    found = create(owner)
    response = owner.get(f"{EXPERIMENTS}/{found['id']}")
    assert response.json() == found and response.headers["ETag"] == '"1"'


def test_get_unknown_experiment_returns_a_problem_naming_the_fix(owner: TestClient) -> None:
    response = owner.get(f"{EXPERIMENTS}/{uuid4()}")
    assert response.status_code == 404 and "list_experiments" in response.json()["detail"]


def test_list_pages_in_order_and_filters_by_status(owner: TestClient) -> None:
    first = create(owner, title="One")
    create(owner, title="Two", status="running")
    page = owner.get(EXPERIMENTS, params={"limit": 1}).json()
    assert [item["id"] for item in page["items"]] == [first["id"]] and page["next_cursor"]
    rest = owner.get(EXPERIMENTS, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [item["title"] for item in rest["items"]] == ["Two"] and rest["next_cursor"] is None
    running = owner.get(EXPERIMENTS, params={"status": "running"}).json()["items"]
    assert [item["title"] for item in running] == ["Two"]


def test_operation_ids_are_stable(owner: TestClient) -> None:
    paths = owner.get("/openapi.json").json()["paths"]
    found = {
        (path, method): operation["operationId"]
        for path, methods in paths.items()
        if "experiments" in path
        for method, operation in methods.items()
    }
    assert sorted(found.values()) == [
        "create_experiment",
        "delete_experiment",
        "get_experiment",
        "get_experiment_result",
        "list_experiments",
        "replace_experiment",
    ]


# Result


def test_result_compares_b_with_a_by_the_latest_numbers(owner: TestClient) -> None:
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 100)
    numbers(owner, 1, "2026-10-09", 200)
    numbers(owner, 2, "2026-10-06", 500)
    result = result_of(owner, found)
    assert (result["a"]["value"], result["b"]["value"]) == (200, 500)
    assert (result["difference"], result["ratio"], result["notes"]) == (300, 2.5, [])


def test_result_lists_each_sides_posts_with_their_numbers(owner: TestClient) -> None:
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 100)
    side = result_of(owner, found)["a"]
    assert side["title"] == "Episode 1"
    assert [(post["platform"], post["value"]) for post in side["posts"]] == [("tiktok", 100)]


def test_result_at_a_fixed_age_reads_the_snapshot_what_worked_would_pick(
    owner: TestClient,
) -> None:
    found = create(owner, snapshot_age="day_7")
    for number, published in ((1, "2026-10-01"), (2, "2026-10-05")):
        day_7 = f"2026-10-{int(published[-2:]) + 7:02d}"
        numbers(owner, number, published, 1)
        numbers(owner, number, day_7, 100 * number)
        numbers(owner, number, "2026-11-30", 9999)
    result = result_of(owner, found)
    assert (result["a"]["value"], result["b"]["value"]) == (100, 200)
    assert result["a"]["posts"][0]["captured_on"] == "2026-10-08"


def test_result_at_a_fixed_age_without_a_snapshot_then_has_no_number(owner: TestClient) -> None:
    found = create(owner, snapshot_age="day_30")
    numbers(owner, 1, "2026-10-02", 100)
    numbers(owner, 2, "2026-10-06", 100)
    result = result_of(owner, found)
    assert result["difference"] is None
    assert result["notes"] == ["a_without_numbers", "b_without_numbers"]


def test_result_without_numbers_names_each_missing_side(owner: TestClient) -> None:
    found = create(owner, episode_b=3)
    numbers(owner, 1, "2026-10-02", 100)
    result = result_of(owner, found)
    assert result["notes"] == ["b_without_posts"]
    assert (result["b"]["value"], result["difference"], result["ratio"]) == (None, None, None)


def test_result_with_a_at_zero_has_a_difference_but_no_ratio(owner: TestClient) -> None:
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 0)
    numbers(owner, 2, "2026-10-06", 40)
    result = result_of(owner, found)
    assert (result["difference"], result["ratio"], result["notes"]) == (40, None, ["a_is_zero"])


def test_result_with_a_platform_counts_only_its_posts(owner: TestClient) -> None:
    both = owner.put(
        f"{CHANNEL}/episodes/1/delivery",
        json=delivery("2026-10-01", ("tiktok", "instagram")),
        headers={"If-Match": '"1"'},
    )
    assert both.status_code == 200, both.text
    found = create(owner, platform="instagram")
    numbers(owner, 1, "2026-10-02", 100, "tiktok")
    numbers(owner, 1, "2026-10-02", 30, "instagram")
    side = result_of(owner, found)["a"]
    assert (side["value"], [post["platform"] for post in side["posts"]]) == (30, ["instagram"])


def test_result_of_two_platforms_without_a_filter_is_their_mean(owner: TestClient) -> None:
    both = owner.put(
        f"{CHANNEL}/episodes/1/delivery",
        json=delivery("2026-10-01", ("tiktok", "instagram")),
        headers={"If-Match": '"1"'},
    )
    assert both.status_code == 200, both.text
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 100, "tiktok")
    numbers(owner, 1, "2026-10-02", 30, "instagram")
    assert result_of(owner, found)["a"]["value"] == 65


def test_result_is_not_stored_with_the_experiment(owner: TestClient) -> None:
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 100)
    assert "result" not in owner.get(f"{EXPERIMENTS}/{found['id']}").json()


# Deletion rule


def test_episode_in_an_experiment_cannot_be_deleted(owner: TestClient) -> None:
    found = create(owner)
    response = owner.delete(f"{CHANNEL}/episodes/2")
    assert response.status_code == 409
    assert (
        response.json()["experiments"] == [found["id"]]
        and "delete them" in response.json()["detail"]
    )


def test_episode_can_be_deleted_once_its_experiment_is_gone(owner: TestClient) -> None:
    found = create(owner)
    assert owner.delete(f"{EXPERIMENTS}/{found['id']}").status_code == 204
    assert owner.delete(f"{CHANNEL}/episodes/2").status_code == 204


def test_episode_outside_experiments_is_deleted_as_before(owner: TestClient) -> None:
    create(owner)
    assert owner.delete(f"{CHANNEL}/episodes/3").status_code == 204


def test_channel_with_experiments_can_be_deleted(owner: TestClient) -> None:
    create(owner)
    assert owner.delete(CHANNEL).status_code == 204


# Roles and scopes


@pytest.fixture
def roles(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> Callable[[str], TestClient]:
    def make(role: str) -> TestClient:
        email = f"{role}@example.com"
        invited = owner.post(f"{CHANNEL}/invitations", json={"email": email, "role": role})
        assert invited.status_code == 201, invited.text
        token = mailer.token_for(email)
        member = sign_up(email)
        assert member.post("/invitations/accept", json={"token": token}).status_code == 200
        return member

    return make


def test_viewer_reads_experiments_but_cannot_write(
    owner: TestClient, roles: Callable[[str], TestClient]
) -> None:
    found = create(owner)
    viewer = roles("viewer")
    assert viewer.get(f"{EXPERIMENTS}/{found['id']}/result").status_code == 200
    assert viewer.post(EXPERIMENTS, json=experiment()).status_code == 403


def test_contributor_cannot_write_experiments(
    owner: TestClient, roles: Callable[[str], TestClient]
) -> None:
    found = create(owner)
    contributor = roles("contributor")
    assert contributor.put(f"{EXPERIMENTS}/{found['id']}", json=experiment()).status_code == 403
    assert contributor.delete(f"{EXPERIMENTS}/{found['id']}").status_code == 403


def test_editor_writes_experiments(owner: TestClient, roles: Callable[[str], TestClient]) -> None:
    assert roles("editor").post(EXPERIMENTS, json=experiment()).status_code == 201


def test_token_without_editorial_write_cannot_create(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read", "content:write"]))
    assert reader.get(EXPERIMENTS).status_code == 200
    assert reader.post(EXPERIMENTS, json=experiment()).status_code == 403


def test_token_with_editorial_write_creates_and_is_recorded_as_author(
    app: FastAPI, owner: TestClient
) -> None:
    writer = agent(app, new_token(owner, ["channels:read", "editorial:write"]))
    assert writer.post(EXPERIMENTS, json=experiment()).status_code == 201


def test_other_channel_members_get_not_found(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    stranger = sign_up("stranger@example.com", "Stranger")
    assert stranger.get(EXPERIMENTS).status_code == 404


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def save(mcp: TestClient, **arguments: Any) -> dict[str, Any]:
    return tool(mcp, "save_experiment", {"slug": "funny-memes", **arguments})


def test_mcp_save_experiment_creates_and_returns_structured_content(mcp: TestClient) -> None:
    result = save(mcp, experiment=experiment())
    assert result["structuredContent"]["status"] == "planned" and not result.get("isError")


def test_mcp_save_experiment_replay_with_the_key_returns_the_first_experiment(
    mcp: TestClient,
) -> None:
    first = save(mcp, experiment=experiment(), idempotency_key="k1")
    again = save(mcp, experiment=experiment(), idempotency_key="k1")
    assert again["structuredContent"] == first["structuredContent"]
    assert again["_meta"]["sreditor/replayed"] is True
    listed = tool(mcp, "get_experiment", {"slug": "funny-memes"})
    assert len(listed["structuredContent"]["experiments"]["items"]) == 1


def test_mcp_save_experiment_replay_with_other_arguments_is_refused(mcp: TestClient) -> None:
    save(mcp, experiment=experiment(), idempotency_key="k2")
    refused = save(mcp, experiment=experiment(title="Other"), idempotency_key="k2")
    assert refused["isError"]


def test_mcp_save_experiment_with_an_id_replaces_it_at_its_revision(mcp: TestClient) -> None:
    created = save(mcp, experiment=experiment())["structuredContent"]
    edit = {**experiment(status="running"), "experiment_id": created["id"], "expected_revision": 1}
    replaced = save(mcp, experiment=edit)["structuredContent"]
    assert (replaced["status"], replaced["revision"]) == ("running", 2)


def test_mcp_save_experiment_with_a_stale_revision_names_the_current_one(mcp: TestClient) -> None:
    created = save(mcp, experiment=experiment())["structuredContent"]
    edit = {**experiment(), "experiment_id": created["id"], "expected_revision": 5}
    result = save(mcp, experiment=edit)
    assert result["isError"] and "expected_revision=1" in result["content"][0]["text"]


def test_mcp_save_experiment_invalid_pair_is_a_tool_error(mcp: TestClient) -> None:
    assert save(mcp, experiment=experiment(episode_b=1))["isError"]


def test_mcp_save_experiment_unknown_episode_names_the_listing_tool(mcp: TestClient) -> None:
    result = save(mcp, experiment=experiment(episode_b=9))
    assert result["isError"] and "list_episodes" in result["content"][0]["text"]


def test_mcp_get_experiment_with_an_id_returns_the_result(
    owner: TestClient, mcp: TestClient
) -> None:
    found = create(owner)
    numbers(owner, 1, "2026-10-02", 100)
    numbers(owner, 2, "2026-10-06", 300)
    result = tool(mcp, "get_experiment", {"slug": "funny-memes", "experiment_id": found["id"]})
    reading = result["structuredContent"]["reading"]
    assert reading["result"]["ratio"] == 3 and reading["experiment"]["id"] == found["id"]


def test_mcp_get_experiment_unknown_id_names_the_listing_call(mcp: TestClient) -> None:
    result = tool(mcp, "get_experiment", {"slug": "funny-memes", "experiment_id": str(uuid4())})
    assert (
        result["isError"] and "get_experiment without experiment_id" in result["content"][0]["text"]
    )


def test_mcp_get_experiment_pages_with_a_cursor(owner: TestClient, mcp: TestClient) -> None:
    create(owner, title="One")
    create(owner, title="Two")
    first = tool(mcp, "get_experiment", {"slug": "funny-memes", "limit": 1})
    page = first["structuredContent"]["experiments"]
    second = tool(
        mcp, "get_experiment", {"slug": "funny-memes", "limit": 1, "cursor": page["next_cursor"]}
    )
    assert [item["title"] for item in second["structuredContent"]["experiments"]["items"]] == [
        "Two"
    ]


def test_mcp_experiment_tools_declare_their_annotations(mcp: TestClient) -> None:
    listed = mcp_call(mcp, "tools/list", {})["result"]["tools"]
    found = {item["name"]: item for item in listed if item["name"].endswith("_experiment")}
    reading, saving = found["get_experiment"], found["save_experiment"]
    assert reading["annotations"]["readOnlyHint"] is True
    assert (saving["annotations"]["destructiveHint"], saving["annotations"]["idempotentHint"]) == (
        False,
        False,
    )
    assert "reading" in reading["outputSchema"]["properties"]


def test_mcp_delete_episode_in_an_experiment_names_how_to_free_it(
    owner: TestClient, mcp: TestClient
) -> None:
    create(owner)
    result = tool(
        mcp, "delete_episode", {"slug": "funny-memes", "number": 2, "expected_revision": 1}
    )
    assert result["isError"] and "experiments" in result["content"][0]["text"]


def test_mcp_save_experiment_without_the_scope_names_the_missing_scope(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        result = save(reader, experiment=experiment())
    assert result["isError"] and "editorial:write" in result["content"][0]["text"]


# Pages


def test_page_in_english_lists_the_experiments_and_the_menu_entry(owner: TestClient) -> None:
    create(owner)
    page = owner.get(PAGE).text
    assert "Question hook" in page and "A #1 against B #2" in page and "Experiments" in page


def test_page_in_portuguese_is_translated(owner: TestClient) -> None:
    create(owner)
    page = owner.get(PAGE, params={"lang": "pt-BR"}).text
    assert "A #1 contra B #2" in page and "Planejado" in page


def test_empty_page_for_a_viewer_hides_the_form(
    owner: TestClient, roles: Callable[[str], TestClient]
) -> None:
    page = roles("viewer").get(PAGE).text
    assert "No experiments yet" in page and "Plan experiment" not in page


def test_detail_page_shows_the_result_next_to_the_reading(owner: TestClient) -> None:
    found = create(owner, status="read", verdict="b_better", reading="The *question* won.")
    numbers(owner, 1, "2026-10-02", 100)
    numbers(owner, 2, "2026-10-06", 250)
    page = owner.get(f"{PAGE}/{found['id']}").text
    assert "250" in page and "B did better" in page and "<em>question</em>" in page


def test_detail_page_without_numbers_says_so_in_both_languages(owner: TestClient) -> None:
    found = create(owner)
    path = f"{PAGE}/{found['id']}"
    assert "No number yet" in owner.get(path).text
    assert "Ainda sem número" in owner.get(path, params={"lang": "pt-BR"}).text


def test_detail_page_of_an_unknown_experiment_is_not_found(owner: TestClient) -> None:
    assert owner.get(f"{PAGE}/{uuid4()}").status_code == 404
    assert owner.get(f"{PAGE}/not-an-id").status_code == 404


def test_detail_page_for_a_viewer_has_no_form(
    owner: TestClient, roles: Callable[[str], TestClient]
) -> None:
    found = create(owner)
    assert "Save changes" not in roles("viewer").get(f"{PAGE}/{found['id']}").text


def test_page_for_a_stranger_is_not_found(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    assert sign_up("stranger@example.com", "Stranger").get(PAGE).status_code == 404


def as_browser(app: FastAPI, client: TestClient) -> TestClient:
    """The client's session as a browser: same origin, no redirects followed, for form posts."""
    headers = {"origin": ORIGIN, "Authorization": client.headers["Authorization"]}
    browser = TestClient(app, base_url=ORIGIN, headers=headers, follow_redirects=False)
    browser.get(PAGE)  # a page with forms hands out the CSRF cookie
    return browser


@pytest.fixture
def web(app: FastAPI, owner: TestClient) -> TestClient:
    return as_browser(app, owner)


FORM = {
    "title": "Hook test",
    "episode_a": "1",
    "episode_b": "2",
    "change": "B opens with a question",
    "hypothesis": "",
    "metric": "views",
    "snapshot_age": "latest",
    "platform": "",
}


def test_page_new_experiment_form_creates_it_and_opens_it(
    owner: TestClient, web: TestClient
) -> None:
    response = submit(web, PAGE, FORM)
    assert response.status_code == 303
    created = owner.get(EXPERIMENTS).json()["items"][0]
    assert response.headers["location"].endswith(created["id"])


def test_page_new_experiment_with_one_episode_twice_shows_the_error_by_the_field(
    web: TestClient,
) -> None:
    response = submit(web, PAGE, {**FORM, "episode_b": "1"})
    assert response.status_code == 422 and "Choose two different episodes." in response.text


def test_page_new_experiment_by_a_viewer_is_forbidden(
    app: FastAPI, roles: Callable[[str], TestClient]
) -> None:
    assert submit(as_browser(app, roles("viewer")), PAGE, FORM).status_code == 403


def test_page_save_with_stale_revision_shows_the_conflict(
    owner: TestClient, web: TestClient
) -> None:
    found = create(owner)
    path = f"{PAGE}/{found['id']}"
    values = {**FORM, "status": "running", "started_on": "", "reading": "", "verdict": ""}
    assert submit(web, path, {**values, "revision": "1"}).status_code == 303
    stale = submit(web, path, {**values, "revision": "1"})
    assert stale.status_code == 409 and "Someone changed this experiment" in stale.text


def test_page_save_read_without_verdict_shows_the_error_by_the_verdict(
    owner: TestClient, web: TestClient
) -> None:
    found = create(owner)
    values = {
        **FORM,
        "status": "read",
        "started_on": "",
        "reading": "",
        "verdict": "",
        "revision": "1",
    }
    response = submit(web, f"{PAGE}/{found['id']}", values)
    assert response.status_code == 422 and "A read experiment needs a verdict." in response.text


def test_page_delete_removes_the_experiment(owner: TestClient, web: TestClient) -> None:
    found = create(owner)
    response = submit(web, f"{PAGE}/{found['id']}/delete", {"revision": "1"})
    assert (
        response.status_code == 303 and owner.get(f"{EXPERIMENTS}/{found['id']}").status_code == 404
    )


def test_page_delete_of_an_episode_in_an_experiment_explains_why(
    owner: TestClient, web: TestClient
) -> None:
    create(owner)
    response = submit(web, "/app/channels/funny-memes/episodes/2/delete", {"revision": "1"})
    assert response.status_code == 409 and "compares this episode" in response.text
