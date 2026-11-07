"""The brand page's fonts: a family and weight per role, and the font file behind each.

Fonts keep their file only while the family stays the same; the file is checked by its content
like every brand file (rules project.brand, web.app.assets).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, status
from pydantic import ValidationError

from sreditor.assets import AssetRejected, BrandSlot
from sreditor.brand import with_font_file, with_fonts
from sreditor.channels import DEFAULT_FONT_WEIGHT, FontInput, FontRole
from sreditor.deps import ServicesDep, within_rate_limit
from sreditor.i18n import N_
from sreditor.members import Permission
from sreditor.web.forms import field_errors
from sreditor.web.routes import TOO_MANY, TOO_MANY_ATTEMPTS, rejected_form, translator
from sreditor.web.workspace.brand_kit import (
    CHOOSE_FILE,
    BrandChange,
    KitWrite,
    read_upload,
)
from sreditor.web.workspace.forms import FontFields, RevisionFields, UploadFields
from sreditor.web.workspace.shell import Flash, Outcome, PageAccess, require

router = APIRouter()

FONT_FIRST = N_("Choose this font's family first, then upload its file.")
FILE_ERROR = "file"


def typed_values(fields: FontFields) -> dict[str, str]:
    """What was typed in a font form, to show again beside its errors."""
    return {"family": fields.family, "weight": fields.weight}


@router.post("/app/channels/{slug}/brand/fonts/{role}")
def save_font(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    role: FontRole,
    fields: Annotated[FontFields, Form()],
) -> Response:
    """Set the family and weight of a font role, keeping its file while the family stays."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    kit = writer.current(change)
    weight = fields.weight.strip() or str(DEFAULT_FONT_WEIGHT)
    try:
        font = FontInput.model_validate({"family": fields.family, "weight": weight})
    except ValidationError as invalid:
        errors = field_errors(invalid, translator(request))
        outcome = Outcome.invalid(errors).model_copy(
            update={"focus": role.value, "values": typed_values(fields)}
        )
        return writer.failed(change, outcome)
    return writer.apply(change, with_fonts(kit, {role: font}), Flash.SAVED)


@router.post("/app/channels/{slug}/brand/fonts/{role}/remove")
def remove_font(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    role: FontRole,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Remove a font role and its file; on a platform, the channel's font shows again."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    return writer.apply(change, with_fonts(writer.current(change), {role: None}), Flash.REMOVED)


def file_refused(
    message: str, role: FontRole, status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT
) -> Outcome:
    """The font form that was sent, reopened with the reason its file was not stored."""
    return Outcome(errors={FILE_ERROR: message}, status_code=status_code, focus=role.value)


@router.post("/app/channels/{slug}/brand/fonts/{role}/file")
def upload_font_file(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    role: FontRole,
    fields: Annotated[UploadFields, Form()],
) -> Response:
    """Store the file of a font whose family is chosen: woff2, ttf or otf, checked by content."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    translate = translator(request)
    writer = KitWrite(request, app, access)
    change = BrandChange.of(fields.platform, fields.revision)
    kit = writer.current(change)
    if role not in kit.fonts:
        return writer.failed(change, file_refused(translate(FONT_FIRST), role))
    if not within_rate_limit(request, app):
        return writer.failed(change, file_refused(translate(TOO_MANY_ATTEMPTS), role, TOO_MANY))
    content = read_upload(fields, app.assets.max_bytes)
    if not content:
        return writer.failed(change, file_refused(translate(CHOOSE_FILE), role))
    try:
        asset = app.assets.save(access.channel.slug, BrandSlot.FONT, content)
    except AssetRejected as rejection:
        message = translate(rejection.message, **rejection.params)
        return writer.failed(change, file_refused(message, role))
    return writer.apply(change, with_font_file(kit, role, asset), Flash.UPLOADED)
