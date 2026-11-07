"""The render worker's loop: claim a job, build and encode it, upload the video
(rules project.render.jobs, project.render.upload and project.render.workers).

The worker pulls over HTTPS with its srw_ token, so it never needs an inbound port. While it works
a heartbeat renews the lease; when the job is canceled or the lease lost, the heartbeat stops the
running tool. The video goes up in parts with their SHA-256, then the whole file's.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx

from sreditor.render_operations import Claimed, Completion, Failure, Hello
from sreditor.render_upload import MAX_PARTS
from sreditor.renders import Progress, RenderStatus, ReportFinding
from sreditor.worker_engine import (
    Canceled,
    CommandFailed,
    EncodeJob,
    Engine,
    WorkerSettings,
)

LOGGER = logging.getLogger("sreditor.render_worker")
NOT_HELD = 409  # the job was canceled, failed or went to another worker
NO_JOB = 204
CHUNK_BYTES = 1024 * 1024
HEARTBEATS_PER_LEASE = 3
SHA256_HEADER = "X-Content-SHA256"
SCENE_FILE = "episode.scene.xml"
VIDEO_FILE = "video.mp4"
REPORT_FILE = "report.json"
BUILT = 0.2  # share of the progress bar the build stands for
ENCODED = 0.9
MAX_REASON = 2000  # Failure.reason's limit
REQUEST_SECONDS = 120.0


class EngineRefused(RuntimeError):
    """sreditor refused the worker, such as for a scene-render version it does not pin."""


def job_path(job_id: UUID, suffix: str = "") -> str:
    """The worker API's address for one job."""
    return f"/render-worker/jobs/{job_id}{suffix}"


def file_sha256(path: Path) -> str:
    """The SHA-256 of a whole file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class WorkerClient:
    """The worker API of sreditor, with the worker's token on every request."""

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def claim(self, hello: Hello) -> Claimed | None:
        """The next waiting job, or None when none waits."""
        response = self.http.post("/render-worker/claim", json=hello.model_dump(mode="json"))
        if response.status_code == NO_JOB:
            return None
        if response.status_code == NOT_HELD:
            raise EngineRefused(response.json().get("detail", response.text))
        response.raise_for_status()
        return Claimed.model_validate(response.json())

    def report(self, job_id: UUID, progress: Progress) -> bool:
        """Renew the lease with the progress; False when the job is no longer this worker's."""
        response = self.http.post(
            job_path(job_id, "/progress"), json=progress.model_dump(mode="json")
        )
        if response.status_code == NOT_HELD:
            return False
        response.raise_for_status()
        return True

    def download(self, job_id: UUID, target: Path) -> None:
        """Save the job's export zip at `target`."""
        with self.http.stream("GET", job_path(job_id, "/export")) as response:
            response.raise_for_status()
            with target.open("wb") as output:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    output.write(chunk)

    def upload(self, job_id: UUID, video: Path, part_bytes: int) -> int:
        """Send the video in parts of at most `part_bytes`; the number of parts."""
        count = 0
        with video.open("rb") as source:
            while (part := source.read(part_bytes)) or count == 0:
                headers = {SHA256_HEADER: hashlib.sha256(part).hexdigest()}
                path = job_path(job_id, f"/parts/{count}")
                self.http.put(path, content=part, headers=headers).raise_for_status()
                count += 1
                if count > MAX_PARTS:
                    raise ValueError(f"the video needs more than {MAX_PARTS} parts")
        return count

    def complete(self, job_id: UUID, completion: Completion) -> None:
        """Join the parts into the episode's render video."""
        body = completion.model_dump(mode="json")
        self.http.post(job_path(job_id, "/complete"), json=body).raise_for_status()

    def fail(self, job_id: UUID, failure: Failure) -> None:
        """Give up on this attempt; a retryable failure goes back to the queue."""
        body = failure.model_dump(mode="json")
        response = self.http.post(job_path(job_id, "/fail"), json=body)
        if response.status_code != NOT_HELD:
            response.raise_for_status()


class Heartbeat:
    """Renews a job's lease while the worker runs it; sets `stop` once the job is lost."""

    def __init__(self, client: WorkerClient, claimed: Claimed) -> None:
        self.client = client
        self.job_id = claimed.job.id
        self.interval = claimed.lease_seconds / HEARTBEATS_PER_LEASE
        self.state = Progress(status=RenderStatus.BUILDING, progress=0)
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name=f"heartbeat-{self.job_id}", daemon=True
        )

    def __enter__(self) -> Self:
        self.beat()
        self.thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.finished.set()
        self.thread.join()

    def move(self, status: RenderStatus, progress: float) -> None:
        """Report a new stage at once; Canceled when the job is gone."""
        self.state = Progress(status=status, progress=progress)
        self.beat()
        if self.stop.is_set():
            raise Canceled

    def beat(self) -> None:
        """Renew the lease with the current stage."""
        try:
            if not self.client.report(self.job_id, self.state):
                self.stop.set()
        except httpx.HTTPError as unreachable:
            # The lease outlasts a few missed beats; a lost job shows up on the next one.
            LOGGER.warning("heartbeat for %s failed: %s", self.job_id, unreachable)

    def run(self) -> None:
        """Beat until the job ends or is lost."""
        while not self.finished.wait(self.interval) and not self.stop.is_set():
            self.beat()


def produce(
    client: WorkerClient, engine: Engine, claimed: Claimed, folder: Path, beat: Heartbeat
) -> tuple[Path, list[ReportFinding]]:
    """Download, build and encode the job; the video and the report's findings."""
    archive = folder / "export.zip"
    client.download(claimed.job.id, archive)
    export = folder / "export"
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(export)
    scene = folder / SCENE_FILE
    engine.build(export, scene, beat.stop)
    beat.move(RenderStatus.RENDERING, BUILT)
    video = folder / VIDEO_FILE
    encode = EncodeJob(
        scene=scene, video=video, report=folder / REPORT_FILE, preset=claimed.job.preset
    )
    findings = engine.encode(encode, beat.stop)
    beat.move(RenderStatus.ENCODING, ENCODED)
    return video, findings


def deliver(
    client: WorkerClient,
    claimed: Claimed,
    video: Path,
    findings: list[ReportFinding],
    part_bytes: int,
) -> None:
    """Upload the video and close the job."""
    parts = client.upload(claimed.job.id, video, part_bytes)
    completion = Completion(parts=parts, sha256=file_sha256(video), findings=findings)
    client.complete(claimed.job.id, completion)


def run_job(
    client: WorkerClient, engine: Engine, claimed: Claimed, settings: WorkerSettings
) -> None:
    """Run one claimed job to its end: done, failed with the reason, or dropped when canceled."""
    job_id = claimed.job.id
    folder = Path(tempfile.mkdtemp(prefix=f"job-{job_id}-", dir=settings.work_dir))
    try:
        with Heartbeat(client, claimed) as beat:
            video, findings = produce(client, engine, claimed, folder, beat)
            deliver(client, claimed, video, findings, settings.part_bytes)
        LOGGER.info("job %s done", job_id)
    except Canceled:
        LOGGER.info("job %s canceled or lost; stopped", job_id)
    except (CommandFailed, zipfile.BadZipFile, OSError, ValueError) as failure:
        LOGGER.warning("job %s failed: %s", job_id, failure)
        # A build that fails on the episode's data fails again; the engine or disk may recover.
        is_retryable = not (isinstance(failure, CommandFailed) and failure.is_build)
        client.fail(job_id, Failure(reason=str(failure)[:MAX_REASON], is_retryable=is_retryable))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def hello(engine: Engine) -> Hello:
    """What the worker tells sreditor about itself when it asks for work."""
    return Hello(engine_version=engine.version(), gpus=engine.gpus())


def run_once(client: WorkerClient, engine: Engine, settings: WorkerSettings) -> bool:
    """Claim and run one job; False when none was waiting."""
    claimed = client.claim(hello(engine))
    if claimed is None:
        return False
    LOGGER.info(
        "job %s claimed: episode %s of %s", claimed.job.id, claimed.job.number, claimed.job.channel
    )
    run_job(client, engine, claimed, settings)
    return True


def run_forever(
    client: WorkerClient, engine: Engine, settings: WorkerSettings, stop: threading.Event
) -> None:
    """Ask for work until `stop` is set, waiting between empty claims or when sreditor is unreachable."""
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        try:
            has_worked = run_once(client, engine, settings)
        except httpx.HTTPError as unreachable:
            LOGGER.warning("sreditor unreachable: %s", unreachable)
            has_worked = False
        if not has_worked:
            stop.wait(settings.poll_seconds)


def connect(settings: WorkerSettings) -> httpx.Client:
    """An HTTP client for sreditor's worker API, with the worker's token."""
    return httpx.Client(
        base_url=settings.server_url,
        headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
        timeout=REQUEST_SECONDS,
    )
