"""Idempotency keys for POST requests that create something (rule agentic.idempotency).

The first request with a key reserves it, runs, and stores its response. A retry with the same
key and body gets that stored response back instead of creating a duplicate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import UploadFile

from sreditor.deps import CurrentUser, ServicesDep, request_token
from sreditor.idempotency_store import IdempotencyStore, KeyInProgress, KeyReused, StoredResponse
from sreditor.problems import ProblemError

KEY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_KEY_LENGTH = 255
IN_PROGRESS_RETRY_SECONDS = 1
TOKEN_KEY_PREFIX = "token:"


@contextmanager
def as_problems() -> Iterator[None]:
    """Answer the store's refusals as the problem responses agents expect."""
    try:
        yield
    except KeyReused as reused:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This Idempotency-Key was already used for a different request",
            "idempotency-key-reused",
        ) from reused
    except KeyInProgress as running:
        raise ProblemError(
            status.HTTP_409_CONFLICT,
            "A request with this Idempotency-Key is still in progress; retry later",
            "idempotency-key-in-progress",
            headers={"Retry-After": str(IN_PROGRESS_RETRY_SECONDS)},
        ) from running


def caller_key(key: str | None, token_id: UUID | None) -> str | None:
    """The key as stored: a token's keys are its own, so one token never replays another's result.

    Keys belong to the user, and tokens of one user may hold different scopes and channels; without
    this, a narrow token could replay what a broader one created (SRED-5).
    """
    if key is None or token_id is None:
        return key
    return f"{TOKEN_KEY_PREFIX}{token_id}:{key}"


def token_id_of(request: Request) -> UUID | None:
    """The id of the API token behind the request, or None for a signed-in session."""
    token = request_token(request)
    return token.id if token is not None else None


def request_hash(method: str, path: str, body: bytes) -> str:
    """Fingerprint a request, so a key reused for a different request can be detected."""
    return hashlib.sha256(b"\n".join([method.encode(), path.encode(), body])).hexdigest()


@dataclass(frozen=True)
class Idempotency:
    """One request's idempotency key and fingerprint, bound to the store and the user."""

    store: IdempotencyStore
    user_id: UUID
    key: str | None
    request_hash: str

    def run(self, produce: Callable[[], BaseModel], status_code: int) -> JSONResponse:
        """Run `produce` at most once per key; replays answer with the stored response."""
        if self.key is None:
            return JSONResponse(produce().model_dump(mode="json"), status_code)
        with as_problems():
            stored = self.store.begin(self.user_id, self.key, self.request_hash)
        if stored is not None:
            return JSONResponse(stored.body, stored.status_code, headers={REPLAYED_HEADER: "true"})
        try:
            body = produce().model_dump(mode="json")
        except BaseException:
            self.store.release(self.user_id, self.key)
            raise
        self.store.complete(self.user_id, self.key, StoredResponse(status_code, body))
        return JSONResponse(body, status_code)

    def attempt[T: BaseModel](self, produce: Callable[[], T], model: type[T]) -> tuple[T, bool]:
        """Run `produce` at most once per key, for callers outside HTTP such as MCP tools.

        Returns the result and whether it is the stored result of an earlier call.
        """
        if self.key is None:
            return produce(), False
        with as_problems():
            stored = self.store.begin(self.user_id, self.key, self.request_hash)
        if stored is not None:
            return model.model_validate(stored.body), True
        try:
            made = produce()
        except BaseException:
            self.store.release(self.user_id, self.key)
            raise
        body = made.model_dump(mode="json")
        self.store.complete(self.user_id, self.key, StoredResponse(status.HTTP_201_CREATED, body))
        return made, False


KeyHeader = Annotated[
    str | None,
    Header(
        alias=KEY_HEADER,
        min_length=1,
        max_length=MAX_KEY_LENGTH,
        description="A unique key per intended operation; a retry with it never repeats it.",
    ),
]
# Uploaded files are hashed in pieces of this size.
HASH_CHUNK_BYTES = 1024 * 1024


async def idempotency(
    request: Request, user: CurrentUser, app: ServicesDep, key: KeyHeader = None
) -> Idempotency:
    """Bind the request's Idempotency-Key header and body fingerprint to its user."""
    body = await request.body()
    return Idempotency(
        store=IdempotencyStore(app.users.database),
        user_id=user.id,
        key=caller_key(key, token_id_of(request)),
        request_hash=request_hash(request.method, request.url.path, body),
    )


async def file_digest(upload: UploadFile) -> bytes:
    """The sha256 of an uploaded file, read in chunks and rewound for the route."""
    digest = hashlib.sha256()
    while chunk := await upload.read(HASH_CHUNK_BYTES):
        digest.update(chunk)
    await upload.seek(0)
    return digest.digest()


async def upload_idempotency(
    request: Request, user: CurrentUser, app: ServicesDep, key: KeyHeader = None
) -> Idempotency:
    """Like `idempotency`, for a multipart upload, hashing each file in chunks."""
    # Reading the raw body would hold a whole media upload in memory; the parsed form is on disk.
    digest = hashlib.sha256(f"{request.method}\n{request.url.path}".encode())
    form = await request.form()
    for name, value in sorted(form.multi_items(), key=lambda item: item[0]):
        part = await file_digest(value) if isinstance(value, UploadFile) else value.encode()
        digest.update(b"\n" + name.encode() + b"=" + part)
    return Idempotency(
        store=IdempotencyStore(app.users.database),
        user_id=user.id,
        key=caller_key(key, token_id_of(request)),
        request_hash=digest.hexdigest(),
    )


IdempotencyDep = Annotated[Idempotency, Depends(idempotency)]
UploadIdempotencyDep = Annotated[Idempotency, Depends(upload_idempotency)]
