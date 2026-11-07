"""Classification routes: an episode's editorial classification and the channel's profile."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sreditor import classification_operations as classifying
from sreditor.classification import (
    SCHEMA_VERSION,
    VOCABULARY,
    ChannelProfile,
    Classification,
    ClassificationRefused,
    Dimension,
    EpisodeClassification,
    ObservedProfile,
)
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["classification"])

CLASSIFICATION = "/channels/{slug}/episodes/{number}/classification"
PROFILE = "/channels/{slug}/profile"


class Vocabulary(BaseModel):
    """The values each dimension accepts, and the taxonomy version they come from."""

    model_config = ConfigDict(frozen=True)

    taxonomy_version: str
    dimensions: dict[Dimension, list[str]]


def tagged[T: EpisodeClassification | ChannelProfile](found: T, response: Response) -> T:
    """The resource, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get("/classification/vocabulary", summary="Read the classification vocabulary")
def get_classification_vocabulary() -> Vocabulary:
    """The values each of the twelve dimensions accepts. Read it before classifying."""
    return Vocabulary(
        taxonomy_version=SCHEMA_VERSION,
        dimensions={dimension: list(values) for dimension, values in VOCABULARY.items()},
    )


@router.get(CLASSIFICATION, summary="Read an episode's classification")
def get_episode_classification(
    number: Number, access: Access, app: ServicesDep, response: Response
) -> EpisodeClassification:
    """Read an episode's editorial classification. Revision 0 means nobody classified it.

    `is_reviewed` is false for a classification an agent proposed and no person confirmed.
    """
    return tagged(classifying.get_classification(app, access, number), response)


@router.put(CLASSIFICATION, summary="Classify an episode")
def classify_episode(
    target: WriteTarget, data: Classification, access: Access, app: ServicesDep, response: Response
) -> EpisodeClassification:
    """Replace an episode's classification: each dimension's state and values, satire and
    parody details, and the method. A present dimension has one primary and up to two
    secondary values from get_classification_vocabulary; AI and mixed methods give each value
    a confidence. Rule breaks answer 422 naming each field. Written with an API token, it is a
    proposal until a person confirms it. Needs content:write; send If-Match ("0" when new).
    """
    written = classifying.write_classification(
        app, access, target.number, data, target.expected_revision
    )
    return tagged(written, response)


@router.get(PROFILE, summary="Read the channel's profile")
def get_channel_profile(access: Access, app: ServicesDep, response: Response) -> ChannelProfile:
    """Read the profile the channel declares, in the same dimensions as an episode."""
    return tagged(classifying.get_profile(app, access), response)


@router.put(PROFILE, summary="Declare the channel's profile")
def declare_channel_profile(
    data: Classification,
    access: Access,
    app: ServicesDep,
    response: Response,
    if_match: IfMatch = None,
) -> ChannelProfile:
    """Replace the profile the channel declares: what it is meant to be, in the same
    dimensions and rules as an episode's classification. Needs editorial:write and an editor
    role. Send If-Match ("0" when new).
    """
    written = classifying.write_profile(app, access, data, expected_revision(if_match))
    return tagged(written, response)


@router.get(f"{PROFILE}/observed", summary="Read the channel's observed profile")
def get_observed_profile(access: Access, app: ServicesDep) -> ObservedProfile:
    """Count each value over the channel's classified episodes: how many hold it as primary,
    as primary or secondary, and its share of all classified episodes. `drift` lists declared
    primary values that fewer than half of them hold.
    """
    return classifying.get_observed_profile(app, access)


def install_error_handlers(app: FastAPI) -> None:
    """A classification that breaks the rules is 422, with one error per field at fault."""

    @app.exception_handler(ClassificationRefused)
    async def refused(request: Request, refusal: ClassificationRefused) -> JSONResponse:
        errors = [
            {
                "location": ["body", *found.field.split(".")],
                "message": found.message,
                "type": "rule",
            }
            for found in refusal.refusals
        ]
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(refusal),
            problem_type("classification-refused"),
            errors=errors,
        )
