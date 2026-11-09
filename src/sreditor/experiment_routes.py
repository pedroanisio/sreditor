"""Experiment routes: A/B experiments linking two episodes of a channel (rule project.experiments)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse

from sreditor import experiment_operations as experiments
from sreditor.deps import Access, ServicesDep
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.experiment_operations import (
    ExperimentChange,
    ExperimentQuery,
    ExperimentReading,
)
from sreditor.experiments import (
    EpisodeInExperiment,
    Experiment,
    ExperimentInput,
    ExperimentNotFound,
    ExperimentStatus,
)
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["experiments"])

EXPERIMENTS = "/channels/{slug}/experiments"
EXPERIMENT = f"{EXPERIMENTS}/{{experiment_id}}"

ExperimentId = Annotated[UUID, Path(description="The experiment's id, from list_experiments.")]


def experiment_change(experiment_id: ExperimentId, if_match: IfMatch = None) -> ExperimentChange:
    """The experiment a write targets and the revision its If-Match names."""
    return ExperimentChange(
        experiment_id=experiment_id, expected_revision=expected_revision(if_match)
    )


def experiment_query(
    status: ExperimentStatus | None = None, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> ExperimentQuery:
    """Which page of experiments to list, and of which status."""
    return ExperimentQuery(status=status, cursor=cursor, limit=limit)


# Path id and If-Match in one parameter, so routes keep to five (code.functions.args).
ExperimentTarget = Annotated[ExperimentChange, Depends(experiment_change)]
ExperimentListing = Annotated[ExperimentQuery, Depends(experiment_query)]


def tagged(found: Experiment, response: Response) -> Experiment:
    """The experiment, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get(EXPERIMENTS, summary="List experiments")
def list_experiments(
    access: Access, app: ServicesDep, query: ExperimentListing
) -> Page[Experiment]:
    """List the channel's A/B experiments (two episodes compared), in the order they were added.

    Pass `status` (planned, running, read, abandoned) to see only those. Read one with its
    computed result using get_experiment_result.
    """
    return experiments.list_experiments(app, access, query)


@router.post(
    EXPERIMENTS,
    status_code=status.HTTP_201_CREATED,
    response_model=Experiment,
    summary="Create an experiment",
)
def create_experiment(
    data: ExperimentInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Link two episodes of the channel as a control (A) and a variant (B) with the change
    between them, the hypothesis, the metric and the snapshot age that judge them.

    Needs editorial:write and the owner or editor role. Both episodes must exist (404 names the
    missing one) and differ (422). A `verdict` goes only with the status `read`. Accepts an
    Idempotency-Key header, so a retry does not add it twice.
    """
    return idempotency.run(
        lambda: experiments.create_experiment(app, access, data), status.HTTP_201_CREATED
    )


@router.get(EXPERIMENT, summary="Read an experiment")
def get_experiment(
    experiment_id: ExperimentId, access: Access, app: ServicesDep, response: Response
) -> Experiment:
    """Read one experiment as written. Its ETag is the revision to send as If-Match when
    changing it; the numbers are in get_experiment_result.
    """
    return tagged(experiments.get_experiment(app, access, experiment_id), response)


@router.get(f"{EXPERIMENT}/result", summary="Read an experiment's result")
def get_experiment_result(
    experiment_id: ExperimentId, access: Access, app: ServicesDep
) -> ExperimentReading:
    """Read an experiment with its result, computed now from the numbers entered with
    record_post_metrics and never stored: each side's posts (on the experiment's platform)
    with the metric at the experiment's snapshot age, the mean per side, B minus A (`difference`),
    B over A (`ratio`) and `notes` for what is still missing. It never decides: the verdict
    is the person's, in the experiment's `reading`.
    """
    return experiments.experiment_result(app, access, experiment_id)


@router.put(EXPERIMENT, summary="Replace an experiment")
def replace_experiment(
    target: ExperimentTarget,
    data: ExperimentInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> Experiment:
    """Replace everything an experiment holds, its reading and verdict included; send every
    field. Needs editorial:write and the owner or editor role. Send If-Match (the ETag of
    get_experiment) so a change you have not seen is never overwritten (412).
    """
    return tagged(experiments.replace_experiment(app, access, target, data), response)


@router.delete(EXPERIMENT, status_code=status.HTTP_204_NO_CONTENT, summary="Delete an experiment")
def delete_experiment(target: ExperimentTarget, access: Access, app: ServicesDep) -> Response:
    """Delete an experiment with its reading; its two episodes are untouched. Needs
    editorial:write and the owner or editor role. Send If-Match to refuse if it changed.
    """
    experiments.delete_experiment(app, access, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install_error_handlers(app: FastAPI) -> None:
    """A missing experiment is 404; an episode that experiments compare cannot be deleted (409)."""

    @app.exception_handler(ExperimentNotFound)
    async def experiment_not_found(request: Request, missing: ExperimentNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Experiment {missing} not found; list_experiments shows the channel's experiments",
        )

    @app.exception_handler(EpisodeInExperiment)
    async def episode_in_experiment(request: Request, taken: EpisodeInExperiment) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            f"Episode {taken} is compared by experiments {', '.join(map(str, taken.experiments))};"
            " delete them first",
            problem_type("episode-in-experiment"),
            experiments=[str(found) for found in taken.experiments],
        )
