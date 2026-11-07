"""Brand routes: rename a channel and read or change its brand kit (rule project.brand)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from sreditor import operations
from sreditor.brand import BrandPatch
from sreditor.channels import BrandKit, Channel, ChannelRename, Platform
from sreditor.deps import Access, ServicesDep
from sreditor.etags import IfMatch, etag, expected_revision

router = APIRouter(tags=["channels"])


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
    body, caption) and removes a role set to null. Files (logo, avatar, stings, font files) are
    uploaded on the brand page, not here. With `platform`, only that account's overrides
    change. Send If-Match with the ETag from get_brand or get_channel to avoid overwriting
    others. Owners and editors only.
    """
    revised = operations.update_brand(
        services, access, target.platform, patch, target.expected_revision
    )
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel.brand_for(target.platform)


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
