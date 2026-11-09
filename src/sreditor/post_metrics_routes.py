"""Post metrics routes: dated numbers per post and a channel's posts compared (project.metrics)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse

from sreditor import post_metrics_operations as metrics
from sreditor.delivery_routes import DELIVERY, PostTarget
from sreditor.deps import Access, ServicesDep
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.post_metrics import Metric, PostMetric, Snapshot, SnapshotInput
from sreditor.problems import problem_response, problem_type
from sreditor.retention_csv import RetentionCsvError
from sreditor.what_worked import WhatWorked, WhatWorkedQuery
from sreditor.what_worked_operations import what_worked as run_what_worked

router = APIRouter(tags=["post metrics"])

SNAPSHOTS = f"{DELIVERY}/posts/{{platform}}/snapshots"


def post_query(
    sort_by: Annotated[Metric, Query(description="The number to rank posts by.")] = Metric.VIEWS,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> metrics.PostQuery:
    """How to compare posts: by which number, and which page."""
    return metrics.PostQuery(sort_by=sort_by, cursor=cursor, limit=limit)


@router.post(
    SNAPSHOTS,
    status_code=status.HTTP_201_CREATED,
    response_model=Snapshot,
    summary="Record a post's numbers",
)
def record_post_metrics(
    target: PostTarget,
    data: SnapshotInput,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Enter a post's numbers for one day: views, reach, likes, comments, shares, saves, average
    watch time and retention per second. Needs content:write and a post on that platform in the
    episode's delivery package (record_post).

    Every number is optional and non-negative. A second entry for the same day merges into the
    first: a number you leave out keeps its value. Accepts an Idempotency-Key header.
    """
    return idempotency.run(
        lambda: metrics.record_metrics(app, access, target.number, target.platform, data),
        status.HTTP_201_CREATED,
    )


@router.post(
    f"{DELIVERY}/posts/{{platform}}/retention-csv",
    status_code=status.HTTP_201_CREATED,
    response_model=Snapshot,
    summary="Import a retention CSV",
)
def import_retention_csv(
    target: PostTarget,
    source: metrics.RetentionImport,
    access: Access,
    app: ServicesDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Import a platform's retention export into a post's snapshot for one day.

    The CSV text needs a header and two columns, the second and the percent still watching
    ("second,percent", or the names Instagram and YouTube exports use); anything else is refused
    with 422 and the rows at fault. It replaces that day's retention and keeps its other numbers.
    """
    return idempotency.run(
        lambda: metrics.import_retention(app, access, target.number, target.platform, source),
        status.HTTP_201_CREATED,
    )


@router.get(SNAPSHOTS, summary="List a post's snapshots")
def list_post_snapshots(
    target: PostTarget,
    access: Access,
    app: ServicesDep,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[Snapshot]:
    """List a post's snapshots, newest day first, each with its retention curve."""
    post = metrics.post_of(app, access, target.number, target.platform)
    return metrics.list_snapshots(app, access, post, cursor, limit)


@router.get("/channels/{slug}/post-metrics", summary="Compare the channel's posts")
def compare_posts(
    query: Annotated[metrics.PostQuery, Depends(post_query)], access: Access, app: ServicesDep
) -> Page[PostMetric]:
    """Compare the channel's posts by their latest numbers, best first by `sort_by`.

    Each row is one post of an episode with its latest snapshot (views, reach, likes, comments,
    shares, saves, average watch time) and how many snapshots it has; read a post's retention
    with list_post_snapshots. Posts nobody measured come last.
    """
    return metrics.compare_posts(app, access, query)


@router.get("/channels/{slug}/post-metrics/what-worked", summary="See what worked")
def what_worked(
    query: Annotated[WhatWorkedQuery, Query()], access: Access, app: ServicesDep
) -> WhatWorked:
    """Group the channel's published posts by one dimension and compare one number per group.

    Choose `group_by`: `classification` with a `classification_dimension` (the primary value of a
    reviewed classification; proposals are left out and counted), `hook_reaction` or
    `hook_length` (from the short script's hook block) or `video_length` (the posted cut's
    video). Each group reports its posts, their episodes and the median and mean of `metric`
    taken from every post's latest snapshot, or from the one nearest `age` days after publishing.
    Groups under `min_group_posts` are flagged `is_too_small`. `excluded` counts the posts no
    group holds and why. Filter by `platform` and by publish day. Use compare_posts to rank
    single posts instead.
    """
    return run_what_worked(app, access, query)


def install_error_handlers(app: FastAPI) -> None:
    """A post that is not in the package is 404; a CSV that is not a retention export is 422."""

    @app.exception_handler(metrics.PostNotRecorded)
    async def post_not_recorded(request: Request, missing: metrics.PostNotRecorded) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"The episode has no post on {missing}; record_post adds it to the delivery package",
            problem_type("post-not-recorded"),
        )

    @app.exception_handler(RetentionCsvError)
    async def bad_csv(request: Request, refused: RetentionCsvError) -> JSONResponse:
        errors = [
            {
                "location": ["csv", problem.row, problem.field],
                "message": problem.message,
                "type": "csv",
            }
            for problem in refused.problems
        ]
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The CSV is not a retention export",
            problem_type("invalid-retention-csv"),
            errors=errors,
        )
