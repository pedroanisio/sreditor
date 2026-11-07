"""Delivery operations shared by the REST routes, the MCP tools and the pages (rule project.delivery).

Reading needs the read permission; writing the package is content work, open to contributors and
the content:write scope. A cut's video and cover must be files of the same episode. Gaps in the
package only warn (they never block a status), so the warnings are computed on every read.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, HttpUrl, ValidationError

from sreditor.channels import Platform
from sreditor.delivery import (
    VIDEO_FAMILY,
    Cut,
    CutName,
    Delivery,
    DeliveryInput,
    Post,
    warnings_of,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.media import MediaFile
from sreditor.members import Permission
from sreditor.operations import gated

IMAGE_FAMILY = "image"
VALUE_ERROR_PREFIX = "Value error, "


class DeliveryOperation:
    """stmt operation names an agent's delivery writes are gated on."""

    WRITE = "delivery.write"
    POST = "delivery.post"


class DeliveryRefused(ValueError):
    """A package that names something the episode or channel does not have, or breaks a rule."""

    def __init__(self, location: tuple[str | int, ...], detail: str) -> None:
        super().__init__(detail)
        self.location = location


class PostInput(BaseModel):
    """What one post says: the cut, the link and when it went live."""

    model_config = ConfigDict(frozen=True)

    cut: CutName
    url: HttpUrl | None = None
    published_at: datetime | None = None


NO_VIDEO = (
    "%(id)s is not a video of this episode; list_media shows the episode's files, and a video is"
    " a visual_effect or render file."
)
NO_COVER = (
    "%(id)s is not an image of this episode; upload one with the API's upload_media as kind cover,"
    " then pass its id from list_media with kind cover."
)
NO_ACCOUNT = (
    "The channel has no %(platform)s account; get_channel lists its accounts, and"
    " put_account adds one."
)


def episode_videos(app: Services, slug: str, number: int) -> list[MediaFile]:
    """The episode's video files, any kind, including finished renders."""
    return app.media.of_family(slug, number, VIDEO_FAMILY)


def episode_images(app: Services, slug: str, number: int) -> list[MediaFile]:
    """The episode's image files, such as covers."""
    return app.media.of_family(slug, number, IMAGE_FAMILY)


def with_warnings(app: Services, delivery: Delivery) -> Delivery:
    """The package with the gaps that weaken it, for a person or agent to read."""
    video_ids = {video.id for video in episode_videos(app, delivery.channel, delivery.number)}
    found = warnings_of(delivery, video_ids)
    return delivery.model_copy(update={"warnings": found})


def get_delivery(app: Services, access: ChannelAccess, number: int) -> Delivery:
    """The episode's delivery package with its warnings; revision 0 means nobody wrote it."""
    slug = episode_access(app, access, number, Permission.READ)
    return with_warnings(app, app.deliveries.delivery(slug, number))


def check_files(app: Services, slug: str, number: int, data: DeliveryInput) -> None:
    """Refuse a cut whose video or cover is not a file of this episode."""
    videos = {video.id for video in episode_videos(app, slug, number)}
    images = {image.id for image in episode_images(app, slug, number)}
    for index, cut in enumerate(data.cuts):
        if cut.video_id is not None and cut.video_id not in videos:
            raise DeliveryRefused(("cuts", index, "video_id"), NO_VIDEO % {"id": cut.video_id})
        if cut.cover_id is not None and cut.cover_id not in images:
            raise DeliveryRefused(("cuts", index, "cover_id"), NO_COVER % {"id": cut.cover_id})


def check_accounts(access: ChannelAccess, posts: list[Post]) -> None:
    """Refuse a post on a platform where the channel has no account."""
    for index, post in enumerate(posts):
        if access.channel.account(post.platform) is None:
            raise DeliveryRefused(
                ("posts", index, "platform"), NO_ACCOUNT % {"platform": post.platform.value}
            )


def consistent(cuts: list[Cut], posts: list[Post]) -> DeliveryInput:
    """A package from cuts and posts; DeliveryRefused when they contradict each other."""
    try:
        return DeliveryInput(cuts=cuts, posts=posts)
    except ValidationError as invalid:
        first = invalid.errors()[0]
        raise DeliveryRefused((), first["msg"].removeprefix(VALUE_ERROR_PREFIX)) from invalid


def write_delivery(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: DeliveryInput,
    expected_revision: int | None,
) -> Delivery:
    """Replace the episode's whole delivery package."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    check_files(app, slug, number, data)
    check_accounts(access, data.posts)
    written = gated(
        app,
        access,
        DeliveryOperation.WRITE,
        lambda: app.deliveries.write(slug, number, data, expected_revision),
    )
    return with_warnings(app, written)


def with_cut(delivery: DeliveryInput, cut: Cut) -> DeliveryInput:
    """The package with the cut added, or replacing the one of the same name."""
    cuts = [cut if other.name == cut.name else other for other in delivery.cuts]
    if all(other.name != cut.name for other in delivery.cuts):
        cuts.append(cut)
    return consistent(cuts, delivery.posts)


def without_cut(delivery: DeliveryInput, name: str) -> DeliveryInput:
    """The package without the named cut; refused while a post still uses it."""
    return consistent([cut for cut in delivery.cuts if cut.name != name], delivery.posts)


def with_post(delivery: DeliveryInput, post: Post) -> DeliveryInput:
    """The package with the platform's post set, replacing its earlier one."""
    others = [other for other in delivery.posts if other.platform != post.platform]
    return consistent(delivery.cuts, [*others, post])


def without_post(delivery: DeliveryInput, platform: Platform) -> DeliveryInput:
    """The package without the platform's post."""
    return consistent(delivery.cuts, [p for p in delivery.posts if p.platform != platform])


def record_post(
    app: Services,
    access: ChannelAccess,
    number: int,
    platform: Platform,
    post: PostInput,
) -> Delivery:
    """Set the post of one platform account in the package, keeping the cuts and other posts."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    current = app.deliveries.delivery(slug, number)
    chosen = Post(platform=platform, cut=post.cut, url=post.url, published_at=post.published_at)
    changed = with_post(current, chosen)
    check_accounts(access, [chosen])
    written = gated(
        app,
        access,
        DeliveryOperation.POST,
        lambda: app.deliveries.write(slug, number, changed, current.revision),
    )
    return with_warnings(app, written)
