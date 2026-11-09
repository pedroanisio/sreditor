"""Rendering an episode on GPU workers (rules project.render, .jobs, .workers, .upload,
agentic.render.mcp and agentic.render.scope)."""

from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import render_operations
from sreditor.database import Database
from sreditor.deps import ChannelAccess, Services
from sreditor.render_upload import ABANDONED_AFTER_SECONDS, PartRejected, PartStore
from sreditor.renders import MAX_ATTEMPTS, NewJob, RenderPreset
from sreditor.settings import DEFAULT_RENDER_BUILDER_VERSION
from tests.conftest import RecordingMailer
from tests.media_samples import MP4, PNG
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

CHANNEL = "/channels/funny-memes"
EPISODE = f"{CHANNEL}/episodes/1"
RENDERS = f"{EPISODE}/renders"
HELLO = {
    "engine_version": "0.9.0",
    "builder_version": DEFAULT_RENDER_BUILDER_VERSION,
    "gpus": [{"name": "RTX 3050", "backend": "vulkan"}],
}
VIDEO = MP4 + b"frames" * 1000
PART_BYTES = 1000
LAPSED = "2000-01-01T00:00:00+00:00"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
RENDER_SCOPES = ["channels:read", "render:write"]


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def export_text() -> list[str]:
    """What the fake export writes in timeline.json; change it to change the episode."""
    return ["first cut"]


@pytest.fixture(autouse=True)
def fake_export(monkeypatch: pytest.MonkeyPatch, export_text: list[str]) -> None:
    """A small zip in place of the srstudio export, which test_srstudio_export covers."""

    def snapshot(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
        target = folder / "episode.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("timeline.json", export_text[0])
        return target

    monkeypatch.setattr(render_operations, "snapshot_export", snapshot)


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Whose Milk"}).status_code == 201
    return client


@pytest.fixture
def admin(sign_up: Callable[..., TestClient], app: FastAPI) -> TestClient:
    client = sign_up("admin@example.com", "Admin")
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin@example.com'")
    return client


@pytest.fixture
def worker(app: FastAPI, admin: TestClient) -> TestClient:
    """A registered worker; not entered as a context, so an MCP client can still start the app."""
    response = admin.post("/render-workers", json={"name": "gpu-1"})
    assert response.status_code == 201, response.text
    return agent(app, response.json()["secret"])


def requested(client: TestClient, preset: str = "master") -> dict[str, Any]:
    response = client.post(RENDERS, json={"preset": preset})
    assert response.status_code == 202, response.text
    found: dict[str, Any] = response.json()
    return found


def claimed(worker: TestClient) -> dict[str, Any]:
    response = worker.post("/render-worker/claim", json=HELLO)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def put_part(worker: TestClient, job_id: str, index: int, part: bytes, digest: str = "") -> int:
    response = worker.put(
        f"/render-worker/jobs/{job_id}/parts/{index}",
        content=part,
        headers={"X-Content-SHA256": digest or sha(part)},
    )
    return response.status_code


def upload(worker: TestClient, job_id: str, content: bytes) -> int:
    """Send the video in parts of PART_BYTES; returns how many."""
    parts = [content[i : i + PART_BYTES] for i in range(0, len(content), PART_BYTES)]
    for index, part in enumerate(parts):
        assert put_part(worker, job_id, index, part) == 200
    return len(parts)


def complete(worker: TestClient, job_id: str, parts: int, digest: str) -> Any:
    body = {"parts": parts, "sha256": digest, "findings": [{"code": "W1", "severity": "warning"}]}
    return worker.post(f"/render-worker/jobs/{job_id}/complete", json=body)


def finished(worker: TestClient, job_id: str) -> dict[str, Any]:
    response = complete(worker, job_id, upload(worker, job_id, VIDEO), sha(VIDEO))
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def lapse_leases(app: FastAPI) -> None:
    database: Database = app.state.services.renders.database
    with database.connect() as connection:
        connection.execute("UPDATE render_jobs SET lease_expires_at = ?", (LAPSED,))


# Requesting


def test_request_render_new_episode_queues_job_first_in_line(owner: TestClient) -> None:
    view = requested(owner)
    assert (view["job"]["status"], view["queue_place"], view["is_reused"]) == ("queued", 0, False)


def test_request_render_same_export_reuses_queued_job(owner: TestClient) -> None:
    first = requested(owner)
    again = requested(owner)
    assert again["job"]["id"] == first["job"]["id"] and again["is_reused"]


def test_request_render_other_srstudio_commit_queues_new_job(
    app: FastAPI, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = requested(owner)
    services: Services = app.state.services
    other_render = services.settings.render.model_copy(update={"builder_version": "0123abc"})
    other = services.settings.model_copy(update={"render": other_render})
    monkeypatch.setattr(services, "settings", other)
    again = requested(owner)
    assert again["job"]["id"] != first["job"]["id"] and not again["is_reused"]
    assert again["job"]["builder_version"] == "0123abc"


def count_jobs(app: FastAPI) -> int:
    database: Database = app.state.services.renders.database
    with database.connect() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM render_jobs").fetchone()[0])


def test_request_render_racing_request_queues_one_job(
    app: FastAPI, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = requested(owner)
    # Both requests passed the look-up before either queued: the second sees nothing earlier.
    monkeypatch.setattr(app.state.services.renders, "reusable", lambda new: None)
    again = requested(owner)
    assert again["job"]["id"] == first["job"]["id"] and again["is_reused"]
    assert count_jobs(app) == 1


def test_add_or_reuse_concurrent_identical_jobs_queue_one(app: FastAPI, owner: TestClient) -> None:
    wanted = NewJob(
        channel="funny-memes",
        number=1,
        preset=RenderPreset.MASTER,
        export_sha256="0" * 64,
        export_path="exports/x.zip",
        engine_version="0.9.0",
        builder_version=DEFAULT_RENDER_BUILDER_VERSION,
        requested_by="test",
    )
    renders = app.state.services.renders
    threads = 8
    start = threading.Barrier(threads)

    def race() -> tuple[UUID, bool]:
        start.wait()
        job, is_reused = renders.add_or_reuse(wanted)
        return job.id, is_reused

    with ThreadPoolExecutor(max_workers=threads) as pool:
        outcomes = [pool.submit(race) for _ in range(threads)]
        answers = [outcome.result() for outcome in outcomes]
    assert len({job_id for job_id, _ in answers}) == 1
    assert sorted(is_reused for _, is_reused in answers) == [False] + [True] * (threads - 1)
    assert count_jobs(app) == 1


def test_request_render_changed_episode_queues_new_job(
    owner: TestClient, export_text: list[str]
) -> None:
    first = requested(owner)
    export_text[0] = "second cut"
    assert requested(owner)["job"]["id"] != first["job"]["id"]


def test_request_render_other_preset_queues_new_job(owner: TestClient) -> None:
    assert requested(owner)["job"]["id"] != requested(owner, "preview")["job"]["id"]


def test_request_render_by_contributor_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.post(RENDERS, json={}).status_code == 403


def test_request_render_token_without_render_scope_is_forbidden(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read", "content:write"])) as client:
        response = client.post(RENDERS, json={})
    assert response.status_code == 403 and "render:write" in response.json()["detail"]


def test_request_render_token_with_render_scope_is_accepted(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read", "render:write"])) as client:
        assert client.post(RENDERS, json={}).status_code == 202


# The worker's journey


def test_worker_flow_claims_reports_uploads_and_finishes_with_video(
    owner: TestClient, worker: TestClient
) -> None:
    job_id = requested(owner)["job"]["id"]
    claim = claimed(worker)
    assert claim["job"]["id"] == job_id and claim["job"]["status"] == "building"
    export = worker.get(claim["export_path"])
    assert zipfile.ZipFile(io.BytesIO(export.content)).read("timeline.json") == b"first cut"
    progress = {"status": "rendering", "progress": 0.5}
    assert worker.post(f"/render-worker/jobs/{job_id}/progress", json=progress).status_code == 200
    assert finished(worker, job_id)["status"] == "done"
    view = owner.get(f"{RENDERS}/{job_id}").json()
    assert view["video"]["kind"] == "render" and view["job"]["findings"][0]["code"] == "W1"
    assert owner.get(view["download_path"]).content == VIDEO


def test_request_render_after_done_reuses_video(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    finished(worker, job_id)
    again = requested(owner)
    assert again["is_reused"] and again["video"] is not None


def test_request_render_after_video_removed_queues_new_job(
    owner: TestClient, worker: TestClient
) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    finished(worker, job_id)
    video_id = owner.get(f"{RENDERS}/{job_id}").json()["video"]["id"]
    assert owner.delete(f"{EPISODE}/media/{video_id}").status_code == 204
    assert requested(owner)["job"]["id"] != job_id


def test_claim_empty_queue_answers_204(worker: TestClient) -> None:
    assert worker.post("/render-worker/claim", json=HELLO).status_code == 204


def test_claim_other_engine_version_is_conflict(owner: TestClient, worker: TestClient) -> None:
    requested(owner)
    response = worker.post("/render-worker/claim", json={**HELLO, "engine_version": "0.8.0"})
    assert response.status_code == 409 and "0.9.0" in response.json()["detail"]


def test_claim_other_builder_version_is_conflict(owner: TestClient, worker: TestClient) -> None:
    requested(owner)
    response = worker.post("/render-worker/claim", json={**HELLO, "builder_version": "0123abc"})
    assert response.status_code == 409
    assert DEFAULT_RENDER_BUILDER_VERSION in response.json()["detail"]


def test_reusable_other_builder_version_is_none(app: FastAPI, owner: TestClient) -> None:
    job = requested(owner)["job"]
    same_but_builder = NewJob(
        channel="funny-memes",
        number=1,
        preset=RenderPreset.MASTER,
        export_sha256=job["export_sha256"],
        export_path="",
        engine_version=job["engine_version"],
        builder_version="0123abc",
        requested_by="test",
    )
    renders = app.state.services.renders
    assert renders.reusable(
        same_but_builder.model_copy(update={"builder_version": job["builder_version"]})
    )
    assert renders.reusable(same_but_builder) is None


def test_claim_records_worker_gpus(admin: TestClient, worker: TestClient) -> None:
    worker.post("/render-worker/claim", json=HELLO)
    listed = admin.get("/render-workers").json()
    assert listed[0]["gpus"][0]["name"] == "RTX 3050" and listed[0]["last_seen_at"]


def test_worker_without_token_is_unauthorized(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.post("/render-worker/claim", json=HELLO).status_code == 401


def test_revoked_worker_is_unauthorized(admin: TestClient, worker: TestClient) -> None:
    worker_id = admin.get("/render-workers").json()[0]["id"]
    assert admin.post(f"/render-workers/{worker_id}/revoke").status_code == 200
    assert worker.post("/render-worker/claim", json=HELLO).status_code == 401


def test_register_worker_by_non_admin_is_forbidden(owner: TestClient) -> None:
    assert owner.post("/render-workers", json={"name": "gpu"}).status_code == 403


def test_revoke_unknown_worker_is_not_found(admin: TestClient) -> None:
    assert admin.post(f"/render-workers/{UNKNOWN_ID}/revoke").status_code == 404


# Canceling and failing


def test_cancel_running_render_stops_worker_report(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    assert owner.post(f"{RENDERS}/{job_id}/cancel").json()["job"]["status"] == "canceled"
    progress = {"status": "encoding", "progress": 0.9}
    response = worker.post(f"/render-worker/jobs/{job_id}/progress", json=progress)
    assert response.status_code == 409


def test_cancel_finished_render_is_conflict(owner: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    owner.post(f"{RENDERS}/{job_id}/cancel")
    assert owner.post(f"{RENDERS}/{job_id}/cancel").status_code == 409


def test_get_render_of_other_episode_is_not_found(owner: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    assert owner.post(f"{CHANNEL}/episodes", json={"title": "Two"}).status_code == 201
    assert owner.get(f"{CHANNEL}/episodes/2/renders/{job_id}").status_code == 404


def test_list_renders_two_pages_newest_first(owner: TestClient, export_text: list[str]) -> None:
    first = requested(owner)["job"]["id"]
    export_text[0] = "second cut"
    second = requested(owner)["job"]["id"]
    page = owner.get(RENDERS, params={"limit": 1}).json()
    rest = owner.get(RENDERS, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [page["items"][0]["job"]["id"], rest["items"][0]["job"]["id"]] == [second, first]
    assert rest["next_cursor"] is None


def test_fail_retryable_attempt_returns_job_to_queue(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    failure = {"reason": "GPU out of memory"}
    job = worker.post(f"/render-worker/jobs/{job_id}/fail", json=failure).json()
    assert (job["status"], job["failure"]) == ("queued", "GPU out of memory")


def test_fail_final_attempt_stays_failed(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    failure = {"reason": "invalid scene", "is_retryable": False}
    job = worker.post(f"/render-worker/jobs/{job_id}/fail", json=failure).json()
    assert job["status"] == "failed"


def test_claim_lapsed_lease_retries_until_attempts_run_out(
    owner: TestClient, worker: TestClient, app: FastAPI
) -> None:
    job_id = requested(owner)["job"]["id"]
    for _ in range(MAX_ATTEMPTS):
        assert claimed(worker)["job"]["id"] == job_id
        lapse_leases(app)
    assert worker.post("/render-worker/claim", json=HELLO).status_code == 204
    assert owner.get(f"{RENDERS}/{job_id}").json()["job"]["status"] == "failed"


# Uploading


def test_complete_with_missing_part_is_refused(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    upload(worker, job_id, VIDEO[:PART_BYTES])
    response = complete(worker, job_id, 2, sha(VIDEO))
    assert response.status_code == 422 and "1" in response.json()["detail"]


def test_complete_with_wrong_checksum_is_refused(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    response = complete(worker, job_id, upload(worker, job_id, VIDEO), sha(b"other"))
    assert response.status_code == 422


def test_complete_with_image_instead_of_video_is_refused(
    owner: TestClient, worker: TestClient
) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    assert complete(worker, job_id, upload(worker, job_id, PNG), sha(PNG)).status_code == 422


def test_upload_part_wrong_checksum_is_refused(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    assert put_part(worker, job_id, 0, b"abc", sha(b"x")) == 422


def test_upload_part_over_media_limit_is_refused(
    owner: TestClient, worker: TestClient, app: FastAPI
) -> None:
    job_id = requested(owner)["job"]["id"]
    claimed(worker)
    big = b"x" * (app.state.services.settings.max_media_bytes + 1)
    assert put_part(worker, job_id, 0, big) == 422


def test_upload_part_job_not_claimed_is_conflict(owner: TestClient, worker: TestClient) -> None:
    job_id = requested(owner)["job"]["id"]
    assert put_part(worker, job_id, 0, b"abc") == 409


def test_upload_media_kind_render_is_refused(owner: TestClient) -> None:
    files = {"file": ("cut.mp4", MP4, "video/mp4")}
    response = owner.post(f"{EPISODE}/media", data={"kind": "render"}, files=files)
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "kind"]


def test_part_store_abandoned_upload_is_discarded(tmp_path: Path) -> None:
    parts = PartStore(tmp_path, 100)
    job_id = uuid4()
    parts.save(job_id, 0, io.BytesIO(b"abc"), sha(b"abc"))
    later = parts.folder(job_id).stat().st_mtime + ABANDONED_AFTER_SECONDS + 1
    parts.discard_abandoned(now=later)
    assert not parts.folder(job_id).exists()


def test_part_store_recent_upload_is_kept(tmp_path: Path) -> None:
    parts = PartStore(tmp_path, 100)
    job_id = uuid4()
    parts.save(job_id, 0, io.BytesIO(b"abc"), sha(b"abc"))
    parts.discard_abandoned()
    assert parts.folder(job_id).exists()


def test_part_store_empty_part_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PartRejected):
        PartStore(tmp_path, 100).save(uuid4(), 0, io.BytesIO(b""), sha(b""))


def test_part_store_negative_index_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PartRejected):
        PartStore(tmp_path, 100).save(uuid4(), -1, io.BytesIO(b"a"), sha(b"a"))


def test_part_store_reader_joins_parts_in_order(tmp_path: Path) -> None:
    parts = PartStore(tmp_path, 100)
    job_id = UUID(UNKNOWN_ID)
    for index, piece in enumerate([b"ab", b"cd", b"e"]):
        parts.save(job_id, index, io.BytesIO(piece), sha(piece))
    with parts.reader(job_id, 3) as reader:
        assert reader.read() == b"abcde"


# MCP


def test_mcp_request_and_follow_render_until_video(
    app: FastAPI, owner: TestClient, worker: TestClient
) -> None:
    episode = {"slug": "funny-memes", "number": 1}
    with agent(app, new_token(owner, RENDER_SCOPES), PUBLIC_URL) as mcp:
        job_id = tool(mcp, "request_render", episode)["structuredContent"]["job"]["id"]
        claimed(worker)
        finished(worker, job_id)
        done = tool(mcp, "get_render", {**episode, "render_id": job_id})["structuredContent"]
    assert done["job"]["status"] == "done" and done["download_path"].endswith("/content")


def test_mcp_get_unknown_render_names_list_renders(app: FastAPI, owner: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "render_id": UNKNOWN_ID}
    with agent(app, new_token(owner, RENDER_SCOPES), PUBLIC_URL) as mcp:
        refused = tool(mcp, "get_render", arguments)
    assert refused["isError"] and "list_renders" in refused["content"][0]["text"]


def test_mcp_list_renders_shows_requested_render(app: FastAPI, owner: TestClient) -> None:
    episode = {"slug": "funny-memes", "number": 1}
    with agent(app, new_token(owner, RENDER_SCOPES), PUBLIC_URL) as mcp:
        job_id = tool(mcp, "request_render", episode)["structuredContent"]["job"]["id"]
        listed = tool(mcp, "list_renders", episode)["structuredContent"]
    assert listed["items"][0]["job"]["id"] == job_id


def test_mcp_cancel_render_twice_explains_refusal(app: FastAPI, owner: TestClient) -> None:
    episode = {"slug": "funny-memes", "number": 1}
    with agent(app, new_token(owner, RENDER_SCOPES), PUBLIC_URL) as mcp:
        job_id = tool(mcp, "request_render", episode)["structuredContent"]["job"]["id"]
        target = {**episode, "render_id": job_id}
        canceled = tool(mcp, "cancel_render", target)["structuredContent"]
        again = tool(mcp, "cancel_render", target)
    assert canceled["job"]["status"] == "canceled"
    assert again["isError"] and "queued or running" in again["content"][0]["text"]


# The change feed (rule agentic.events)


def render_events(client: TestClient) -> list[tuple[str, str | None, str | None]]:
    """The feed's render events as (operation, status, actor type), oldest first."""
    items = client.get(f"{CHANNEL}/events").json()["items"]
    return [
        (item["operation"], item["status"], item["actor"]["type"] if item["actor"] else None)
        for item in items
        if item["operation"].startswith("render.")
    ]


def test_events_render_to_done_records_each_transition_with_its_worker(
    owner: TestClient, worker: TestClient
) -> None:
    job = requested(owner)["job"]
    claimed(worker)
    finished(worker, job["id"])
    assert render_events(owner) == [
        ("render.status", "queued", None),
        ("render.request", None, "user"),
        ("render.status", "building", "worker"),
        ("render.status", "done", "worker"),
    ]


def test_events_render_canceled_by_agent_records_request_and_cancel(
    app: FastAPI, owner: TestClient
) -> None:
    bot = agent(app, new_token(owner, RENDER_SCOPES))
    job = requested(bot)["job"]
    assert bot.post(f"{RENDERS}/{job['id']}/cancel").status_code == 200
    events = owner.get(f"{CHANNEL}/events").json()["items"]
    gated = [item for item in events if item["operation"] in ("render.request", "render.cancel")]
    assert [item["operation"] for item in gated] == ["render.request", "render.cancel"]
    assert all(item["actor"]["type"] == "token" for item in gated)
    assert ("render.status", "canceled", None) in render_events(owner)


def test_events_render_lapsed_on_every_attempt_records_failed(
    app: FastAPI, owner: TestClient, worker: TestClient
) -> None:
    requested(owner)
    for _ in range(MAX_ATTEMPTS):
        claimed(worker)
        lapse_leases(app)
    worker.post("/render-worker/claim", json=HELLO)
    assert render_events(owner)[-1][:2] == ("render.status", "failed")
