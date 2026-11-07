"""Library routes: a channel's characters, locations, styles and props; each episode's cast and style."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sreditor import library_operations as library
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.library import EntryInput, EpisodeStyle, LibraryEntry, LibraryKind, StyleChoice
from sreditor.library_operations import Cast, EntryChange
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page

router = APIRouter(tags=["library"])


class KindNames(BaseModel):
    """How the routes of one kind of entry are named: character/characters, a character."""

    model_config = ConfigDict(frozen=True)

    kind: LibraryKind
    plural: str
    example: str
    usage: str


BY_NAME = "Episodes refer to them by name from their screenplays."
KINDS = (
    KindNames(
        kind=LibraryKind.CHARACTER, plural="characters", example="ANA, the host", usage=BY_NAME
    ),
    KindNames(
        kind=LibraryKind.LOCATION,
        plural="locations",
        example="KITCHEN, the studio set",
        usage=BY_NAME,
    ),
    KindNames(
        kind=LibraryKind.STYLE,
        plural="styles",
        example="Warm documentary: handheld, natural light",
        usage="Each episode picks one with set_episode_style.",
    ),
    KindNames(
        kind=LibraryKind.PROP,
        plural="props",
        example="Laptop, the one Rex types on",
        usage="srstudio draws each with the rig its sheet names (set_prop_sheet).",
    ),
)


def add_reads(names: KindNames) -> None:
    """GET routes for one kind: the list and one entry."""
    kind, plural = names.kind, names.plural
    collection = f"/channels/{{slug}}/{plural}"

    @router.get(
        collection,
        name=f"list_{plural}",
        summary=f"List the channel's {plural}",
        description=f"The {plural} in the channel's library, such as {names.example}, in the "
        f"order they were added. {names.usage}",
    )
    def list_entries(
        access: Access, app: ServicesDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
    ) -> Page[LibraryEntry]:
        return library.list_entries(app, access, kind, cursor, limit)

    @router.get(
        f"{collection}/{{entry_id}}",
        name=f"get_{kind}",
        summary=f"Read one {kind}",
        description=f"One {kind} of the library. Its ETag is the revision to send as If-Match "
        "when changing it.",
    )
    def get_entry(
        entry_id: UUID, access: Access, app: ServicesDep, response: Response
    ) -> LibraryEntry:
        entry = library.get_entry(app, access, kind, entry_id)
        response.headers["ETag"] = etag(entry.revision)
        return entry


def add_writes(names: KindNames) -> None:
    """POST, PUT and DELETE routes for one kind."""
    kind, plural = names.kind, names.plural
    collection = f"/channels/{{slug}}/{plural}"

    @router.post(
        collection,
        name=f"create_{kind}",
        status_code=status.HTTP_201_CREATED,
        response_model=LibraryEntry,
        summary=f"Add a {kind} to the library",
        description=f"Add a {kind}: its name (for a character or location, exactly as the "
        f"screenplay writes it) and a short description. {names.usage} Names are unique per channel ignoring case; a taken name answers 409. "
        "Needs content:write. Accepts an Idempotency-Key header.",
    )
    def create_entry(
        data: EntryInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
    ) -> JSONResponse:
        return idempotency.run(
            lambda: library.create_entry(app, access, kind, data), status.HTTP_201_CREATED
        )

    @router.put(
        f"{collection}/{{entry_id}}",
        name=f"replace_{kind}",
        response_model=LibraryEntry,
        summary=f"Rename or describe a {kind}",
        description=f"Replace a {kind}'s name and description. Needs content:write. Send "
        "If-Match so a change you have not seen is never overwritten.",
    )
    def replace_entry(
        entry_id: UUID, data: EntryInput, access: Access, app: ServicesDep, if_match: IfMatch = None
    ) -> Response:
        change = EntryChange(
            kind=kind, entry_id=entry_id, expected_revision=expected_revision(if_match)
        )
        entry = library.replace_entry(app, access, change, data)
        return JSONResponse(entry.model_dump(mode="json"), headers={"ETag": etag(entry.revision)})

    @router.delete(
        f"{collection}/{{entry_id}}",
        name=f"delete_{kind}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary=f"Remove a {kind} from the library",
        description=f"Remove a {kind}; episodes that name it no longer refer to anything. Needs a "
        "role that may change the channel (owner or editor).",
    )
    def delete_entry(
        entry_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
    ) -> Response:
        change = EntryChange(
            kind=kind, entry_id=entry_id, expected_revision=expected_revision(if_match)
        )
        library.delete_entry(app, access, change)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


for kind_names in KINDS:
    add_reads(kind_names)
    add_writes(kind_names)


@router.get("/channels/{slug}/episodes/{number}/cast", summary="List an episode's cast")
def get_episode_cast(number: Number, access: Access, app: ServicesDep) -> Cast:
    """List who speaks in an episode's screenplay and where its scenes happen.

    Each name comes with the library entry it refers to, or null when the library has none yet;
    add the missing ones with create_character or create_location.
    """
    return library.episode_cast(app, access, number)


@router.get(
    "/channels/{slug}/episodes/{number}/style",
    response_model=EpisodeStyle,
    summary="Read an episode's visual style",
)
def get_episode_style(number: Number, access: Access, app: ServicesDep) -> JSONResponse:
    """Read the visual style an episode picked from the channel's library; style is null when
    it picked none or the style was removed. Its ETag is the revision to send as If-Match."""
    chosen = library.get_episode_style(app, access, number)
    return JSONResponse(chosen.model_dump(mode="json"), headers={"ETag": etag(chosen.revision)})


@router.put(
    "/channels/{slug}/episodes/{number}/style",
    response_model=EpisodeStyle,
    summary="Set an episode's visual style",
)
def set_episode_style(
    target: WriteTarget, choice: StyleChoice, access: Access, app: ServicesDep
) -> JSONResponse:
    """Pick an episode's visual style by its id from list_styles, or null for none. An id that
    is not one of this channel's styles is refused with 422. Needs content:write; send
    If-Match ("0" before the first pick) so a change you have not seen is never overwritten."""
    chosen = library.choose_episode_style(
        app, access, target.number, choice, target.expected_revision
    )
    return JSONResponse(chosen.model_dump(mode="json"), headers={"ETag": etag(chosen.revision)})
