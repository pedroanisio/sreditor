"""Delivery operations shared by the REST routes, the MCP tools and the pages (rule project.delivery).

Reading needs the read permission; writing the package is content work, open to contributors and
the content:write scope. A cut's video and cover must be files of the same episode. Gaps in the
package only warn (they never block a status), so the warnings are computed on every read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from sreditor.channels import Platform
from sreditor.delivery import (
    MAX_POST_HASHTAGS,
    VIDEO_FAMILY,
    AltText,
    Caption,
    Cut,
    CutName,
    Delivery,
    DeliveryFile,
    DeliveryInput,
    Hashtag,
    Post,
    warnings_of,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.i18n import N_
from sreditor.media import MediaFile
from sreditor.media_operations import measured
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.settings import DeliveryRules

IMAGE_FAMILY = "image"
VALUE_ERROR_PREFIX = "Value error, "
POST_FIELD_DEPTH = 2  # skips "posts" and the index in a refusal's location


class DeliveryOperation:
    """stmt operation names an agent's delivery writes are gated on."""

    WRITE = "delivery.write"
    POST = "delivery.post"


class DeliveryRefused(ValueError):
    """A package that names something the episode or channel does not have, or breaks a rule."""

    def __init__(
        self,
        location: tuple[str | int, ...],
        detail: str,
        values: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.location = location
        # What the message names, so pages can translate it with the same numbers or patterns.
        self.values = dict(values or {})


class PostInput(BaseModel):
    """What one post says: the cut, the link, when it went live and its own text.

    A text field left out keeps what the platform's earlier post had, so adding a link never
    wipes the post's caption; an empty caption, alt text or list clears it back to the cut's.
    """

    model_config = ConfigDict(frozen=True)

    cut: CutName
    url: HttpUrl | None = None
    published_at: datetime | None = None
    caption: Caption | None = Field(
        default=None,
        description="The platform's own caption, which must carry the AI notice; empty uses the "
        "cut's, left out keeps the earlier post's.",
    )
    alt_text: AltText | None = Field(
        default=None,
        description="The platform's own alt text; empty uses the cut's, left out keeps the "
        "earlier post's.",
    )
    hashtags: list[Hashtag] | None = Field(
        default=None,
        max_length=MAX_POST_HASHTAGS,
        description="The platform's own hashtags, at most 5; empty uses the cut's, left out "
        "keeps the earlier post's.",
    )


NO_VIDEO = (
    "%(id)s is not a video of this episode; list_media shows the episode's files, and a video is"
    " a final_video, visual_effect or render file."
)
NO_COVER = (
    "%(id)s is not an image of this episode; upload one with the API's upload_media as kind cover,"
    " then pass its id from list_media with kind cover."
)
AI_NOTICE_REQUIRED = N_(
    'The caption needs an AI notice, such as "Made with AI" or "Feito com IA"; it must match'
    " the channel's pattern %(pattern)s."
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


def files_of(delivery: Delivery, media: list[MediaFile]) -> list[DeliveryFile]:
    """The media files the cuts use as video or cover, with their SHA-256."""
    used = {file_id for cut in delivery.cuts for file_id in (cut.video_id, cut.cover_id) if file_id}
    return [
        DeliveryFile(
            id=file.id,
            kind=file.kind,
            name=file.name,
            media_type=file.media_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
        )
        for file in media
        if file.id in used
    ]


def with_warnings(app: Services, delivery: Delivery) -> Delivery:
    """The package with the gaps that weaken it and the files it uses, for a reader."""
    videos = episode_videos(app, delivery.channel, delivery.number)
    durations = {}
    for video in videos:
        measures = measured(app, video).measures
        durations[video.id] = None if measures is None else measures.duration_seconds
    found = warnings_of(delivery, durations, app.settings.delivery)
    images = episode_images(app, delivery.channel, delivery.number)
    files = files_of(delivery, [*videos, *images])
    return delivery.model_copy(update={"warnings": found, "files": files})


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


def check_post_texts(rules: DeliveryRules, posts: list[Post], stored: list[Post]) -> None:
    """Refuse a post caption without the AI notice; one it already held is not checked again.

    Skipping unchanged captions keeps a package editable after the channel's pattern changes.
    """
    if not rules.ai_notice_pattern:
        return
    held = {post.platform: post.caption for post in stored}
    for index, post in enumerate(posts):
        is_new = post.caption != held.get(post.platform)
        if post.caption and is_new and not re.search(rules.ai_notice_pattern, post.caption):
            values = {"pattern": rules.ai_notice_pattern}
            raise DeliveryRefused(("posts", index, "caption"), AI_NOTICE_REQUIRED % values, values)


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
    check_post_texts(app.settings.delivery, data.posts, app.deliveries.delivery(slug, number).posts)
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


def merged_post(platform: Platform, post: PostInput, earlier: Post | None) -> Post:
    """The post as recorded: text the input leaves out stays as the earlier post had it."""
    held = earlier or Post(platform=platform, cut=post.cut)
    return Post(
        platform=platform,
        cut=post.cut,
        url=post.url,
        published_at=post.published_at,
        caption=held.caption if post.caption is None else post.caption,
        alt_text=held.alt_text if post.alt_text is None else post.alt_text,
        hashtags=held.hashtags if post.hashtags is None else post.hashtags,
    )


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
    earlier = next((other for other in current.posts if other.platform == platform), None)
    chosen = merged_post(platform, post, earlier)
    changed = with_post(current, chosen)
    check_accounts(access, [chosen])
    try:
        check_post_texts(app.settings.delivery, [chosen], current.posts)
    except DeliveryRefused as refused:
        # The body of this call is one post, so the field is named without the list around it.
        location = refused.location[POST_FIELD_DEPTH:]
        raise DeliveryRefused(location, str(refused), refused.values) from refused
    written = gated(
        app,
        access,
        DeliveryOperation.POST,
        lambda: app.deliveries.write(slug, number, changed, current.revision),
    )
    return with_warnings(app, written)
