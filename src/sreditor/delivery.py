"""An episode's delivery package: its cuts and where each was posted (rule project.delivery).

A cut is one version of the episode (main, vertical, teaser...) with its final video and cover,
caption, alt text and hashtags; a post says which cut went to which platform account of the
channel, with the post's link and publish time. The whole package is stored and revised on its
own, with its own revision for the ETag. Missing pieces only warn, never block a status.
"""

from __future__ import annotations

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

from sreditor.channels import SLUG_PATTERN, Platform
from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.i18n import N_
from sreditor.media import MediaStore

CAPTION_LENGTH = 1000
ALT_TEXT_LENGTH = 1000
CUT_NAME_LENGTH = 40
HASHTAG_LENGTH = 100
MAX_CUTS = 10
MAX_HASHTAGS = 30
HASHTAG_SIGIL = "#"
VIDEO_FAMILY = "video"  # the media-type family of a cut's final video
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


class Cut(BaseModel):
    """One version of the episode, with the files and texts it is posted with."""

    model_config = ConfigDict(frozen=True)

    name: CutName = Field(
        description="A slug such as main, vertical or teaser; unique per episode."
    )
    video_id: UUID | None = Field(
        default=None, description="The id of a video media file of this episode (list_media)."
    )
    cover_id: UUID | None = Field(
        default=None, description="The id of an image media file of this episode (kind cover)."
    )
    caption: Caption = ""
    alt_text: AltText = ""
    hashtags: list[Hashtag] = Field(default_factory=list, max_length=MAX_HASHTAGS)

    @model_validator(mode="after")
    def has_distinct_hashtags(self) -> Self:
        """Refuse the same hashtag twice, ignoring case, since platforms treat them as one."""
        folded = [tag.casefold() for tag in self.hashtags]
        if len(set(folded)) != len(folded):
            raise ValueError(N_("Each hashtag may appear once, whatever its case"))
        return self


class Post(BaseModel):
    """What went to one platform account of the channel."""

    model_config = ConfigDict(frozen=True)

    platform: Platform = Field(description="The platform of one of the channel's accounts.")
    cut: CutName = Field(description="The name of the cut that was posted.")
    url: HttpUrl | None = Field(default=None, description="The post's link, http or https.")
    published_at: Annotated[datetime, AfterValidator(in_utc)] | None = None


class DeliveryInput(BaseModel):
    """The whole delivery package, as written."""

    model_config = ConfigDict(frozen=True)

    cuts: list[Cut] = Field(default_factory=list, max_length=MAX_CUTS)
    posts: list[Post] = Field(default_factory=list, max_length=len(Platform))

    @model_validator(mode="after")
    def is_consistent(self) -> Self:
        """Cut names are unique, each account has one post and each post names an existing cut."""
        names = [cut.name for cut in self.cuts]
        if len(set(names)) != len(names):
            raise ValueError(N_("Each cut needs its own name"))
        platforms = [post.platform for post in self.posts]
        if len(set(platforms)) != len(platforms):
            raise ValueError(N_("Each platform account has at most one post"))
        unknown = sorted({post.cut for post in self.posts} - set(names))
        if unknown:
            raise ValueError(N_("A post names a cut that is not in this package"))
        return self


class WarningKind(StrEnum):
    """What makes a delivery package incomplete without making it unusable."""

    NO_VIDEO = "no_video"
    VIDEO_REMOVED = "video_removed"
    NO_LINK = "no_link"


WARNING_MESSAGES: dict[WarningKind, str] = {
    WarningKind.NO_VIDEO: N_("The cut %(cut)s has no final video."),
    WarningKind.VIDEO_REMOVED: N_(
        "The final video of the cut %(cut)s is no longer among the episode's media."
    ),
    WarningKind.NO_LINK: N_("The post on %(platform)s has no link."),
}


class DeliveryWarning(BaseModel):
    """A gap in the package, with the values its message names."""

    model_config = ConfigDict(frozen=True)

    kind: WarningKind
    message: str
    values: dict[str, str] = Field(default_factory=dict)


def warning(kind: WarningKind, **values: object) -> DeliveryWarning:
    """A warning with its English message filled in."""
    shown = {name: str(value) for name, value in values.items()}
    return DeliveryWarning(kind=kind, message=WARNING_MESSAGES[kind] % shown, values=shown)


def warnings_of(delivery: DeliveryInput, video_ids: set[UUID]) -> list[DeliveryWarning]:
    """Cuts without a usable video and posts without a link; `video_ids` are the episode's videos."""
    found: list[DeliveryWarning] = []
    for cut in delivery.cuts:
        if cut.video_id is None:
            found.append(warning(WarningKind.NO_VIDEO, cut=cut.name))
        elif cut.video_id not in video_ids:
            found.append(warning(WarningKind.VIDEO_REMOVED, cut=cut.name))
    found.extend(
        warning(WarningKind.NO_LINK, platform=post.platform.value)
        for post in delivery.posts
        if post.url is None
    )
    return found


class Delivery(DeliveryInput):
    """An episode's delivery package with its revision and the warnings it raises."""

    channel: str
    number: int
    revision: int
    updated_at: datetime | None = None
    warnings: list[DeliveryWarning] = Field(default_factory=list)


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
    deliveries: DeliveryStore, media: MediaStore, channel: str, number: int
) -> list[DeliveryWarning]:
    """What the episode's package lacks now; shown when the episode moves to published."""
    package = deliveries.delivery(channel, number)
    videos = {video.id for video in media.of_family(channel, number, VIDEO_FAMILY)}
    return warnings_of(package, videos)
