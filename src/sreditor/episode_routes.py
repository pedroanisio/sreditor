"""Episode routes: a channel's numbered productions (rule project.episodes)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sreditor import episode_operations
from sreditor.deps import Access, ServicesDep
from sreditor.episodes import Episode, EpisodeInput, RevisedEpisode
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page

router = APIRouter(tags=["episodes"])

# RFC 9111 section 5.5 Warning with code 199, "miscellaneous warning": the change went through.
WARNING_HEADER = "Warning"
MISCELLANEOUS_WARNING = 199

Number = Annotated[int, Path(ge=1, description="The episode's number within the channel.")]


class EpisodeWrite(BaseModel):
    """The episode a write targets and the revision its If-Match header expects."""

    model_config = ConfigDict(frozen=True)

    number: int
    expected_revision: int | None = None


def episode_write(number: Number, if_match: IfMatch = None) -> EpisodeWrite:
    """The episode in the path and the revision in If-Match, read together."""
    return EpisodeWrite(number=number, expected_revision=expected_revision(if_match))


# A write's episode and If-Match in one parameter, so routes keep to five (code.functions.args).
WriteTarget = Annotated[EpisodeWrite, Depends(episode_write)]


def with_etag(revised: RevisedEpisode, response: Response) -> Episode:
    """The episode, with its revision as the response's ETag and any warning as a Warning header."""
    response.headers["ETag"] = etag(revised.revision)
    if revised.claims_warning:
        response.headers[WARNING_HEADER] = (
            f'{MISCELLANEOUS_WARNING} sreditor "{revised.claims_warning}"'
        )
    return revised.episode


@router.get("/channels/{slug}/episodes", summary="List episodes")
def list_episodes(
    access: Access, app: ServicesDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> Page[Episode]:
    """List the channel's episodes, ordered by number."""
    return episode_operations.list_episodes(app, access, cursor, limit)


@router.post(
    "/channels/{slug}/episodes",
    status_code=status.HTTP_201_CREATED,
    response_model=Episode,
    summary="Create an episode",
)
def create_episode(
    data: EpisodeInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Add an episode with the channel's next number. Needs content:write.

    Accepts an Idempotency-Key header, so a retried request does not add a second episode.
    """
    return idempotency.run(
        lambda: episode_operations.create_episode(app, access, data).episode,
        status.HTTP_201_CREATED,
    )


@router.get("/channels/{slug}/episodes/{number}", summary="Read an episode")
def get_episode(number: Number, access: Access, app: ServicesDep, response: Response) -> Episode:
    """Read one episode. Its ETag is the revision to send as If-Match when changing it."""
    return with_etag(episode_operations.get_episode(app, access, number), response)


@router.put("/channels/{slug}/episodes/{number}", summary="Replace an episode")
def replace_episode(
    target: WriteTarget, data: EpisodeInput, access: Access, app: ServicesDep, response: Response
) -> Episode:
    """Replace an episode's title, summary and status. Send If-Match to keep others' changes."""
    revised = episode_operations.replace_episode(
        app, access, target.number, data, target.expected_revision
    )
    return with_etag(revised, response)


@router.delete(
    "/channels/{slug}/episodes/{number}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an episode",
)
def delete_episode(
    number: Number, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Delete an episode; its number is never reused. Needs channels:write and an editor role."""
    episode_operations.delete_episode(app, access, number, expected_revision(if_match))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
