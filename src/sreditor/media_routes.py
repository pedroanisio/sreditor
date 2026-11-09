"""Media routes: an episode's voice-over, sound effects, score and visual effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor import media_operations as media
from sreditor.assets import AssetMissing
from sreditor.deps import Access, Services, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import UploadIdempotencyDep
from sreditor.media import (
    EXTENSIONS,
    NAME_LENGTH,
    MediaFile,
    MediaInput,
    MediaKind,
    MediaNote,
    MediaNotFound,
    NewMedia,
    default_name,
)
from sreditor.media_operations import MediaChange, MediaQuery
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page

router = APIRouter(tags=["media"])

EPISODE = "/channels/{slug}/episodes/{number}"
ONE = f"{EPISODE}/media/{{media_id}}"
# A file never changes under its id, so a client may keep it for good.
CACHE_FOREVER = "private, max-age=31536000, immutable"
NOSNIFF = {"X-Content-Type-Options": "nosniff"}

UploadName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=NAME_LENGTH)]


class MediaUpload(BaseModel):
    """A multipart upload: the file, what it is for, and optionally a name and a note."""

    model_config = ConfigDict(frozen=True)

    kind: MediaKind = Field(description="What the file is for in the episode.")
    file: UploadFile = Field(
        description="Audio (MP3, WAV, M4A) for voice_over, sound_effect and score; "
        "video (MP4, WebM) for visual_effect and final_video; images (PNG, JPEG, WebP) for cover and "
        "still."
    )
    name: UploadName = Field(default="", description="Defaults to the file's name.")
    note: MediaNote = ""

    def described(self) -> MediaInput:
        """The name and note to store; without a name, the file's name without its extension."""
        name = self.name or default_name(self.file.filename, self.kind)
        return MediaInput(name=name, note=self.note)


def media_file(app: Services, found: MediaFile, headers: Mapping[str, str]) -> FileResponse:
    """The file behind a media row, played inline, with a name people recognise."""
    try:
        target = app.assets.path_of(found.stored())
    except AssetMissing as missing:
        raise MediaNotFound(str(found.id)) from missing
    return FileResponse(
        target,
        media_type=found.media_type,
        headers={**headers, "Cache-Control": CACHE_FOREVER},
        filename=found.name + EXTENSIONS[found.media_type],
        content_disposition_type="inline",
    )


def tagged(found: MediaFile) -> JSONResponse:
    """The file's details, with its revision as the ETag."""
    return JSONResponse(found.model_dump(mode="json"), headers={"ETag": etag(found.revision)})


def change_of(number: int, media_id: UUID, if_match: str | None) -> MediaChange:
    """The file a write targets and the revision it expects."""
    return MediaChange(
        number=number, media_id=media_id, expected_revision=expected_revision(if_match)
    )


def media_query(
    number: Number,
    kind: MediaKind | None = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> MediaQuery:
    """The list's filter and page, read from the query string."""
    return MediaQuery(number=number, kind=kind, cursor=cursor, limit=limit)


@router.get(f"{EPISODE}/media", summary="List an episode's media")
def list_media(
    query: Annotated[MediaQuery, Depends(media_query)], access: Access, app: ServicesDep
) -> Page[MediaFile]:
    """List an episode's media files, of one kind or all, in the order they were added.

    Each item has its media type, size and sha256; download one with download_media.
    """
    return media.list_media(app, access, query)


@router.post(
    f"{EPISODE}/media",
    status_code=status.HTTP_201_CREATED,
    response_model=MediaFile,
    summary="Upload a media file",
)
def upload_media(
    number: Number,
    upload: Annotated[MediaUpload, Form(media_type="multipart/form-data")],
    access: Access,
    app: ServicesDep,
    idempotency: UploadIdempotencyDep,
) -> JSONResponse:
    """Upload a voice-over, sound effect, score or visual effect to an episode.

    Send multipart/form-data with `kind` and `file`. The type comes from the file's content,
    not its name; a file of the wrong type or over the size limit is refused with 422. Needs
    content:write. Accepts an Idempotency-Key header, so a retry does not add the file twice.
    """
    new = NewMedia(
        channel=access.channel.slug, number=number, kind=upload.kind, data=upload.described()
    )
    return idempotency.run(
        lambda: media.add_media(app, access, new, upload.file.file), status.HTTP_201_CREATED
    )


@router.get(ONE, response_model=MediaFile, summary="Read a media file's details")
def get_media(number: Number, media_id: UUID, access: Access, app: ServicesDep) -> JSONResponse:
    """Read one media file's details. Its ETag is the revision to send as If-Match."""
    return tagged(media.get_media(app, access, number, media_id))


@router.get(f"{ONE}/content", response_class=FileResponse, summary="Download a media file")
def download_media(number: Number, media_id: UUID, access: Access, app: ServicesDep) -> Response:
    """Download a media file's content, with its media type. Supports Range requests."""
    return media_file(app, media.get_media(app, access, number, media_id), NOSNIFF)


@router.put(ONE, response_model=MediaFile, summary="Rename a media file")
def rename_media(
    target: WriteTarget, media_id: UUID, data: MediaInput, access: Access, app: ServicesDep
) -> JSONResponse:
    """Rename a media file or change its note; the file itself never changes. Send If-Match."""
    change = MediaChange(
        number=target.number, media_id=media_id, expected_revision=target.expected_revision
    )
    return tagged(media.rename_media(app, access, change, data))


@router.delete(ONE, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a media file")
def remove_media(
    number: Number, media_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Take a media file out of the episode. Needs content:write."""
    media.remove_media(app, access, change_of(number, media_id, if_match))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
