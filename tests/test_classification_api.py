"""Classification through the store, the API and the MCP tools (project.classification...)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.classification import (
    Classification,
    ClassificationStore,
    Dimension,
    StoredClassification,
)
from sreditor.database import Database
from sreditor.episode_texts import PartChanged, TextKey, TextPart
from sreditor.episodes import EpisodeInput, EpisodeStore
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1/classification"
PROFILE = "/channels/funny-memes/profile"
OBSERVED = PROFILE + "/observed"


def present(*values: str, confidence: float | None = None) -> dict[str, Any]:
    """A present dimension: the first value primary, the rest secondary."""
    items = [
        {"value": value, "role": "primary" if index == 0 else "secondary", "confidence": confidence}
        for index, value in enumerate(values)
    ]
    return {"state": "present", "items": items}


IRONIC = {"dimensions": {"tones": present("ironic")}}
SERIOUS = {"dimensions": {"tones": present("serious")}}


# Store


@pytest.fixture
def classifications(tmp_path: Path) -> ClassificationStore:
    """A store over a database with episodes 1 and 2 of funny-memes."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    episodes = EpisodeStore(database)
    episodes.create("funny-memes", EpisodeInput(title="Pilot"))
    episodes.create("funny-memes", EpisodeInput(title="Second"))
    return ClassificationStore(database)


def key(number: int = 1) -> TextKey:
    return TextKey(part=TextPart.CLASSIFICATION, channel="funny-memes", number=number)


def stored(document: dict[str, Any], is_reviewed: bool = True) -> StoredClassification:
    return StoredClassification(
        classification=Classification.model_validate(document), is_reviewed=is_reviewed
    )


def test_store_episode_never_written_is_revision_zero(classifications: ClassificationStore) -> None:
    found = classifications.episode("funny-memes", 1)
    assert (found.revision, found.is_reviewed) == (0, False)


def test_store_write_episode_then_read_returns_it(classifications: ClassificationStore) -> None:
    written = classifications.write_episode(key(), stored(IRONIC), 0)
    assert written.revision == 1 and written.is_reviewed
    assert written.classification.primary(Dimension.TONES) == "ironic"
    assert classifications.episode("funny-memes", 1).classification == written.classification


def test_store_write_episode_stale_revision_raises(classifications: ClassificationStore) -> None:
    classifications.write_episode(key(), stored(IRONIC), 0)
    with pytest.raises(PartChanged):
        classifications.write_episode(key(), stored(SERIOUS), 0)


def test_store_classified_lists_only_written_episodes(
    classifications: ClassificationStore,
) -> None:
    classifications.write_episode(key(2), stored(SERIOUS, is_reviewed=False), 0)
    found = classifications.classified("funny-memes")
    assert [(episode.number, episode.is_reviewed) for episode in found] == [(2, False)]


def test_store_profile_write_then_stale_raises(classifications: ClassificationStore) -> None:
    data = Classification.model_validate(SERIOUS)
    assert classifications.write_profile("funny-memes", data, 0).revision == 1
    with pytest.raises(PartChanged):
        classifications.write_profile("funny-memes", data, 0)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def test_vocabulary_lists_twelve_dimensions(owner: TestClient) -> None:
    found = owner.get("/classification/vocabulary").json()
    assert found["taxonomy_version"] == "1.0.0"
    assert len(found["dimensions"]) == 12
    assert "deadpan" in found["dimensions"]["humor_devices"]


def test_classify_episode_as_person_is_reviewed_with_etag(owner: TestClient) -> None:
    response = owner.put(EPISODE, json=IRONIC, headers={"If-Match": '"0"'})
    assert response.status_code == 200, response.text
    assert response.json()["is_reviewed"] is True
    assert response.headers["ETag"] == '"1"'
    assert owner.get(EPISODE).json()["classification"]["dimensions"]["tones"]["state"] == "present"


def test_classify_episode_stale_if_match_returns_precondition_failed(owner: TestClient) -> None:
    owner.put(EPISODE, json=IRONIC, headers={"If-Match": '"0"'})
    assert owner.put(EPISODE, json=SERIOUS, headers={"If-Match": '"0"'}).status_code == 412


def test_classify_episode_comedy_without_device_returns_field_errors(owner: TestClient) -> None:
    body = {"dimensions": {"genres": present("comedy")}}
    response = owner.put(EPISODE, json=body)
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"].endswith("classification-refused")
    assert problem["errors"][0]["location"] == ["body", "dimensions", "humor_devices"]


def test_classify_episode_with_token_is_unreviewed_proposal(
    app: FastAPI, owner: TestClient
) -> None:
    body = {**IRONIC, "method": "ai", "dimensions": {"tones": present("ironic", confidence=0.7)}}
    response = agent(app, new_token(owner)).put(EPISODE, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["is_reviewed"] is False


def test_classify_episode_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/", "classification.write")
    assert agent(app, new_token(owner)).put(EPISODE, json=IRONIC).status_code == 403


def test_classify_unknown_episode_returns_not_found(owner: TestClient) -> None:
    assert (
        owner.put("/channels/funny-memes/episodes/9/classification", json=IRONIC).status_code == 404
    )


def test_declare_profile_as_contributor_returns_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.put(PROFILE, json=SERIOUS).status_code == 403
    assert contributor.put(EPISODE, json=IRONIC).status_code == 200


def test_observed_profile_declared_value_rare_drifts(owner: TestClient) -> None:
    assert owner.put(PROFILE, json=SERIOUS, headers={"If-Match": '"0"'}).status_code == 200
    assert owner.get(PROFILE).headers["ETag"] == '"1"'
    owner.put(EPISODE, json=IRONIC)
    observed = owner.get(OBSERVED).json()
    assert observed["sample_size"] == 1
    assert [(found["value"], found["prevalence"]) for found in observed["drift"]] == [
        ("serious", 0.0)
    ]


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_vocabulary_lists_dimensions(mcp: TestClient) -> None:
    found = tool(mcp, "get_classification_vocabulary", {})["structuredContent"]
    assert "comedy" in found["dimensions"]["genres"]


def test_mcp_classify_episode_is_proposal(mcp: TestClient) -> None:
    classification = {"method": "ai", "dimensions": {"tones": present("warm", confidence=0.9)}}
    arguments = {"slug": "funny-memes", "number": 1, "classification": classification}
    written = tool(mcp, "classify_episode", arguments)["structuredContent"]
    assert (written["revision"], written["is_reviewed"]) == (1, False)
    read = tool(mcp, "get_episode_classification", {"slug": "funny-memes", "number": 1})
    assert read["structuredContent"]["revision"] == 1


def test_mcp_classify_rule_break_is_tool_error_naming_fix(mcp: TestClient) -> None:
    classification = {"method": "ai", "dimensions": {"tones": present("warm")}}
    arguments = {"slug": "funny-memes", "number": 1, "classification": classification}
    result = tool(mcp, "classify_episode", arguments)
    text = result["content"][0]["text"]
    assert result["isError"] and "dimensions.tones" in text
    assert "get_classification_vocabulary" in text


def test_mcp_channel_profile_declared_and_observed(mcp: TestClient, owner: TestClient) -> None:
    owner.put(EPISODE, json=IRONIC)
    found = tool(mcp, "get_channel_profile", {"slug": "funny-memes"})["structuredContent"]
    assert found["declared"]["revision"] == 0
    assert found["observed"]["sample_size"] == 1
