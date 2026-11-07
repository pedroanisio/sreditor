"""Research routes: an episode's notes and the sources behind its claims."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from sreditor import research_operations as research
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.research import NotesInput, ResearchNotes, Source, SourceInput
from sreditor.research_operations import SourceChange

router = APIRouter(tags=["research"])

EPISODE = "/channels/{slug}/episodes/{number}"


def tagged[T: ResearchNotes | Source](found: T, response: Response) -> T:
    """The notes or source, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get(f"{EPISODE}/research", summary="Read research notes")
def get_research_notes(
    number: Number, access: Access, app: ServicesDep, response: Response
) -> ResearchNotes:
    """Read an episode's research notes. Revision 0 (ETag "0") means nobody wrote them yet."""
    return tagged(research.get_notes(app, access, number), response)


@router.put(f"{EPISODE}/research", summary="Write research notes")
def write_research_notes(
    target: WriteTarget, data: NotesInput, access: Access, app: ServicesDep, response: Response
) -> ResearchNotes:
    """Replace an episode's research notes. Send If-Match to keep changes you have not seen."""
    notes = research.write_notes(app, access, target.number, data, target.expected_revision)
    return tagged(notes, response)


@router.get(f"{EPISODE}/sources", summary="List sources")
def list_sources(
    number: Number,
    access: Access,
    app: ServicesDep,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[Source]:
    """List the sources an episode cites, in the order they were added."""
    return research.list_sources(app, access, number, cursor, limit)


@router.post(
    f"{EPISODE}/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=Source,
    summary="Cite a source",
)
def add_source(
    number: Number,
    data: SourceInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Cite a source: its link, title, the claim it supports and when it was read.

    Needs content:write. Accepts an Idempotency-Key header, so a retry does not cite it twice.
    """
    return idempotency.run(
        lambda: research.add_source(app, access, number, data), status.HTTP_201_CREATED
    )


@router.get(f"{EPISODE}/sources/{{source_id}}", summary="Read a source")
def get_source(
    number: Number, source_id: UUID, access: Access, app: ServicesDep, response: Response
) -> Source:
    """Read one source. Its ETag is the revision to send as If-Match when changing it."""
    return tagged(research.get_source(app, access, number, source_id), response)


@router.put(f"{EPISODE}/sources/{{source_id}}", response_model=Source, summary="Replace a source")
def replace_source(
    target: WriteTarget, source_id: UUID, data: SourceInput, access: Access, app: ServicesDep
) -> Response:
    """Replace a source's link, title, claim and access date. Send If-Match to be safe."""
    change = SourceChange(
        number=target.number, source_id=source_id, expected_revision=target.expected_revision
    )
    source = research.replace_source(app, access, change, data)
    return JSONResponse(source.model_dump(mode="json"), headers={"ETag": etag(source.revision)})


@router.delete(
    f"{EPISODE}/sources/{{source_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a source",
)
def remove_source(
    number: Number, source_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Stop citing a source. Needs content:write."""
    change = SourceChange(
        number=number, source_id=source_id, expected_revision=expected_revision(if_match)
    )
    research.remove_source(app, access, change)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
