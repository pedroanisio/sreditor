"""Signed file links for agents: who may get one, and what a link delivers (rule agentic.mcp.files).

Issuing a link asks the same role and scope as the API call it stands for: reading a file needs
channels:read (an export or book too, as the API's downloads), uploading episode media or a
third-party copy needs content:write, and uploading a brand file or exporting for srstudio needs
the right to change the channel (channels:write). A link is only another way to run that call, so
redeeming it checks the issuing token again: a revoked token, or a member who left, makes its
links useless. Waivers have no link kind at all.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Annotated, BinaryIO, Literal
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from sreditor import (
    brand_file_operations,
    external_media_operations,
    media_operations,
    render_operations,
)
from sreditor.assets import (
    BYTES_PER_MEGABYTE,
    AssetMissing,
    BrandSlot,
    FileFormat,
    StoredFile,
    accepted_formats,
)
from sreditor.brand import BrandFileRef, file_in, referenced_assets
from sreditor.brand_file_operations import BrandFileChange, BrandFileSlot
from sreditor.channels import Asset, Platform
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.episode_operations import episode_access
from sreditor.export_formats import MEDIA_TYPES
from sreditor.export_requests import (
    ExportRequest,
    build_export,
    check_request,
    export_filename,
)
from sreditor.external_media import COPY_FORMATS
from sreditor.external_media import EXTENSIONS as COPY_EXTENSIONS
from sreditor.external_media_operations import ItemChange
from sreditor.field_types import MediaType
from sreditor.file_links import FileLink, LinkGone, LinkKind, NewLink
from sreditor.media import (
    EXTENSIONS,
    KIND_FORMATS,
    UPLOADABLE_KINDS,
    MediaInput,
    MediaKind,
    MediaName,
    MediaNote,
    NewMedia,
)
from sreditor.media_operations import RenderNotUploadable
from sreditor.members import Permission
from sreditor.problems import ProblemError
from sreditor.srstudio_export import missing_pieces, srstudio_zip
from sreditor.srstudio_operations import get_episode_slug

FILES_PATH = "/files"
# Files up to this size may also travel inside the tool result, as base64.
INLINE_MAX_BYTES = BYTES_PER_MEGABYTE
ZIP_MEDIA_TYPE = "application/zip"
UPLOAD_METHOD = "PUT"
NOT_FOUND = "file-not-found"


class FileSource(StrEnum):
    """What a download link can point at."""

    BRAND_FILE = "brand_file"
    EPISODE_MEDIA = "episode_media"
    RENDER = "render"
    EXTERNAL_MEDIA = "external_media"
    SRSTUDIO_EXPORT = "srstudio_export"
    EXPORT = "export"


class FileTarget(BaseModel):
    """Which file a download link is for; each source names the fields it needs."""

    model_config = ConfigDict(frozen=True)

    source: FileSource = Field(
        description="brand_file needs brand_slot (and platform for an account's kit); "
        "episode_media needs number and media_id; render needs number and render_id; "
        "external_media needs item_id; srstudio_export needs number; export needs export."
    )
    number: int | None = Field(default=None, description="The episode's number.")
    media_id: UUID | None = Field(default=None, description="An id from list_media.")
    render_id: UUID | None = Field(default=None, description="An id from list_renders.")
    item_id: UUID | None = Field(default=None, description="An id from list_external_media.")
    brand_slot: BrandSlot | None = Field(
        default=None, description="The brand file: avatar, logo, banner, watermark and so on."
    )
    platform: Platform | None = Field(
        default=None, description="Read the brand file as this account's kit sees it."
    )
    export: ExportRequest | None = Field(
        default=None,
        description="For source export: an episode or channel export, or a book, to build when "
        "the link is opened.",
    )


EPISODE_MEDIA_TARGET: Literal["episode_media"] = "episode_media"
EXTERNAL_COPY_TARGET = "external_copy"
BRAND_FILE_TARGET = "brand_file"


class UploadRequest(BaseModel):
    """A file an agent will send into an episode as media: where it goes and what to call it.

    This is the original shape of create_upload_link's `upload`, still valid without `target`.
    """

    model_config = ConfigDict(frozen=True)

    target: Literal["episode_media"] = Field(
        default=EPISODE_MEDIA_TARGET,
        description="episode_media: a file of an episode (the default when `target` is left out).",
    )
    number: int = Field(description="The episode's number.")
    kind: MediaKind = Field(description="The media kind, which decides the accepted types.")
    name: MediaName
    note: MediaNote = ""


class ExternalCopyRequest(BaseModel):
    """A copy of a third-party item an agent will send."""

    model_config = ConfigDict(frozen=True)

    target: Literal["external_copy"] = Field(
        description="external_copy: the uploaded copy of a third-party item, replacing any earlier one."
    )
    item_id: UUID = Field(description="The item's id from list_external_media.")


class BrandFileRequest(BrandFileRef):
    """A brand file an agent will send: the slot (a font by its role) and whose kit it is in."""

    target: Literal["brand_file"] = Field(
        description="brand_file: a file of the brand kit, replacing the one in the slot."
    )
    platform: Platform | None = Field(
        default=None, description="Put the file in this account's overrides, not the channel's."
    )


def upload_tag(value: object) -> str:
    """Which target an upload argument names; one with no `target` is the original episode shape."""
    found = value.get("target") if isinstance(value, Mapping) else getattr(value, "target", None)
    return found if isinstance(found, str) else EPISODE_MEDIA_TARGET


UploadTarget = Annotated[
    Annotated[UploadRequest, Tag(EPISODE_MEDIA_TARGET)]
    | Annotated[ExternalCopyRequest, Tag(EXTERNAL_COPY_TARGET)]
    | Annotated[BrandFileRequest, Tag(BRAND_FILE_TARGET)],
    Discriminator(upload_tag),
]


class UploadPlan(BaseModel):
    """What a signed upload link stands for and what it accepts."""

    model_config = ConfigDict(frozen=True)

    link: NewLink
    formats: tuple[FileFormat, ...]
    max_bytes: int


class StoredUpload(BaseModel):
    """What an upload created or changed, and the revision its ETag is built from."""

    model_config = ConfigDict(frozen=True)

    payload: BaseModel
    revision: int


class FileOffer(BaseModel):
    """A link to download a file, with what the file is."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(description="Open it once, with GET and no credentials, within 15 minutes.")
    expires_at: datetime
    media_type: MediaType
    size_bytes: int | None = Field(
        description="Null for an srstudio export or an export, which are built when the link is "
        "opened, unless an export was asked for inline."
    )
    filename: str
    content_base64: str | None = Field(
        default=None, description="The file itself, only when asked for and 1 MB or less."
    )


class UploadOffer(BaseModel):
    """A link to upload one media file, and what it accepts."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(description="Send the file's bytes as the request body, once, in 15 minutes.")
    method: str
    expires_at: datetime
    max_bytes: int
    accepted_types: list[str]


class ExportCheck(BaseModel):
    """Whether an episode can be exported for srstudio, and if so the link to the zip."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    missing: list[str]
    slug: str
    seed: int
    file_link: FileOffer | None = None


class LinkedFile(BaseModel):
    """A file a link points at; `path` is None while an export is still to be built."""

    model_config = ConfigDict(frozen=True)

    path: Path | None
    media_type: MediaType
    size_bytes: int | None
    filename: str
    is_inline: bool


class FileDelivery(BaseModel):
    """A file ready to send; `cleanup` is a folder to remove once it was sent."""

    model_config = ConfigDict(frozen=True)

    path: Path
    media_type: MediaType
    filename: str
    is_inline: bool
    cleanup: Path | None = None


def file_url(app: Services, secret: str) -> str:
    """The public URL of a link, built from SREDITOR_PUBLIC_URL."""
    return f"{app.settings.public_url.rstrip('/')}{FILES_PATH}/{secret}"


def needs[T](value: T | None, source: str, name: str) -> T:
    """The value a source cannot do without, or a 422 that names it."""
    if value is None:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A {source} link needs {name}; pass it in target",
            "missing-target-field",
        )
    return value


def not_found(detail: str) -> ProblemError:
    """A 404 problem for a file that is not there, with the call that finds the right one."""
    return ProblemError(status.HTTP_404_NOT_FOUND, detail, NOT_FOUND)


def stored_path(app: Services, stored: Asset | StoredFile) -> Path:
    """Where a stored file is on disk; 404 when it is missing."""
    try:
        return app.assets.path_of(stored)
    except AssetMissing as missing:
        raise not_found(f"The file {stored.path} is missing from storage") from missing


def issuer_token(access: ChannelAccess) -> UUID:
    """The id of the API token that asks; links are only for agents, so a session has none."""
    if access.token is None:
        raise ProblemError(
            status.HTTP_403_FORBIDDEN,
            "File links are issued only to API tokens; sign-in sessions use the pages",
            "token-required",
        )
    return access.token.id


def base_link(access: ChannelAccess, kind: LinkKind) -> NewLink:
    """A link of this kind for the asker's channel, before its target is filled in."""
    return NewLink(
        token_id=issuer_token(access),
        channel=access.channel.slug,
        kind=kind,
    )


def brand_link(access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to the file in one of the channel's brand slots."""
    slot = needs(target.brand_slot, target.source, "brand_slot")
    access.require(Permission.READ)
    if target.platform is not None and access.channel.account(target.platform) is None:
        raise not_found(
            f"The channel has no {target.platform} account; get_channel lists its accounts"
        )
    asset = file_in(access.channel.brand_for(target.platform), slot)
    if asset is None:
        raise not_found(f"The brand kit has no {slot} file; get_channel shows which ones are set")
    return base_link(access, LinkKind.BRAND_FILE).model_copy(update={"reference": asset.path})


def media_link(access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to one of an episode's media files."""
    number = needs(target.number, target.source, "number")
    media_id = needs(target.media_id, target.source, "media_id")
    return base_link(access, LinkKind.MEDIA_FILE).model_copy(
        update={"number": number, "reference": str(media_id)}
    )


def render_link(app: Services, access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to a finished render's video, which is a media file of the episode."""
    number = needs(target.number, target.source, "number")
    render_id = needs(target.render_id, target.source, "render_id")
    video = render_operations.get_render(app, access, number, render_id).video
    if video is None:
        raise ProblemError(
            status.HTTP_409_CONFLICT,
            f"Render {render_id} has no video yet; call get_render until its status is done",
            "render-not-done",
        )
    return base_link(access, LinkKind.MEDIA_FILE).model_copy(
        update={"number": number, "reference": str(video.id)}
    )


def external_link(access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to a third-party item's uploaded copy."""
    item_id = needs(target.item_id, target.source, "item_id")
    return base_link(access, LinkKind.EXTERNAL_COPY).model_copy(update={"reference": str(item_id)})


def srstudio_link(access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to the episode's srstudio export, built when the link is opened."""
    number = needs(target.number, target.source, "number")
    return base_link(access, LinkKind.SRSTUDIO_ZIP).model_copy(update={"number": number})


def export_link(access: ChannelAccess, target: FileTarget) -> NewLink:
    """A link to an export or book, which stores only the request, never the file."""
    request = needs(target.export, target.source, "export")
    return base_link(access, LinkKind.EXPORT_FILE).model_copy(
        update={"number": request.number, "reference": request.model_dump_json()}
    )


def link_for(app: Services, access: ChannelAccess, target: FileTarget) -> NewLink:
    """The link a target asks for, once its fields are present and its permission is held."""
    builders: dict[FileSource, Callable[[], NewLink]] = {
        FileSource.BRAND_FILE: lambda: brand_link(access, target),
        FileSource.EPISODE_MEDIA: lambda: media_link(access, target),
        FileSource.RENDER: lambda: render_link(app, access, target),
        FileSource.EXTERNAL_MEDIA: lambda: external_link(access, target),
        FileSource.SRSTUDIO_EXPORT: lambda: srstudio_link(access, target),
        FileSource.EXPORT: lambda: export_link(access, target),
    }
    return builders[target.source]()


def describe_brand(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """A brand file, only while the channel's kit or an account's kit still points to it."""
    access.require(Permission.READ)
    asset = next(
        (item for item in referenced_assets(access.channel) if item.path == link.reference), None
    )
    if asset is None:
        raise not_found(
            "That file is no longer in the brand kit; call get_channel for the current ones"
        )
    path = stored_path(app, asset)
    return LinkedFile(
        path=path,
        media_type=asset.media_type,
        size_bytes=path.stat().st_size,
        filename=PurePath(asset.path).name,
        is_inline=asset.media_type.startswith("image/"),
    )


def describe_media(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """An episode's media file."""
    number = needs(link.number, link.kind, "number")
    found = media_operations.get_media(app, access, number, UUID(link.reference))
    return LinkedFile(
        path=stored_path(app, found.stored()),
        media_type=found.media_type,
        size_bytes=found.size_bytes,
        filename=found.name + EXTENSIONS[found.media_type],
        is_inline=True,
    )


def describe_external(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """A third-party item's uploaded copy."""
    item = external_media_operations.get_item(app, access, UUID(link.reference))
    stored = item.stored()
    if item.copy_file is None or stored is None:
        raise not_found(f"Item {item.id} has no uploaded copy; only its link is kept")
    return LinkedFile(
        path=stored_path(app, stored),
        media_type=item.copy_file.media_type,
        size_bytes=item.copy_file.size_bytes,
        filename=item.title + COPY_EXTENSIONS[item.copy_file.media_type],
        is_inline=item.copy_file.is_inline(),
    )


def describe_export(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """The srstudio export zip; it does not exist until the link is opened."""
    number = needs(link.number, link.kind, "number")
    episode_access(app, access, number, Permission.WRITE_CHANNEL)
    return LinkedFile(
        path=None,
        media_type=ZIP_MEDIA_TYPE,
        size_bytes=None,
        filename=get_episode_slug(app, access, number).slug + ".zip",
        is_inline=False,
    )


def describe_document(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """An export or book; it does not exist until the link is opened, but its request must hold."""
    request = ExportRequest.model_validate_json(link.reference)
    check_request(app, access, request)
    return LinkedFile(
        path=None,
        media_type=MEDIA_TYPES[request.format],
        size_bytes=None,
        filename=export_filename(access.channel.slug, request, datetime.now(UTC)),
        is_inline=False,
    )


def describe(app: Services, access: ChannelAccess, link: NewLink) -> LinkedFile:
    """The file a download link points at, checking the role and that it still exists."""
    describers: dict[LinkKind, Callable[[Services, ChannelAccess, NewLink], LinkedFile]] = {
        LinkKind.BRAND_FILE: describe_brand,
        LinkKind.MEDIA_FILE: describe_media,
        LinkKind.EXTERNAL_COPY: describe_external,
        LinkKind.SRSTUDIO_ZIP: describe_export,
        LinkKind.EXPORT_FILE: describe_document,
    }
    return describers[link.kind](app, access, link)


def inline_content(content: bytes | None) -> str | None:
    """The file as base64 when it is small enough to travel in a tool result."""
    if content is None or len(content) > INLINE_MAX_BYTES:
        return None
    return base64.b64encode(content).decode("ascii")


def inline_parts(
    app: Services, access: ChannelAccess, link: NewLink, facts: LinkedFile
) -> tuple[int | None, str | None]:
    """The size and base64 content of the file for a tool result.

    An export has neither until it is built, so asking for it inline builds it here too;
    the link still builds it again when it is opened, from the data as it is then.
    """
    if link.kind is LinkKind.EXPORT_FILE:
        built = build_export(app, access, ExportRequest.model_validate_json(link.reference))
        return len(built.content), inline_content(built.content)
    if facts.path is None:
        return facts.size_bytes, None
    return facts.size_bytes, inline_content(facts.path.read_bytes())


def issue_download(
    app: Services, access: ChannelAccess, target: FileTarget, is_inline: bool = False
) -> FileOffer:
    """A single-use link to download the file, after the role, scope and file are checked."""
    new = link_for(app, access, target)
    facts = describe(app, access, new)
    size_bytes, content = (
        inline_parts(app, access, new, facts) if is_inline else (facts.size_bytes, None)
    )
    issued = app.file_links.issue(new)
    return FileOffer(
        url=file_url(app, issued.secret),
        expires_at=issued.link.expires_at,
        media_type=facts.media_type,
        size_bytes=size_bytes,
        filename=facts.filename,
        content_base64=content,
    )


def media_plan(app: Services, access: ChannelAccess, request: UploadRequest) -> UploadPlan:
    """A link to upload one media file into an episode (project.media rules apply)."""
    if request.kind not in UPLOADABLE_KINDS:
        raise RenderNotUploadable
    episode_access(app, access, request.number, Permission.WRITE_CONTENT)
    link = base_link(access, LinkKind.MEDIA_UPLOAD).model_copy(
        update={
            "number": request.number,
            "reference": request.kind.value,
            "name": request.name,
            "note": request.note,
        }
    )
    return UploadPlan(
        link=link, formats=tuple(KIND_FORMATS[request.kind]), max_bytes=app.media.max_bytes
    )


def external_plan(app: Services, access: ChannelAccess, request: ExternalCopyRequest) -> UploadPlan:
    """A link to upload a third-party item's copy; its kind decides the accepted types."""
    access.require(Permission.WRITE_CONTENT)
    item = external_media_operations.get_item(app, access, request.item_id)
    link = base_link(access, LinkKind.EXTERNAL_UPLOAD).model_copy(
        update={"reference": str(item.id)}
    )
    return UploadPlan(link=link, formats=COPY_FORMATS[item.kind], max_bytes=app.media.max_bytes)


def brand_plan(app: Services, access: ChannelAccess, request: BrandFileRequest) -> UploadPlan:
    """A link to upload a brand file into a slot of the channel's kit or an account's overrides."""
    slot = BrandFileSlot(
        file=BrandFileRef(slot=request.slot, role=request.role), platform=request.platform
    )
    brand_file_operations.check_target(access, slot)
    link = base_link(access, LinkKind.BRAND_UPLOAD).model_copy(
        update={"reference": slot.model_dump_json()}
    )
    return UploadPlan(
        link=link, formats=accepted_formats(request.slot), max_bytes=app.assets.max_bytes
    )


def plan_upload(app: Services, access: ChannelAccess, request: UploadTarget) -> UploadPlan:
    """The link a target asks for, once its permission is held and its place exists."""
    match request:
        case UploadRequest():
            return media_plan(app, access, request)
        case ExternalCopyRequest():
            return external_plan(app, access, request)
        case BrandFileRequest():
            return brand_plan(app, access, request)


def issue_upload(app: Services, access: ChannelAccess, request: UploadTarget) -> UploadOffer:
    """A single-use link to upload one file, after the role, scope and place are checked."""
    plan = plan_upload(app, access, request)
    issued = app.file_links.issue(plan.link)
    return UploadOffer(
        url=file_url(app, issued.secret),
        method=UPLOAD_METHOD,
        expires_at=issued.link.expires_at,
        max_bytes=plan.max_bytes,
        accepted_types=[item.media_type for item in plan.formats],
    )


def check_export(app: Services, access: ChannelAccess, number: int) -> ExportCheck:
    """What an srstudio export still lacks and, when nothing, a link to the zip."""
    lacking = missing_pieces(app, access, number)
    saved = get_episode_slug(app, access, number)
    offer = None
    if not lacking:
        target = FileTarget(source=FileSource.SRSTUDIO_EXPORT, number=number)
        offer = issue_download(app, access, target)
    return ExportCheck(
        ready=not lacking,
        missing=[item.english() for item in lacking],
        slug=saved.slug,
        seed=saved.seed,
        file_link=offer,
    )


def issuer_access(app: Services, link: FileLink) -> ChannelAccess:
    """The issuing token's access to the channel now; the link is useless once that is gone."""
    live = app.tokens.live(link.token_id)
    if live is None:
        raise LinkGone
    user, token = live
    return access_for(app, link.channel, user, token)


def built_delivery(prefix: str, facts: LinkedFile, build: Callable[[Path], Path]) -> FileDelivery:
    """A file `build` writes into a new folder, which is removed once the file was sent."""
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        built = build(folder)
    except Exception:
        # The folder may hold converted WAVs or a half-written file; a refused build must not
        # leave them behind.
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return FileDelivery(
        path=built,
        media_type=facts.media_type,
        filename=built.name,
        is_inline=False,
        cleanup=folder,
    )


def write_export(app: Services, access: ChannelAccess, link: FileLink, folder: Path) -> Path:
    """Build the export a link stands for and write it into the folder."""
    built = build_export(app, access, ExportRequest.model_validate_json(link.reference))
    path = folder / built.filename
    path.write_bytes(built.content)
    return path


def deliver(app: Services, link: FileLink) -> FileDelivery:
    """The file a claimed download link points at, built first when it is an export."""
    access = issuer_access(app, link)
    facts = describe(app, access, link)
    if facts.path is not None:
        return FileDelivery(
            path=facts.path,
            media_type=facts.media_type,
            filename=facts.filename,
            is_inline=facts.is_inline,
        )
    if link.kind is LinkKind.EXPORT_FILE:
        return built_delivery(
            "export-", facts, lambda folder: write_export(app, access, link, folder)
        )
    number = needs(link.number, link.kind, "number")
    return built_delivery(
        "srstudio-", facts, lambda folder: srstudio_zip(app, access, number, folder)
    )


def wrong_method(link: FileLink) -> ProblemError:
    """A 405 for a link opened with the wrong method, saying which one it takes."""
    allowed = UPLOAD_METHOD if link.kind.is_upload else "GET"
    return ProblemError(
        status.HTTP_405_METHOD_NOT_ALLOWED,
        f"This link takes {allowed}, not the method you used",
        "wrong-link-method",
        headers={"Allow": allowed},
    )


@contextmanager
def claimed(app: Services, secret: str, is_upload: bool) -> Iterator[FileLink]:
    """Spend a link for the method it takes; a failure inside the block gives it back.

    Nothing was delivered or stored on a failure such as a missing srstudio piece, a wrong file
    type or a file too large, so the link stays usable until it expires.
    """
    link = app.file_links.claim(secret)
    try:
        if link.kind.is_upload is not is_upload:
            raise wrong_method(link)
        yield link
    except Exception:
        app.file_links.release(secret)
        raise


def redeem_download(app: Services, secret: str) -> FileDelivery:
    """Spend a download link and return its file."""
    with claimed(app, secret, is_upload=False) as link:
        return deliver(app, link)


def upload_limit(app: Services, link: FileLink) -> int:
    """The most bytes the link's upload may carry: a brand file's limit, or the media limit."""
    return app.assets.max_bytes if link.kind is LinkKind.BRAND_UPLOAD else app.media.max_bytes


def store_media(app: Services, link: FileLink, source: BinaryIO) -> StoredUpload:
    """Store an uploaded file as upload_media would, with the issuing token as author."""
    access = issuer_access(app, link)
    new = NewMedia(
        channel=link.channel,
        number=needs(link.number, link.kind, "number"),
        kind=MediaKind(link.reference),
        data=MediaInput(name=link.name, note=link.note),
    )
    added = media_operations.add_media(app, access, new, source)
    return StoredUpload(payload=added, revision=added.revision)


def store_copy(app: Services, link: FileLink, source: BinaryIO) -> StoredUpload:
    """Store an uploaded file as upload_external_media_copy would."""
    change = ItemChange(media_id=UUID(link.reference))
    item = external_media_operations.attach_copy(app, issuer_access(app, link), change, source)
    return StoredUpload(payload=item, revision=item.revision)


def store_brand_file(app: Services, link: FileLink, source: BinaryIO) -> StoredUpload:
    """Store an uploaded file as upload_brand_file would."""
    slot = BrandFileSlot.model_validate_json(link.reference)
    change = BrandFileChange(target=slot)
    revised = brand_file_operations.put_brand_file(app, issuer_access(app, link), change, source)
    return StoredUpload(payload=revised.channel.brand_for(slot.platform), revision=revised.revision)


def store_upload(app: Services, link: FileLink, source: BinaryIO) -> StoredUpload:
    """Run the operation an upload link stands for, with the issuing token as author."""
    stores: dict[LinkKind, Callable[[Services, FileLink, BinaryIO], StoredUpload]] = {
        LinkKind.MEDIA_UPLOAD: store_media,
        LinkKind.EXTERNAL_UPLOAD: store_copy,
        LinkKind.BRAND_UPLOAD: store_brand_file,
    }
    return stores[link.kind](app, link, source)
