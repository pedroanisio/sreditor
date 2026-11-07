"""The channel's library page: the characters, locations, styles and props its episodes share."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.library import (
    DESCRIPTION_LENGTH,
    NAME_LENGTH,
    EntryExists,
    EntryInput,
    EntryNotFound,
    LibraryKind,
)
from sreditor.library_operations import (
    EntryChange,
    create_entry,
    delete_entry,
    list_entries,
    replace_entry,
)
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.web.forms import field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import EntryFields, RevisionFields, revision_of
from sreditor.web.workspace.screenplay import screenplay_url
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

LIBRARY_SUFFIX = "/library"
# The path segment of each kind, as in /app/channels/funny-memes/library/characters.
KIND_PATHS = {
    "characters": LibraryKind.CHARACTER,
    "locations": LibraryKind.LOCATION,
    "styles": LibraryKind.STYLE,
    "props": LibraryKind.PROP,
}
PATH_OF = {kind: path for path, kind in KIND_PATHS.items()}
NAME_TAKEN = {
    LibraryKind.CHARACTER: N_("There is already a character with this name."),
    LibraryKind.LOCATION: N_("There is already a location with this name."),
    LibraryKind.STYLE: N_("There is already a style with this name."),
    LibraryKind.PROP: N_("There is already a prop with this name."),
}
AS_WRITTEN = N_("As the screenplay writes it; capitals do not matter.")
ENTRY_GONE = N_("This entry was changed or removed after you opened the page. Reload the page.")
KIND_TEXT: dict[LibraryKind, dict[str, str]] = {
    LibraryKind.CHARACTER: {
        "title": N_("Characters"),
        "add": N_("Add character"),
        "empty": N_(
            "No characters yet. Add who appears in the episodes, here or from a screenplay."
        ),
        "remove": N_("Remove this character from the library?"),
        "more": N_("More characters"),
        "name_hint": AS_WRITTEN,
        "description_hint": "",
    },
    LibraryKind.LOCATION: {
        "title": N_("Locations"),
        "add": N_("Add location"),
        "empty": N_("No locations yet. Add where the episodes happen, here or from a screenplay."),
        "remove": N_("Remove this location from the library?"),
        "more": N_("More locations"),
        "name_hint": AS_WRITTEN,
        "description_hint": "",
    },
    LibraryKind.STYLE: {
        "title": N_("Visual styles"),
        "add": N_("Add style"),
        "empty": N_("No visual styles yet. Describe a look, then pick it on an episode's media."),
        "remove": N_("Remove this style from the library? Episodes that use it will have none."),
        "more": N_("More styles"),
        "name_hint": N_("A short name people recognise, such as Warm documentary."),
        "description_hint": N_("The look: palette, framing, type and motion."),
    },
    LibraryKind.PROP: {
        "title": N_("Props"),
        "add": N_("Add prop"),
        "empty": N_("No props yet. Add the objects characters hold or use, such as a laptop."),
        "remove": N_("Remove this prop from the library?"),
        "more": N_("More props"),
        "name_hint": N_("What people call it, such as Laptop."),
        "description_hint": "",
    },
}


def library_url(slug: str, kind: LibraryKind | None = None) -> str:
    """The library page, or where one kind's forms post."""
    suffix = LIBRARY_SUFFIX if kind is None else f"{LIBRARY_SUFFIX}/{PATH_OF[kind]}"
    return channel_url(slug, suffix)


def kind_of(path: str) -> LibraryKind:
    """The kind a path segment names; 404 for any other segment."""
    try:
        return KIND_PATHS[path]
    except KeyError as unknown:
        raise PageNotFound(path) from unknown


class EntryPath(BaseModel):
    """The kind and entry a form's address names."""

    model_config = ConfigDict(frozen=True)

    kind: LibraryKind
    entry_id: str


def entry_path(kind_path: str, entry_id: str) -> EntryPath:
    """The kind and entry in the address; 404 for an unknown kind."""
    return EntryPath(kind=kind_of(kind_path), entry_id=entry_id)


# The address's kind and entry in one parameter, so handlers keep to five (code.functions.args).
EntryTarget = Annotated[EntryPath, Depends(entry_path)]


class LibraryView(BaseModel):
    """The library page, with the form that was sent, its kind and its errors."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    kind: LibraryKind | None = None
    sent: EntryFields | None = None
    editing: str = ""  # the id of the entry whose dialog was sent
    cursor: str = ""
    cursor_kind: str = ""


def kind_lists(app: Services, access: ChannelAccess, shown: LibraryView) -> list[dict[str, Any]]:
    """Each kind with its entries, its next page and its texts."""
    lists = []
    for kind in LibraryKind:
        cursor = shown.cursor if shown.cursor_kind == kind else None
        found = list_entries(app, access, kind, cursor or None, DEFAULT_LIMIT)
        lists.append(
            {
                "kind": kind.value,
                "path": PATH_OF[kind],
                "entries": found.items,
                "next_cursor": found.next_cursor,
                "text": KIND_TEXT[kind],
                "link": library_url(access.channel.slug, kind),
            }
        )
    return lists


def library_page(
    request: Request, app: Services, access: ChannelAccess, shown: LibraryView
) -> Response:
    """Both lists, and for those who may write, the dialogs to add and change entries."""
    context = {
        "kinds": kind_lists(app, access, shown),
        "errors": shown.outcome.errors,
        "sent_kind": shown.kind.value if shown.kind else "",
        "sent": shown.sent,
        "editing": shown.editing,
        "library_link": library_url(access.channel.slug),
        "name_limit": NAME_LENGTH,
        "description_limit": DESCRIPTION_LENGTH,
    }
    view = View(
        template="workspace/library.html",
        section="library",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/library")
def library(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    cursor: str = "",
    kind: str = "",
) -> Response:
    """The channel's characters and locations."""
    return library_page(request, app, access, LibraryView(cursor=cursor, cursor_kind=kind))


def entry_input(fields: EntryFields) -> EntryInput:
    """The entry a form describes."""
    return EntryInput.model_validate({"name": fields.name, "description": fields.description})


def came_from(access: ChannelAccess, fields: EntryFields) -> str:
    """Where to go after adding: back to the screenplay it came from, or the library."""
    if fields.episode.isdigit():
        return screenplay_url(access.channel.slug, int(fields.episode))
    # No #fragment: a redirect that changes only the fragment would not reload the page.
    return library_url(access.channel.slug)


def entry_errors(
    kind: LibraryKind, action: Callable[[], object], translate: Translator
) -> Outcome | None:
    """Run a create or save; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except EntryExists:
        return Outcome.conflict({"name": translate(NAME_TAKEN[kind])})
    except (EntryNotFound, PartChanged):
        return Outcome.conflict({"form": translate(ENTRY_GONE)})
    return None


@router.post("/app/channels/{slug}/library/{kind_path}")
def new_entry(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    kind_path: str,
    fields: Annotated[EntryFields, Form()],
) -> Response:
    """Add a character or location, from the library or from a screenplay."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    kind = kind_of(kind_path)
    require(access, Permission.WRITE_CONTENT)

    def create() -> None:
        create_entry(app, access, kind, entry_input(fields))

    outcome = entry_errors(kind, create, translator(request))
    if outcome is None:
        return done(request, app, came_from(access, fields), Flash.LIBRARY_ADDED)
    shown = LibraryView(outcome=outcome, kind=kind, sent=fields)
    return library_page(request, app, access, shown)


def entry_change(kind: LibraryKind, entry_id: str, revision: str) -> EntryChange:
    """The change a form targets; a malformed id is an entry that does not exist."""
    try:
        parsed = UUID(entry_id)
    except ValueError as malformed:
        raise EntryNotFound(entry_id) from malformed
    return EntryChange(kind=kind, entry_id=parsed, expected_revision=revision_of(revision))


@router.post("/app/channels/{slug}/library/{kind_path}/{entry_id}")
def save_entry(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: EntryTarget,
    fields: Annotated[EntryFields, Form()],
) -> Response:
    """Rename or describe a character or location."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)

    def save() -> None:
        change = entry_change(target.kind, target.entry_id, fields.revision)
        replace_entry(app, access, change, entry_input(fields))

    outcome = entry_errors(target.kind, save, translator(request))
    if outcome is None:
        return done(request, app, library_url(access.channel.slug), Flash.SAVED)
    shown = LibraryView(outcome=outcome, kind=target.kind, sent=fields, editing=target.entry_id)
    return library_page(request, app, access, shown)


@router.post("/app/channels/{slug}/library/{kind_path}/{entry_id}/remove")
def remove_entry(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: EntryTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Remove a character or location from the library."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)

    def remove() -> None:
        delete_entry(app, access, entry_change(target.kind, target.entry_id, fields.revision))

    outcome = entry_errors(target.kind, remove, translator(request))
    if outcome is None:
        return done(request, app, library_url(access.channel.slug), Flash.REMOVED)
    return library_page(request, app, access, LibraryView(outcome=outcome))
