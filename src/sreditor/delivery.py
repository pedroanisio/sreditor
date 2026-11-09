"""An episode's delivery package: its cuts and where each was posted (rule project.delivery).

A cut is one version of the episode (main, vertical, teaser...) with its final video and cover,
caption, alt text and hashtags; a post says which cut went to which platform account of the
channel, with the post's link and publish time. The whole package is stored and revised on its
own, with its own revision for the ETag. A post may carry its own text for its platform
(caption, alt text, hashtags), refused when it has more than MAX_POST_HASHTAGS hashtags or no AI
notice; a post without its own text uses its cut's. Missing pieces only warn, never block a status.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from sreditor.channels import Platform
from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.field_types import SLUG_PATTERN, ChannelSlug, Sha256
from sreditor.i18n import N_
from sreditor.media import MediaKind, MediaStore
from sreditor.settings import DeliveryRules

CAPTION_LENGTH = 1000
ALT_TEXT_LENGTH = 1000
CUT_NAME_LENGTH = 40
HASHTAG_LENGTH = 100
MAX_CUTS = 10
MAX_POSTS = MAX_CUTS * len(Platform)  # one post per cut on each platform account
MAX_HASHTAGS = 30
MAX_POST_HASHTAGS = 5  # a post's own text is refused above this, where a cut only warns
HASHTAG_SIGIL = "#"
VIDEO_FAMILY = "video"  # the media-type family of a cut's final video
LOCATION_LENGTH = 500
CUT_NAME_PATTERN = SLUG_PATTERN
# Letters, digits and underscores of any language: "#cachorrinho" and "#açaí" are valid.
HASHTAG_PATTERN = r"^\w+$"

CutName = Annotated[str, StringConstraints(max_length=CUT_NAME_LENGTH, pattern=CUT_NAME_PATTERN)]
Caption = Annotated[str, StringConstraints(strip_whitespace=True, max_length=CAPTION_LENGTH)]
AltText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=ALT_TEXT_LENGTH)]


def without_sigil(value: object) -> object:
    """A hashtag as people type it, "#funny", without the #; other values pass on unchanged."""
    return value.strip().lstrip(HASHTAG_SIGIL) if isinstance(value, str) else value


Hashtag = Annotated[
    str,
    BeforeValidator(without_sigil),
    StringConstraints(min_length=1, max_length=HASHTAG_LENGTH, pattern=HASHTAG_PATTERN),
]


def in_utc(moment: datetime) -> datetime:
    """A moment with no zone is read as UTC, so every stored time compares with the others."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


class ExternalVideo(BaseModel):
    """A master kept outside sreditor, such as in a Drive: where it is and what it measures.

    sreditor never opens the location, so the measures are as the caller declares them.
    """

    model_config = ConfigDict(frozen=True)

    location: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LOCATION_LENGTH)
    ] = Field(description="Where the file lives, such as a Drive path or an https link.")
    sha256: Sha256 = Field(description="The SHA-256 of the file, to tell masters apart.")
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    loudness_lufs: float | None = None


def distinct_hashtags(hashtags: list[str]) -> None:
    """Refuse the same hashtag twice, ignoring case, since platforms treat them as one."""
    folded = [tag.casefold() for tag in hashtags]
    if len(set(folded)) != len(folded):
        raise ValueError(N_("Each hashtag may appear once, whatever its case"))


class Cut(BaseModel):
    """One version of the episode, with the files and texts it is posted with."""

    model_config = ConfigDict(frozen=True)

    name: CutName = Field(
        description="A slug such as main, vertical or teaser; unique per episode."
    )
    video_id: UUID | None = Field(
        default=None,
        description="The id of a video media file of this episode (list_media), such as a "
        "final_video uploaded with create_upload_link.",
    )
    external_video: ExternalVideo | None = Field(
        default=None,
        description="A master registered by reference, with no upload; not with video_id.",
    )
    cover_id: UUID | None = Field(
        default=None, description="The id of an image media file of this episode (kind cover)."
    )
    caption: Caption = ""
    alt_text: AltText = ""
    hashtags: list[Hashtag] = Field(default_factory=list, max_length=MAX_HASHTAGS)

    @model_validator(mode="after")
    def has_one_video(self) -> Self:
        """Refuse an uploaded video and a referenced one on the same cut."""
        if self.video_id is not None and self.external_video is not None:
            raise ValueError(N_("A cut has an uploaded video or a referenced one, not both"))
        return self

    @model_validator(mode="after")
    def has_distinct_hashtags(self) -> Self:
        """Refuse the same hashtag twice, ignoring case."""
        distinct_hashtags(self.hashtags)
        return self


class Post(BaseModel):
    """What went to one platform account of the channel."""

    model_config = ConfigDict(frozen=True)

    platform: Platform = Field(description="The platform of one of the channel's accounts.")
    cut: CutName = Field(description="The name of the cut that was posted.")
    url: HttpUrl | None = Field(default=None, description="The post's link, http or https.")
    published_at: Annotated[datetime, AfterValidator(in_utc)] | None = None
    caption: Caption = Field(
        default="",
        description="This platform's own caption, which must carry the AI notice; empty uses "
        "the cut's caption.",
    )
    alt_text: AltText = Field(
        default="", description="This platform's own alt text; empty uses the cut's."
    )
    hashtags: list[Hashtag] = Field(
        default_factory=list,
        max_length=MAX_POST_HASHTAGS,
        description="This platform's own hashtags, at most 5; empty uses the cut's.",
    )

    @model_validator(mode="after")
    def has_distinct_hashtags(self) -> Self:
        """Refuse the same hashtag twice, ignoring case."""
        distinct_hashtags(self.hashtags)
        return self


def post_key(post: Post) -> tuple[Platform, str]:
    """What identifies a post: its platform account and the cut it carries."""
    return (post.platform, post.cut)


class PostedText(BaseModel):
    """The text that goes out on one platform: the post's own, field by field, else its cut's."""

    model_config = ConfigDict(frozen=True)

    caption: str
    alt_text: str
    hashtags: list[str]


def posted_text(post: Post, cut: Cut) -> PostedText:
    """What the post publishes; each field it leaves empty falls back to the cut's."""
    return PostedText(
        caption=post.caption or cut.caption,
        alt_text=post.alt_text or cut.alt_text,
        hashtags=post.hashtags or cut.hashtags,
    )


class DeliveryInput(BaseModel):
    """The whole delivery package, as written."""

    model_config = ConfigDict(frozen=True)

    cuts: list[Cut] = Field(default_factory=list, max_length=MAX_CUTS)
    posts: list[Post] = Field(default_factory=list, max_length=MAX_POSTS)

    @model_validator(mode="after")
    def is_consistent(self) -> Self:
        """Cut names are unique, each (platform, cut) has one post and each post names a cut."""
        names = [cut.name for cut in self.cuts]
        if len(set(names)) != len(names):
            raise ValueError(N_("Each cut needs its own name"))
        keys = [post_key(post) for post in self.posts]
        if len(set(keys)) != len(keys):
            raise ValueError(N_("Each cut has at most one post on a platform account"))
        unknown = sorted({post.cut for post in self.posts} - set(names))
        if unknown:
            raise ValueError(N_("A post names a cut that is not in this package"))
        return self


class DeliveryWarningKind(StrEnum):
    """What makes a delivery package incomplete without making it unusable."""

    NO_VIDEO = "no_video"
    VIDEO_REMOVED = "video_removed"
    NO_LINK = "no_link"
    TOO_LONG = "too_long"
    TOO_MANY_HASHTAGS = "too_many_hashtags"
    NO_AI_NOTICE = "no_ai_notice"
    POST_TOO_MANY_HASHTAGS = "post_too_many_hashtags"
    POST_NO_AI_NOTICE = "post_no_ai_notice"


WARNING_MESSAGES: dict[DeliveryWarningKind, str] = {
    DeliveryWarningKind.NO_VIDEO: N_("The cut %(cut)s has no final video."),
    DeliveryWarningKind.VIDEO_REMOVED: N_(
        "The final video of the cut %(cut)s is no longer among the episode's media."
    ),
    DeliveryWarningKind.NO_LINK: N_("The post of the cut %(cut)s on %(platform)s has no link."),
    DeliveryWarningKind.TOO_LONG: N_(
        "The video of the cut %(cut)s runs %(seconds)s s, over the %(limit)s s limit."
    ),
    DeliveryWarningKind.TOO_MANY_HASHTAGS: N_(
        "The cut %(cut)s has %(count)s hashtags, over the limit of %(limit)s."
    ),
    DeliveryWarningKind.NO_AI_NOTICE: N_("The caption of the cut %(cut)s has no AI notice line."),
    DeliveryWarningKind.POST_TOO_MANY_HASHTAGS: N_(
        "The post of the cut %(cut)s on %(platform)s goes out with %(count)s hashtags, over the"
        " limit of %(limit)s."
    ),
    DeliveryWarningKind.POST_NO_AI_NOTICE: N_(
        "The caption of the cut %(cut)s posted on %(platform)s has no AI notice line."
    ),
}


class DeliveryWarning(BaseModel):
    """A gap in the package, with the values its message names."""

    model_config = ConfigDict(frozen=True)

    kind: DeliveryWarningKind
    message: str
    values: dict[str, str] = Field(default_factory=dict)


def warning(kind: DeliveryWarningKind, **values: object) -> DeliveryWarning:
    """A warning with its English message filled in."""
    shown = {name: str(value) for name, value in values.items()}
    return DeliveryWarning(kind=kind, message=WARNING_MESSAGES[kind] % shown, values=shown)


def duration_of(cut: Cut, durations: Mapping[UUID, float | None]) -> float | None:
    """How long the cut's video runs, when known: measured if uploaded, declared if referenced."""
    if cut.external_video is not None:
        return cut.external_video.duration_seconds
    return None if cut.video_id is None else durations.get(cut.video_id)


def cut_warnings(
    cut: Cut, durations: Mapping[UUID, float | None], rules: DeliveryRules
) -> list[DeliveryWarning]:
    """The gaps of one cut: its video, and the channel rules for length, hashtags and notice."""
    found: list[DeliveryWarning] = []
    if cut.video_id is None and cut.external_video is None:
        found.append(warning(DeliveryWarningKind.NO_VIDEO, cut=cut.name))
    elif cut.video_id is not None and cut.video_id not in durations:
        found.append(warning(DeliveryWarningKind.VIDEO_REMOVED, cut=cut.name))
    seconds = duration_of(cut, durations)
    if seconds is not None and seconds > rules.max_seconds:
        found.append(
            warning(
                DeliveryWarningKind.TOO_LONG,
                cut=cut.name,
                seconds=f"{seconds:.1f}",
                limit=f"{rules.max_seconds:g}",
            )
        )
    if len(cut.hashtags) > rules.max_hashtags:
        found.append(
            warning(
                DeliveryWarningKind.TOO_MANY_HASHTAGS,
                cut=cut.name,
                count=len(cut.hashtags),
                limit=rules.max_hashtags,
            )
        )
    if rules.ai_notice_pattern and not re.search(rules.ai_notice_pattern, cut.caption):
        found.append(warning(DeliveryWarningKind.NO_AI_NOTICE, cut=cut.name))
    return found


def post_warnings(post: Post, cut: Cut, rules: DeliveryRules) -> list[DeliveryWarning]:
    """The gaps of one post: its link, and the text it goes out with, its own or its cut's."""
    found: list[DeliveryWarning] = []
    platform = post.platform.value
    if post.url is None:
        found.append(warning(DeliveryWarningKind.NO_LINK, cut=post.cut, platform=platform))
    text = posted_text(post, cut)
    if len(text.hashtags) > MAX_POST_HASHTAGS:
        found.append(
            warning(
                DeliveryWarningKind.POST_TOO_MANY_HASHTAGS,
                cut=post.cut,
                platform=platform,
                count=len(text.hashtags),
                limit=MAX_POST_HASHTAGS,
            )
        )
    if rules.ai_notice_pattern and not re.search(rules.ai_notice_pattern, text.caption):
        found.append(
            warning(DeliveryWarningKind.POST_NO_AI_NOTICE, cut=post.cut, platform=platform)
        )
    return found


def warnings_of(
    delivery: DeliveryInput, durations: Mapping[UUID, float | None], rules: DeliveryRules
) -> list[DeliveryWarning]:
    """Gaps in cuts and posts; `durations` maps each of the episode's videos to its length."""
    found = [item for cut in delivery.cuts for item in cut_warnings(cut, durations, rules)]
    cuts = {cut.name: cut for cut in delivery.cuts}
    for post in delivery.posts:
        found.extend(post_warnings(post, cuts[post.cut], rules))
    return found


class DeliveryFile(BaseModel):
    """A media file a cut uses, with the SHA-256 of its content, to tell masters apart."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: MediaKind
    name: str
    media_type: str
    size_bytes: int
    sha256: Sha256


class Delivery(DeliveryInput):
    """An episode's delivery package with its revision and the warnings it raises."""

    channel: ChannelSlug
    number: int
    revision: int
    updated_at: datetime | None = None
    warnings: list[DeliveryWarning] = Field(default_factory=list)
    files: list[DeliveryFile] = Field(
        default_factory=list,
        description="Read-only: the media files the cuts use as video or cover, by id, with "
        "their SHA-256; ignored on write.",
    )


def delivery_key(channel: str, number: int) -> TextKey:
    """Where an episode's delivery package is stored."""
    return TextKey(part=TextPart.DELIVERY, channel=channel, number=number)


class DeliveryStore:
    """Delivery packages as JSON in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.texts = EpisodeTexts(database)

    def delivery(self, channel: str, number: int) -> Delivery:
        """The episode's package, empty at revision 0 when nobody has written it; no warnings."""
        stored = self.texts.read(delivery_key(channel, number))
        content = DeliveryInput.model_validate_json(stored.text) if stored.text else DeliveryInput()
        return Delivery(
            **content.model_dump(),
            channel=channel,
            number=number,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )

    def write(
        self, channel: str, number: int, data: DeliveryInput, expected_revision: int | None
    ) -> Delivery:
        """Replace the package; with `expected_revision`, only if it is still at it."""
        key = delivery_key(channel, number)
        if self.texts.write(key, data.model_dump_json(), expected_revision) is None:
            current = self.texts.read(key).revision
            raise PartChanged(f"Delivery of {channel} #{number}", current)
        return self.delivery(channel, number)


def delivery_gaps(
    deliveries: DeliveryStore, media: MediaStore, rules: DeliveryRules, channel: str, number: int
) -> list[DeliveryWarning]:
    """What the episode's package lacks now; shown when the episode moves to published."""
    package = deliveries.delivery(channel, number)
    durations = {
        video.id: (found.duration_seconds if (found := media.measures_of(video.sha256)) else None)
        for video in media.of_family(channel, number, VIDEO_FAMILY)
    }
    return warnings_of(package, durations, rules)
