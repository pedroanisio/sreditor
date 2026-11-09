"""Brand routes: rename a channel and read or change its brand kit (rule project.brand)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Query, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor import brand_file_operations, operations
from sreditor.assets import BrandSlot
from sreditor.brand import BrandFileRef, BrandPatch
from sreditor.brand_file_operations import BrandFileChange, BrandFileSlot
from sreditor.channels import BrandKit, Channel, ChannelRename, FontRole, Platform
from sreditor.deps import Access, ServicesDep
from sreditor.etags import IfMatch, etag, expected_revision

router = APIRouter(tags=["channels"])

# What pydantic puts before the message of a ValueError raised by one of our validators.
VALUE_ERROR_PREFIX = "Value error, "


class BrandTarget(BaseModel):
    """Which kit a write changes (the channel's, or one account's) and the revision it expects."""

    model_config = ConfigDict(frozen=True)

    platform: Platform | None
    expected_revision: int | None


def brand_target(
    platform: Annotated[
        Platform | None, Query(description="Change this account's overrides, not the channel's.")
    ] = None,
    if_match: IfMatch = None,
) -> BrandTarget:
    """The kit and revision a brand write targets, from the query and If-Match."""
    return BrandTarget(platform=platform, expected_revision=expected_revision(if_match))


class BrandFileUpload(BaseModel):
    """A multipart upload of a brand file."""

    model_config = ConfigDict(frozen=True)

    file: UploadFile = Field(
        description="An image (PNG, JPEG, WebP; the avatar must be square), an opening or "
        "closing sting (MP4 or WebM) or a font file (WOFF2, TTF or OTF). The type comes from "
        "the content, never the name."
    )


def brand_file_change(
    slot: Annotated[BrandSlot, Path(description="The slot to fill or empty.")],
    if_match: IfMatch = None,
    role: Annotated[
        FontRole | None, Query(description="Required for slot font; not allowed for the others.")
    ] = None,
    platform: Annotated[
        Platform | None, Query(description="Change this account's overrides, not the channel's.")
    ] = None,
) -> BrandFileChange:
    """The slot a brand file write targets, with its role, account and If-Match revision."""
    try:
        reference = BrandFileRef(slot=slot, role=role)
    except ValidationError as invalid:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "role"),
                    "msg": str(error["msg"]).removeprefix(VALUE_ERROR_PREFIX),
                    "input": role,
                }
                for error in invalid.errors()
            ]
        ) from invalid
    return BrandFileChange(
        target=BrandFileSlot(file=reference, platform=platform),
        expected_revision=expected_revision(if_match),
    )


@router.get("/channels/{slug}/brand", summary="Get the brand kit")
def get_brand(access: Access, response: Response, platform: Platform | None = None) -> BrandKit:
    """The channel's brand kit: colors with roles, fonts by role, logo, avatar and stings.

    With `platform`, that account's overrides are applied. Its ETag is the revision to send as
    If-Match to patch_brand or rename_channel.
    """
    kit = operations.get_brand(access, platform)
    response.headers["ETag"] = etag(access.revision)
    return kit


@router.patch("/channels/{slug}/brand", summary="Change brand colors and fonts")
def patch_brand(
    patch: BrandPatch,
    access: Access,
    services: ServicesDep,
    response: Response,
    target: Annotated[BrandTarget, Depends(brand_target)],
) -> BrandKit:
    """Change the brand kit's colors and fonts; what the body leaves out stays as it is.

    `colors` replaces the whole palette (#RRGGBB with a role: primary, secondary, accent, text
    or background; each role once, accent may repeat). `fonts` sets the roles it names (heading,
    body, caption) and removes a role set to null. `clear_files` empties file slots (upload a
    file with upload_brand_file). With `platform`, only that account's overrides
    change. Send If-Match with the ETag from get_brand or get_channel to avoid overwriting
    others. Owners and editors only.
    """
    revised = operations.update_brand(
        services, access, target.platform, patch, target.expected_revision
    )
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel.brand_for(target.platform)


@router.put("/channels/{slug}/brand/files/{slot}", summary="Upload a brand file")
def upload_brand_file(
    upload: Annotated[BrandFileUpload, Form(media_type="multipart/form-data")],
    change: Annotated[BrandFileChange, Depends(brand_file_change)],
    access: Access,
    services: ServicesDep,
    response: Response,
) -> BrandKit:
    """Put a file in a brand slot, replacing the one there; send multipart/form-data `file`.

    `slot` is avatar (square), logo, banner, watermark, thumbnail_template (images), intro (the
    opening sting) or outro (the closing sting) (MP4 or WebM), or font with `role` (heading,
    body or caption), a WOFF2, TTF or OTF file for a font whose family update_brand already
    set. The type comes from the content and a wrong type or a file over the limit is refused
    with 422. With `platform`, only that account's overrides change. Answers the kit; its ETag
    is the revision to send as If-Match to the next change. Needs owner or editor role and
    channels:write; send If-Match with the ETag from get_brand to avoid overwriting others.
    """
    revised = brand_file_operations.put_brand_file(services, access, change, upload.file.file)
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel.brand_for(change.target.platform)


@router.delete(
    "/channels/{slug}/brand/files/{slot}",
    status_code=status.HTTP_200_OK,
    summary="Remove a brand file",
)
def remove_brand_file(
    change: Annotated[BrandFileChange, Depends(brand_file_change)],
    access: Access,
    services: ServicesDep,
    response: Response,
) -> BrandKit:
    """Empty a brand slot (a font's file by `role`; its family stays). With `platform`, the
    channel's file shows again on that account. Answers the kit with its new ETag. Needs owner
    or editor role and channels:write; send If-Match with the ETag from get_brand.
    """
    patch = BrandPatch(clear_files=[change.target.file])
    revised = operations.update_brand(
        services, access, change.target.platform, patch, change.expected_revision
    )
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel.brand_for(change.target.platform)


@router.patch("/channels/{slug}", summary="Rename a channel")
def rename_channel(
    rename: ChannelRename,
    access: Access,
    services: ServicesDep,
    response: Response,
    if_match: IfMatch = None,
) -> Channel:
    """Change the channel's display name; its slug, which addresses it, never changes.

    Send If-Match with the channel's ETag to avoid overwriting a change you have not seen.
    Owners and editors only.
    """
    revised = operations.rename_channel(services, access, rename, expected_revision(if_match))
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel
