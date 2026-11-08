"""An episode's media page: its visual style, voice-over, sound effects, score and visual effects.

Files are checked by their content and stored by hash (rule web.app.assets); the page plays them
from a route that serves only the episode's own files, and only to the channel's members.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.assets import BYTES_PER_MEGABYTE, AssetRejected
from sreditor.deps import ChannelAccess, Services, ServicesDep, within_rate_limit
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.library import LibraryKind, StyleChoice, StyleNotInLibrary
from sreditor.library_operations import choose_episode_style, get_episode_style, list_entries
from sreditor.media import (
    NAME_LENGTH,
    NOTE_LENGTH,
    UPLOADABLE_KINDS,
    MediaFile,
    MediaInput,
    MediaKind,
    MediaNotFound,
    NewMedia,
    accept_of,
    default_name,
    formats_of,
)
from sreditor.media_operations import (
    MediaChange,
    MediaQuery,
    add_media,
    get_media,
    list_media,
    remove_media,
    rename_media,
)
from sreditor.media_routes import media_file
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.web.forms import field_errors
from sreditor.web.routes import (
    SECURITY_HEADERS,
    TOO_MANY,
    TOO_MANY_ATTEMPTS,
    rejected_form,
    translator,
)
from sreditor.web.workspace.brand_kit import CHOOSE_FILE
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import MediaFields, RevisionFields, StyleFields, revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

MEDIA_SUFFIX = "/media"
STYLE_SUFFIX = "/style"
PAGE = "/app/channels/{slug}/episodes/{number}/media"
# The path segment of each kind, as in .../episodes/1/media/upload/voice-over.
KIND_PATHS = {
    "voice-over": MediaKind.VOICE_OVER,
    "sound-effects": MediaKind.SOUND_EFFECT,
    "score": MediaKind.SCORE,
    "visual-effects": MediaKind.VISUAL_EFFECT,
    "covers": MediaKind.COVER,
    "final-videos": MediaKind.FINAL_VIDEO,
}
PATH_OF = {kind: path for path, kind in KIND_PATHS.items()}
NO_STYLE = N_("No style")
STYLE_GONE = N_("This style is no longer in the library. Reload the page.")
STYLE_CHANGED = N_("Someone picked a style after you opened the page. Reload the page.")
MEDIA_GONE = N_("This file was changed or removed after you opened the page. Reload the page.")
KIND_TEXT: dict[MediaKind, dict[str, str]] = {
    MediaKind.VOICE_OVER: {
        "title": N_("Voice-over"),
        "upload": N_("Upload voice-over"),
        "empty": N_("No voice-over yet. Upload the narration, a take at a time."),
        "more": N_("More voice-over"),
    },
    MediaKind.SOUND_EFFECT: {
        "title": N_("Sound effects"),
        "upload": N_("Upload sound effect"),
        "empty": N_("No sound effects yet."),
        "more": N_("More sound effects"),
    },
    MediaKind.SCORE: {
        "title": N_("Score"),
        "upload": N_("Upload score"),
        "empty": N_("No score yet. Upload the music the episode uses."),
        "more": N_("More score"),
    },
    MediaKind.VISUAL_EFFECT: {
        "title": N_("Visual effects"),
        "upload": N_("Upload visual effect"),
        "empty": N_("No visual effects yet."),
        "more": N_("More visual effects"),
    },
    MediaKind.COVER: {
        "title": N_("Covers"),
        "upload": N_("Upload cover"),
        "empty": N_("No covers yet. Upload the image each cut is posted with."),
        "more": N_("More covers"),
    },
    MediaKind.FINAL_VIDEO: {
        "title": N_("Final videos"),
        "upload": N_("Upload final video"),
        "empty": N_("No final videos yet. Upload a master made outside sreditor."),
        "more": N_("More final videos"),
    },
}


def media_url(slug: str, number: int) -> str:
    """The media page of an episode."""
    return episodes_url(slug, number) + MEDIA_SUFFIX


def kind_of(path: str) -> MediaKind:
    """The kind a path segment names; 404 for any other segment."""
    try:
        return KIND_PATHS[path]
    except KeyError as unknown:
        raise PageNotFound(path) from unknown


def media_id_of(value: str) -> UUID:
    """The id in a path or form; a malformed one is a file that does not exist."""
    try:
        return UUID(value)
    except ValueError as malformed:
        raise MediaNotFound(value) from malformed


class UploadPath(BaseModel):
    """The episode and kind an upload form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    kind: MediaKind


def upload_path(number: int, kind_path: str) -> UploadPath:
    """The episode and kind in the address; 404 for an unknown kind."""
    return UploadPath(number=number, kind=kind_of(kind_path))


class MediaPath(BaseModel):
    """The episode and file a form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    media_id: str


def media_path(number: int, media_id: str) -> MediaPath:
    """The episode and file in the address."""
    return MediaPath(number=number, media_id=media_id)


# Path values in one parameter each, so handlers keep to five (code.functions.args).
UploadTarget = Annotated[UploadPath, Depends(upload_path)]
MediaTarget = Annotated[MediaPath, Depends(media_path)]


class MediaView(BaseModel):
    """The media page, with the form that was sent, its kind and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    kind: MediaKind | None = None
    sent: MediaFields | None = None
    editing: str = ""  # the id of the file whose dialog was sent
    cursor: str = ""
    cursor_kind: str = ""


def kind_lists(app: Services, access: ChannelAccess, shown: MediaView) -> list[dict[str, Any]]:
    """Each kind with its files, its next page, its texts and what it accepts."""
    base = media_url(access.channel.slug, shown.number)
    lists = []
    for kind in UPLOADABLE_KINDS:  # renders have their own page (rule project.render)
        cursor = shown.cursor if shown.cursor_kind == kind else ""
        query = MediaQuery(
            number=shown.number, kind=kind, cursor=cursor or None, limit=DEFAULT_LIMIT
        )
        found = list_media(app, access, query)
        lists.append(
            {
                "kind": kind.value,
                "path": PATH_OF[kind],
                "files": found.items,
                "next_cursor": found.next_cursor,
                "text": KIND_TEXT[kind],
                "upload_link": f"{base}/upload/{PATH_OF[kind]}",
                "accept": accept_of(kind),
                "formats": formats_of(kind),
            }
        )
    return lists


def style_context(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> dict[str, Any]:
    """The episode's style and the library's styles to pick from."""
    chosen = get_episode_style(app, access, number)
    styles = list_entries(app, access, LibraryKind.STYLE, None, MAX_LIMIT).items
    return {
        "style": chosen,
        "style_options": [("", translate(NO_STYLE))]
        + [(str(entry.id), entry.name) for entry in styles],
        "style_link": episodes_url(access.channel.slug, number) + STYLE_SUFFIX,
    }


def media_page(
    request: Request, app: Services, access: ChannelAccess, shown: MediaView
) -> Response:
    """The episode's files by kind, with players and, for those who may write, the dialogs."""
    current = found_episode(app, access, shown.number)
    context = {
        **style_context(app, access, shown.number, translator(request)),
        "episode": current.episode,
        "kinds": kind_lists(app, access, shown),
        "errors": shown.outcome.errors,
        "sent_kind": shown.kind.value if shown.kind else "",
        "sent": shown.sent,
        "editing": shown.editing,
        "media_link": media_url(access.channel.slug, shown.number),
        **episode_frame(app, access, shown.number, translator(request)),
        "episode_tab": "media",
        "name_limit": NAME_LENGTH,
        "note_limit": NOTE_LENGTH,
        "max_bytes": app.media.max_bytes,
        "max_megabytes": app.media.max_bytes // BYTES_PER_MEGABYTE,
        **labels(),
    }
    view = View(
        template="workspace/media.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


def page_view(number: int, cursor: str = "", kind: str = "") -> MediaView:
    """The page as its address asks for it: which kind's list to page, and from where."""
    return MediaView(number=number, cursor=cursor, cursor_kind=kind)


@router.get(PAGE)
def media(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    shown: Annotated[MediaView, Depends(page_view)],
) -> Response:
    """An episode's voice-over, sound effects, score and visual effects."""
    return media_page(request, app, access, shown)


def described(fields: MediaFields, kind: MediaKind) -> MediaInput:
    """The name and note a form gives; an upload without a name takes the file's."""
    filename = fields.file.filename if fields.file else None
    name = fields.name.strip() or default_name(filename, kind)
    return MediaInput.model_validate({"name": name, "note": fields.note})


def media_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run an upload or edit; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except AssetRejected as rejection:
        return Outcome.invalid({"file": translate(rejection.message, **rejection.params)})
    except (MediaNotFound, PartChanged):
        return Outcome.conflict({"form": translate(MEDIA_GONE)})
    return None


def upload_refusal(request: Request, app: Services, file: UploadFile | None) -> Outcome | None:
    """Why an upload cannot even start: too many attempts, or no file chosen."""
    translate = translator(request)
    if not within_rate_limit(request, app):
        return Outcome.refused({"file": translate(TOO_MANY_ATTEMPTS)}, TOO_MANY)
    if file is None or not file.filename:
        return Outcome.invalid({"file": translate(CHOOSE_FILE)})
    return None


@router.post(f"{PAGE}/upload/{{kind_path}}")
def upload(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: UploadTarget,
    fields: Annotated[MediaFields, Form()],
) -> Response:
    """Upload a file of one kind to the episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    number, kind = target.number, target.kind
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def store() -> None:
        new = NewMedia(
            channel=access.channel.slug, number=number, kind=kind, data=described(fields, kind)
        )
        assert fields.file is not None  # upload_refusal turned away a form without a file
        add_media(app, access, new, fields.file.file)

    outcome = upload_refusal(request, app, fields.file) or media_errors(store, translator(request))
    if outcome is None:
        # No #fragment: a redirect that changes only the fragment would not reload the page.
        return done(request, app, media_url(access.channel.slug, number), Flash.UPLOADED)
    shown = MediaView(number=number, outcome=outcome, kind=kind, sent=fields)
    return media_page(request, app, access, shown)


@router.post(f"{PAGE}/{{media_id}}")
def save_media(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: MediaTarget,
    fields: Annotated[MediaFields, Form()],
) -> Response:
    """Rename a file or change its note."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    number, media_id = target.number, target.media_id
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def save() -> None:
        change = MediaChange(
            number=number,
            media_id=media_id_of(media_id),
            expected_revision=revision_of(fields.revision),
        )
        current = get_media(app, access, number, change.media_id)
        rename_media(app, access, change, described(fields, current.kind))

    outcome = media_errors(save, translator(request))
    if outcome is None:
        return done(request, app, media_url(access.channel.slug, number), Flash.SAVED)
    shown = MediaView(number=number, outcome=outcome, sent=fields, editing=media_id)
    return media_page(request, app, access, shown)


@router.post(f"{PAGE}/{{media_id}}/remove")
def drop_media(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: MediaTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Take a file out of the episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    number, media_id = target.number, target.media_id
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def remove() -> None:
        change = MediaChange(
            number=number,
            media_id=media_id_of(media_id),
            expected_revision=revision_of(fields.revision),
        )
        remove_media(app, access, change)

    outcome = media_errors(remove, translator(request))
    if outcome is None:
        return done(request, app, media_url(access.channel.slug, number), Flash.REMOVED)
    return media_page(request, app, access, MediaView(number=number, outcome=outcome))


@router.get(f"{PAGE}/{{media_id}}/file")
def play_media(app: ServicesDep, access: PageAccess, number: int, media_id: str) -> Response:
    """The file itself, for the page's players; only the episode's own files are served."""
    found_episode(app, access, number)
    try:
        found: MediaFile = get_media(app, access, number, media_id_of(media_id))
        return media_file(app, found, SECURITY_HEADERS)
    except MediaNotFound as missing:
        raise PageNotFound(media_id) from missing


def style_id_of(value: str) -> UUID | None:
    """The style a form picked; empty is none, and a malformed id is not in the library."""
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError as malformed:
        raise StyleNotInLibrary(UUID(int=0)) from malformed


def style_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Pick a style; the error to show next to the picker, or None when it worked."""
    try:
        action()
    except StyleNotInLibrary:
        return Outcome.invalid({"style_id": translate(STYLE_GONE)})
    except PartChanged:
        return Outcome.conflict({"style_id": translate(STYLE_CHANGED)})
    return None


@router.post("/app/channels/{slug}/episodes/{number}" + STYLE_SUFFIX)
def save_style(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[StyleFields, Form()],
) -> Response:
    """Pick the episode's visual style from the library, or none."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)

    def save() -> None:
        choice = StyleChoice(style_id=style_id_of(fields.style_id))
        choose_episode_style(app, access, number, choice, revision_of(fields.revision))

    outcome = style_errors(save, translator(request))
    if outcome is None:
        return done(request, app, media_url(access.channel.slug, number), Flash.SAVED)
    return media_page(request, app, access, MediaView(number=number, outcome=outcome))
