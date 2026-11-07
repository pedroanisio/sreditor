"""The brand kit: files and colors at the channel's level and each account's overrides.

Files are checked by their content, not their name, and stored by hash (rule web.app.assets).
They are served only to the channel's members, and only while the kit points to them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.assets import (
    BYTES_PER_MEGABYTE,
    AssetMissing,
    AssetRejected,
    BrandSlot,
    FileFamily,
    accept_attribute,
)
from sreditor.assets import format_names as slot_formats
from sreditor.brand import (
    NoSuchAccount,
    file_in,
    own_brand,
    referenced_assets,
    with_brand,
    with_color,
    with_file,
    without_color,
)
from sreditor.channels import BrandKit, Channel, Color, Platform
from sreditor.deps import ChannelAccess, Services, ServicesDep, within_rate_limit
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.operations import replace_channel
from sreditor.problems import ProblemError
from sreditor.repository import RevisionMismatch
from sreditor.web.forms import field_errors
from sreditor.web.labels import COLOR_ROLE_LABELS, SLOT_LABELS
from sreditor.web.routes import (
    SECURITY_HEADERS,
    TOO_MANY,
    TOO_MANY_ATTEMPTS,
    rejected_form,
    translator,
)
from sreditor.web.workspace.brand_rows import color_role_options, font_rows
from sreditor.web.workspace.forms import (
    ColorFields,
    RevisionFields,
    UploadFields,
    platform_or_none,
    revision_of,
)
from sreditor.web.workspace.shell import (
    CHANGED_MEANWHILE,
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    level_tabs,
    refused,
    require,
)

router = APIRouter()

BRAND_SUFFIX = "/brand"
CHOOSE_FILE = N_("Choose a file.")
# Files are named by their hash, so a browser may keep them for good.
CACHE_FOREVER = "private, max-age=31536000, immutable"


class BrandChange(BaseModel):
    """A change to the kit at one level, from a form that names the level and its revision."""

    model_config = ConfigDict(frozen=True)

    platform: Platform | None
    revision: int | None

    @classmethod
    def of(cls, platform: str, revision: str) -> BrandChange:
        """The level and revision as a form posts them."""
        return cls(platform=platform_or_none(platform), revision=revision_of(revision))


def brand_url(slug: str, platform: Platform | None) -> str:
    """The brand page at a level."""
    query = f"?platform={platform.value}" if platform else ""
    return channel_url(slug, BRAND_SUFFIX) + query


def file_rows(channel: Channel, platform: Platform | None) -> list[dict[str, Any]]:
    """Each slot with its own file and, on a platform, the channel's file it falls back to."""
    own = own_brand(channel, platform)
    return [
        {
            "slot": slot,
            "label": SLOT_LABELS[slot],
            "own": file_in(own, slot),
            "inherited": file_in(channel.brand, slot) if platform else None,
            "accept": accept_attribute(slot),
            "formats": slot_formats(slot),
        }
        for slot in BrandSlot
        if slot is not BrandSlot.FONT or file_in(own, slot) is not None
    ]


def colors_context(
    channel: Channel, platform: Platform | None, translate: Translator
) -> dict[str, Any]:
    """The palette the brand page lists, with its role labels and the picker's options."""
    return {
        "colors": own_brand(channel, platform).colors,
        "inherited_colors": channel.brand.colors if platform else [],
        "color_roles": color_role_options(translate),
        "role_labels": {role: translate(label) for role, label in COLOR_ROLE_LABELS.items()},
    }


def brand_page(
    request: Request,
    app: Services,
    access: ChannelAccess,
    platform: Platform | None,
    outcome: Outcome,
) -> Response:
    """The kit at one level, with the tabs for the others."""
    channel = access.channel
    translate = translator(request)
    context = {
        "errors": outcome.errors,
        "focus": outcome.focus,
        "values": outcome.values,
        "platform": platform,
        "tabs": level_tabs(channel, BRAND_SUFFIX, translate),
        "files": file_rows(channel, platform),
        "fonts": font_rows(channel, platform, translate),
        "image": FileFamily.IMAGE,
        "max_megabytes": app.assets.max_bytes // BYTES_PER_MEGABYTE,
        **colors_context(channel, platform, translate),
    }
    view = View(
        template="workspace/brand.html",
        section="brand",
        status_code=outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/brand")
def brand(request: Request, app: ServicesDep, access: PageAccess, platform: str = "") -> Response:
    """The brand kit at the channel's level, or an account's overrides."""
    level = platform_or_none(platform)
    if level is not None and access.channel.account(level) is None:
        raise PageNotFound(platform)
    return brand_page(request, app, access, level, Outcome())


def save_kit(app: Services, access: ChannelAccess, change: BrandChange, kit: BrandKit) -> None:
    """Store the kit at the change's level."""
    updated = with_brand(access.channel, change.platform, kit)
    replace_channel(app, access, updated, change.revision)


class KitWrite:
    """Runs a kit change and turns its refusals into the page's errors."""

    def __init__(self, request: Request, app: Services, access: ChannelAccess) -> None:
        self.request = request
        self.app = app
        self.access = access

    def apply(self, change: BrandChange, kit: BrandKit, flash: Flash) -> Response:
        """Save the kit and go back to its page, or show why it was not saved."""
        translate = translator(self.request)
        try:
            save_kit(self.app, self.access, change, kit)
        except ValidationError as invalid:
            return self.failed(change, Outcome.invalid(field_errors(invalid, translate)))
        except RevisionMismatch:
            return self.failed(change, Outcome.conflict({"form": translate(CHANGED_MEANWHILE)}))
        except ProblemError as problem:
            return self.failed(change, refused(problem, translate))
        url = brand_url(self.access.channel.slug, change.platform)
        return done(self.request, self.app, url, flash)

    def failed(self, change: BrandChange, outcome: Outcome) -> Response:
        """The brand page again, with the errors."""
        return brand_page(self.request, self.app, self.access, change.platform, outcome)

    def current(self, change: BrandChange) -> BrandKit:
        """The kit at the change's level; 404 when the account is gone."""
        try:
            return own_brand(self.access.channel, change.platform)
        except NoSuchAccount as missing:
            raise PageNotFound(str(change.platform)) from missing


def read_upload(fields: UploadFields, limit: int) -> bytes:
    """The uploaded bytes, reading one more than the limit so an oversized file is noticed."""
    if fields.file is None or not fields.file.filename:
        return b""
    return fields.file.file.read(limit + 1)


@router.post("/app/channels/{slug}/brand/files/{slot}")
def upload_file(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    slot: BrandSlot,
    fields: Annotated[UploadFields, Form()],
) -> Response:
    """Store a file in a slot, replacing the one there."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    translate = translator(request)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    kit = writer.current(change)
    if not within_rate_limit(request, app):
        busy = Outcome.refused({slot.value: translate(TOO_MANY_ATTEMPTS)}, TOO_MANY)
        return writer.failed(change, busy)
    content = read_upload(fields, app.assets.max_bytes)
    if not content:
        return writer.failed(change, Outcome.invalid({slot.value: translate(CHOOSE_FILE)}))
    try:
        asset = app.assets.save(access.channel.slug, slot, content)
    except AssetRejected as rejection:
        message = translate(rejection.message, **rejection.params)
        return writer.failed(change, Outcome.invalid({slot.value: message}))
    return writer.apply(change, with_file(kit, slot, asset), Flash.UPLOADED)


@router.post("/app/channels/{slug}/brand/files/{slot}/remove")
def remove_file(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    slot: BrandSlot,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Empty a slot; on a platform, the channel's file shows again."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    return writer.apply(change, with_file(writer.current(change), slot, None), Flash.REMOVED)


@router.post("/app/channels/{slug}/brand/colors")
def save_color(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[ColorFields, Form()],
) -> Response:
    """Add a color, or recolor the one with the same name."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    kit = writer.current(change)
    try:
        color = Color.model_validate(
            {"name": fields.name, "hex": fields.hex.strip(), "role": fields.role}
        )
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        return writer.failed(change, outcome)
    return writer.apply(change, with_color(kit, color), Flash.SAVED)


@router.post("/app/channels/{slug}/brand/colors/remove")
def remove_color(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[ColorFields, Form()],
) -> Response:
    """Remove a color from the kit."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    return writer.apply(change, without_color(writer.current(change), fields.name), Flash.REMOVED)


@router.get("/app/channels/{slug}/files/{name}")
def brand_file(app: ServicesDep, access: PageAccess, name: str) -> Response:
    """A file the channel's kit points to; anything else is 404."""
    path = f"{access.channel.slug}/{name}"
    asset = next((item for item in referenced_assets(access.channel) if item.path == path), None)
    if asset is None:
        raise PageNotFound(name)
    try:
        target = app.assets.path_of(asset)
    except AssetMissing as missing:
        raise PageNotFound(name) from missing
    disposition = "inline" if asset.media_type.startswith("image/") else "attachment"
    headers = {**SECURITY_HEADERS, "Cache-Control": CACHE_FOREVER}
    return FileResponse(
        target,
        media_type=asset.media_type,
        headers=headers,
        filename=name,
        content_disposition_type=disposition,
    )
