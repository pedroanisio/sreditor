"""Render jobs and the GPU workers that run them (rules project.render, .jobs and .workers).

A job holds a snapshot of the episode's srstudio export, so what renders is what was asked for
even if the episode changes meanwhile. Workers pull jobs and hold each on a lease they renew;
a lapsed lease sends the job back to the queue, and a job that fails MAX_ATTEMPTS times stays
failed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.database import Database, parse, stamp
from sreditor.security import hash_token

WORKER_TOKEN_PREFIX = "srw_"
# Characters of the secret kept in clear, so an admin can tell workers apart.
VISIBLE_CHARACTERS = len(WORKER_TOKEN_PREFIX) + 6
MAX_ATTEMPTS = 3
MAX_FINDINGS = 200
FAILURE_LENGTH = 2000

WorkerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
EngineVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
]


class RenderStatus(StrEnum):
    """Where a render job is in its life (rule project.render.jobs)."""

    QUEUED = "queued"
    BUILDING = "building"
    RENDERING = "rendering"
    ENCODING = "encoding"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


RUNNING = frozenset({RenderStatus.BUILDING, RenderStatus.RENDERING, RenderStatus.ENCODING})
FINISHED = frozenset({RenderStatus.DONE, RenderStatus.FAILED, RenderStatus.CANCELED})


class RenderPreset(StrEnum):
    """What the worker encodes: the full master, or a quick preview at a lower quality."""

    MASTER = "master"
    PREVIEW = "preview"


class ReportFinding(BaseModel):
    """One finding of scene-render's render report (scene-render-report/1)."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(max_length=120)
    severity: str = Field(max_length=20)
    message: str = Field(default="", max_length=2000)


class Gpu(BaseModel):
    """A GPU a worker reported, as `scene-render gpus --json` lists it."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(max_length=200)
    backend: str = Field(default="", max_length=40)
    device_type: str = Field(default="", max_length=40)


class RenderJob(BaseModel):
    """A request to render an episode, and how far it got."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    number: int
    preset: RenderPreset
    status: RenderStatus
    progress: float
    export_sha256: str
    engine_version: str
    worker_id: UUID | None
    attempts: int
    failure: str | None
    findings: list[ReportFinding]
    media_id: UUID | None
    requested_by: str
    revision: int
    created_at: datetime
    updated_at: datetime


class RenderWorker(BaseModel):
    """A registered GPU machine; its secret is shown once and stored only as a hash."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    prefix: str
    created_by: UUID
    created_at: datetime
    last_seen_at: datetime | None
    engine_version: str | None
    gpus: list[Gpu]
    is_revoked: bool


class NewWorker(BaseModel):
    """A worker just registered, with the secret its machine must keep."""

    model_config = ConfigDict(frozen=True)

    worker: RenderWorker
    secret: str


class NewJob(BaseModel):
    """What a new job renders: the episode, its export snapshot, the preset and engine."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    preset: RenderPreset
    export_sha256: str
    export_path: str
    engine_version: str
    requested_by: str


class Lease(BaseModel):
    """A worker's hold on a job: who holds it and until when."""

    model_config = ConfigDict(frozen=True)

    worker_id: UUID
    until: datetime


class Progress(BaseModel):
    """What a worker reports while it works on a job."""

    model_config = ConfigDict(frozen=True)

    status: RenderStatus
    progress: float = Field(ge=0, le=1)


class RenderOutcome(BaseModel):
    """How a job ended: its video, the scene it was built into and its report's findings."""

    model_config = ConfigDict(frozen=True)

    media_id: UUID
    scene_path: str | None = None
    findings: list[ReportFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)


class RenderNotFound(LookupError):
    """No such render job, or not in this episode."""


class WorkerNotFound(LookupError):
    """No such render worker."""


class JobNotHeld(ValueError):
    """The worker does not hold this job's lease, or the job already finished."""

    def __init__(self, job_id: UUID, status: RenderStatus) -> None:
        super().__init__(f"Render {job_id} is {status.value} and not held by this worker")
        self.job_id = job_id
        self.status = status


class RenderFinished(ValueError):
    """The job already finished, so it cannot be canceled."""

    def __init__(self, job: RenderJob) -> None:
        super().__init__(f"Render {job.id} is already {job.status.value}")
        self.job = job


JOB_COLUMNS = (
    "id, channel_slug, number, preset, status, progress, export_sha256, export_path,"
    " engine_version, worker_id, lease_expires_at, attempts, failure, findings, media_id,"
    " scene_path, requested_by, revision, created_at, updated_at"
)
WORKER_COLUMNS = (
    "id, name, prefix, created_by, created_at, last_seen_at, engine_version, gpus, revoked_at"
)


def optional_uuid(value: str | None) -> UUID | None:
    """A stored id, or None when the column is empty."""
    return UUID(value) if value else None


def to_job(row: sqlite3.Row) -> RenderJob:
    """A render_jobs row as a model."""
    return RenderJob(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        number=row["number"],
        preset=RenderPreset(row["preset"]),
        status=RenderStatus(row["status"]),
        progress=row["progress"],
        export_sha256=row["export_sha256"],
        engine_version=row["engine_version"],
        worker_id=optional_uuid(row["worker_id"]),
        attempts=row["attempts"],
        failure=row["failure"],
        findings=[ReportFinding.model_validate(item) for item in json.loads(row["findings"])],
        media_id=optional_uuid(row["media_id"]),
        requested_by=row["requested_by"],
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def to_worker(row: sqlite3.Row) -> RenderWorker:
    """A render_workers row as a model."""
    return RenderWorker(
        id=UUID(row["id"]),
        name=row["name"],
        prefix=row["prefix"],
        created_by=UUID(row["created_by"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_seen_at=parse(row["last_seen_at"]),
        engine_version=row["engine_version"],
        gpus=[Gpu.model_validate(item) for item in json.loads(row["gpus"])],
        is_revoked=row["revoked_at"] is not None,
    )


class RenderStore:
    """Render jobs and workers in the application database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def now(self) -> datetime:
        """The current time from the database's clock, which tests replace."""
        return self.database.clock()

    def add(self, new: NewJob) -> RenderJob:
        """Queue a job."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO render_jobs (id, channel_slug, number, preset, status, export_sha256,"
                " export_path, engine_version, requested_by, revision, created_at, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING {JOB_COLUMNS}",
                (
                    str(uuid4()),
                    new.channel,
                    new.number,
                    new.preset,
                    RenderStatus.QUEUED,
                    new.export_sha256,
                    new.export_path,
                    new.engine_version,
                    new.requested_by,
                    now,
                    now,
                ),
            ).fetchone()
        return to_job(row)

    def get(self, job_id: UUID) -> RenderJob:
        """One job by id."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM render_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise RenderNotFound(str(job_id))
        return to_job(row)

    def export_path(self, job_id: UUID) -> str:
        """Where the job's export snapshot sits in the assets directory."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT export_path FROM render_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise RenderNotFound(str(job_id))
        return str(row["export_path"])

    def of_episode(
        self, channel: str, number: int, after: int | None, limit: int
    ) -> list[RenderJob]:
        """The episode's jobs, newest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM render_jobs WHERE channel_slug = ? AND number = ?"
                " AND (? IS NULL OR position < ?) ORDER BY position DESC LIMIT ?",
                (channel, number, after, after, limit),
            ).fetchall()
        return [to_job(row) for row in rows]

    def position(self, job_id: UUID) -> int:
        """Where a job sits in the list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM render_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise RenderNotFound(str(job_id))
        return int(row["position"])

    def queue_place(self, job: RenderJob) -> int | None:
        """How many queued jobs are ahead of this one, or None when it is not queued."""
        if job.status is not RenderStatus.QUEUED:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS ahead FROM render_jobs WHERE status = ? AND position <"
                " (SELECT position FROM render_jobs WHERE id = ?)",
                (RenderStatus.QUEUED, str(job.id)),
            ).fetchone()
        return int(row["ahead"])

    def reusable(self, new: NewJob) -> RenderJob | None:
        """A job for the same export, preset and engine that is running or done with its video."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM render_jobs WHERE channel_slug = ? AND number = ?"
                " AND export_sha256 = ? AND preset = ? AND engine_version = ?"
                " AND status NOT IN (?, ?) AND (status != ? OR media_id IN"
                " (SELECT id FROM episode_media)) ORDER BY position DESC LIMIT 1",
                (
                    new.channel,
                    new.number,
                    new.export_sha256,
                    new.preset,
                    new.engine_version,
                    RenderStatus.FAILED,
                    RenderStatus.CANCELED,
                    RenderStatus.DONE,
                ),
            ).fetchone()
        return to_job(row) if row else None

    def claim(self, lease: Lease, engine_version: str) -> RenderJob | None:
        """Give the worker the oldest job it can run: queued, or one whose lease lapsed."""
        now = self.database.now()
        with self.database.connect() as connection:
            give_up_lapsed(connection, now)
            row = connection.execute(
                "UPDATE render_jobs SET status = ?, progress = 0, worker_id = ?,"
                " lease_expires_at = ?, attempts = attempts + 1, revision = revision + 1,"
                " updated_at = ? WHERE id = (SELECT id FROM render_jobs WHERE engine_version = ?"
                " AND (status = ? OR (status IN (?, ?, ?) AND lease_expires_at < ?))"
                f" ORDER BY position LIMIT 1) RETURNING {JOB_COLUMNS}",
                (
                    RenderStatus.BUILDING,
                    str(lease.worker_id),
                    stamp(lease.until),
                    now,
                    engine_version,
                    RenderStatus.QUEUED,
                    *sorted(RUNNING),
                    now,
                ),
            ).fetchone()
        return to_job(row) if row else None

    def report(self, job_id: UUID, lease: Lease, progress: Progress) -> RenderJob:
        """Record a worker's progress and renew its lease; JobNotHeld when it lost the job."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE render_jobs SET status = ?, progress = ?, lease_expires_at = ?,"
                " revision = revision + 1, updated_at = ? WHERE id = ? AND worker_id = ?"
                f" AND status IN (?, ?, ?) RETURNING {JOB_COLUMNS}",
                (
                    progress.status,
                    progress.progress,
                    stamp(lease.until),
                    self.database.now(),
                    str(job_id),
                    str(lease.worker_id),
                    *sorted(RUNNING),
                ),
            ).fetchone()
        if row is None:
            raise JobNotHeld(job_id, self.get(job_id).status)
        return to_job(row)

    def holds(self, job_id: UUID, worker_id: UUID) -> RenderJob:
        """The job, if this worker holds it and it still runs; JobNotHeld otherwise."""
        job = self.get(job_id)
        if job.worker_id != worker_id or job.status not in RUNNING:
            raise JobNotHeld(job_id, job.status)
        return job

    def finish(self, job: RenderJob, outcome: RenderOutcome) -> RenderJob:
        """Mark the job done with its video, scene and findings."""
        findings = json.dumps([item.model_dump() for item in outcome.findings])
        return self.close(
            job,
            "status = ?, progress = 1, media_id = ?, scene_path = ?, findings = ?",
            (RenderStatus.DONE, str(outcome.media_id), outcome.scene_path, findings),
        )

    def fail(self, job: RenderJob, reason: str, is_retryable: bool) -> RenderJob:
        """End the attempt: back to the queue while attempts remain, else failed."""
        again = is_retryable and job.attempts < MAX_ATTEMPTS
        status = RenderStatus.QUEUED if again else RenderStatus.FAILED
        return self.close(
            job, "status = ?, progress = 0, failure = ?", (status, reason[:FAILURE_LENGTH])
        )

    def cancel(self, job: RenderJob) -> RenderJob:
        """Stop a queued or running job; its worker learns at its next report."""
        if job.status in FINISHED:
            raise RenderFinished(job)
        return self.close(job, "status = ?", (RenderStatus.CANCELED,))

    def close(self, job: RenderJob, assignments: str, values: tuple[object, ...]) -> RenderJob:
        """Apply a final change to a job that is still where the caller saw it."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"UPDATE render_jobs SET {assignments}, lease_expires_at = NULL,"
                " revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?"
                f" RETURNING {JOB_COLUMNS}",
                (*values, self.database.now(), str(job.id), job.revision),
            ).fetchone()
        if row is None:
            raise JobNotHeld(job.id, self.get(job.id).status)
        return to_job(row)

    def register(self, name: str, created_by: UUID) -> NewWorker:
        """Register a worker and make its secret, which is never stored."""
        secret = WORKER_TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO render_workers (id, name, token_hash, prefix, created_by, created_at)"
                f" VALUES (?, ?, ?, ?, ?, ?) RETURNING {WORKER_COLUMNS}",
                (
                    str(uuid4()),
                    name,
                    hash_token(secret),
                    secret[:VISIBLE_CHARACTERS],
                    str(created_by),
                    self.database.now(),
                ),
            ).fetchone()
        return NewWorker(worker=to_worker(row), secret=secret)

    def workers(self) -> list[RenderWorker]:
        """Every worker, in the order they were registered."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {WORKER_COLUMNS} FROM render_workers ORDER BY created_at, name"
            ).fetchall()
        return [to_worker(row) for row in rows]

    def revoke(self, worker_id: UUID) -> RenderWorker:
        """Stop a worker's secret from working; its running job returns to the queue later."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE render_workers SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?"
                f" RETURNING {WORKER_COLUMNS}",
                (self.database.now(), str(worker_id)),
            ).fetchone()
        if row is None:
            raise WorkerNotFound(str(worker_id))
        return to_worker(row)

    def authenticate(self, secret: str) -> RenderWorker | None:
        """The worker a secret belongs to, unless it was revoked."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {WORKER_COLUMNS} FROM render_workers"
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (hash_token(secret),),
            ).fetchone()
        return to_worker(row) if row else None

    def seen(self, worker_id: UUID, engine_version: str, gpus: list[Gpu]) -> None:
        """Record that the worker asked for work, with its engine and GPUs."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE render_workers SET last_seen_at = ?, engine_version = ?, gpus = ?"
                " WHERE id = ?",
                (
                    self.database.now(),
                    engine_version,
                    json.dumps([gpu.model_dump() for gpu in gpus]),
                    str(worker_id),
                ),
            )


def give_up_lapsed(connection: sqlite3.Connection, now: str) -> None:
    """Fail the lapsed jobs that used all their attempts, so they leave the queue."""
    connection.execute(
        "UPDATE render_jobs SET status = ?, failure = ?, lease_expires_at = NULL,"
        " revision = revision + 1, updated_at = ? WHERE status IN (?, ?, ?)"
        " AND lease_expires_at < ? AND attempts >= ?",
        (
            RenderStatus.FAILED,
            "The worker stopped reporting on every attempt",
            now,
            *sorted(RUNNING),
            now,
            MAX_ATTEMPTS,
        ),
    )


def lease_until(now: datetime, seconds: int) -> datetime:
    """When a lease taken or renewed now runs out."""
    return now + timedelta(seconds=seconds)
