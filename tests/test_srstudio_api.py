"""srstudio sheets, episode slugs and relationships through the API (rules project.srstudio.*)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import agent, block, new_token
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

CHANNEL = "/channels/funny-memes"
EPISODES = f"{CHANNEL}/episodes"
RELATIONSHIPS = f"{CHANNEL}/relationships"
DOG_SHEET = {"kind": "character", "world": "okayville", "rig": "toon-dog"}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


def created(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


@pytest.fixture
def rex(owner: TestClient) -> str:
    return str(created(owner, f"{CHANNEL}/characters", {"name": "Rex"})["id"])


@pytest.fixture
def fern(owner: TestClient) -> str:
    return str(created(owner, f"{CHANNEL}/characters", {"name": "Fern"})["id"])


@pytest.fixture
def published(owner: TestClient) -> int:
    """Episode 1, published, so facts may cite it."""
    created(owner, EPISODES, {"title": "Whose Milk"})
    owner.put(f"{EPISODES}/1", json={"title": "Whose Milk", "status": "published"})
    return 1


def fact(episode: int) -> dict[str, Any]:
    return {"fact": "Rex labels his milk.", "field": "identity", "episode": episode,
            "kind": "line", "quote": "Mine. It's labeled."}  # fmt: skip


def test_character_sheet_unsaved_is_proposal(owner: TestClient, rex: str) -> None:
    response = owner.get(f"{CHANNEL}/characters/{rex}/srstudio")
    body = response.json()
    assert response.headers["etag"] == '"0"' and body["slug"] == "rex" and body["revision"] == 0


def test_character_sheet_put_saves_and_returns_etag(owner: TestClient, rex: str) -> None:
    body = {"slug": "rex", "sheet": DOG_SHEET}
    response = owner.put(
        f"{CHANNEL}/characters/{rex}/srstudio", json=body, headers={"If-Match": '"0"'}
    )
    assert response.headers["etag"] == '"1"' and response.json()["sheet"]["rig"] == "toon-dog"


def test_character_sheet_with_place_kind_is_refused(owner: TestClient, rex: str) -> None:
    body = {"slug": "rex", "sheet": {"kind": "location"}}
    response = owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body)
    assert response.status_code == 422 and response.json()["errors"][0]["location"][-1] == "sheet"


def test_character_sheet_bad_slug_is_refused(owner: TestClient, rex: str) -> None:
    body = {"slug": "Rex Dog", "sheet": DOG_SHEET}
    assert owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body).status_code == 422


def test_character_sheet_taken_slug_answers_conflict(
    owner: TestClient, rex: str, fern: str
) -> None:
    owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json={"slug": "rex", "sheet": DOG_SHEET})
    response = owner.put(
        f"{CHANNEL}/characters/{fern}/srstudio", json={"slug": "rex", "sheet": DOG_SHEET}
    )
    assert response.status_code == 409 and response.json()["type"].endswith("slug-taken")


def test_character_fact_citing_unpublished_episode_is_refused(owner: TestClient, rex: str) -> None:
    created(owner, EPISODES, {"title": "Draft"})
    body = {"slug": "rex", "sheet": {**DOG_SHEET, "canon": [fact(1)]}}
    response = owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body)
    assert response.status_code == 422 and response.json()["type"].endswith("unpublished-episode")


def test_character_rig_outside_toon_cast_is_refused_naming_the_field(
    owner: TestClient, rex: str
) -> None:
    body = {"slug": "rex", "sheet": {**DOG_SHEET, "rig": "toon-host"}}
    response = owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body)
    problem = response.json()
    assert response.status_code == 422 and problem["type"].endswith("unknown-rig")
    assert problem["errors"][0]["location"] == ["body", "sheet", "sheet", "rig"]


def test_character_fact_citing_published_episode_is_kept(
    owner: TestClient, rex: str, published: int
) -> None:
    body = {"slug": "rex", "sheet": {**DOG_SHEET, "canon": [fact(published)]}}
    response = owner.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body)
    assert response.json()["sheet"]["canon"][0]["quote"] == "Mine. It's labeled."


def test_style_sheet_keeps_srstudio_style_and_palette(owner: TestClient) -> None:
    style = created(owner, f"{CHANNEL}/styles", {"name": "Warm toon"})["id"]
    palette = {"ink": "#1a1a1a", "white": "#ffffff", "shade": "#cccccc", "background": "#fdf7eb"}
    body = {"slug": "warm-toon", "sheet": {"kind": "style", "style": "toon", "palette": palette}}
    response = owner.put(f"{CHANNEL}/styles/{style}/srstudio", json=body)
    assert response.json()["sheet"]["palette"]["background"] == "#fdf7eb"


def test_prop_sheet_keeps_rig(owner: TestClient) -> None:
    laptop = created(owner, f"{CHANNEL}/props", {"name": "Laptop"})["id"]
    body = {"slug": "laptop", "sheet": {"kind": "prop", "rig": "laptop"}}
    assert (
        owner.put(f"{CHANNEL}/props/{laptop}/srstudio", json=body).json()["sheet"]["rig"]
        == "laptop"
    )


def test_sheet_put_by_viewer_is_forbidden(
    owner: TestClient, rex: str, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    body = {"slug": "rex", "sheet": DOG_SHEET}
    assert viewer.put(f"{CHANNEL}/characters/{rex}/srstudio", json=body).status_code == 403


def test_sheet_put_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, rex: str, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "character_sheet.write")
    body = {"slug": "rex", "sheet": DOG_SHEET}
    response = agent(app, new_token(owner)).put(f"{CHANNEL}/characters/{rex}/srstudio", json=body)
    assert response.status_code == 403


def test_episode_slug_proposed_then_saved(owner: TestClient) -> None:
    created(owner, EPISODES, {"title": "Whose Milk"})
    assert owner.get(f"{EPISODES}/1/srstudio").json()["slug"] == "whose-milk"
    saved = owner.put(
        f"{EPISODES}/1/srstudio", json={"slug": "milk", "seed": 42}, headers={"If-Match": '"0"'}
    )
    assert saved.headers["etag"] == '"1"' and saved.json()["seed"] == 42


def test_episode_slug_of_missing_episode_answers_not_found(owner: TestClient) -> None:
    assert owner.get(f"{EPISODES}/9/srstudio").status_code == 404


def test_relationship_create_list_replace_delete(owner: TestClient, rex: str, fern: str) -> None:
    pair = created(owner, RELATIONSHIPS, {"first": rex, "second": fern, "relation": "Coworkers"})
    assert owner.get(RELATIONSHIPS).json()[0]["relation"] == "Coworkers"
    body = {"first": rex, "second": fern, "relation": "Boss and employee"}
    replaced = owner.put(f"{RELATIONSHIPS}/{pair['id']}", json=body, headers={"If-Match": '"1"'})
    assert replaced.headers["etag"] == '"2"'
    assert owner.delete(f"{RELATIONSHIPS}/{pair['id']}").status_code == 204


def test_relationship_with_same_character_is_refused(owner: TestClient, rex: str) -> None:
    response = owner.post(RELATIONSHIPS, json={"first": rex, "second": rex})
    assert response.status_code == 422 and response.json()["errors"][0]["location"][-1] == "second"


def test_relationship_with_location_answers_not_found(owner: TestClient, rex: str) -> None:
    place = created(owner, f"{CHANNEL}/locations", {"name": "Office"})["id"]
    assert owner.post(RELATIONSHIPS, json={"first": rex, "second": place}).status_code == 404


def test_relationship_missing_answers_not_found(owner: TestClient, rex: str) -> None:
    missing = f"{RELATIONSHIPS}/00000000-0000-0000-0000-000000000000"
    assert owner.delete(missing).status_code == 404


def test_relationship_delete_by_contributor_is_forbidden(
    owner: TestClient,
    rex: str,
    fern: str,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
) -> None:
    pair = created(owner, RELATIONSHIPS, {"first": rex, "second": fern})
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.delete(f"{RELATIONSHIPS}/{pair['id']}").status_code == 403
