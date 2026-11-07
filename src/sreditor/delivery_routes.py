"""Delivery routes: an episode's cuts and where each was posted (rule project.delivery)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sreditor import delivery_operations as deliveries
from sreditor.channels import Platform
from sreditor.delivery import Delivery, DeliveryInput
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import etag
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["delivery"])

DELIVERY = "/channels/{slug}/episodes/{number}/delivery"

PlatformPath = Annotated[
    Platform, Path(description="The platform of one of the channel's accounts.")
]


class PostPath(BaseModel):
    """The episode and platform account a post's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    platform: Platform


def post_path(number: Number, platform: PlatformPath) -> PostPath:
    """The episode and platform in the address."""
    return PostPath(number=number, platform=platform)


# Both path values in one parameter, so routes keep to five (code.functions.args).
PostTarget = Annotated[PostPath, Depends(post_path)]


def tagged(delivery: Delivery, response: Response) -> Delivery:
    """The package, with its revision as the ETag."""
    response.headers["ETag"] = etag(delivery.revision)
    return delivery


@router.get(DELIVERY, summary="Read the delivery package")
def get_delivery(number: Number, access: Access, app: ServicesDep, response: Response) -> Delivery:
    """Read an episode's delivery package: its cuts (final video, cover, caption, alt text,
    hashtags) and the post on each platform account (cut, link, publish time).

    `warnings` lists a cut without a video and a post without a link; they never block a
    status. Revision 0 (ETag "0") means nobody wrote the package yet.
    """
    return tagged(deliveries.get_delivery(app, access, number), response)


@router.put(DELIVERY, summary="Write the delivery package")
def write_delivery(
    target: WriteTarget,
    data: DeliveryInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> Delivery:
    """Replace an episode's whole delivery package. Needs content:write.

    A cut's video must be a video media file of the episode and its cover an image one; a post
    needs a platform the channel has an account on and the name of a cut in the same package.
    Send If-Match with the ETag you read ("0" when empty) so a change you have not seen is
    never overwritten.
    """
    written = deliveries.write_delivery(app, access, target.number, data, target.expected_revision)
    return tagged(written, response)


@router.put(f"{DELIVERY}/posts/{{platform}}", summary="Record a post")
def record_post(
    target: PostTarget,
    data: deliveries.PostInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> Delivery:
    """Set the post on one platform account: which cut, its link and when it went live.

    Needs content:write. It replaces that platform's earlier post and keeps the cuts and the
    other posts, so use it to add a link after publishing without rewriting the package.
    """
    return tagged(
        deliveries.record_post(app, access, target.number, target.platform, data), response
    )


def install_error_handlers(app: FastAPI) -> None:
    """A package that contradicts the episode or channel is 422, naming the field at fault."""

    @app.exception_handler(deliveries.DeliveryRefused)
    async def delivery_refused(
        request: Request, refused: deliveries.DeliveryRefused
    ) -> JSONResponse:
        errors = [{"location": list(refused.location), "message": str(refused), "type": "delivery"}]
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(refused),
            problem_type("delivery-refused"),
            errors=errors,
        )
