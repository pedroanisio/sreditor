"""Public routes for signed file links: no Authorization header, the link is the credential.

Agents get a link from the MCP tools get_file_link, create_upload_link or check_srstudio_export
(rule agentic.mcp.files) and fetch or send the file from a sandbox that cannot use the MCP
connection's OAuth token. A link works once, for 15 minutes, for the one target it was issued for.
"""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from sreditor import file_operations
from sreditor.assets import BYTES_PER_MEGABYTE, CHUNK_BYTES, FileTooLarge
from sreditor.deps import ServicesDep
from sreditor.etags import etag
from sreditor.file_links import FileLinkNotFound, LinkGone
from sreditor.file_operations import FILES_PATH
from sreditor.media import MediaFile
from sreditor.problems import problem_response, problem_type
from sreditor.web.routes import SECURITY_HEADERS

router = APIRouter(tags=["files"])

LINK = FILES_PATH + "/{link}"
# A link is spent once, so the file must not be kept by browsers or proxies.
NO_STORE = "private, no-store"


def discard(folder: Path) -> None:
    """Remove the folder a built export was written in, once it was sent."""
    shutil.rmtree(folder, ignore_errors=True)


def spooled() -> BinaryIO:
    """A temporary file that stays in memory until it outgrows one chunk, then moves to disk."""
    return cast(BinaryIO, SpooledTemporaryFile(max_size=CHUNK_BYTES))


@router.get(
    LINK,
    response_class=FileResponse,
    summary="Download a file with a signed link",
    responses={
        404: {"description": "No such link."},
        410: {"description": "The link was used already, or has expired."},
    },
)
def fetch_file_by_link(link: str, app: ServicesDep) -> FileResponse:
    """Download the file a download link from get_file_link or check_srstudio_export points to.

    Send no Authorization header: the link is the credential. It works once and expires 15
    minutes after it was issued; ask the tool for a new one when it answers 410.
    """
    delivery = file_operations.redeem_download(app, link)
    cleanup = BackgroundTask(discard, delivery.cleanup) if delivery.cleanup else None
    return FileResponse(
        delivery.path,
        media_type=delivery.media_type,
        headers={**SECURITY_HEADERS, "Cache-Control": NO_STORE},
        filename=delivery.filename,
        content_disposition_type="inline" if delivery.is_inline else "attachment",
        background=cleanup,
    )


async def copy_body(request: Request, target: BinaryIO, max_bytes: int) -> None:
    """Write the request body to `target` as it arrives; FileTooLarge once it passes the limit."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FileTooLarge(size=max_bytes // BYTES_PER_MEGABYTE)
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > max_bytes:
            raise FileTooLarge(size=max_bytes // BYTES_PER_MEGABYTE)
        target.write(chunk)


@router.put(
    LINK,
    status_code=status.HTTP_201_CREATED,
    response_model=MediaFile,
    summary="Upload a media file with a signed link",
    responses={
        404: {"description": "No such link."},
        410: {"description": "The link was used already, or has expired."},
        422: {"description": "The file is of the wrong type or over the size limit."},
    },
)
async def send_file_by_link(link: str, request: Request, app: ServicesDep) -> JSONResponse:
    """Upload the file with the PUT body of an upload link from create_upload_link.

    Send the file's bytes as the request body and no Authorization header. The type comes from
    the content, never the name; a wrong type or a file over the limit is refused with 422 and
    the link stays usable. A success creates the episode's media file and answers with it.
    """
    with (
        file_operations.claimed(app, link, is_upload=True) as upload,
        closing(spooled()) as body,
    ):
        await copy_body(request, body, app.media.max_bytes)
        body.seek(0)
        found = await run_in_threadpool(file_operations.store_upload, app, upload, body)
    return JSONResponse(
        found.model_dump(mode="json"),
        status.HTTP_201_CREATED,
        headers={"ETag": etag(found.revision)},
    )


def install_error_handlers(app: FastAPI) -> None:
    """A link that does not exist is 404; one used or expired is 410."""

    @app.exception_handler(FileLinkNotFound)
    async def link_not_found(request: Request, _: FileLinkNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            "No such file link; get_file_link and create_upload_link issue them",
            problem_type("link-not-found"),
        )

    @app.exception_handler(LinkGone)
    async def link_gone(request: Request, _: LinkGone) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_410_GONE,
            "This file link was used already or has expired, or its token was revoked; "
            "ask for a new one with get_file_link or create_upload_link",
            problem_type("link-gone"),
        )
