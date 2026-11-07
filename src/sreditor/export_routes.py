"""Export routes: download an episode, or a summary of its channel, as JSON, Excel or PDF
(rule project.export)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from sreditor import export_operations as exporting
from sreditor.deps import Access, ServicesDep, request_locale
from sreditor.episode_routes import Number
from sreditor.export_formats import MEDIA_TYPES, ExportFormat
from sreditor.export_operations import CHANNEL_EXPORT, EPISODE_EXPORT
from sreditor.i18n import Translator
from sreditor.web.export_files import (
    ExportFile,
    channel_file,
    episode_file,
)

router = APIRouter(tags=["export"])

Format = Annotated[
    ExportFormat,
    Query(
        alias="format",
        description="json (everything, English field names), xlsx (one sheet per table) "
        "or pdf (a readable dossier in the request's language).",
    ),
]
FILE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "description": "The file, as an attachment.",
        "content": {media_type: {} for media_type in MEDIA_TYPES.values()},
    }
}


def attachment(found: ExportFile) -> Response:
    """The file as a download, never shown inline."""
    disposition = f'attachment; filename="{found.filename}"'
    return Response(
        content=found.content,
        media_type=found.media_type,
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


@router.get(
    EPISODE_EXPORT,
    summary="Export an episode",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_episode(
    number: Number, export_format: Format, request: Request, access: Access, app: ServicesDep
) -> Response:
    """Download everything an episode holds as one file: its data, research and sources, pitch
    with its notes and sources, screenplay, short script, classification, visual style and
    third-party uses with credits. Waivers and media files are never included. Use json to
    read it back as data (it carries format_version); xlsx and pdf are for people. Needs read.
    """
    translate = Translator(request_locale(request))
    found = exporting.export_episode(app, access, number)
    return attachment(episode_file(found, export_format, translate))


@router.get(
    CHANNEL_EXPORT,
    summary="Export a channel summary",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_channel(
    export_format: Format, request: Request, access: Access, app: ServicesDep
) -> Response:
    """Download a summary of the channel as one file: its editorial, its declared and observed
    profile, and every episode with its classification. For one episode's full content, use
    export_episode. Needs read.
    """
    translate = Translator(request_locale(request))
    return attachment(channel_file(exporting.export_channel(app, access), export_format, translate))
