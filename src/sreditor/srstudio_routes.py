"""srstudio routes: library sheets, episode slugs and relationships (rules project.srstudio.*)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from sreditor import srstudio_operations as srstudio
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.library import LibraryKind
from sreditor.library_operations import EntryChange
from sreditor.problems import problem_response, problem_type
from sreditor.srstudio import (
    EntrySheet,
    EpisodeSlug,
    EpisodeSlugInput,
    Relationship,
    RelationshipInput,
    RelationshipNotFound,
    SameCharacter,
    SheetInput,
    SheetMismatch,
    SlugLocked,
    SlugTaken,
)
from sreditor.srstudio_export import SrstudioNotReady, srstudio_zip
from sreditor.srstudio_operations import FactNotPublished, NotInLibrary, RelationshipChange

router = APIRouter(tags=["srstudio"])

PLURALS = {
    LibraryKind.CHARACTER: "characters",
    LibraryKind.LOCATION: "locations",
    LibraryKind.STYLE: "styles",
    LibraryKind.PROP: "props",
}
SHEET_FIELDS = {
    LibraryKind.CHARACTER: "the bible entry: world, status, kind, rig, look, voice, who, comic "
    "role, what never changes, open questions and canon facts",
    LibraryKind.LOCATION: "the world, the rig that draws it and the facts episodes established",
    LibraryKind.STYLE: "the srstudio style it renders with and an optional palette",
    LibraryKind.PROP: "the srstudio rig that draws it",
}


def tagged[T: BaseModel](model: T, revision: int) -> JSONResponse:
    """A model as JSON with its revision as the ETag."""
    return JSONResponse(model.model_dump(mode="json"), headers={"ETag": etag(revision)})


def add_sheet_routes(kind: LibraryKind) -> None:
    """GET and PUT of one kind's srstudio sheet."""
    path = f"/channels/{{slug}}/{PLURALS[kind]}/{{entry_id}}/srstudio"

    @router.get(
        path,
        name=f"get_{kind}_sheet",
        response_model=EntrySheet,
        summary=f"Read a {kind}'s srstudio sheet",
        description=f"What srstudio needs about a {kind}: its slug and {SHEET_FIELDS[kind]}. "
        "Revision 0 with a slug proposed from the name means nobody has saved one yet. Its ETag "
        "is the revision to send as If-Match.",
    )
    def get_sheet(entry_id: UUID, access: Access, app: ServicesDep) -> JSONResponse:
        sheet = srstudio.get_sheet(app, access, kind, entry_id)
        return tagged(sheet, sheet.revision)

    @router.put(
        path,
        name=f"set_{kind}_sheet",
        response_model=EntrySheet,
        summary=f"Save a {kind}'s srstudio sheet",
        description=f"Replace a {kind}'s srstudio slug and sheet ({SHEET_FIELDS[kind]}); the "
        f'sheet\'s kind must be "{kind}". Slugs are lowercase words joined by hyphens and unique '
        "per channel and kind (409 when taken); once an srstudio export carried the slug, "
        "exported_at is set and only the same slug is accepted (409 slug-locked). Facts may only cite published episodes (422). "
        'Needs content:write; send If-Match ("0" before the first save).',
    )
    def set_sheet(
        entry_id: UUID, data: SheetInput, access: Access, app: ServicesDep, if_match: IfMatch = None
    ) -> JSONResponse:
        change = EntryChange(
            kind=kind, entry_id=entry_id, expected_revision=expected_revision(if_match)
        )
        sheet = srstudio.save_sheet(app, access, change, data)
        return tagged(sheet, sheet.revision)


for sheet_kind in LibraryKind:
    add_sheet_routes(sheet_kind)


@router.get(
    "/channels/{slug}/episodes/{number}/srstudio",
    response_model=EpisodeSlug,
    summary="Read an episode's srstudio slug and seed",
)
def get_episode_slug(number: Number, access: Access, app: ServicesDep) -> JSONResponse:
    """Read the slug srstudio names the episode by and the seed that makes its frames repeatable.
    Revision 0 means they are still the proposal made from the title and number."""
    found = srstudio.get_episode_slug(app, access, number)
    return tagged(found, found.revision)


@router.put(
    "/channels/{slug}/episodes/{number}/srstudio",
    response_model=EpisodeSlug,
    summary="Save an episode's srstudio slug and seed",
)
def set_episode_slug(
    target: WriteTarget, data: EpisodeSlugInput, access: Access, app: ServicesDep
) -> JSONResponse:
    """Set the episode's srstudio slug (lowercase words joined by hyphens, unique in the channel;
    409 when taken) and seed. Once an srstudio export carried the slug, exported_at is set and only
    the same slug is accepted (409 slug-locked); the seed stays editable. Needs content:write; send If-Match ("0" before the first save)."""
    saved = srstudio.save_episode_slug(app, access, target.number, data, target.expected_revision)
    return tagged(saved, saved.revision)


ZIP_MEDIA_TYPE = "application/zip"


@router.get(
    "/channels/{slug}/episodes/{number}/srstudio.zip",
    summary="Export an episode for srstudio",
    response_class=FileResponse,
    responses={200: {"content": {ZIP_MEDIA_TYPE: {}}}},
)
def export_srstudio(number: Number, access: Access, app: ServicesDep) -> FileResponse:
    """Download the episode as the zip srstudio reads unchanged: timeline.json with one WAV take
    per spoken block, the channel bible, the beats when the episode's style draws them, and
    episode.json with its slug and seed. Owners and editors only. When a piece is missing the
    answer is 409 with an `errors` list naming each one (generate the voice-over, set a
    character's world, pick a style...), and nothing is exported. Never includes waivers."""
    folder = Path(tempfile.mkdtemp(prefix="srstudio-"))
    try:
        archive = srstudio_zip(app, access, number, folder)
    except Exception:
        # The folder holds converted WAVs; a refused export must not leave them behind.
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return FileResponse(
        archive,
        media_type=ZIP_MEDIA_TYPE,
        filename=archive.name,
        background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
    )


@router.get("/channels/{slug}/relationships", summary="List how the channel's characters relate")
def list_relationships(access: Access, app: ServicesDep) -> list[Relationship]:
    """List the relationships between pairs of library characters, as srstudio's
    relationships.json holds them: relation, how they know each other, dynamic, tension, where
    they meet, status and history facts. Characters are referred to by their library ids."""
    return srstudio.list_relationships(app, access)


@router.post(
    "/channels/{slug}/relationships",
    status_code=status.HTTP_201_CREATED,
    response_model=Relationship,
    summary="Record how two characters relate",
)
def create_relationship(
    data: RelationshipInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Record a relationship between two different characters from list_characters. History
    facts may only cite published episodes. Needs content:write. Accepts an Idempotency-Key."""
    return idempotency.run(
        lambda: srstudio.add_relationship(app, access, data), status.HTTP_201_CREATED
    )


@router.put(
    "/channels/{slug}/relationships/{relationship_id}",
    response_model=Relationship,
    summary="Change how two characters relate",
)
def replace_relationship(
    relationship_id: UUID,
    data: RelationshipInput,
    access: Access,
    app: ServicesDep,
    if_match: IfMatch = None,
) -> JSONResponse:
    """Replace a relationship from list_relationships. Needs content:write; send If-Match so a
    change you have not seen is never overwritten."""
    change = RelationshipChange(
        relationship_id=relationship_id, expected_revision=expected_revision(if_match)
    )
    saved = srstudio.replace_relationship(app, access, change, data)
    return tagged(saved, saved.revision)


@router.delete(
    "/channels/{slug}/relationships/{relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a relationship",
)
def delete_relationship(
    relationship_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Remove a relationship from the library. Needs a role that may change the channel."""
    change = RelationshipChange(
        relationship_id=relationship_id, expected_revision=expected_revision(if_match)
    )
    srstudio.delete_relationship(app, access, change)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def refused(request: Request, message: str, field: str, kind: str) -> JSONResponse:
    """A 422 problem naming the field at fault."""
    return problem_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        problem_type(kind),
        errors=[{"location": ["body", field], "message": message, "type": kind}],
    )


def install_srstudio_error_handlers(app: FastAPI) -> None:
    """The problems srstudio sheets, slugs and relationships answer with."""

    @app.exception_handler(SlugTaken)
    async def slug_taken(request: Request, taken: SlugTaken) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(taken), problem_type("slug-taken")
        )

    @app.exception_handler(SlugLocked)
    async def slug_locked(request: Request, locked: SlugLocked) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(locked), problem_type("slug-locked")
        )

    @app.exception_handler(RelationshipNotFound)
    async def relationship_not_found(
        request: Request, missing: RelationshipNotFound
    ) -> JSONResponse:
        return problem_response(
            request, status.HTTP_404_NOT_FOUND, f"Relationship {missing} not found"
        )

    @app.exception_handler(SheetMismatch)
    async def sheet_mismatch(request: Request, mismatch: SheetMismatch) -> JSONResponse:
        return refused(request, str(mismatch), "sheet", "sheet-kind")

    @app.exception_handler(FactNotPublished)
    async def fact_not_published(request: Request, unpublished: FactNotPublished) -> JSONResponse:
        return refused(request, str(unpublished), "episode", "unpublished-episode")

    @app.exception_handler(NotInLibrary)
    async def not_in_library(request: Request, missing: NotInLibrary) -> JSONResponse:
        kind = "not-in-library"
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(missing),
            problem_type(kind),
            errors=[
                {"location": ["body", *missing.location], "message": str(missing), "type": kind}
            ],
        )

    @app.exception_handler(SrstudioNotReady)
    async def not_ready(request: Request, lacking: SrstudioNotReady) -> JSONResponse:
        kind = "srstudio-not-ready"
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            "The episode is not ready for srstudio: " + str(lacking),
            problem_type(kind),
            errors=[
                {"location": ["episode"], "message": item.english(), "type": kind}
                for item in lacking.missing
            ],
        )

    @app.exception_handler(SameCharacter)
    async def same_character(request: Request, same: SameCharacter) -> JSONResponse:
        message = "A relationship pairs two different characters"
        return refused(request, message, "second", "same-character")
