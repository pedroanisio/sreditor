"""What a render worker runs on its own machine: the scene build, scene-render and the GPU lock
(rules project.render and project.render.workers).

The build is a configured command, srstudio's build from sreditor's export, that turns the
episode's data into a scene-render document. A job brings data only: the worker never runs code
that came with a job (Pedro's decision of 2026-11-07; user build.py waits for an isolated runner).
Every scene-render call holds srstudio's GPU lock, the one flock file the machine's renders share;
the build takes that lock itself around its own engine calls, so the worker does not hold it then.

scene-render passes through what it cannot draw, such as a shader that fails to compile, and still
exits 0 with a note; the worker fails such a render instead of delivering a picture that is wrong.
"""

from __future__ import annotations

import fcntl
import json
import shlex
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from sreditor.renders import Gpu, RenderPreset, ReportFinding
from sreditor.settings import DEFAULT_MAX_MEDIA_BYTES

DEFAULT_SCENE_RENDER = "scene-render"
DEFAULT_GPU_LOCK = Path("/run/lock/srstudio-gpu.lock")  # srstudio's lock (its ADR 0171)
DEFAULT_WORK_DIR = Path("/var/lib/sreditor-worker")
DEFAULT_OUTPUT_ID = "main"
DEFAULT_POLL_SECONDS = 15.0
LOCK_POLL_SECONDS = 0.25
WAIT_SECONDS = 1.0  # how often a running command checks whether its job was canceled
LOG_TAIL_CHARACTERS = 1500
EXPORT_PLACEHOLDER = "{export}"
SCENE_PLACEHOLDER = "{scene}"
PREVIEW_FLAGS = ("--quality", "draft")
# What scene-render prints for each thing it passed through rather than drew (its main.rs).
NOT_RENDERED_NOTE = "note: not rendered yet:"


class WorkerSetting(StrEnum):
    """The environment variables a worker reads."""

    SERVER_URL = "SREDITOR_WORKER_SERVER_URL"
    TOKEN = "SREDITOR_WORKER_TOKEN"
    BUILD_COMMAND = "SREDITOR_WORKER_BUILD_COMMAND"
    WORK_DIR = "SREDITOR_WORKER_DIR"
    SCENE_RENDER = "SREDITOR_SCENE_RENDER"
    GPU_LOCK = "SRSTUDIO_GPU_LOCK"
    OUTPUT_ID = "SREDITOR_WORKER_OUTPUT_ID"
    POLL_SECONDS = "SREDITOR_WORKER_POLL_SECONDS"
    PART_BYTES = "SREDITOR_WORKER_PART_BYTES"
    BUILDER_VERSION = "SREDITOR_WORKER_BUILDER_VERSION"


class WorkerSettings(BaseModel):
    """How a worker reaches sreditor and runs its tools."""

    model_config = ConfigDict(frozen=True)

    server_url: str = Field(min_length=1)
    token: SecretStr
    build_command: str = Field(min_length=1)
    work_dir: Path = DEFAULT_WORK_DIR
    scene_render: str = DEFAULT_SCENE_RENDER
    gpu_lock: Path = DEFAULT_GPU_LOCK
    output_id: str = DEFAULT_OUTPUT_ID
    poll_seconds: float = Field(default=DEFAULT_POLL_SECONDS, gt=0)
    part_bytes: int = Field(default=DEFAULT_MAX_MEDIA_BYTES, gt=0)
    # The srstudio commit the build command runs, which sreditor checks against the one it pins.
    builder_version: str = Field(min_length=1)


class WorkerMisconfigured(ValueError):
    """A setting the worker needs is missing or invalid."""


def worker_settings(environ: Mapping[str, str]) -> WorkerSettings:
    """The worker's settings from its environment; unset ones keep their defaults."""
    given: dict[str, Any] = {
        field: environ[variable]
        for field, variable in (
            ("server_url", WorkerSetting.SERVER_URL),
            ("token", WorkerSetting.TOKEN),
            ("build_command", WorkerSetting.BUILD_COMMAND),
            ("work_dir", WorkerSetting.WORK_DIR),
            ("scene_render", WorkerSetting.SCENE_RENDER),
            ("gpu_lock", WorkerSetting.GPU_LOCK),
            ("output_id", WorkerSetting.OUTPUT_ID),
            ("poll_seconds", WorkerSetting.POLL_SECONDS),
            ("part_bytes", WorkerSetting.PART_BYTES),
            ("builder_version", WorkerSetting.BUILDER_VERSION),
        )
        if environ.get(variable)
    }
    try:
        return WorkerSettings.model_validate(given)
    except ValidationError as invalid:
        names = ", ".join(str(error["loc"][0]) for error in invalid.errors())
        raise WorkerMisconfigured(f"worker settings missing or invalid: {names}") from invalid


class Canceled(Exception):
    """The job was canceled, or its lease lost, while the worker ran it."""


class CommandFailed(RuntimeError):
    """A tool exited with an error; the message ends with what it printed last."""

    def __init__(self, tool: str, code: int, log: Path, is_build: bool) -> None:
        tail = log.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL_CHARACTERS:]
        super().__init__(f"{tool} exited with {code}: {tail.strip()}")
        self.tool = tool
        self.is_build = is_build


class NotRendered(RuntimeError):
    """scene-render finished but passed through part of the scene; the message lists what."""

    def __init__(self, notes: list[str]) -> None:
        super().__init__("scene-render did not render: " + "; ".join(notes))


def not_rendered(log: Path) -> list[str]:
    """What scene-render's output in `log` says it passed through instead of drawing."""
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return [
        line.split(NOT_RENDERED_NOTE, 1)[1].strip() for line in lines if NOT_RENDERED_NOTE in line
    ]


class Step(BaseModel):
    """A tool to run: its arguments, where its output goes and whether it is the build."""

    model_config = ConfigDict(frozen=True)

    arguments: list[str]
    log: Path
    is_build: bool = False


def run_command(step: Step, stop: threading.Event) -> None:
    """Run a tool to its end, its output in its log; Canceled kills it when `stop` is set."""
    arguments, log = step.arguments, step.log
    with log.open("ab") as output:
        process = subprocess.Popen(arguments, stdout=output, stderr=subprocess.STDOUT)
        while True:
            try:
                code = process.wait(timeout=WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                if stop.is_set():
                    process.kill()
                    process.wait()
                    raise Canceled from None
                continue
            break
    if code != 0:
        raise CommandFailed(Path(arguments[0]).name, code, log, step.is_build)


@contextmanager
def gpu_held(path: Path, stop: threading.Event) -> Iterator[None]:
    """Hold the machine's GPU lock; wait for it while the job lasts, Canceled if it ends."""
    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stop.wait(LOCK_POLL_SECONDS):
                    raise Canceled from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_arguments(template: str, export: Path, scene: Path) -> list[str]:
    """The build command's arguments, its placeholders filled; no shell ever reads them."""
    return [
        part.replace(EXPORT_PLACEHOLDER, str(export)).replace(SCENE_PLACEHOLDER, str(scene))
        for part in shlex.split(template)
    ]


class EncodeJob(BaseModel):
    """One encode: the scene, where the video and report go, and the preset."""

    model_config = ConfigDict(frozen=True)

    scene: Path
    video: Path
    report: Path
    preset: RenderPreset


def gpus_from(envelope: str) -> list[Gpu]:
    """The adapters in `scene-render gpus --json`'s envelope (scene-render-cli/1)."""
    try:
        adapters = json.loads(envelope).get("result", {}).get("adapters", [])
        return [Gpu.model_validate(adapter) for adapter in adapters]
    except (ValueError, AttributeError, ValidationError):
        return []


def findings_from(report: Path) -> list[ReportFinding]:
    """The findings of a scene-render-report/1 file; none when it is missing or unreadable."""
    try:
        document = json.loads(report.read_text(encoding="utf-8"))
        return [
            ReportFinding(
                code=finding["code"],
                severity=finding["severity"],
                message=finding.get("message", ""),
            )
            for finding in document.get("findings", [])
        ]
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return []


class Engine:
    """The worker's tools: the build command and scene-render."""

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings

    def version(self) -> str:
        """The installed scene-render's version, as `scene-render --version` prints it."""
        printed = subprocess.run(
            [self.settings.scene_render, "--version"], check=True, capture_output=True, text=True
        )
        return printed.stdout.split()[-1]

    def gpus(self) -> list[Gpu]:
        """The adapters scene-render sees; none when it cannot list them."""
        listed = subprocess.run(
            [self.settings.scene_render, "gpus", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        return gpus_from(listed.stdout)

    def build(self, export: Path, scene: Path, stop: threading.Event) -> None:
        """Turn the unzipped export into a scene-render document at `scene`."""
        arguments = build_arguments(self.settings.build_command, export, scene)
        run_command(Step(arguments=arguments, log=scene.parent / "build.log", is_build=True), stop)

    def encode(self, job: EncodeJob, stop: threading.Event) -> list[ReportFinding]:
        """Render and encode the scene's output under the GPU lock; the report's findings.
        NotRendered when the engine passed through part of the scene."""
        arguments = [
            self.settings.scene_render,
            "encode",
            str(job.scene),
            "--output",
            self.settings.output_id,
            "-o",
            str(job.video),
            "--parallel",
            "auto",
            "--no-upload",
            "--report",
            str(job.report),
            *(PREVIEW_FLAGS if job.preset is RenderPreset.PREVIEW else ()),
        ]
        log = job.video.parent / "encode.log"
        with gpu_held(self.settings.gpu_lock, stop):
            run_command(Step(arguments=arguments, log=log), stop)
        if notes := not_rendered(log):
            raise NotRendered(notes)
        return findings_from(job.report)
