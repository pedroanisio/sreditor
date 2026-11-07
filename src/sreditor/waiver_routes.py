"""Waiver routes: the signed releases behind an episode (rule project.waivers)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sreditor import waiver_operations as waivers
from sreditor.assets import AssetMissing
from sreditor.deps import Access, Services, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import UploadIdempotencyDep
from sreditor.media_routes import NOSNIFF
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.waiver_operations import WaiverChange
from sreditor.waivers import Covers, NewWaiver, Signer, Waiver, WaiverInput, WaiverNotFound

router = APIRouter(tags=["waivers"])

EPISODE = "/channels/{slug}/episodes/{number}"
ONE = f"{EPISODE}/waivers/{{waiver_id}}"
# Personal data: kept by the browser only for this person, and never by shared caches.
CACHE_PRIVATE = "private, no-store"


class WaiverUpload(BaseModel):
    """A multipart upload: the signed file and what it says."""

    model_config = ConfigDict(frozen=True)

    file: UploadFile = Field(description="The signed waiver: PDF, JPEG or PNG.")
    signer: Signer = Field(description="Who signed it.")
    covers: Covers = Field(default="", description="What it lets the channel use.")
    signed_on: date | None = None

    def described(self) -> WaiverInput:
        """What the waiver says, without the file."""
        return WaiverInput(signer=self.signer, covers=self.covers, signed_on=self.signed_on)


def waiver_file(app: Services, found: Waiver, headers: Mapping[str, str]) -> FileResponse:
    """The signed file behind a waiver, always as a download (rule project.waivers)."""
    try:
        target = app.assets.path_of(found.stored())
    except AssetMissing as missing:
        raise WaiverNotFound(str(found.id)) from missing
    return FileResponse(
        target,
        media_type=found.media_type,
        headers={**headers, "Cache-Control": CACHE_PRIVATE},
        filename=found.filename(),
    )


def tagged(found: Waiver) -> JSONResponse:
    """The waiver's details, with its revision as the ETag."""
    return JSONResponse(found.model_dump(mode="json"), headers={"ETag": etag(found.revision)})


@router.get(f"{EPISODE}/waivers", summary="List an episode's waivers")
def list_waivers(
    number: Number,
    access: Access,
    app: ServicesDep,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[Waiver]:
    """List the signed waivers behind an episode, in the order they were added.

    Waivers hold personal data: only owners and editors may read them, and a token needs the
    waivers:read scope. Download one's file with download_waiver.
    """
    return waivers.list_waivers(app, access, number, cursor, limit)


@router.post(
    f"{EPISODE}/waivers",
    status_code=status.HTTP_201_CREATED,
    response_model=Waiver,
    summary="Upload a signed waiver",
)
def upload_waiver(
    number: Number,
    upload: Annotated[WaiverUpload, Form(media_type="multipart/form-data")],
    access: Access,
    app: ServicesDep,
    idempotency: UploadIdempotencyDep,
) -> JSONResponse:
    """Upload a signed waiver (image, voice or location release) to an episode.

    Send multipart/form-data with `file` and `signer`. The type comes from the file's content;
    a file that is not PDF, JPEG or PNG, or is over the size limit, is refused with 422. Needs
    an owner or editor and, for a token, both waivers:read and editorial:write. Accepts an
    Idempotency-Key header, so a retry does not add the waiver twice.
    """
    new = NewWaiver(channel=access.channel.slug, number=number, data=upload.described())
    return idempotency.run(
        lambda: waivers.add_waiver(app, access, new, upload.file.file), status.HTTP_201_CREATED
    )


@router.get(ONE, response_model=Waiver, summary="Read a waiver's details")
def get_waiver(number: Number, waiver_id: UUID, access: Access, app: ServicesDep) -> JSONResponse:
    """Read one waiver's details. Its ETag is the revision to send as If-Match."""
    return tagged(waivers.get_waiver(app, access, number, waiver_id))


@router.get(f"{ONE}/content", response_class=FileResponse, summary="Download a waiver")
def download_waiver(number: Number, waiver_id: UUID, access: Access, app: ServicesDep) -> Response:
    """Download a waiver's signed file, always as an attachment."""
    return waiver_file(app, waivers.get_waiver(app, access, number, waiver_id), NOSNIFF)


@router.put(ONE, response_model=Waiver, summary="Update a waiver's details")
def update_waiver(
    target: WriteTarget, waiver_id: UUID, data: WaiverInput, access: Access, app: ServicesDep
) -> JSONResponse:
    """Change who signed a waiver, when, or what it covers; the file never changes. Send
    If-Match. Needs an owner or editor and, for a token, waivers:read and editorial:write."""
    change = WaiverChange(
        number=target.number, waiver_id=waiver_id, expected_revision=target.expected_revision
    )
    return tagged(waivers.update_waiver(app, access, change, data))


@router.delete(ONE, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a waiver")
def remove_waiver(
    number: Number, waiver_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Take a waiver out of the episode. Needs an owner or editor and, for a token, waivers:read
    and editorial:write."""
    change = WaiverChange(
        number=number, waiver_id=waiver_id, expected_revision=expected_revision(if_match)
    )
    waivers.remove_waiver(app, access, change)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
