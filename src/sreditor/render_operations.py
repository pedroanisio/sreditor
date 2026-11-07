"""Render operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

People and agents request, follow and cancel renders (rules project.render and
agentic.render.scope); workers claim jobs, report progress and upload the finished video
(rules project.render.workers and project.render.upload); instance admins register workers.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import IO, BinaryIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sreditor.assets import CHUNK_BYTES, ZIP, StoredFile
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.media import VIDEO_FORMATS, MediaFile, MediaInput, MediaKind, NewMedia
from sreditor.members import Permission
from sreditor.operations import gated, owner_of
from sreditor.pagination import Page, encode_cursor
from sreditor.renders import (
    EngineVersion,
    Gpu,
    Lease,
    NewJob,
    NewWorker,
    Pinned,
    Progress,
    RenderJob,
    RenderNotFound,
    RenderOutcome,
    RenderPreset,
    RenderWorker,
    ReportFinding,
    WorkerName,
    lease_until,
)
from sreditor.srstudio_export import srstudio_zip
from sreditor.users import User

SHA256_PATTERN = "^[0-9a-fA-F]{64}$"


class RenderOperation:
    """stmt operation names a render request is gated on (rule agentic.render.scope)."""

    REQUEST = "render.request"
    CANCEL = "render.cancel"


class RenderRequest(BaseModel):
    """Which episode to render, and how."""

    model_config = ConfigDict(frozen=True)

    number: int
    preset: RenderPreset = RenderPreset.MASTER


class RenderView(BaseModel):
    """A render job as people and agents see it: where it is and, when done, its video."""

    model_config = ConfigDict(frozen=True)

    job: RenderJob
    is_reused: bool = Field(
        default=False, description="True when an earlier render of the same export answered."
    )
    queue_place: int | None = Field(
        default=None, description="Queued jobs ahead of this one; null unless queued."
    )
    video: MediaFile | None = Field(default=None, description="The finished video, once done.")
    download_path: str | None = Field(
        default=None, description="API path that downloads the video with the same token."
    )


class NotAdmin(PermissionError):
    """Only an instance admin manages render workers (rule project.render.workers)."""

    def __init__(self) -> None:
        super().__init__("Only an instance admin can manage render workers")


class EngineMismatch(ValueError):
    """The worker runs another scene-render or srstudio version than the instance pins."""

    def __init__(self, tool: str, found: str, wanted: str) -> None:
        super().__init__(f"This worker runs {tool} {found}; install {wanted} to take jobs")
        self.found = found
        self.wanted = wanted


class VideoMismatch(ValueError):
    """The assembled video is not the file the worker said it sent."""

    def __init__(self) -> None:
        super().__init__("The assembled video's SHA-256 does not match the one sent")


class Hello(BaseModel):
    """What a worker says when it asks for work."""

    model_config = ConfigDict(frozen=True)

    engine_version: EngineVersion
    builder_version: EngineVersion
    gpus: list[Gpu] = Field(default_factory=list, max_length=16)


class Claimed(BaseModel):
    """A job given to a worker, with where to fetch its export and how long it holds it."""

    model_config = ConfigDict(frozen=True)

    job: RenderJob
    export_path: str
    lease_seconds: int


class Completion(BaseModel):
    """A finished upload: how many parts, the whole file's SHA-256 and the report's findings."""

    model_config = ConfigDict(frozen=True)

    parts: int = Field(ge=1, le=10_000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    findings: list[ReportFinding] = Field(default_factory=list, max_length=200)


class Failure(BaseModel):
    """Why an attempt failed, and whether trying again could help."""

    model_config = ConfigDict(frozen=True)

    reason: str = Field(min_length=1, max_length=2000)
    is_retryable: bool = True


class PartUpload(BaseModel):
    """One part of a video: its job, its index from 0 and its SHA-256."""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    index: int
    sha256: str = Field(pattern=SHA256_PATTERN)


def export_digest(archive: Path) -> str:
    """The SHA-256 of the export's contents, entry by entry, so the zip's timestamps never count."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(archive) as opened:
        for name in sorted(opened.namelist()):
            entry = hashlib.sha256()
            with opened.open(name) as member:
                while chunk := member.read(CHUNK_BYTES):
                    entry.update(chunk)
            digest.update(f"{name}\0{entry.hexdigest()}\n".encode())
    return digest.hexdigest()


def download_path(video: MediaFile) -> str:
    """The API path that downloads a media file."""
    return f"/channels/{video.channel}/episodes/{video.number}/media/{video.id}/content"


def view(app: Services, job: RenderJob, is_reused: bool = False) -> RenderView:
    """The job with its place in the queue and, once done, its video."""
    video = app.media.get(job.channel, job.number, job.media_id) if job.media_id else None
    return RenderView(
        job=job,
        is_reused=is_reused,
        queue_place=app.renders.queue_place(job),
        video=video,
        download_path=download_path(video) if video else None,
    )


def snapshot_export(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
    """The episode's srstudio zip; SrstudioNotReady lists what it lacks.

    The export itself asks for channels:write from a token, but rendering needs only
    render:write: the role was already checked for RENDER, which owners and editors hold.
    """
    return srstudio_zip(app, dataclasses.replace(access, token=None), number, folder)


def request_render(app: Services, access: ChannelAccess, request: RenderRequest) -> RenderView:
    """Queue a render of the episode as it is now, or answer with an earlier one of the same export."""
    slug = episode_access(app, access, request.number, Permission.RENDER)
    folder = Path(tempfile.mkdtemp(prefix="render-"))
    try:
        archive = snapshot_export(app, access, request.number, folder)
        wanted = NewJob(
            channel=slug,
            number=request.number,
            preset=request.preset,
            export_sha256=export_digest(archive),
            export_path="",
            engine_version=app.settings.render.engine_version,
            builder_version=app.settings.render.builder_version,
            requested_by=owner_of(access.user, access.token).id,
        )
        earlier = app.renders.reusable(wanted)
        if earlier is not None:
            return view(app, earlier, is_reused=True)
        return view(
            app, gated(app, access, RenderOperation.REQUEST, lambda: queue(app, wanted, archive))
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def queue(app: Services, wanted: NewJob, archive: Path) -> RenderJob:
    """Keep the export where workers can fetch it, then queue the job."""
    with archive.open("rb") as source:
        stored = app.assets.save_stream(
            wanted.channel, (ZIP,), source, app.settings.render.max_render_bytes
        )
    return app.renders.add(wanted.model_copy(update={"export_path": stored.path}))


def episode_job(app: Services, access: ChannelAccess, number: int, job_id: UUID) -> RenderJob:
    """A job of this episode; RenderNotFound when it belongs to another."""
    slug = episode_access(app, access, number, Permission.READ)
    job = app.renders.get(job_id)
    if job.channel != slug or job.number != number:
        raise RenderNotFound(str(job_id))
    return job


def get_render(app: Services, access: ChannelAccess, number: int, job_id: UUID) -> RenderView:
    """One render of the episode, with its progress and, once done, its video."""
    return view(app, episode_job(app, access, number, job_id))


def list_renders(
    app: Services, access: ChannelAccess, number: int, cursor: str | None, limit: int
) -> Page[RenderView]:
    """The episode's renders, newest first."""
    slug = episode_access(app, access, number, Permission.READ)
    rows = app.renders.of_episode(slug, number, after_number(cursor), limit + 1)
    items = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = encode_cursor(str(app.renders.position(items[-1].id))) if has_more else None
    return Page[RenderView](items=[view(app, job) for job in items], next_cursor=next_cursor)


def cancel_render(app: Services, access: ChannelAccess, number: int, job_id: UUID) -> RenderView:
    """Stop a queued or running render; RenderFinished when it already ended."""
    job = episode_job(app, access, number, job_id)
    access.require(Permission.RENDER)
    return view(app, gated(app, access, RenderOperation.CANCEL, lambda: app.renders.cancel(job)))


def require_admin(user: User) -> None:
    """Refuse anyone but an instance admin."""
    if not user.is_admin:
        raise NotAdmin


def register_worker(app: Services, user: User, name: WorkerName) -> NewWorker:
    """Register a GPU machine; the secret is shown once, here."""
    require_admin(user)
    return app.renders.register(name, user.id)


def list_workers(app: Services, user: User) -> list[RenderWorker]:
    """Every registered worker, with what it last reported."""
    require_admin(user)
    return app.renders.workers()


def revoke_worker(app: Services, user: User, worker_id: UUID) -> RenderWorker:
    """Stop a worker; a job it held returns to the queue when its lease lapses."""
    require_admin(user)
    return app.renders.revoke(worker_id)


def lease_for(app: Services, worker: RenderWorker) -> Lease:
    """A lease for this worker, starting now."""
    seconds = app.settings.render.lease_seconds
    return Lease(worker_id=worker.id, until=lease_until(app.renders.now(), seconds))


def claim_job(app: Services, worker: RenderWorker, hello: Hello) -> Claimed | None:
    """Give the worker the oldest job it can run, or None when the queue is empty."""
    app.renders.seen(worker.id, hello.engine_version, hello.gpus)
    pinned = app.settings.render
    if hello.engine_version != pinned.engine_version:
        raise EngineMismatch("scene-render", hello.engine_version, pinned.engine_version)
    if hello.builder_version != pinned.builder_version:
        raise EngineMismatch("srstudio", hello.builder_version, pinned.builder_version)
    app.render_parts.discard_abandoned()
    toolchain = Pinned(engine_version=pinned.engine_version, builder_version=pinned.builder_version)
    job = app.renders.claim(lease_for(app, worker), toolchain)
    if job is None:
        return None
    return Claimed(
        job=job,
        export_path=f"/render-worker/jobs/{job.id}/export",
        lease_seconds=app.settings.render.lease_seconds,
    )


def report_progress(
    app: Services, worker: RenderWorker, job_id: UUID, progress: Progress
) -> RenderJob:
    """Record progress and renew the lease; JobNotHeld tells a worker to stop."""
    return app.renders.report(job_id, lease_for(app, worker), progress)


def export_file(app: Services, worker: RenderWorker, job_id: UUID) -> Path:
    """The export snapshot of a job the worker holds."""
    job = app.renders.holds(job_id, worker.id)
    stored = StoredFile(
        path=app.renders.export_path(job.id),
        media_type=ZIP.media_type,
        sha256=job.export_sha256,
        size_bytes=0,
    )
    return app.assets.path_of(stored)


def upload_part(app: Services, worker: RenderWorker, part: PartUpload, source: IO[bytes]) -> int:
    """Keep one part of the video; returns its size."""
    app.renders.holds(part.job_id, worker.id)
    return app.render_parts.save(part.job_id, part.index, source, part.sha256)


def complete_upload(
    app: Services, worker: RenderWorker, job_id: UUID, completion: Completion
) -> RenderJob:
    """Assemble the parts into the episode's render video and finish the job."""
    job = app.renders.holds(job_id, worker.id)
    reader = app.render_parts.reader(job_id, completion.parts)
    try:
        stored = store_video(app, job, reader)
    finally:
        reader.close()
    if stored.sha256 != completion.sha256.lower():
        app.render_parts.discard(job_id)
        raise VideoMismatch
    video = app.media.insert(render_media(job), stored)
    app.render_parts.discard(job_id)
    return app.renders.finish(job, RenderOutcome(media_id=video.id, findings=completion.findings))


def store_video(app: Services, job: RenderJob, reader: BinaryIO) -> StoredFile:
    """Check the assembled stream is a video and store it under the channel."""
    return app.assets.save_stream(
        job.channel, VIDEO_FORMATS, reader, app.settings.render.max_render_bytes
    )


def render_media(job: RenderJob) -> NewMedia:
    """The media row a finished render becomes."""
    name = f"Render {job.preset.value} {job.created_at:%Y-%m-%d %H:%M}"
    note = (
        f"Render {job.id}, export {job.export_sha256[:12]}, scene-render {job.engine_version},"
        f" srstudio {job.builder_version[:12]}"
    )
    return NewMedia(
        channel=job.channel,
        number=job.number,
        kind=MediaKind.RENDER,
        data=MediaInput(name=name, note=note),
    )


def fail_job(app: Services, worker: RenderWorker, job_id: UUID, failure: Failure) -> RenderJob:
    """End the worker's attempt; the job is queued again while attempts remain."""
    job = app.renders.holds(job_id, worker.id)
    app.render_parts.discard(job_id)
    return app.renders.fail(job, failure.reason, failure.is_retryable)
