"""What a render worker runs on its own machine: the scene build, scene-render and the GPU lock
(rules project.render, project.render.workers and project.render.checked).

The build is a configured command, srstudio's build from sreditor's export, that turns the
episode's data into a scene-render document. A job brings data only: the worker never runs code
that came with a job (Pedro's decision of 2026-11-07; user build.py waits for an isolated runner).
Every scene-render call holds srstudio's GPU lock, the one flock file the machine's renders share;
the build takes that lock itself around its own engine calls, so the worker does not hold it then.

Before any GPU time the worker validates the scene the build wrote: an invalid scene fails for good,
since it fails the same way every time. Every tool runs under a time limit, so a hung engine frees
its job for another attempt instead of holding the worker forever.

scene-render passes through what it cannot draw, such as a shader that fails to compile, and still
exits 0, listing it in its JSON output; the worker fails such a render instead of delivering a
picture that is wrong.
"""

from __future__ import annotations

import fcntl
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from sreditor.engine_output import adapters_from, encoded_from, findings_from, validation_from
from sreditor.renders import Gpu, RenderPreset, ReportFinding
from sreditor.settings import DEFAULT_MAX_MEDIA_BYTES

DEFAULT_SCENE_RENDER = "scene-render"
DEFAULT_GPU_LOCK = Path("/run/lock/srstudio-gpu.lock")  # srstudio's lock (its ADR 0171)
DEFAULT_WORK_DIR = Path("/var/lib/sreditor-worker")
DEFAULT_OUTPUT_ID = "main"
DEFAULT_POLL_SECONDS = 15.0
# The longest a build, validation or encode may run; a long master on a slow GPU stays well inside.
DEFAULT_COMMAND_SECONDS = 6 * 60 * 60.0
LOCK_POLL_SECONDS = 0.25
WAIT_SECONDS = 1.0  # how often a running command checks whether its job was canceled
LOG_TAIL_CHARACTERS = 1500
EXPORT_PLACEHOLDER = "{export}"
SCENE_PLACEHOLDER = "{scene}"
PREVIEW_FLAGS = ("--quality", "draft")
SUCCESS = 0
INVALID = 1  # scene-render's exit status for a document that does not validate
USAGE = 2  # its exit status for usage, I/O and internal errors, which its JSON output explains


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
    COMMAND_SECONDS = "SREDITOR_WORKER_COMMAND_SECONDS"


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
    command_seconds: float = Field(default=DEFAULT_COMMAND_SECONDS, gt=0)


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
            ("command_seconds", WorkerSetting.COMMAND_SECONDS),
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
    """A tool failed; `is_build` when it was the build, whose failures repeat."""

    def __init__(self, message: str, is_build: bool) -> None:
        super().__init__(message)
        self.is_build = is_build


class CommandTimedOut(CommandFailed):
    """A tool ran past the worker's time limit and was killed."""


class NotRendered(RuntimeError):
    """scene-render finished but passed through part of the scene; the message lists what."""

    def __init__(self, notes: list[str]) -> None:
        super().__init__("scene-render did not render: " + "; ".join(notes))


class SceneInvalid(ValueError):
    """The scene the build wrote does not validate; the message lists each error."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("the built scene does not validate: " + "; ".join(errors))


class Step(BaseModel):
    """A tool to run: its arguments, where its output goes, how long it may run, which exit
    statuses are results rather than failures, and whether it is the build."""

    model_config = ConfigDict(frozen=True)

    arguments: list[str]
    log: Path
    # Where standard output goes when the tool prints a result there; its log otherwise.
    output: Path | None = None
    seconds: float = Field(default=DEFAULT_COMMAND_SECONDS, gt=0)
    accepted: frozenset[int] = frozenset({SUCCESS})
    is_build: bool = False

    def tool(self) -> str:
        """The tool's name, for messages."""
        return Path(self.arguments[0]).name


def exited(step: Step, code: int) -> CommandFailed:
    """The failure of a tool that exited with `code`; the message ends with what it printed last."""
    tail = step.log.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL_CHARACTERS:]
    return CommandFailed(f"{step.tool()} exited with {code}: {tail.strip()}", step.is_build)


def stopped(process: subprocess.Popen[bytes]) -> None:
    """Kill a tool and wait for it to go."""
    process.kill()
    process.wait()


def waited(process: subprocess.Popen[bytes], step: Step, stop: threading.Event) -> int:
    """The tool's exit status; Canceled when `stop` is set, CommandTimedOut past its time."""
    deadline = time.monotonic() + step.seconds
    while True:
        try:
            return process.wait(timeout=min(WAIT_SECONDS, step.seconds))
        except subprocess.TimeoutExpired:
            if stop.is_set():
                stopped(process)
                raise Canceled from None
            if time.monotonic() >= deadline:
                stopped(process)
                raise CommandTimedOut(
                    f"{step.tool()} timed out after {step.seconds:g} s", step.is_build
                ) from None


def run_command(step: Step, stop: threading.Event) -> int:
    """Run a tool to its end, its output in its log, and return its exit status when the step
    accepts it; CommandFailed otherwise."""
    with step.log.open("ab") as log, ExitStack() as files:
        output = files.enter_context(step.output.open("wb")) if step.output else log
        process = subprocess.Popen(step.arguments, stdout=output, stderr=log)
        code = waited(process, step, stop)
    if code not in step.accepted:
        raise exited(step, code)
    return code


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
        """The adapters scene-render sees; EngineOutputInvalid or EngineFailed when it cannot
        list them, since such an engine cannot render either."""
        listed = subprocess.run(
            [self.settings.scene_render, "gpus", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        return [
            Gpu(name=adapter.name, backend=adapter.backend, device_type=adapter.device_type)
            for adapter in adapters_from(listed.stdout)
        ]

    def build(self, export: Path, scene: Path, stop: threading.Event) -> None:
        """Turn the unzipped export into a scene-render document at `scene`."""
        arguments = build_arguments(self.settings.build_command, export, scene)
        step = Step(
            arguments=arguments,
            log=scene.parent / "build.log",
            seconds=self.settings.command_seconds,
            is_build=True,
        )
        run_command(step, stop)

    def validate(self, scene: Path, stop: threading.Event) -> None:
        """Check the built scene before any GPU time; SceneInvalid lists its errors, and
        EngineFailed says why scene-render could not check it."""
        printed = scene.parent / "validate.json"
        step = Step(
            arguments=[self.settings.scene_render, "validate", "--format", "json", str(scene)],
            log=scene.parent / "validate.log",
            output=printed,
            seconds=self.settings.command_seconds,
            accepted=frozenset({SUCCESS, INVALID, USAGE}),
        )
        run_command(step, stop)
        validation = validation_from(printed.read_text(encoding="utf-8"))
        if not validation.is_valid:
            raise SceneInvalid(validation.errors())

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
            "--json",
            *(PREVIEW_FLAGS if job.preset is RenderPreset.PREVIEW else ()),
        ]
        printed = job.video.parent / "encode.json"
        step = Step(
            arguments=arguments,
            log=job.video.parent / "encode.log",
            output=printed,
            seconds=self.settings.command_seconds,
        )
        with gpu_held(self.settings.gpu_lock, stop):
            run_command(step, stop)
        encoded = encoded_from(printed.read_text(encoding="utf-8"))
        if notes := [note for output in encoded.outputs for note in output.unsupported]:
            raise NotRendered(notes)
        return findings_from(job.report)
