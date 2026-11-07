"""Third-party media pages: the channel's library of items made by others, one item with its
copy, and the episode tab that records each use and lists the credits it owes
(rules project.external-media and project.external-media.use).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.assets import BYTES_PER_MEGABYTE, FBX, GLB, AssetRejected
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.external_media import (
    CREDIT_LENGTH,
    NOTE_LENGTH,
    PAGES_LENGTH,
    SCENE_LENGTH,
    ExternalKind,
    ExternalMedia,
    ExternalMediaInput,
    ExternalMediaInUse,
    ExternalMediaNotFound,
    MediaUseInput,
    MediaUseNotFound,
    Problem,
    Rights,
    UsePurpose,
    UseRefused,
    copy_accept,
    copy_formats,
)
from sreditor.external_media_operations import (
    ItemChange,
    ItemQuery,
    UseChange,
    UseQuery,
    add_use,
    attach_copy,
    create_item,
    delete_item,
    detach_copy,
    get_credits,
    get_item,
    item_episodes,
    list_items,
    list_uses,
    remove_use,
    replace_item,
)
from sreditor.external_media_routes import copy_file
from sreditor.field_types import TITLE_LENGTH
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import SECURITY_HEADERS, rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import RevisionFields, revision_of
from sreditor.web.workspace.media import upload_refusal
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    require,
)

router = APIRouter()

LIBRARY = "/app/channels/{slug}/external-media"
ITEM = f"{LIBRARY}/{{media_id}}"
USES = "/app/channels/{slug}/episodes/{number}/external-media"
SUFFIX = "/external-media"
SECTION = "external-media"
ALL_KINDS = ""
# The viewer's loader for each model it can draw.
MODEL_FORMATS = {GLB.media_type: "glb", FBX.media_type: "fbx"}

KIND_LABELS: dict[ExternalKind, str] = {
    ExternalKind.VIDEO: N_("Video"),
    ExternalKind.IMAGE: N_("Image"),
    ExternalKind.AUDIO: N_("Audio"),
    ExternalKind.DOCUMENT: N_("Document"),
    ExternalKind.WEB_PAGE: N_("Web page"),
    ExternalKind.MODEL_3D: N_("3D model"),
    ExternalKind.TEXTURE: N_("Texture"),
    ExternalKind.SHADER: N_("Shader"),
}
KIND_TABS: dict[str, str] = {
    ALL_KINDS: N_("All"),
    ExternalKind.VIDEO: N_("Videos"),
    ExternalKind.IMAGE: N_("Images"),
    ExternalKind.AUDIO: N_("Audio"),
    ExternalKind.DOCUMENT: N_("Documents"),
    ExternalKind.WEB_PAGE: N_("Web pages"),
    ExternalKind.MODEL_3D: N_("3D models"),
    ExternalKind.TEXTURE: N_("Textures"),
    ExternalKind.SHADER: N_("Shaders"),
}
RIGHTS_LABELS: dict[Rights, str] = {
    Rights.PUBLIC_DOMAIN: N_("Public domain"),
    Rights.OPEN_LICENSE: N_("Open license"),
    Rights.LICENSED: N_("Licensed"),
    Rights.CITATION: N_("Citation or fair use"),
    Rights.OWN: N_("Our own"),
    Rights.UNKNOWN: N_("Rights unknown"),
}
RIGHTS_TONES: dict[Rights, str] = {
    Rights.PUBLIC_DOMAIN: "success",
    Rights.OPEN_LICENSE: "success",
    Rights.LICENSED: "success",
    Rights.CITATION: "accent",
    Rights.OWN: "success",
    Rights.UNKNOWN: "warning",
}
PURPOSE_LABELS: dict[UsePurpose, str] = {
    UsePurpose.ILLUSTRATION: N_("Illustration"),
    UsePurpose.EVIDENCE: N_("Evidence"),
    UsePurpose.REACTION: N_("Reaction"),
    UsePurpose.QUOTE: N_("Quote"),
    UsePurpose.PRODUCTION: N_("Production element"),
}
PROBLEM_TEXT: dict[Problem, str] = {
    Problem.RIGHTS_UNKNOWN: N_("Rights unknown: find out whether the channel may show it."),
    Problem.NO_REASON: N_("Citation without a reason: say why the use is allowed."),
    Problem.NO_RANGE: N_("Citation without a time range: say which part the episode shows."),
    Problem.NO_CREDIT: N_("No credit line: add the credit its license or citation asks for."),
}
ITEM_GONE = N_("This item was changed or removed after you opened the page. Reload the page.")
USE_GONE = N_("This use was changed or removed after you opened the page. Reload the page.")
IN_USE = N_("Episodes still use this item. Remove those uses first.")
PICK_ITEM = N_("Pick an item from the channel's third-party media.")
BAD_TIME = N_("Write a time as minutes:seconds, such as 1:05.")
NO_RANGE_HERE = N_("Only video and audio take a time range.")
NO_PAGES_HERE = N_("Only documents and web pages take pages.")
# A time as seconds, minutes:seconds or hours:minutes:seconds.
CLOCK = re.compile(r"^(?:(\d+):)??(?:(\d+):)?(\d+)$")
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
# The form's field that each refused API field shows next to.
FORM_FIELD = {"start_seconds": "start", "end_seconds": "end", "pages": "pages"}
REFUSAL_TEXT = {"start_seconds": NO_RANGE_HERE, "pages": NO_PAGES_HERE}


class ItemFields(SubmittedFields):
    """A third-party item as a form sends it; `revision` only when editing."""

    kind: str = ""
    title: str = ""
    url: str = ""
    creator: str = ""
    published_on: str = ""
    accessed_on: str = ""
    rights: str = Rights.UNKNOWN.value
    license_name: str = ""
    license_url: str = ""
    attribution: str = ""
    rights_note: str = ""
    revision: str = ""


class CopyFields(SubmittedFields):
    """A copy of an item to upload, and the item's revision the page showed."""

    revision: str = ""
    file: UploadFile | None = None


class UseFields(SubmittedFields):
    """One use of an item in an episode; times are minutes:seconds."""

    media_id: str = ""
    purpose: str = UsePurpose.ILLUSTRATION.value
    start: str = ""
    end: str = ""
    pages: str = ""
    scene: str = ""
    credit: str = ""
    note: str = ""


class RemoveUseFields(SubmittedFields):
    """Take one use out of an episode."""

    use_id: str = ""
    revision: str = ""


class TimeUnreadable(ValueError):
    """A time a person wrote that is not seconds, minutes:seconds or hours:minutes:seconds."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def seconds_of(value: str, field: str) -> int | None:
    """Seconds from 75, 1:15 or 0:01:15; None when empty."""
    text = value.strip()
    if not text:
        return None
    found = CLOCK.match(text)
    if found is None:
        raise TimeUnreadable(field)
    hours, minutes, seconds = (int(part) if part else 0 for part in found.groups())
    return hours * SECONDS_PER_HOUR + minutes * SECONDS_PER_MINUTE + seconds


def clock_of(seconds: int | None) -> str:
    """75 as 1:15 and 3675 as 1:01:15; empty for None."""
    if seconds is None:
        return ""
    hours, rest = divmod(seconds, SECONDS_PER_HOUR)
    minutes, second = divmod(rest, SECONDS_PER_MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{second:02d}"
    return f"{minutes}:{second:02d}"


def library_url(slug: str, kind: str = ALL_KINDS) -> str:
    """The library page, optionally showing one kind."""
    return channel_url(slug, SUFFIX) + (f"?kind={kind}" if kind else "")


def item_url(slug: str, media_id: UUID | str) -> str:
    """One item's page."""
    return f"{channel_url(slug, SUFFIX)}/{media_id}"


def uses_url(slug: str, number: int) -> str:
    """The episode's third-party media tab."""
    return episodes_url(slug, number) + SUFFIX


def id_of(value: str) -> UUID:
    """The id in a path or form; a malformed one is an item that does not exist."""
    try:
        return UUID(value)
    except ValueError as malformed:
        raise ExternalMediaNotFound(value) from malformed


def kind_or_all(value: str) -> ExternalKind | None:
    """The kind a ?kind= names, or None for all of them."""
    try:
        return ExternalKind(value)
    except ValueError:
        return None


def item_input(fields: ItemFields) -> ExternalMediaInput:
    """The item a form describes; empty dates and license link are none."""
    return ExternalMediaInput.model_validate(
        {
            "kind": fields.kind,
            "title": fields.title,
            "url": fields.url,
            "creator": fields.creator,
            "published_on": fields.published_on or None,
            "accessed_on": fields.accessed_on or None,
            "rights": fields.rights,
            "license_name": fields.license_name,
            "license_url": fields.license_url or None,
            "attribution": fields.attribution,
            "rights_note": fields.rights_note,
        }
    )


def use_input(fields: UseFields) -> MediaUseInput:
    """The use a form describes, with its times read as seconds."""
    return MediaUseInput.model_validate(
        {
            "media_id": fields.media_id,
            "purpose": fields.purpose,
            "start_seconds": seconds_of(fields.start, "start"),
            "end_seconds": seconds_of(fields.end, "end"),
            "pages": fields.pages,
            "scene": fields.scene,
            "credit": fields.credit,
            "note": fields.note,
        }
    )


def options(texts: dict[Any, str], translate: Translator) -> list[tuple[str, str]]:
    """(value, label) pairs for a select."""
    return [(str(key), translate(label)) for key, label in texts.items()]


def item_labels(translate: Translator) -> dict[str, Any]:
    """What the item templates need to name kinds and rights and size their fields."""
    return {
        "kind_labels": {kind.value: label for kind, label in KIND_LABELS.items()},
        "rights_labels": {rights.value: label for rights, label in RIGHTS_LABELS.items()},
        "rights_tones": {rights.value: tone for rights, tone in RIGHTS_TONES.items()},
        "kind_options": options(KIND_LABELS, translate),
        "rights_options": options(RIGHTS_LABELS, translate),
        "title_limit": TITLE_LENGTH,
        "credit_limit": CREDIT_LENGTH,
        "note_limit": NOTE_LENGTH,
    }


def item_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run a change to an item; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except AssetRejected as rejection:
        return Outcome.invalid({"file": translate(rejection.message, **rejection.params)})
    except ExternalMediaInUse:
        return Outcome.conflict({"form": translate(IN_USE)})
    except (ExternalMediaNotFound, PartChanged):
        return Outcome.conflict({"form": translate(ITEM_GONE)})
    return None


# Library


class ExternalMediaView(BaseModel):
    """The library page, with the add form as it was sent."""

    model_config = ConfigDict(frozen=True)

    kind: str = ALL_KINDS
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: ItemFields | None = None


def library_page(
    request: Request, app: Services, access: ChannelAccess, shown: ExternalMediaView
) -> Response:
    """The channel's items, one kind or all, and the dialog to add one."""
    translate = translator(request)
    slug = access.channel.slug
    query = ItemQuery(
        kind=kind_or_all(shown.kind), cursor=shown.cursor or None, limit=DEFAULT_LIMIT
    )
    found = list_items(app, access, query)
    tabs = [(library_url(slug, key), translate(label), key) for key, label in KIND_TABS.items()]
    context = {
        "items": found.items,
        "next_cursor": found.next_cursor,
        "kind_tabs": tabs,
        "kind": shown.kind if kind_or_all(shown.kind) else ALL_KINDS,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "library_link": channel_url(slug, SUFFIX),
        **item_labels(translate),
    }
    view = View(
        template="workspace/external_media.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(LIBRARY)
def library(
    request: Request, app: ServicesDep, access: PageAccess, kind: str = "", cursor: str = ""
) -> Response:
    """The channel's third-party media."""
    return library_page(request, app, access, ExternalMediaView(kind=kind, cursor=cursor))


@router.post(LIBRARY)
def add_item(
    request: Request, app: ServicesDep, access: PageAccess, fields: Annotated[ItemFields, Form()]
) -> Response:
    """Add an item to the library and open its page."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    created: list[ExternalMedia] = []

    def create() -> None:
        created.append(create_item(app, access, item_input(fields)))

    outcome = item_errors(create, translator(request))
    if outcome is None:
        return done(request, app, item_url(access.channel.slug, created[0].id), Flash.ITEM_ADDED)
    return library_page(request, app, access, ExternalMediaView(outcome=outcome, sent=fields))


# One item


class ItemPath(BaseModel):
    """The item a page's address names."""

    model_config = ConfigDict(frozen=True)

    media_id: str


def item_path(media_id: str) -> ItemPath:
    """The item in the address."""
    return ItemPath(media_id=media_id)


ItemTarget = Annotated[ItemPath, Depends(item_path)]


class ItemView(BaseModel):
    """One item's page, with the dialog that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    media_id: str
    outcome: Outcome = Outcome()
    sent: ItemFields | None = None
    dialog: str = ""  # "edit-item" or "upload-copy" when that dialog was sent


def found_item(app: Services, access: ChannelAccess, media_id: str) -> ExternalMedia:
    """The item in the path; 404 when the channel has none with that id."""
    try:
        return get_item(app, access, id_of(media_id))
    except ExternalMediaNotFound as missing:
        raise PageNotFound(media_id) from missing


def viewer_texts(translate: Translator) -> dict[str, str]:
    """What the 3D viewer says while it loads, once it shows the model, when it cannot, and its button."""
    return {
        "loading": translate("Loading the 3D model…"),
        "ready": translate("Drag to turn, scroll or pinch to zoom."),
        "error": translate("This model cannot be shown here. Download it to open it."),
        "reset": translate("Reset view"),
    }


def item_page(request: Request, app: Services, access: ChannelAccess, shown: ItemView) -> Response:
    """An item's details, its copy, the episodes that use it and, for editors, the dialogs."""
    item = found_item(app, access, shown.media_id)
    slug = access.channel.slug
    context = {
        "item": item,
        "episodes": [
            (number, uses_url(slug, number)) for number in item_episodes(app, access, item.id)
        ],
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "dialog": shown.dialog,
        "item_link": item_url(slug, item.id),
        "library_link": channel_url(slug, SUFFIX),
        "model_formats": MODEL_FORMATS,
        "viewer_texts": viewer_texts(translator(request)),
        "accept": copy_accept(item.kind),
        "formats": copy_formats(item.kind),
        "max_bytes": app.external_media.max_bytes,
        "max_megabytes": app.external_media.max_bytes // BYTES_PER_MEGABYTE,
        **item_labels(translator(request)),
    }
    view = View(
        template="workspace/external_item.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(ITEM)
def item(request: Request, app: ServicesDep, access: PageAccess, target: ItemTarget) -> Response:
    """One third-party item."""
    return item_page(request, app, access, ItemView(media_id=target.media_id))


def item_change(media_id: str, revision: str) -> ItemChange:
    """The item a form targets and the revision it was showing."""
    return ItemChange(media_id=id_of(media_id), expected_revision=revision_of(revision))


@router.post(ITEM)
def save_item(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: ItemTarget,
    fields: Annotated[ItemFields, Form()],
) -> Response:
    """Change an item's details."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)

    def save() -> None:
        change = item_change(target.media_id, fields.revision)
        replace_item(app, access, change, item_input(fields))

    outcome = item_errors(save, translator(request))
    if outcome is None:
        return done(request, app, item_url(access.channel.slug, target.media_id), Flash.SAVED)
    shown = ItemView(media_id=target.media_id, outcome=outcome, sent=fields, dialog="edit-item")
    return item_page(request, app, access, shown)


@router.post(f"{ITEM}/copy")
def upload_copy(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: ItemTarget,
    fields: Annotated[CopyFields, Form()],
) -> Response:
    """Upload a copy of the item, replacing any earlier one."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)

    def store() -> None:
        assert fields.file is not None  # upload_refusal turned away a form without a file
        attach_copy(app, access, item_change(target.media_id, fields.revision), fields.file.file)

    refusal = upload_refusal(request, app, fields.file)
    outcome = refusal or item_errors(store, translator(request))
    if outcome is None:
        return done(request, app, item_url(access.channel.slug, target.media_id), Flash.UPLOADED)
    shown = ItemView(media_id=target.media_id, outcome=outcome, dialog="upload-copy")
    return item_page(request, app, access, shown)


@router.post(f"{ITEM}/copy/remove")
def remove_copy(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: ItemTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Take the uploaded copy off the item."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)

    def remove() -> None:
        detach_copy(app, access, item_change(target.media_id, fields.revision))

    outcome = item_errors(remove, translator(request))
    if outcome is None:
        return done(request, app, item_url(access.channel.slug, target.media_id), Flash.REMOVED)
    return item_page(request, app, access, ItemView(media_id=target.media_id, outcome=outcome))


@router.post(f"{ITEM}/delete")
def delete(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: ItemTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Remove the item from the library; refused while episodes use it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)

    def remove() -> None:
        delete_item(app, access, item_change(target.media_id, fields.revision))

    outcome = item_errors(remove, translator(request))
    if outcome is None:
        return done(request, app, library_url(access.channel.slug), Flash.ITEM_DELETED)
    return item_page(request, app, access, ItemView(media_id=target.media_id, outcome=outcome))


@router.get(f"{ITEM}/copy")
def download_copy(app: ServicesDep, access: PageAccess, target: ItemTarget) -> Response:
    """The uploaded copy: pictures, video and sound inline, documents as a download."""
    try:
        return copy_file(app, found_item(app, access, target.media_id), SECURITY_HEADERS)
    except ExternalMediaNotFound as missing:
        raise PageNotFound(target.media_id) from missing


# The episode tab


class UsesView(BaseModel):
    """The episode's third-party media tab, with the add form as it was sent."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: UseFields | None = None


def library_choices(app: Services, access: ChannelAccess) -> list[ExternalMedia]:
    """The items an episode may use, as many as one page can hold."""
    return list_items(app, access, ItemQuery(limit=MAX_LIMIT)).items


def uses_page(request: Request, app: Services, access: ChannelAccess, shown: UsesView) -> Response:
    """The episode's uses, its credits and warnings, and the dialog to add a use."""
    translate = translator(request)
    current = found_episode(app, access, shown.number)
    query = UseQuery(number=shown.number, cursor=shown.cursor or None, limit=DEFAULT_LIMIT)
    choices = library_choices(app, access)
    context = {
        "episode": current.episode,
        "uses": list_uses(app, access, query),
        "credits": get_credits(app, access, shown.number),
        "items_by_id": {str(choice.id): choice for choice in choices},
        "item_options": [(str(choice.id), choice.title) for choice in choices],
        "purpose_options": options(PURPOSE_LABELS, translate),
        "purpose_labels": {purpose.value: label for purpose, label in PURPOSE_LABELS.items()},
        "problem_text": {problem.value: text for problem, text in PROBLEM_TEXT.items()},
        "clock": clock_of,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "uses_link": uses_url(access.channel.slug, shown.number),
        "library_link": channel_url(access.channel.slug, SUFFIX),
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": SECTION,
        "pages_limit": PAGES_LENGTH,
        "scene_limit": SCENE_LENGTH,
        "credit_limit": CREDIT_LENGTH,
        "note_limit": NOTE_LENGTH,
        **labels(),
    }
    view = View(
        template="workspace/episode_external_media.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(USES)
def uses(
    request: Request, app: ServicesDep, access: PageAccess, number: int, cursor: str = ""
) -> Response:
    """The third-party media an episode uses and the credits it owes."""
    return uses_page(request, app, access, UsesView(number=number, cursor=cursor))


def use_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run a change to a use; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except TimeUnreadable as unreadable:
        return Outcome.invalid({unreadable.field: translate(BAD_TIME)})
    except UseRefused as refused:
        return Outcome.invalid({FORM_FIELD[refused.field]: translate(REFUSAL_TEXT[refused.field])})
    except ExternalMediaNotFound:
        return Outcome.invalid({"media_id": translate(PICK_ITEM)})
    except (MediaUseNotFound, PartChanged):
        return Outcome.conflict({"form": translate(USE_GONE)})
    return None


@router.post(USES)
def new_use(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[UseFields, Form()],
) -> Response:
    """Record a use of a library item in the episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def create() -> None:
        add_use(app, access, number, use_input(fields))

    outcome = use_errors(create, translator(request))
    if outcome is None:
        return done(request, app, uses_url(access.channel.slug, number), Flash.USE_ADDED)
    return uses_page(request, app, access, UsesView(number=number, outcome=outcome, sent=fields))


@router.post(f"{USES}/remove")
def drop_use(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[RemoveUseFields, Form()],
) -> Response:
    """Take a use out of the episode; the item stays in the library."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def remove() -> None:
        try:
            use_id = UUID(fields.use_id)
        except ValueError as malformed:
            raise MediaUseNotFound(fields.use_id) from malformed
        change = UseChange(
            number=number, use_id=use_id, expected_revision=revision_of(fields.revision)
        )
        remove_use(app, access, change)

    outcome = use_errors(remove, translator(request))
    if outcome is None:
        return done(request, app, uses_url(access.channel.slug, number), Flash.REMOVED)
    return uses_page(request, app, access, UsesView(number=number, outcome=outcome))
