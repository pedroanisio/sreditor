"""The render worker against the real app, with fake scene-render and build tools
(rules project.render.workers, project.render.jobs and project.render.upload)."""

from __future__ import annotations

import fcntl
import json
import sys
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sreditor import render_operations
from sreditor.cli import EXIT_ERROR, main
from sreditor.database import Database
from sreditor.deps import ChannelAccess, Services
from sreditor.render_operations import Claimed
from sreditor.render_worker import (
    EngineRefused,
    Heartbeat,
    WorkerClient,
    run_forever,
    run_once,
)
from sreditor.renders import RenderJob
from sreditor.worker_engine import (
    Canceled,
    Engine,
    Step,
    WorkerMisconfigured,
    WorkerSettings,
    build_arguments,
    findings_from,
    gpu_held,
    gpus_from,
    run_command,
    worker_settings,
)
from tests.media_samples import MP4
from tests.test_agentic import agent
from tests.test_api import FUNNY_MEMES

RENDERS = "/channels/funny-memes/episodes/1/renders"
VIDEO = MP4 + b"frames" * 3000

SCENE_RENDER = """#!{python}
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as calls:
    calls.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("scene-render {version}")
elif args[:1] == ["gpus"]:
    adapter = {{"name": "RTX 3050", "backend": "Vulkan", "device_type": "DiscreteGpu"}}
    print(json.dumps({{"result": {{"adapters": [adapter]}}}}))
elif args[:1] == ["encode"] and {fails!r}:
    print("Not enough memory left")
    sys.exit(1)
elif args[:1] == ["encode"]:
    shutil.copyfile({source!r}, args[args.index("-o") + 1])
    finding = {{"code": "A07", "severity": "warning", "path": "/scene", "message": "size"}}
    Path(args[args.index("--report") + 1]).write_text(json.dumps({{"findings": [finding]}}))
else:
    sys.exit(2)
"""

BUILD = """#!{python}
import sys
from pathlib import Path
export, scene = Path(sys.argv[1]), Path(sys.argv[2])
if not (export / "timeline.json").is_file() or {fails!r}:
    print("unknown rig toon-cat")
    sys.exit(3)
scene.write_text("<scene/>")
"""


def executable(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


Tools = Callable[..., WorkerSettings]


@pytest.fixture
def tools(tmp_path: Path) -> Tools:
    """Worker settings whose scene-render and build are small scripts."""

    def make(
        version: str = "0.9.0", is_encode_failing: bool = False, **changes: Any
    ) -> WorkerSettings:
        source = tmp_path / "source.mp4"
        source.write_bytes(VIDEO)
        values = {"python": sys.executable, "calls": str(tmp_path / "calls.jsonl")}
        scene_render = SCENE_RENDER.format(
            **values, version=version, fails=is_encode_failing, source=str(source)
        )
        build = BUILD.format(python=sys.executable, fails=changes.pop("is_build_failing", False))
        settings = WorkerSettings(
            server_url="https://testserver",
            token=SecretStr("srw_test"),
            build_command=executable(tmp_path / "build", build) + " {export} {scene}",
            work_dir=tmp_path / "work",
            scene_render=executable(tmp_path / "scene-render", scene_render),
            gpu_lock=tmp_path / "gpu.lock",
        )
        settings.work_dir.mkdir(exist_ok=True)
        return settings.model_copy(update=changes)

    return make


def calls(settings: WorkerSettings) -> list[list[str]]:
    lines = (Path(settings.scene_render).parent / "calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture(autouse=True)
def fake_export(monkeypatch: pytest.MonkeyPatch) -> None:
    def snapshot(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
        target = folder / "episode.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("timeline.json", "first cut")
        return target

    monkeypatch.setattr(render_operations, "snapshot_export", snapshot)


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert (
        client.post("/channels/funny-memes/episodes", json={"title": "Whose Milk"}).status_code
        == 201
    )
    return client


@pytest.fixture
def client(app: FastAPI, sign_up: Callable[..., TestClient]) -> WorkerClient:
    admin = sign_up("admin@example.com", "Admin")
    database: Database = app.state.services.users.database
    with database.connect() as connection:
        connection.execute("UPDATE users SET is_admin = 1 WHERE email = 'admin@example.com'")
    secret = admin.post("/render-workers", json={"name": "gpu-1"}).json()["secret"]
    return WorkerClient(agent(app, secret))


def requested(owner: TestClient, preset: str = "master") -> str:
    response = owner.post(RENDERS, json={"preset": preset})
    assert response.status_code == 202, response.text
    return str(response.json()["job"]["id"])


def render(owner: TestClient, job_id: str) -> dict[str, Any]:
    response = owner.get(f"{RENDERS}/{job_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_worker_settings_from_environment_reads_each() -> None:
    settings = worker_settings(
        {
            "SREDITOR_WORKER_SERVER_URL": "https://sreditor.example",
            "SREDITOR_WORKER_TOKEN": "srw_x",
            "SREDITOR_WORKER_BUILD_COMMAND": "srstudio build-export {export} {scene}",
            "SREDITOR_WORKER_PART_BYTES": "1000",
        }
    )
    assert settings.server_url == "https://sreditor.example" and settings.part_bytes == 1000


def test_worker_settings_without_token_names_it() -> None:
    with pytest.raises(WorkerMisconfigured, match="token"):
        worker_settings(
            {"SREDITOR_WORKER_SERVER_URL": "https://x", "SREDITOR_WORKER_BUILD_COMMAND": "b"}
        )


def test_build_arguments_fill_placeholders_without_shell() -> None:
    arguments = build_arguments(
        "srstudio build-export {export} '{scene}' ; rm", Path("/e x"), Path("/s")
    )
    assert arguments == ["srstudio", "build-export", "/e x", "/s", ";", "rm"]


def test_gpus_from_unreadable_envelope_is_empty() -> None:
    assert gpus_from("not json") == []


def test_findings_from_missing_report_is_empty(tmp_path: Path) -> None:
    assert findings_from(tmp_path / "missing.json") == []


def test_engine_gpus_lists_adapters(tools: Tools) -> None:
    assert [gpu.name for gpu in Engine(tools()).gpus()] == ["RTX 3050"]


def test_run_once_without_jobs_returns_false(client: WorkerClient, tools: Tools) -> None:
    settings = tools()
    assert run_once(client, Engine(settings), settings) is False


def test_run_once_renders_and_uploads_video(
    app: FastAPI, owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools(part_bytes=10_000)
    assert run_once(client, Engine(settings), settings) is True
    shown = render(owner, job_id)
    assert shown["job"]["status"] == "done" and shown["video"]["size_bytes"] == len(VIDEO)
    assert shown["job"]["findings"][0]["code"] == "A07"
    encode = next(call for call in calls(settings) if call[0] == "encode")
    assert ["--output", "main"] == encode[2:4] and "--quality" not in encode
    assert list(settings.work_dir.iterdir()) == []


def test_run_once_preview_encodes_at_draft_quality(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    requested(owner, "preview")
    settings = tools()
    run_once(client, Engine(settings), settings)
    encode = next(call for call in calls(settings) if call[0] == "encode")
    assert encode[-2:] == ["--quality", "draft"]


def test_run_once_build_failure_fails_job_for_good(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools(is_build_failing=True)
    run_once(client, Engine(settings), settings)
    job = render(owner, job_id)["job"]
    assert job["status"] == "failed" and "unknown rig toon-cat" in job["failure"]


def test_run_once_encode_failure_returns_job_to_queue(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools(is_encode_failing=True)
    run_once(client, Engine(settings), settings)
    job = render(owner, job_id)["job"]
    assert job["status"] == "queued" and "Not enough memory left" in job["failure"]


def test_run_once_canceled_job_stops_without_upload(
    owner: TestClient, client: WorkerClient, tools: Tools, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = requested(owner)
    settings = tools()
    engine = Engine(settings)
    original = engine.build

    def build_then_cancel(export: Path, scene: Path, stop: threading.Event) -> None:
        original(export, scene, stop)
        assert owner.post(f"{RENDERS}/{job_id}/cancel").status_code == 200

    monkeypatch.setattr(engine, "build", build_then_cancel)
    run_once(client, engine, settings)
    shown = render(owner, job_id)
    assert shown["job"]["status"] == "canceled" and shown["video"] is None


def test_run_once_other_engine_version_is_refused(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    requested(owner)
    settings = tools(version="0.8.0")
    with pytest.raises(EngineRefused, match="0.8.0"):
        run_once(client, Engine(settings), settings)


def test_run_command_with_stop_set_kills_tool(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(Canceled):
        run_command(Step(arguments=["sleep", "30"], log=tmp_path / "log"), stop)


def test_gpu_held_while_taken_elsewhere_waits_until_canceled(tmp_path: Path) -> None:
    lock = tmp_path / "gpu.lock"
    stop = threading.Event()
    stop.set()
    with lock.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(Canceled), gpu_held(lock, stop):
            pass


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def test_heartbeat_unreachable_server_keeps_job() -> None:
    http = httpx.Client(transport=httpx.MockTransport(unreachable), base_url="https://x")
    client = WorkerClient(http)
    job = RenderJob.model_construct(id=uuid4())
    beat = Heartbeat(client, Claimed.model_construct(job=job, export_path="", lease_seconds=120))
    beat.beat()
    assert not beat.stop.is_set()


def test_run_forever_unreachable_server_waits_and_stops(tools: Tools) -> None:
    stop = threading.Event()

    def refuse_and_stop(request: httpx.Request) -> httpx.Response:
        stop.set()
        return unreachable(request)

    http = httpx.Client(transport=httpx.MockTransport(refuse_and_stop), base_url="https://x")
    settings = tools()
    run_forever(WorkerClient(http), Engine(settings), settings, stop)
    assert stop.is_set()


def test_cli_worker_without_settings_fails() -> None:
    assert main(["worker"], {}) == EXIT_ERROR
