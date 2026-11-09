"""Review routes: who reviewed an episode, what they saw and what they decided (project.reviews)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse

from sreditor import review_operations as reviews
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type
from sreditor.review_operations import ReviewQuery, ReviewRef
from sreditor.reviews import Review, ReviewInput, ReviewNotFound

router = APIRouter(tags=["reviews"])

REVIEWS = "/channels/{slug}/episodes/{number}/reviews"

ReviewId = Annotated[UUID, Path(description="The review's id, from list_reviews.")]


def review_query(
    number: Number, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> ReviewQuery:
    """Which page of an episode's reviews to list."""
    return ReviewQuery(number=number, cursor=cursor, limit=limit)


def review_ref(number: Number, review_id: ReviewId) -> ReviewRef:
    """The review an address names."""
    return ReviewRef(number=number, review_id=review_id)


# Path ids in one parameter, so routes keep to five (code.functions.args).
ReviewTarget = Annotated[ReviewRef, Depends(review_ref)]


@router.get(REVIEWS, summary="List an episode's reviews")
def list_reviews(
    query: Annotated[ReviewQuery, Depends(review_query)], access: Access, app: ServicesDep
) -> Page[Review]:
    """List an episode's reviews in the order they were added, each with its reviewer, when it
    was made, the file reviewed (with its SHA-256) and the time ranges seen, its notes
    (Markdown) and its verdict (approved, changes_requested or rejected). The latest verdict,
    by `reviewed_at`, is the episode's. Needs channels:read."""
    return reviews.list_reviews(app, access, query)


@router.post(
    REVIEWS,
    status_code=status.HTTP_201_CREATED,
    response_model=Review,
    summary="Add a review",
)
def add_review(
    number: Number,
    data: ReviewInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Record a review of an episode: the `verdict`, `notes` in Markdown and, optionally, the
    media file reviewed (`media_id`, from list_media) with the `seen` time ranges in seconds,
    and `reviewed_at` when the review was made earlier.

    Needs the owner or editor role and the editorial:write scope. The reviewer is you, or the
    token's user, and the token is recorded. A review cannot be edited afterwards, only deleted.
    A file that is not the episode's, a range that ends before it starts or past the end of a
    measured file, or a future time is refused with 422 naming the field. Accepts an
    Idempotency-Key header, so a retry does not record the review twice.
    """
    return idempotency.run(
        lambda: reviews.add_review(app, access, number, data), status.HTTP_201_CREATED
    )


@router.delete(
    f"{REVIEWS}/{{review_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a review",
)
def remove_review(target: ReviewTarget, access: Access, app: ServicesDep) -> Response:
    """Delete a review. Only the channel's owners, or the reviewer who wrote it, may; a review
    is never edited. Needs editorial:write, or members:write for another person's review."""
    reviews.remove_review(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install_error_handlers(app: FastAPI) -> None:
    """A missing review is 404; a review that names a file or time that cannot be is 422."""

    @app.exception_handler(ReviewNotFound)
    async def review_not_found(request: Request, missing: ReviewNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Review {missing} not found; list_reviews shows the episode's reviews",
        )

    @app.exception_handler(reviews.ReviewRefused)
    async def review_refused(request: Request, refused: reviews.ReviewRefused) -> JSONResponse:
        errors = [{"location": list(refused.location), "message": str(refused), "type": "review"}]
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(refused),
            problem_type("review-refused"),
            errors=errors,
        )
