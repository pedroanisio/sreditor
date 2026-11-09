"""Brand file uploads shared by the JSON API and signed upload links (rule project.brand).

A file is checked by its content and stored by hash like the one a page uploads; the kit then
points to it, at the channel's level or in one account's overrides. Changing a kit needs the
right to change the channel (channels:write for a token), as the brand page does.
"""

from __future__ import annotations

from typing import BinaryIO

from fastapi import status
from pydantic import BaseModel, ConfigDict

from sreditor import operations
from sreditor.brand import BrandFileRef, own_brand, with_brand, with_file, with_font_file
from sreditor.channels import BrandKit, Platform
from sreditor.deps import ChannelAccess, Services
from sreditor.members import Permission
from sreditor.operations import Revised, require_account
from sreditor.problems import ProblemError

FONT_FAMILY_FIRST = "font-family-missing"


class BrandFileSlot(BaseModel):
    """Where an upload goes: a slot (with its font role) at the channel's level or an account's."""

    model_config = ConfigDict(frozen=True)

    file: BrandFileRef
    platform: Platform | None = None


class BrandFileChange(BaseModel):
    """A slot to fill and the channel revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    target: BrandFileSlot
    expected_revision: int | None = None


def check_font_family(kit: BrandKit, ref: BrandFileRef) -> None:
    """Refuse a font file for a role whose family is not chosen yet, naming the call that sets it."""
    if ref.role is not None and ref.role not in kit.fonts:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The brand kit has no {ref.role} font yet; set its family with update_brand "
            "(PATCH the brand's fonts), then upload its file",
            FONT_FAMILY_FIRST,
            errors=[
                {
                    "location": ["query", "role"],
                    "message": f"No {ref.role} font family is set",
                    "type": FONT_FAMILY_FIRST,
                }
            ],
        )


def check_target(access: ChannelAccess, target: BrandFileSlot) -> BrandKit:
    """The kit the target lives in, once the role and scope allow changing it and it exists."""
    access.require(Permission.WRITE_CHANNEL)
    require_account(access.channel, target.platform)
    kit = own_brand(access.channel, target.platform)
    check_font_family(kit, target.file)
    return kit


def put_brand_file(
    app: Services, access: ChannelAccess, change: BrandFileChange, source: BinaryIO
) -> Revised:
    """Check and store the file, then point the kit's slot at it, replacing the file there."""
    target = change.target
    kit = check_target(access, target)
    # One byte more than the limit, so the store notices an oversized file instead of cutting it.
    content = source.read(app.assets.max_bytes + 1)
    asset = app.assets.save(access.channel.slug, target.file.slot, content)
    if target.file.role is not None:
        updated = with_font_file(kit, target.file.role, asset)
    else:
        updated = with_file(kit, target.file.slot, asset)
    channel = with_brand(access.channel, target.platform, updated)
    return operations.replace_channel(app, access, channel, change.expected_revision)
