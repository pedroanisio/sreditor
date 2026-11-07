"""Third-party media routes: the channel's library of items made by others and their episode uses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Form, Path, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sreditor import external_media_operations as external
from sreditor.assets import AssetMissing
from sreditor.deps import Access, Services, ServicesDep
from sreditor.episode_routes import Number
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.external_media import (
    EXTENSIONS,
    EpisodeCredits,
    ExternalKind,
    ExternalMedia,
    ExternalMediaInput,
    ExternalMediaInUse,
    ExternalMediaNotFound,
    MediaUse,
    MediaUseInput,
    MediaUseNotFound,
    UseRefused,
)
from sreditor.external_media_operations import ItemChange, ItemQuery, UseChange, UseQuery
from sreditor.idempotency import IdempotencyDep
from sreditor.media_routes import CACHE_FOREVER, NOSNIFF
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["external-media"])

LIBRARY = "/channels/{slug}/external-media"
ITEM = f"{LIBRARY}/{{media_id}}"
USES = "/channels/{slug}/episodes/{number}/external-media"
USE = f"{USES}/{{use_id}}"

MediaId = Annotated[UUID, Path(description="The item's id, from list_external_media.")]
UseId = Annotated[UUID, Path(description="The use's id, from list_external_media_uses.")]


class CopyUpload(BaseModel):
    """A multipart upload of an item's copy."""

    model_config = ConfigDict(frozen=True)

    file: UploadFile = Field(
        description="Video (MP4, WebM), image (PNG, JPEG, WebP), audio (MP3, WAV, M4A) or "
        "PDF, matching the item's kind; a web page is kept as a PDF or a screenshot. A 3D model "
        "is GLB, FBX (binary) or BLEND; a texture PNG, JPEG, WebP, EXR, KTX2 or DDS; a shader "
        "UTF-8 text. Those three may also be a ZIP package, kept unopened."
    )


def item_change(media_id: MediaId, if_match: IfMatch = None) -> ItemChange:
    """The item a write targets and the revision its If-Match names."""
    return ItemChange(media_id=media_id, expected_revision=expected_revision(if_match))


def use_change(number: Number, use_id: UseId, if_match: IfMatch = None) -> UseChange:
    """The use a write targets and the revision its If-Match names."""
    return UseChange(number=number, use_id=use_id, expected_revision=expected_revision(if_match))


def item_query(
    kind: ExternalKind | None = None, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> ItemQuery:
    """Which page of the library to list, and of which kind."""
    return ItemQuery(kind=kind, cursor=cursor, limit=limit)


def use_query(number: Number, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT) -> UseQuery:
    """Which page of an episode's uses to list."""
    return UseQuery(number=number, cursor=cursor, limit=limit)


# Path ids and If-Match in one parameter, so routes keep to five (code.functions.args).
ItemTarget = Annotated[ItemChange, Depends(item_change)]
UseTarget = Annotated[UseChange, Depends(use_change)]
ItemListing = Annotated[ItemQuery, Depends(item_query)]
UseListing = Annotated[UseQuery, Depends(use_query)]


def tagged[T: ExternalMedia | MediaUse](found: T, response: Response) -> T:
    """The resource, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


def copy_file(app: Services, item: ExternalMedia, headers: Mapping[str, str]) -> FileResponse:
    """An item's copy: pictures, video and sound play inline, documents always download."""
    stored = item.stored()
    if item.copy_file is None or stored is None:
        raise ExternalMediaNotFound(f"{item.id} has no copy")
    try:
        target = app.assets.path_of(stored)
    except AssetMissing as missing:
        raise ExternalMediaNotFound(str(item.id)) from missing
    return FileResponse(
        target,
        media_type=item.copy_file.media_type,
        headers={**headers, "Cache-Control": CACHE_FOREVER},
        filename=item.title + EXTENSIONS[item.copy_file.media_type],
        content_disposition_type="inline" if item.copy_file.is_inline() else "attachment",
    )


@router.get(LIBRARY, summary="List third-party media")
def list_external_media(
    access: Access, app: ServicesDep, query: ItemListing
) -> Page[ExternalMedia]:
    """List the channel's third-party media: videos, images, audio, documents, web pages, 3D
    models, textures and shaders made by others, in the order they were added. Pass `kind` to see only one kind.
    """
    return external.list_items(app, access, query)


@router.post(
    LIBRARY,
    status_code=status.HTTP_201_CREATED,
    response_model=ExternalMedia,
    summary="Add third-party media",
)
def create_external_media(
    data: ExternalMediaInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Add an item made by others to the channel's library: its kind, title, page link,
    creator, rights, license and credit line. sreditor never downloads from the link; upload a
    copy with upload_external_media_copy. Needs content:write. Accepts an Idempotency-Key.
    """
    return idempotency.run(lambda: external.create_item(app, access, data), status.HTTP_201_CREATED)


@router.get(ITEM, summary="Read third-party media")
def get_external_media(
    media_id: MediaId, access: Access, app: ServicesDep, response: Response
) -> ExternalMedia:
    """Read one item of the library. Its ETag is the revision to send as If-Match."""
    return tagged(external.get_item(app, access, media_id), response)


@router.put(ITEM, summary="Replace third-party media")
def replace_external_media(
    target: ItemTarget,
    data: ExternalMediaInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> ExternalMedia:
    """Replace an item's details; its copy stays. Send If-Match to keep others' changes."""
    return tagged(external.replace_item(app, access, target, data), response)


@router.delete(ITEM, status_code=status.HTTP_204_NO_CONTENT, summary="Delete third-party media")
def delete_external_media(target: ItemTarget, access: Access, app: ServicesDep) -> Response:
    """Remove an item from the library. Needs channels:write and an editor role.

    Refused with 409 while episodes use it; remove those uses first.
    """
    external.delete_item(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(f"{ITEM}/episodes", summary="List an item's episodes")
def list_external_media_episodes(media_id: MediaId, access: Access, app: ServicesDep) -> list[int]:
    """The numbers of the episodes that use the item; it cannot be deleted while any do."""
    return external.item_episodes(app, access, media_id)


@router.put(f"{ITEM}/copy", summary="Upload a copy of third-party media")
def upload_external_media_copy(
    target: ItemTarget,
    upload: Annotated[CopyUpload, Form(media_type="multipart/form-data")],
    access: Access,
    app: ServicesDep,
    response: Response,
) -> ExternalMedia:
    """Upload a copy of the item, replacing any earlier one; send multipart/form-data `file`.

    The type comes from the content and must match the item's kind; a wrong type or a file
    over the size limit is refused with 422. Needs content:write; send If-Match to be safe.
    """
    return tagged(external.attach_copy(app, access, target, upload.file.file), response)


@router.delete(f"{ITEM}/copy", summary="Remove a copy of third-party media")
def remove_external_media_copy(
    target: ItemTarget, access: Access, app: ServicesDep, response: Response
) -> ExternalMedia:
    """Take the uploaded copy off an item; the item and its link stay. Needs content:write."""
    return tagged(external.detach_copy(app, access, target), response)


@router.get(
    f"{ITEM}/copy/content", response_class=FileResponse, summary="Download a third-party copy"
)
def download_external_media_copy(media_id: MediaId, access: Access, app: ServicesDep) -> Response:
    """Download an item's uploaded copy. Images, video and audio play inline; PDFs download.

    Answers 404 when the item has no copy.
    """
    return copy_file(app, external.get_item(app, access, media_id), NOSNIFF)


@router.get(USES, summary="List an episode's third-party media")
def list_external_media_uses(access: Access, app: ServicesDep, query: UseListing) -> Page[MediaUse]:
    """List how an episode uses third-party items: each use's item, time range or pages,
    purpose and scene, in the order they were added.
    """
    return external.list_uses(app, access, query)


@router.post(
    USES,
    status_code=status.HTTP_201_CREATED,
    response_model=MediaUse,
    summary="Use third-party media in an episode",
)
def add_external_media_use(
    number: Number,
    data: MediaUseInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Record one use of a library item in the episode: a time range for video and audio,
    pages for documents and web pages, the purpose, the scene and an optional credit.

    The item must be in the channel's library first (create_external_media). Needs
    content:write. Accepts an Idempotency-Key header, so a retry does not add it twice.
    """
    return idempotency.run(
        lambda: external.add_use(app, access, number, data), status.HTTP_201_CREATED
    )


@router.get(USE, summary="Read a third-party media use")
def get_external_media_use(
    number: Number, use_id: UseId, access: Access, app: ServicesDep, response: Response
) -> MediaUse:
    """Read one use of the episode. Its ETag is the revision to send as If-Match."""
    return tagged(external.get_use(app, access, number, use_id), response)


@router.put(USE, summary="Replace a third-party media use")
def replace_external_media_use(
    target: UseTarget, data: MediaUseInput, access: Access, app: ServicesDep, response: Response
) -> MediaUse:
    """Replace a use's item, range, purpose, scene, credit and note. Send If-Match."""
    return tagged(external.replace_use(app, access, target, data), response)


@router.delete(
    USE, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a third-party media use"
)
def remove_external_media_use(target: UseTarget, access: Access, app: ServicesDep) -> Response:
    """Take a use out of the episode; the item stays in the library. Needs content:write."""
    external.remove_use(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/channels/{slug}/episodes/{number}/credits", summary="Read an episode's credits")
def get_episode_credits(number: Number, access: Access, app: ServicesDep) -> EpisodeCredits:
    """The credit lines an episode owes for its third-party media, one per item, and the
    rights warnings to look at: unknown rights, a citation with no reason or range, a missing
    credit. Warnings never block a status.
    """
    return external.get_credits(app, access, number)


def install_error_handlers(app: FastAPI) -> None:
    """Third-party media errors as problem details: 404, 409 in use, 422 for a misfit use."""

    @app.exception_handler(ExternalMediaNotFound)
    async def item_not_found(request: Request, missing: ExternalMediaNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Third-party item {missing} not found; list_external_media shows the library",
        )

    @app.exception_handler(MediaUseNotFound)
    async def use_not_found(request: Request, missing: MediaUseNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Use {missing} not found; list_external_media_uses shows the episode's uses",
        )

    @app.exception_handler(ExternalMediaInUse)
    async def item_in_use(request: Request, taken: ExternalMediaInUse) -> JSONResponse:
        numbers = ", ".join(f"#{number}" for number in taken.episodes)
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            f"Third-party item {taken} is used by episodes {numbers}; remove those uses first",
            problem_type("external-media-in-use"),
            episodes=taken.episodes,
        )

    @app.exception_handler(UseRefused)
    async def use_refused(request: Request, refused: UseRefused) -> JSONResponse:
        message = str(refused)
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            message,
            problem_type("use-does-not-fit"),
            errors=[{"location": ["body", refused.field], "message": message, "type": "misfit"}],
        )
