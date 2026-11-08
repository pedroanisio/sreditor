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
from sreditor.engine_output import EngineOutputInvalid
from sreditor.render_operations import Claimed
from sreditor.render_worker import (
    EngineRefused,
    Heartbeat,
    WorkerClient,
    run_forever,
    run_once,
)
from sreditor.renders import RenderJob
from sreditor.settings import DEFAULT_RENDER_BUILDER_VERSION
from sreditor.worker_engine import (
    Canceled,
    CommandTimedOut,
    Engine,
    Step,
    WorkerMisconfigured,
    WorkerSettings,
    build_arguments,
    gpu_held,
    run_command,
    worker_settings,
)
from tests import engine_samples as samples
from tests.media_samples import MP4
from tests.test_agentic import agent
from tests.test_api import FUNNY_MEMES

RENDERS = "/channels/funny-memes/episodes/1/renders"
VIDEO = MP4 + b"frames" * 3000

SCENE_RENDER = """#!{python}
import json, shutil, sys, time
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as calls:
    calls.write(json.dumps(args) + "\\n")
plan = json.loads(Path({plan!r}).read_text())
if args == ["--version"]:
    print("scene-render " + plan["version"])
elif args[:1] == ["gpus"]:
    print(plan["gpus"])
elif args[:1] == ["validate"]:
    print(plan["validate"])
    sys.exit(plan["validate_code"])
elif args[:1] == ["encode"]:
    if plan["is_hanging"]:
        time.sleep(30)
    if plan["encode_error"]:
        print(plan["encode_error"], file=sys.stderr)
        sys.exit(1)
    shutil.copyfile(plan["source"], args[args.index("-o") + 1])
    if plan["report"] is not None:
        Path(args[args.index("--report") + 1]).write_text(plan["report"])
    print(plan["encoded"])
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
        plan: dict[str, Any] | None = None, is_build_failing: bool = False, **changes: Any
    ) -> WorkerSettings:
        source = tmp_path / "source.mp4"
        source.write_bytes(VIDEO)
        engine_plan = {
            "version": "0.9.0",
            "gpus": samples.text(samples.gpus_envelope()),
            "validate": samples.text(samples.validated([])),
            "validate_code": 0,
            "is_hanging": False,
            "encode_error": None,
            "source": str(source),
            "report": samples.text(samples.report([samples.FINDING])),
            "encoded": samples.text(samples.encoded("/work/video.mp4")),
        } | (plan or {})
        (tmp_path / "plan.json").write_text(json.dumps(engine_plan))
        scene_render = SCENE_RENDER.format(
            python=sys.executable,
            calls=str(tmp_path / "calls.jsonl"),
            plan=str(tmp_path / "plan.json"),
        )
        build = BUILD.format(python=sys.executable, fails=is_build_failing)
        settings = WorkerSettings(
            server_url="https://testserver",
            token=SecretStr("srw_test"),
            build_command=executable(tmp_path / "build", build) + " {export} {scene}",
            work_dir=tmp_path / "work",
            scene_render=executable(tmp_path / "scene-render", scene_render),
            gpu_lock=tmp_path / "gpu.lock",
            builder_version=DEFAULT_RENDER_BUILDER_VERSION,
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
            "SREDITOR_WORKER_BUILDER_VERSION": "41a43948",
        }
    )
    assert settings.server_url == "https://sreditor.example" and settings.part_bytes == 1000
    assert settings.builder_version == "41a43948"


def test_worker_settings_without_builder_version_names_it() -> None:
    with pytest.raises(WorkerMisconfigured, match="builder_version"):
        worker_settings(
            {
                "SREDITOR_WORKER_SERVER_URL": "https://x",
                "SREDITOR_WORKER_TOKEN": "srw_x",
                "SREDITOR_WORKER_BUILD_COMMAND": "b",
            }
        )


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


def test_engine_gpus_lists_adapters(tools: Tools) -> None:
    assert [gpu.name for gpu in Engine(tools()).gpus()] == ["RTX 3050"]


def test_engine_gpus_unreadable_list_raises(tools: Tools) -> None:
    with pytest.raises(EngineOutputInvalid):
        Engine(tools(plan={"gpus": "not json"})).gpus()


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
    encode = next(call for call in calls(settings) if call[0] == "encode")
    assert ["--output", "main"] == encode[2:4] and "--quality" not in encode
    assert list(settings.work_dir.iterdir()) == []


def test_run_once_keeps_each_field_of_a_finding(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools()
    run_once(client, Engine(settings), settings)
    (finding,) = render(owner, job_id)["job"]["findings"]
    assert finding["code"] == "LEG-CONTRAST" and finding["severity"] == "warning"
    assert (finding["path"], finding["node"], finding["node_name"]) == (
        samples.FINDING["path"],
        "title",
        "Title",
    )
    assert (finding["time"], finding["measured"], finding["limit"], finding["unit"]) == (
        [0.0, 2.5],
        2.1,
        4.5,
        "ratio",
    )


def test_run_once_validates_scene_before_encoding(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    requested(owner)
    settings = tools()
    run_once(client, Engine(settings), settings)
    commands = [call[0] for call in calls(settings) if call[0] in ("validate", "encode")]
    assert commands == ["validate", "encode"]
    validate = next(call for call in calls(settings) if call[0] == "validate")
    assert validate[1:3] == ["--format", "json"] and validate[3].endswith("episode.scene.xml")


def test_run_once_invalid_scene_fails_job_for_good_without_encoding(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    invalid = samples.text(samples.validated([samples.DIAGNOSTIC]))
    settings = tools(plan={"validate": invalid, "validate_code": 1})
    run_once(client, Engine(settings), settings)
    shown = render(owner, job_id)
    assert shown["job"]["status"] == "failed" and shown["video"] is None
    assert "S01 at /bad: root element is <bad>" in shown["job"]["failure"]
    assert not any(call[0] == "encode" for call in calls(settings))


def test_run_once_validate_that_cannot_read_returns_job_to_queue(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    unreadable = samples.text(samples.failed("validate", "io", "cannot read the scene"))
    settings = tools(plan={"validate": unreadable, "validate_code": 2})
    run_once(client, Engine(settings), settings)
    job = render(owner, job_id)["job"]
    assert job["status"] == "queued" and "cannot read the scene" in job["failure"]


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
    settings = tools(plan={"encode_error": "error: Not enough memory left"})
    run_once(client, Engine(settings), settings)
    job = render(owner, job_id)["job"]
    assert job["status"] == "queued" and "Not enough memory left" in job["failure"]


def test_run_once_unrendered_content_fails_job_for_good(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    passed = samples.encoded("/work/video.mp4", ["fx: GLSL compile/link failed; passed through."])
    settings = tools(plan={"encoded": samples.text(passed)})
    run_once(client, Engine(settings), settings)
    shown = render(owner, job_id)
    assert shown["job"]["status"] == "failed" and shown["video"] is None
    assert "GLSL compile/link failed" in shown["job"]["failure"]
    encode = next(call for call in calls(settings) if call[0] == "encode")
    assert "--json" in encode


def test_run_once_unreadable_report_fails_job_for_good(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools(plan={"report": None})
    run_once(client, Engine(settings), settings)
    shown = render(owner, job_id)
    assert shown["job"]["status"] == "failed" and shown["video"] is None
    assert "scene-render-report/1" in shown["job"]["failure"]


def test_run_once_encode_past_its_time_returns_job_to_queue(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    job_id = requested(owner)
    settings = tools(plan={"is_hanging": True}, command_seconds=0.5)
    run_once(client, Engine(settings), settings)
    job = render(owner, job_id)["job"]
    assert job["status"] == "queued" and "timed out after 0.5 s" in job["failure"]


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
    settings = tools(plan={"version": "0.8.0"})
    with pytest.raises(EngineRefused, match="0.8.0"):
        run_once(client, Engine(settings), settings)


def test_run_once_other_builder_version_is_refused(
    owner: TestClient, client: WorkerClient, tools: Tools
) -> None:
    requested(owner)
    settings = tools(builder_version="0123abc")
    with pytest.raises(EngineRefused, match="0123abc"):
        run_once(client, Engine(settings), settings)


def test_run_command_with_stop_set_kills_tool(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(Canceled):
        run_command(Step(arguments=["sleep", "30"], log=tmp_path / "log"), stop)


def test_run_command_past_its_time_kills_tool(tmp_path: Path) -> None:
    step = Step(arguments=["sleep", "30"], log=tmp_path / "log", seconds=0.2)
    with pytest.raises(CommandTimedOut, match="sleep timed out after 0.2 s"):
        run_command(step, threading.Event())


def test_run_command_accepted_failure_returns_its_code(tmp_path: Path) -> None:
    step = Step(arguments=["false"], log=tmp_path / "log", accepted=frozenset({0, 1}))
    assert run_command(step, threading.Event()) == 1


def test_run_command_with_output_keeps_stdout_apart_from_log(tmp_path: Path) -> None:
    script = "import sys; print('result'); print('progress', file=sys.stderr)"
    step = Step(
        arguments=[sys.executable, "-c", script], log=tmp_path / "log", output=tmp_path / "out"
    )
    run_command(step, threading.Event())
    assert (tmp_path / "out").read_text() == "result\n"
    assert (tmp_path / "log").read_text() == "progress\n"


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


def test_run_forever_engine_that_cannot_list_gpus_waits_and_stops(
    tools: Tools, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    settings = tools(poll_seconds=0.01)
    engine = Engine(settings)

    def unreadable_then_stop() -> list[object]:
        stop.set()
        raise EngineOutputInvalid("scene-render gpus printed no scene-render-cli/1 envelope")

    monkeypatch.setattr(engine, "gpus", unreadable_then_stop)
    unused = httpx.Client(transport=httpx.MockTransport(unreachable), base_url="https://x")
    run_forever(WorkerClient(unused), engine, settings, stop)
    assert stop.is_set()


def test_cli_worker_without_settings_fails() -> None:
    assert main(["worker"], {}) == EXIT_ERROR
