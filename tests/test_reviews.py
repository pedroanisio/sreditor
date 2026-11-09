"""An episode's reviews, the still media kind and SHA-256 on files (rule project.reviews)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.database import MIGRATIONS, Database
from sreditor.media import MediaMeasures
from sreditor.reviews import ReviewInput, SeenRange, Verdict, warning_text
from sreditor.settings import AppSettings
from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_delivery import DELIVERY, MEDIA, package, upload
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1"
REVIEWS = f"{EPISODE}/reviews"
ZERO_ID = "00000000-0000-0000-0000-000000000000"
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
APPROVED = {"verdict": "approved", "notes": "Looks **good**."}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def render_file(owner: TestClient) -> str:
    """The id of a video file of episode 1."""
    return upload(owner, "final_video", sample.MP4, "master.mp4")


def review(owner: TestClient, **body: Any) -> dict[str, Any]:
    response = owner.post(REVIEWS, json={**APPROVED, **body})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def publish(owner: TestClient) -> Any:
    return owner.put(EPISODE, json={"title": "Pilot", "summary": "", "status": "published"})


def services(app: FastAPI) -> Any:
    return app.state.services


# Models


def test_seen_range_ending_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError):
        SeenRange(start_s=3, end_s=3)


def test_review_input_ranges_without_a_file_are_refused() -> None:
    with pytest.raises(ValidationError):
        ReviewInput(verdict=Verdict.APPROVED, seen=[SeenRange(start_s=0, end_s=1)])


def test_review_input_with_too_many_ranges_is_refused() -> None:
    ranges = [SeenRange(start_s=index, end_s=index + 1) for index in range(51)]
    with pytest.raises(ValidationError):
        ReviewInput(verdict=Verdict.APPROVED, media_id=ZERO_ID, seen=ranges)  # type: ignore[arg-type]


def test_warning_text_without_a_review_names_add_review() -> None:
    assert "add_review" in str(warning_text(None))


# Migration


def test_migration_on_a_fresh_database_creates_the_review_tables(tmp_path: Path) -> None:
    Database(tmp_path / "fresh.sqlite")
    with closing(sqlite3.connect(tmp_path / "fresh.sqlite")) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"episode_reviews", "review_ranges"} <= tables


def test_migration_on_a_database_before_it_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    Database(path)
    previous = MIGRATIONS[-1].version - 1
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE review_ranges")
        connection.execute("DROP TABLE episode_reviews")
        connection.execute("DROP TRIGGER episode_media_forget_reviews")
        connection.execute("DELETE FROM schema_version WHERE version > ?", (previous,))
        connection.execute(
            "INSERT INTO users (id, email, display_name, password_hash, status, created_at)"
            " VALUES ('u', 'a@b.c', 'A', 'x', 'active', '2026-11-09T00:00:00+00:00')"
        )
        connection.commit()
    Database(path)
    with closing(sqlite3.connect(path)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        reviews = connection.execute("SELECT COUNT(*) FROM episode_reviews").fetchone()[0]
    assert (count, reviews) == (1, 0)


# API


def test_review_add_returns_it_with_the_reviewer_and_markdown_notes(owner: TestClient) -> None:
    created = review(owner)
    me = owner.get("/me").json()["user"]["id"]
    assert created["verdict"] == "approved" and created["reviewer_id"] == me
    assert created["token_id"] is None and created["notes"] == "Looks **good**."


def test_review_list_returns_reviews_in_the_order_they_were_added(owner: TestClient) -> None:
    review(owner, verdict="changes_requested")
    review(owner)
    listed = owner.get(REVIEWS).json()["items"]
    assert [item["verdict"] for item in listed] == ["changes_requested", "approved"]


def test_review_list_pages_with_a_cursor(owner: TestClient) -> None:
    for _ in range(3):
        review(owner)
    first = owner.get(REVIEWS, params={"limit": 2}).json()
    second = owner.get(REVIEWS, params={"cursor": first["next_cursor"]}).json()
    assert (len(first["items"]), len(second["items"]), second["next_cursor"]) == (2, 1, None)


def test_review_with_a_file_and_ranges_keeps_the_files_sha256(
    owner: TestClient, render_file: str
) -> None:
    seen = [{"start_s": 0, "end_s": 3.5}, {"start_s": 10, "end_s": 12}]
    created = review(owner, media_id=render_file, seen=seen)
    media = owner.get(f"{MEDIA}/{render_file}").json()
    assert created["media_sha256"] == media["sha256"] and created["seen"] == seen


def test_review_of_a_file_of_another_episode_is_refused_naming_the_field(
    owner: TestClient, render_file: str
) -> None:
    owner.post("/channels/funny-memes/episodes", json={"title": "Two"})
    response = owner.post(
        "/channels/funny-memes/episodes/2/reviews", json={**APPROVED, "media_id": render_file}
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["media_id"]


def test_review_with_a_range_ending_before_it_starts_is_refused(
    owner: TestClient, render_file: str
) -> None:
    seen = [{"start_s": 5, "end_s": 2}]
    response = owner.post(REVIEWS, json={**APPROVED, "media_id": render_file, "seen": seen})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"][:2] == ["body", "seen"]


def test_review_with_ranges_past_the_measured_end_is_refused(
    app: FastAPI, owner: TestClient, render_file: str
) -> None:
    sha = owner.get(f"{MEDIA}/{render_file}").json()["sha256"]
    services(app).media.save_measures(sha, MediaMeasures(duration_seconds=10))
    seen = [{"start_s": 0, "end_s": 11}]
    response = owner.post(REVIEWS, json={**APPROVED, "media_id": render_file, "seen": seen})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["seen", 0, "end_s"]


def test_review_with_ranges_within_the_measured_end_is_accepted(
    app: FastAPI, owner: TestClient, render_file: str
) -> None:
    sha = owner.get(f"{MEDIA}/{render_file}").json()["sha256"]
    services(app).media.save_measures(sha, MediaMeasures(duration_seconds=10))
    review(owner, media_id=render_file, seen=[{"start_s": 0, "end_s": 10}])


def test_review_dated_in_the_future_is_refused(owner: TestClient) -> None:
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    response = owner.post(REVIEWS, json={**APPROVED, "reviewed_at": later})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["reviewed_at"]


def test_review_dated_in_the_past_keeps_its_time(owner: TestClient) -> None:
    created = review(owner, reviewed_at="2026-01-02T03:04:05Z")
    assert created["reviewed_at"].startswith("2026-01-02T03:04:05")


def test_review_with_an_unknown_verdict_is_refused(owner: TestClient) -> None:
    assert owner.post(REVIEWS, json={"verdict": "meh"}).status_code == 422


def test_review_add_for_a_missing_episode_is_not_found(owner: TestClient) -> None:
    response = owner.post("/channels/funny-memes/episodes/9/reviews", json=APPROVED)
    assert response.status_code == 404


def test_review_add_with_idempotency_key_replays_the_first_response(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "review-1"}
    first = owner.post(REVIEWS, json=APPROVED, headers=headers)
    again = owner.post(REVIEWS, json=APPROVED, headers=headers)
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    assert len(owner.get(REVIEWS).json()["items"]) == 1


def test_review_has_no_edit_route(owner: TestClient) -> None:
    created = review(owner)
    assert owner.put(f"{REVIEWS}/{created['id']}", json=APPROVED).status_code == 405


def test_review_operations_have_snake_case_ids_and_descriptions(owner: TestClient) -> None:
    paths = owner.get("/openapi.json").json()["paths"]["/channels/{slug}/episodes/{number}/reviews"]
    assert paths["get"]["operationId"] == "list_reviews"
    assert paths["post"]["operationId"] == "add_review" and paths["post"]["description"]


def test_review_removed_with_the_media_it_looked_at_keeps_its_verdict(
    owner: TestClient, render_file: str
) -> None:
    review(owner, media_id=render_file)
    assert owner.delete(f"{MEDIA}/{render_file}").status_code == 204
    kept = owner.get(REVIEWS).json()["items"][0]
    assert kept["verdict"] == "approved" and kept["media_id"] is None


# Roles, scopes and the gate


def test_review_add_as_contributor_returns_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.post(REVIEWS, json=APPROVED).status_code == 403


def test_review_add_as_editor_is_allowed(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    editor = joined(owner, sign_up, mailer, "editor")
    assert editor.post(REVIEWS, json=APPROVED).status_code == 201


def test_review_list_as_viewer_is_allowed(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    review(owner)
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert len(viewer.get(REVIEWS).json()["items"]) == 1


def test_review_add_with_token_without_editorial_scope_is_forbidden(
    app: FastAPI, owner: TestClient
) -> None:
    token = new_token(owner, ["channels:read", "content:write"])
    assert agent(app, token).post(REVIEWS, json=APPROVED).status_code == 403


def test_review_list_with_read_only_token_is_allowed(app: FastAPI, owner: TestClient) -> None:
    review(owner)
    token = new_token(owner, ["channels:read"])
    assert agent(app, token).get(REVIEWS).status_code == 200


def test_review_add_with_token_records_the_token_and_its_user(
    app: FastAPI, owner: TestClient
) -> None:
    token = new_token(owner)
    token_id = owner.get("/me/tokens").json()["items"][0]["id"]
    created = agent(app, token).post(REVIEWS, json=APPROVED).json()
    assert (
        created["token_id"] == token_id
        and created["reviewer_id"] == owner.get("/me").json()["user"]["id"]
    )


def test_review_add_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/", "review.create")
    response = agent(app, new_token(owner)).post(REVIEWS, json=APPROVED)
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


def test_review_delete_by_the_reviewer_removes_it(owner: TestClient) -> None:
    created = review(owner)
    assert owner.delete(f"{REVIEWS}/{created['id']}").status_code == 204
    assert owner.get(REVIEWS).json()["items"] == []


def test_review_delete_by_an_owner_of_an_editors_review_removes_it(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    editor = joined(owner, sign_up, mailer, "editor")
    created = editor.post(REVIEWS, json=APPROVED).json()
    assert owner.delete(f"{REVIEWS}/{created['id']}").status_code == 204


def test_review_delete_by_another_editor_returns_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    editor = joined(owner, sign_up, mailer, "editor")
    created = owner.post(REVIEWS, json=APPROVED).json()
    assert editor.delete(f"{REVIEWS}/{created['id']}").status_code == 403


def test_review_delete_unknown_id_is_not_found(owner: TestClient) -> None:
    assert owner.delete(f"{REVIEWS}/{ZERO_ID}").status_code == 404


# Latest verdict and the published warning


def test_episode_published_without_a_review_warns_and_succeeds(owner: TestClient) -> None:
    response = publish(owner)
    assert response.status_code == 200
    assert "add_review" in response.json()["review_warning"] and "warning" in response.headers


def test_episode_published_after_a_rejection_warns_naming_the_verdict(owner: TestClient) -> None:
    review(owner, verdict="rejected")
    assert "rejected" in publish(owner).json()["review_warning"]


def test_episode_published_after_an_approval_has_no_review_warning(owner: TestClient) -> None:
    review(owner)
    assert publish(owner).json()["review_warning"] is None


def test_episode_latest_verdict_is_the_one_made_last_not_the_one_added_last(
    owner: TestClient,
) -> None:
    review(owner, verdict="changes_requested", reviewed_at="2026-01-02T00:00:00Z")
    review(owner, verdict="approved", reviewed_at="2026-01-01T00:00:00Z")
    assert "changes_requested" in publish(owner).json()["review_warning"]


def test_episode_moved_to_another_status_has_no_review_warning(owner: TestClient) -> None:
    response = owner.put(EPISODE, json={"title": "Pilot", "summary": "", "status": "script"})
    assert response.json()["review_warning"] is None


# Stills and SHA-256


def test_still_upload_of_a_png_is_stored_with_its_content_hash(owner: TestClient) -> None:
    response = owner.post(MEDIA, data={"kind": "still"}, files={"file": ("f.png", sample.PNG)})
    assert response.status_code == 201 and response.json()["kind"] == "still"
    assert len(response.json()["sha256"]) == 64


@pytest.mark.parametrize("content", [JPEG, WEBP])
def test_still_upload_of_other_images_is_accepted(owner: TestClient, content: bytes) -> None:
    response = owner.post(MEDIA, data={"kind": "still"}, files={"file": ("f.img", content)})
    assert response.status_code == 201


def test_still_upload_sniffs_the_content_not_the_name(owner: TestClient) -> None:
    response = owner.post(MEDIA, data={"kind": "still"}, files={"file": ("f.png", sample.WAV)})
    assert response.status_code == 422


def test_media_list_filters_by_the_still_kind(owner: TestClient) -> None:
    upload(owner, "still", sample.PNG, "frame.png")
    upload(owner, "cover", sample.PNG, "cover.png")
    listed = owner.get(MEDIA, params={"kind": "still"}).json()["items"]
    assert [item["kind"] for item in listed] == ["still"]


def test_media_sha256_is_the_sha256_of_the_uploaded_bytes(owner: TestClient) -> None:
    import hashlib

    media_id = upload(owner, "still", sample.PNG, "frame.png")
    assert (
        owner.get(f"{MEDIA}/{media_id}").json()["sha256"] == hashlib.sha256(sample.PNG).hexdigest()
    )


def test_delivery_read_lists_the_files_its_cuts_use_with_their_sha256(owner: TestClient) -> None:
    video = upload(owner, "final_video", sample.MP4, "final.mp4")
    cover = upload(owner, "cover", sample.PNG, "cover.png")
    assert (
        owner.put(DELIVERY, json=package(video, cover), headers={"If-Match": '"0"'}).status_code
        == 200
    )
    files = owner.get(DELIVERY).json()["files"]
    media = {item["id"]: item["sha256"] for item in owner.get(MEDIA).json()["items"]}
    assert {item["id"]: item["sha256"] for item in files} == media


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def add_review_tool(mcp: TestClient, **extra: Any) -> dict[str, Any]:
    arguments = {"slug": "funny-memes", "number": 1, "review": APPROVED, **extra}
    return tool(mcp, "add_review", arguments)


def test_mcp_add_review_returns_it(mcp: TestClient) -> None:
    result = add_review_tool(mcp)["structuredContent"]
    assert result["verdict"] == "approved" and result["token_id"]


def test_mcp_add_review_with_idempotency_key_replays_the_first_result(
    owner: TestClient, mcp: TestClient
) -> None:
    first = add_review_tool(mcp, idempotency_key="review-key")["structuredContent"]
    again = add_review_tool(mcp, idempotency_key="review-key")
    assert again["structuredContent"]["id"] == first["id"]
    assert again["_meta"]["sreditor/replayed"] is True
    assert len(owner.get(REVIEWS).json()["items"]) == 1


def test_mcp_add_review_with_a_different_review_under_the_same_key_is_refused(
    mcp: TestClient,
) -> None:
    add_review_tool(mcp, idempotency_key="review-key")
    other = add_review_tool(mcp, idempotency_key="review-key", review={"verdict": "rejected"})
    assert other["isError"]


def test_mcp_add_review_of_a_file_not_in_the_episode_names_list_media(mcp: TestClient) -> None:
    result = add_review_tool(mcp, review={**APPROVED, "media_id": ZERO_ID})
    assert result["isError"] and "list_media" in result["content"][0]["text"]


def test_mcp_get_episode_shows_the_latest_review_and_the_count(mcp: TestClient) -> None:
    add_review_tool(mcp, review={"verdict": "changes_requested"})
    add_review_tool(mcp, review={"verdict": "approved"})
    result = tool(mcp, "get_episode", {"slug": "funny-memes", "number": 1})["structuredContent"]
    assert result["reviews"]["count"] == 2
    assert result["reviews"]["latest"]["verdict"] == "approved"


def test_mcp_add_review_without_editorial_scope_names_the_scope(
    app: FastAPI, owner: TestClient
) -> None:
    token = new_token(owner, ["channels:read"])
    with agent(app, token, PUBLIC_URL) as reader:
        refused = add_review_tool(reader)
    assert refused["isError"] and "editorial:write" in refused["content"][0]["text"]


def test_mcp_update_episode_to_published_repeats_the_review_warning(mcp: TestClient) -> None:
    arguments = {
        "slug": "funny-memes",
        "number": 1,
        "episode": {"title": "Pilot", "summary": "", "status": "published"},
    }
    result = tool(mcp, "update_episode", arguments)["structuredContent"]
    assert "add_review" in result["review_warning"]


def test_mcp_list_media_describes_the_still_kind_and_sha256(mcp: TestClient) -> None:
    from tests.test_mcp_contract import registered

    description = registered(mcp)["list_media"]["description"]
    assert "still" in description and "sha256" in description
