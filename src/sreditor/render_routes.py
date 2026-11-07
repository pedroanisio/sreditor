"""Render routes: people and agents request renders, workers run them, admins register workers.

Rules project.render, project.render.jobs, project.render.workers, project.render.upload and
agentic.render.scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from tempfile import SpooledTemporaryFile
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from sreditor import render_operations as renders
from sreditor.deps import Access, CurrentUser, ServicesDep, sign_in_first
from sreditor.episode_routes import Number
from sreditor.idempotency import IdempotencyDep
from sreditor.media_operations import RenderNotUploadable
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type
from sreditor.render_operations import (
    Claimed,
    Completion,
    EngineMismatch,
    Failure,
    Hello,
    NotAdmin,
    PartUpload,
    RenderRequest,
    RenderView,
    VideoMismatch,
)
from sreditor.render_upload import PartRejected, PartsMissing
from sreditor.renders import (
    JobNotHeld,
    NewWorker,
    Progress,
    RenderFinished,
    RenderJob,
    RenderNotFound,
    RenderPreset,
    RenderWorker,
    WorkerName,
    WorkerNotFound,
)

router = APIRouter(tags=["renders"])
worker_router = APIRouter(tags=["render workers"])

EPISODE = "/channels/{slug}/episodes/{number}"
ONE = f"{EPISODE}/renders/{{render_id}}"
JOB = "/render-worker/jobs/{job_id}"
ZIP_MEDIA_TYPE = "application/zip"
SHA256_HEADER = "X-Content-SHA256"
# A part is kept in memory up to this size, then in a temporary file (rule project.render.upload).
SPOOL_IN_MEMORY_BYTES = 1024 * 1024


class RenderInput(BaseModel):
    """How to render the episode."""

    model_config = ConfigDict(frozen=True)

    preset: RenderPreset = RenderPreset.MASTER


class WorkerInput(BaseModel):
    """A name for a new worker, such as the machine's host name."""

    model_config = ConfigDict(frozen=True)

    name: WorkerName


def current_worker(request: Request, app: ServicesDep) -> RenderWorker:
    """The worker behind an `Authorization: Bearer srw_...` header."""
    scheme, _, secret = request.headers.get("authorization", "").partition(" ")
    worker = app.renders.authenticate(secret) if scheme.lower() == "bearer" and secret else None
    if worker is None:
        raise sign_in_first()
    return worker


Worker = Annotated[RenderWorker, Depends(current_worker)]


@router.post(
    f"{EPISODE}/renders",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RenderView,
    summary="Request a render of an episode",
)
def request_render(
    number: Number,
    data: RenderInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Queue a video render of the episode as it is now and answer at once; poll get_render for
    progress. The same export, preset and engine answers with the earlier render (is_reused)
    and spends no GPU time. Owners and editors; a token needs render:write. When the episode
    lacks a piece srstudio needs, 409 lists each one. Accepts an Idempotency-Key header."""
    request = RenderRequest(number=number, preset=data.preset)
    return idempotency.run(
        lambda: renders.request_render(app, access, request), status.HTTP_202_ACCEPTED
    )


@router.get(f"{EPISODE}/renders", summary="List an episode's renders")
def list_renders(
    number: Number,
    access: Access,
    app: ServicesDep,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[RenderView]:
    """List the episode's renders, newest first, each with its status, progress and, once
    done, its video. Pass `next_cursor` as `cursor` for more."""
    return renders.list_renders(app, access, number, cursor, limit)


@router.get(ONE, summary="Read a render")
def get_render(number: Number, render_id: UUID, access: Access, app: ServicesDep) -> RenderView:
    """Read one render: its status (queued, building, rendering, encoding, done, failed,
    canceled), progress from 0 to 1, place in the queue and scene-render's findings. Once
    done, `video` is the episode's media file and `download_path` downloads it."""
    return renders.get_render(app, access, number, render_id)


@router.post(f"{ONE}/cancel", summary="Cancel a render")
def cancel_render(number: Number, render_id: UUID, access: Access, app: ServicesDep) -> RenderView:
    """Stop a queued or running render; its worker stops at its next report. Owners and
    editors; a token needs render:write. 409 when the render already ended."""
    return renders.cancel_render(app, access, number, render_id)


@router.post(
    "/render-workers",
    status_code=status.HTTP_201_CREATED,
    response_model=NewWorker,
    summary="Register a render worker",
)
def register_worker(data: WorkerInput, user: CurrentUser, app: ServicesDep) -> NewWorker:
    """Register a GPU machine that runs the sreditor worker image. The answer holds its secret
    (`srw_...`) once; set it as the worker's SREDITOR_WORKER_TOKEN. Instance admins only. Takes
    no Idempotency-Key: a lost answer means revoking the worker and registering another."""
    return renders.register_worker(app, user, data.name)


@router.get("/render-workers", summary="List render workers")
def list_workers(user: CurrentUser, app: ServicesDep) -> list[RenderWorker]:
    """List every registered worker with the engine version and GPUs it last reported and
    when it last asked for work. Instance admins only."""
    return renders.list_workers(app, user)


@router.post("/render-workers/{worker_id}/revoke", summary="Revoke a render worker")
def revoke_worker(worker_id: UUID, user: CurrentUser, app: ServicesDep) -> RenderWorker:
    """Stop a worker's secret from working. A job it held returns to the queue when its lease
    lapses. Instance admins only."""
    return renders.revoke_worker(app, user, worker_id)


@worker_router.post(
    "/render-worker/claim",
    response_model=Claimed,
    responses={204: {"description": "No job is waiting."}},
    summary="Claim the next render job",
)
def claim_job(hello: Hello, worker: Worker, app: ServicesDep) -> Response:
    """For render workers: take the oldest waiting job, with where to fetch its export and how
    long the lease lasts; 204 when none waits. 409 when the worker's scene-render version is
    not the one the instance pins."""
    claimed = renders.claim_job(app, worker, hello)
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return JSONResponse(claimed.model_dump(mode="json"))


@worker_router.post(f"{JOB}/progress", summary="Report a render job's progress")
def report_progress(
    job_id: UUID, progress: Progress, worker: Worker, app: ServicesDep
) -> RenderJob:
    """For render workers: report the stage and progress and renew the lease. 409 means the
    job was canceled or given to another worker: stop working on it."""
    return renders.report_progress(app, worker, job_id, progress)


@worker_router.get(
    f"{JOB}/export",
    response_class=FileResponse,
    responses={200: {"content": {ZIP_MEDIA_TYPE: {}}}},
    summary="Download a render job's export",
)
def download_export(job_id: UUID, worker: Worker, app: ServicesDep) -> FileResponse:
    """For render workers: the srstudio zip snapshot the job renders."""
    return FileResponse(
        renders.export_file(app, worker, job_id),
        media_type=ZIP_MEDIA_TYPE,
        filename=f"{job_id}.zip",
    )


Sha256Header = Annotated[
    str,
    Header(alias=SHA256_HEADER, pattern="^[0-9a-fA-F]{64}$", description="The part's SHA-256."),
]
PartIndex = Annotated[int, Path(ge=0, description="The part's place, from 0.")]


def part_target(job_id: UUID, index: PartIndex, sha256: Sha256Header) -> PartUpload:
    """The part a PUT names: its job and index in the path, its SHA-256 in a header."""
    return PartUpload(job_id=job_id, index=index, sha256=sha256)


PartTarget = Annotated[PartUpload, Depends(part_target)]


@worker_router.put(f"{JOB}/parts/{{index}}", summary="Upload one part of a rendered video")
async def upload_part(
    part: PartTarget, request: Request, worker: Worker, app: ServicesDep
) -> dict[str, int]:
    """For render workers: send one part of the finished video as the raw body, at most
    SREDITOR_MAX_MEDIA_BYTES, with its SHA-256 in X-Content-SHA256. Sending a part again
    replaces it, so a failed part can simply be retried."""
    with SpooledTemporaryFile(SPOOL_IN_MEMORY_BYTES) as spooled:
        await spool(request, spooled, part.index, app.settings.max_media_bytes)
        size = renders.upload_part(app, worker, part, spooled)
    return {"index": part.index, "size_bytes": size}


async def spool(
    request: Request, target: SpooledTemporaryFile[bytes], index: int, max_bytes: int
) -> None:
    """Copy the request body into `target`, refused once it passes the part limit."""
    async for chunk in request.stream():
        target.write(chunk)
        if target.tell() > max_bytes:
            raise PartRejected(index, f"a part holds at most {max_bytes} bytes")
    target.seek(0)


@worker_router.post(f"{JOB}/complete", summary="Finish a render job's upload")
def complete_upload(
    job_id: UUID, completion: Completion, worker: Worker, app: ServicesDep
) -> RenderJob:
    """For render workers: join the uploaded parts 0..parts-1 into the episode's render video,
    check its type and SHA-256, record the report's findings and mark the job done. 422 lists
    missing parts or a checksum that does not match."""
    return renders.complete_upload(app, worker, job_id, completion)


@worker_router.post(f"{JOB}/fail", summary="Report a failed render attempt")
def fail_job(job_id: UUID, failure: Failure, worker: Worker, app: ServicesDep) -> RenderJob:
    """For render workers: give up on the attempt with the reason. A retryable failure returns
    the job to the queue while attempts remain; any other fails it for good."""
    return renders.fail_job(app, worker, job_id, failure)


def refused(request: Request, code: int, error: Exception, kind: str) -> JSONResponse:
    """A problem response for a render error, with its type."""
    return problem_response(request, code, str(error), problem_type(kind))


def install_error_handlers(app: FastAPI) -> None:
    """Render errors as RFC 9457 problems (rule agentic.errors.problem)."""
    statuses: dict[type[Exception], tuple[int, str]] = {
        RenderNotFound: (status.HTTP_404_NOT_FOUND, "render-not-found"),
        WorkerNotFound: (status.HTTP_404_NOT_FOUND, "worker-not-found"),
        NotAdmin: (status.HTTP_403_FORBIDDEN, "admin-only"),
        RenderFinished: (status.HTTP_409_CONFLICT, "render-finished"),
        JobNotHeld: (status.HTTP_409_CONFLICT, "job-not-held"),
        EngineMismatch: (status.HTTP_409_CONFLICT, "engine-mismatch"),
        PartRejected: (status.HTTP_422_UNPROCESSABLE_CONTENT, "part-rejected"),
        PartsMissing: (status.HTTP_422_UNPROCESSABLE_CONTENT, "parts-missing"),
        VideoMismatch: (status.HTTP_422_UNPROCESSABLE_CONTENT, "video-mismatch"),
    }
    for error_type, (code, kind) in statuses.items():
        app.add_exception_handler(error_type, handler_for(code, kind))

    @app.exception_handler(RenderNotUploadable)
    async def render_not_uploadable(request: Request, error: RenderNotUploadable) -> JSONResponse:
        message = str(error)
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            message,
            problem_type("render-not-uploadable"),
            errors=[{"location": ["body", "kind"], "message": message, "type": "enum"}],
        )


def handler_for(code: int, kind: str) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """An exception handler answering with this status and problem type."""

    async def handle(request: Request, error: Exception) -> JSONResponse:
        return refused(request, code, error, kind)

    return handle
