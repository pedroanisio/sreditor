"""Claim routes: the figures and facts an episode states, tied to the sources that back them."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse

from sreditor import claim_operations as claims
from sreditor.claim_operations import ClaimChange, ClaimQuery, SourceCheck
from sreditor.claims import (
    BackingNotFound,
    CheckInput,
    Claim,
    ClaimInput,
    ClaimNotFound,
    ClaimWarnings,
    SourceNotAvailable,
)
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["claims"])

CLAIMS = "/channels/{slug}/episodes/{number}/claims"
CLAIM = f"{CLAIMS}/{{claim_id}}"

ClaimId = Annotated[UUID, Path(description="The claim's id, from list_claims.")]
SourceId = Annotated[
    UUID, Path(description="The id of one of the claim's backing sources, from the claim.")
]


def claim_change(number: Number, claim_id: ClaimId, if_match: IfMatch = None) -> ClaimChange:
    """The claim a write targets and the revision its If-Match names."""
    return ClaimChange(
        number=number, claim_id=claim_id, expected_revision=expected_revision(if_match)
    )


def claim_query(number: Number, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT) -> ClaimQuery:
    """Which page of an episode's claims to list."""
    return ClaimQuery(number=number, cursor=cursor, limit=limit)


def source_check(
    number: Number, claim_id: ClaimId, source_id: SourceId, if_match: IfMatch = None
) -> SourceCheck:
    """The backing source a check targets, with the claim's revision from If-Match."""
    return SourceCheck(change=claim_change(number, claim_id, if_match), source_id=source_id)


# Path ids and If-Match in one parameter, so routes keep to five (code.functions.args).
ClaimTarget = Annotated[ClaimChange, Depends(claim_change)]
CheckTarget = Annotated[SourceCheck, Depends(source_check)]
ClaimListing = Annotated[ClaimQuery, Depends(claim_query)]


def tagged(found: Claim, response: Response) -> Claim:
    """The claim, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get(CLAIMS, summary="List an episode's claims")
def list_claims(access: Access, app: ServicesDep, query: ClaimListing) -> Page[Claim]:
    """List the claims of an episode (each figure or fact it states), in the order they were
    added, each with its backing sources and whether it has a checked primary source.
    """
    return claims.list_claims(app, access, query)


@router.post(
    CLAIMS, status_code=status.HTTP_201_CREATED, response_model=Claim, summary="Add a claim"
)
def add_claim(
    number: Number,
    data: ClaimInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Add a claim: the quote, where it appears and the backing sources (ids of the episode's
    own sources or of its pitches' sources, each primary or not). Nothing starts checked.

    Needs content:write. Accepts an Idempotency-Key header, so a retry does not add it twice.
    """
    return idempotency.run(
        lambda: claims.add_claim(app, access, number, data), status.HTTP_201_CREATED
    )


@router.get(f"{CLAIMS}/unchecked", summary="List an episode's unchecked claims")
def list_unchecked_claims(number: Number, access: Access, app: ServicesDep) -> ClaimWarnings:
    """The claims with no checked primary source. This is a warning only: it never blocks the
    episode, not even when it moves to published.
    """
    return claims.unchecked_claims(app, access, number)


@router.get(CLAIM, summary="Read a claim")
def get_claim(
    number: Number, claim_id: ClaimId, access: Access, app: ServicesDep, response: Response
) -> Claim:
    """Read one claim. Its ETag is the revision to send as If-Match when changing it."""
    return tagged(claims.get_claim(app, access, number, claim_id), response)


@router.put(CLAIM, summary="Replace a claim")
def replace_claim(
    target: ClaimTarget, data: ClaimInput, access: Access, app: ServicesDep, response: Response
) -> Claim:
    """Replace a claim's quote, place and backing sources. Sources that stay keep their check.
    Send If-Match to keep changes you have not seen.
    """
    return tagged(claims.replace_claim(app, access, target, data), response)


@router.put(f"{CLAIM}/sources/{{source_id}}/check", summary="Check a claim's source")
def check_claim_source(
    target: CheckTarget, data: CheckInput, access: Access, app: ServicesDep, response: Response
) -> Claim:
    """Record that you checked one backing source of a claim (`is_checked` true), or take the
    mark back (false). The check names who made it (you, or the API token) and when. A claim
    counts as backed once a primary source is checked. Needs content:write.
    """
    check = target.model_copy(update={"is_checked": data.is_checked})
    return tagged(claims.check_claim_source(app, access, check), response)


@router.delete(CLAIM, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a claim")
def remove_claim(target: ClaimTarget, access: Access, app: ServicesDep) -> Response:
    """Remove a claim with its backing links; the sources themselves stay. Needs content:write."""
    claims.remove_claim(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install_error_handlers(app: FastAPI) -> None:
    """Claim errors as problem details: a missing claim or backing is 404, a bad source 422."""

    @app.exception_handler(ClaimNotFound)
    async def claim_not_found(request: Request, missing: ClaimNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Claim {missing} not found; list_claims shows the episode's claims",
        )

    @app.exception_handler(BackingNotFound)
    async def backing_not_found(request: Request, missing: BackingNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Source {missing} does not back this claim; read the claim for its source ids",
        )

    @app.exception_handler(SourceNotAvailable)
    async def source_not_available(request: Request, unknown: SourceNotAvailable) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Source {unknown} is neither a source of this episode nor of one of its pitches",
            problem_type("claim-source-unavailable"),
        )
