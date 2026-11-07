"""Pitch routes: a channel's pitches (pautas), their research, and the pitch an episode uses."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse

from sreditor import pitch_operations as pitches
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.pitch_operations import PitchChange, PitchQuery, PitchSourceChange
from sreditor.pitches import (
    EpisodePitch,
    Pitch,
    PitchChoice,
    PitchInput,
    PitchInUse,
    PitchNotes,
    PitchNotesInput,
    PitchNotFound,
    PitchSource,
    PitchStatus,
)
from sreditor.problems import problem_response, problem_type
from sreditor.research import SourceInput

router = APIRouter(tags=["pitches"])

PITCHES = "/channels/{slug}/pitches"
PITCH = f"{PITCHES}/{{pitch_id}}"
SOURCE = f"{PITCH}/sources/{{source_id}}"

PitchId = Annotated[UUID, Path(description="The pitch's id, from list_pitches.")]
SourceId = Annotated[UUID, Path(description="The source's id, from list_pitch_sources.")]


def pitch_change(pitch_id: PitchId, if_match: IfMatch = None) -> PitchChange:
    """The pitch a write targets and the revision its If-Match names."""
    return PitchChange(pitch_id=pitch_id, expected_revision=expected_revision(if_match))


def source_change(
    pitch_id: PitchId, source_id: SourceId, if_match: IfMatch = None
) -> PitchSourceChange:
    """The pitch source a write targets and the revision its If-Match names."""
    return PitchSourceChange(
        pitch_id=pitch_id, source_id=source_id, expected_revision=expected_revision(if_match)
    )


def pitch_query(
    status: PitchStatus | None = None, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> PitchQuery:
    """Which page of pitches to list, and of which status."""
    return PitchQuery(status=status, cursor=cursor, limit=limit)


# Path ids and If-Match in one parameter, so routes keep to five (code.functions.args).
PitchTarget = Annotated[PitchChange, Depends(pitch_change)]
SourceTarget = Annotated[PitchSourceChange, Depends(source_change)]
PitchListing = Annotated[PitchQuery, Depends(pitch_query)]


def tagged[T: Pitch | PitchNotes | PitchSource | EpisodePitch](found: T, response: Response) -> T:
    """The resource, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get(PITCHES, summary="List pitches")
def list_pitches(access: Access, app: ServicesDep, query: PitchListing) -> Page[Pitch]:
    """List the channel's pitches (pautas: story ideas), in the order they were added.

    Pass `status` (idea, research, approved, discarded) to see only those.
    """
    return pitches.list_pitches(app, access, query)


@router.post(
    PITCHES, status_code=status.HTTP_201_CREATED, response_model=Pitch, summary="Create a pitch"
)
def create_pitch(
    data: PitchInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Add a pitch: a story idea with a title, a summary and a status (idea by default).

    Needs content:write. Accepts an Idempotency-Key header, so a retry does not add it twice.
    """
    return idempotency.run(lambda: pitches.create_pitch(app, access, data), status.HTTP_201_CREATED)


@router.get(PITCH, summary="Read a pitch")
def get_pitch(pitch_id: PitchId, access: Access, app: ServicesDep, response: Response) -> Pitch:
    """Read one pitch. Its ETag is the revision to send as If-Match when changing it."""
    return tagged(pitches.get_pitch(app, access, pitch_id), response)


@router.get(f"{PITCH}/episodes", summary="List a pitch's episodes")
def list_pitch_episodes(pitch_id: PitchId, access: Access, app: ServicesDep) -> list[int]:
    """The numbers of the episodes that refer to the pitch; it cannot be deleted while any do."""
    return pitches.pitch_episodes(app, access, pitch_id)


@router.put(PITCH, summary="Replace a pitch")
def replace_pitch(
    target: PitchTarget, data: PitchInput, access: Access, app: ServicesDep, response: Response
) -> Pitch:
    """Replace a pitch's title, summary and status. Send If-Match to keep others' changes."""
    return tagged(pitches.replace_pitch(app, access, target, data), response)


@router.delete(PITCH, status_code=status.HTTP_204_NO_CONTENT, summary="Delete a pitch")
def delete_pitch(target: PitchTarget, access: Access, app: ServicesDep) -> Response:
    """Delete a pitch with its notes and sources. Needs channels:write and an editor role.

    Refused with 409 while episodes refer to it; set their pitch to none first.
    """
    pitches.delete_pitch(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(f"{PITCH}/research", summary="Read pitch notes")
def get_pitch_notes(
    pitch_id: PitchId, access: Access, app: ServicesDep, response: Response
) -> PitchNotes:
    """Read a pitch's research notes. Revision 0 (ETag "0") means nobody wrote them yet."""
    return tagged(pitches.get_notes(app, access, pitch_id), response)


@router.put(f"{PITCH}/research", summary="Write pitch notes")
def write_pitch_notes(
    target: PitchTarget,
    data: PitchNotesInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> PitchNotes:
    """Replace a pitch's research notes. Send If-Match to keep changes you have not seen."""
    return tagged(pitches.write_notes(app, access, target, data), response)


@router.get(f"{PITCH}/sources", summary="List pitch sources")
def list_pitch_sources(
    pitch_id: PitchId,
    access: Access,
    app: ServicesDep,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[PitchSource]:
    """List the sources a pitch cites, in the order they were added."""
    return pitches.list_sources(app, access, pitch_id, cursor, limit)


@router.post(
    f"{PITCH}/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=PitchSource,
    summary="Cite a pitch source",
)
def add_pitch_source(
    pitch_id: PitchId,
    data: SourceInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Cite a source for a pitch: its link, title, the claim it supports and when it was read.

    Needs content:write. Accepts an Idempotency-Key header, so a retry does not cite it twice.
    """
    return idempotency.run(
        lambda: pitches.add_source(app, access, pitch_id, data), status.HTTP_201_CREATED
    )


@router.get(SOURCE, summary="Read a pitch source")
def get_pitch_source(
    pitch_id: PitchId, source_id: SourceId, access: Access, app: ServicesDep, response: Response
) -> PitchSource:
    """Read one source of a pitch. Its ETag is the revision to send as If-Match."""
    return tagged(pitches.get_source(app, access, pitch_id, source_id), response)


@router.put(SOURCE, summary="Replace a pitch source")
def replace_pitch_source(
    target: SourceTarget, data: SourceInput, access: Access, app: ServicesDep, response: Response
) -> PitchSource:
    """Replace a pitch source's link, title, claim and access date. Send If-Match to be safe."""
    return tagged(pitches.replace_source(app, access, target, data), response)


@router.delete(SOURCE, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a pitch source")
def remove_pitch_source(target: SourceTarget, access: Access, app: ServicesDep) -> Response:
    """Stop citing a source for a pitch. Needs content:write."""
    pitches.remove_source(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/channels/{slug}/episodes/{number}/pitch", summary="Read an episode's pitch")
def get_episode_pitch(
    number: Number, access: Access, app: ServicesDep, response: Response
) -> EpisodePitch:
    """Which pitch an episode refers to; `pitch_id` is null when none. Its research is the
    pitch's own, read with get_pitch_notes and list_pitch_sources, never a copy.
    """
    return tagged(pitches.get_episode_pitch(app, access, number), response)


@router.put("/channels/{slug}/episodes/{number}/pitch", summary="Set an episode's pitch")
def set_episode_pitch(
    target: WriteTarget, data: PitchChoice, access: Access, app: ServicesDep, response: Response
) -> EpisodePitch:
    """Point an episode at one of the channel's pitches, or send `pitch_id: null` for none.

    Several episodes may share a pitch. Needs content:write; send If-Match to be safe.
    """
    chosen = pitches.choose_episode_pitch(
        app, access, target.number, data, target.expected_revision
    )
    return tagged(chosen, response)


def install_error_handlers(app: FastAPI) -> None:
    """Pitch errors as problem details: a missing pitch is 404, one still in use is 409."""

    @app.exception_handler(PitchNotFound)
    async def pitch_not_found(request: Request, missing: PitchNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Pitch {missing} not found; list_pitches shows the channel's pitches",
        )

    @app.exception_handler(PitchInUse)
    async def pitch_in_use(request: Request, taken: PitchInUse) -> JSONResponse:
        numbers = ", ".join(f"#{number}" for number in taken.episodes)
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            f"Pitch {taken} is used by episodes {numbers}; set their pitch to none first",
            problem_type("pitch-in-use"),
            episodes=taken.episodes,
        )
