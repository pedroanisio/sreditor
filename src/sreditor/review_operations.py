"""Review operations shared by the REST routes, the MCP tools and the pages (rule project.reviews).

Reading needs the read permission. Adding a review is editorial work, open to owners and editors
and the editorial:write scope. Reviews are append-only: there is no edit, and a review is
deleted only by a channel owner or by the person who wrote it. A review records its author, the
user, and the API token an agent acted through (rule agentic.audit).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access, get_episode
from sreditor.episodes import RevisedEpisode
from sreditor.media import MediaNotFound
from sreditor.media_operations import measured
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.reviews import NewReview, Review, ReviewAuthor, ReviewInput, ReviewSummary

# A reviewer's clock may run a little ahead of the server's.
CLOCK_SKEW = timedelta(minutes=5)
# A range may run this far past a measured duration, since durations are rounded.
RANGE_TOLERANCE_S = 0.5

NO_MEDIA = (
    "%(id)s is not a media file of this episode; list_media shows the episode's files, such as"
    " a render or a cut's final video."
)
PAST_THE_END = (
    "The range ends at %(end)s s, past the end of the file, which runs %(duration)s s;"
    " use times within the file."
)
IN_THE_FUTURE = (
    "A review cannot be dated in the future; leave reviewed_at out to use the current time."
)


class ReviewOperation:
    """stmt operation names an agent's review writes are gated on."""

    CREATE = "review.create"
    DELETE = "review.delete"


class ReviewRefused(ValueError):
    """A review that names a file the episode lacks, ranges past its end or a future time."""

    def __init__(
        self,
        location: tuple[str | int, ...],
        detail: str,
        values: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.location = location
        self.values = dict(values or {})


class ReviewQuery(BaseModel):
    """Which page of an episode's reviews to list."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str | None = None
    limit: int


class ReviewRef(BaseModel):
    """Which review of which episode a call targets."""

    model_config = ConfigDict(frozen=True)

    number: int
    review_id: UUID


def list_reviews(app: Services, access: ChannelAccess, query: ReviewQuery) -> Page[Review]:
    """The episode's reviews in the order they were added."""
    slug = episode_access(app, access, query.number, Permission.READ)
    rows = app.reviews.page(slug, query.number, after_number(query.cursor), query.limit + 1)
    items = rows[: query.limit]
    has_more = len(rows) > query.limit
    cursor = encode_cursor(str(app.reviews.position(items[-1].id))) if has_more else None
    return Page[Review](items=items, next_cursor=cursor)


def review_summary(app: Services, access: ChannelAccess, number: int) -> ReviewSummary:
    """How many reviews the episode has and the latest one."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.reviews.summary(slug, number)


def check_file(app: Services, slug: str, number: int, data: ReviewInput) -> None:
    """Refuse a file that is not the episode's, or ranges that run past its measured end."""
    if data.media_id is None:
        return
    try:
        media = measured(app, app.media.get(slug, number, data.media_id))
    except MediaNotFound as missing:
        raise ReviewRefused(("media_id",), NO_MEDIA % {"id": data.media_id}) from missing
    if media.measures is None:
        return
    limit = media.measures.duration_seconds + RANGE_TOLERANCE_S
    for index, seen in enumerate(data.seen):
        if seen.end_s > limit:
            values = {"end": f"{seen.end_s:g}", "duration": f"{media.measures.duration_seconds:g}"}
            raise ReviewRefused(("seen", index, "end_s"), PAST_THE_END % values, values)


def reviewed_when(app: Services, data: ReviewInput) -> datetime:
    """The time of the review: the one given, not in the future, else now."""
    now = app.reviews.moment()
    if data.reviewed_at is None:
        return now
    if data.reviewed_at > now + CLOCK_SKEW:
        raise ReviewRefused(("reviewed_at",), IN_THE_FUTURE)
    return data.reviewed_at


def add_review(app: Services, access: ChannelAccess, number: int, data: ReviewInput) -> Review:
    """Append a review of the episode, written by the user or the token an agent acts through."""
    slug = episode_access(app, access, number, Permission.WRITE_EDITORIAL)
    check_file(app, slug, number, data)
    token = access.token
    new = NewReview(
        channel=slug,
        number=number,
        author=ReviewAuthor(user_id=access.user.id, token_id=token.id if token else None),
        reviewed_at=reviewed_when(app, data),
        data=data,
    )
    return gated(app, access, ReviewOperation.CREATE, lambda: app.reviews.add(new))


def remove_review(app: Services, access: ChannelAccess, ref: ReviewRef) -> Review:
    """Delete a review; only a channel owner, or the reviewer themself, may. Returns it."""
    slug = episode_access(app, access, ref.number, Permission.READ)
    review = app.reviews.get(slug, ref.number, ref.review_id)
    if review.reviewer_id == access.user.id:
        access.require(Permission.WRITE_EDITORIAL)
    else:
        access.require(Permission.MANAGE_OWNERS)
    gated(app, access, ReviewOperation.DELETE, lambda: app.reviews.delete(review))
    return review


class ReadEpisode(RevisedEpisode):
    """An episode with its revision and what its reviews say: how many and the latest."""

    reviews: ReviewSummary


def read_episode(app: Services, access: ChannelAccess, number: int) -> ReadEpisode:
    """The episode as get_episode reads it, with its reviews' summary."""
    revised = get_episode(app, access, number)
    summary = app.reviews.summary(access.channel.slug, number)
    return ReadEpisode(**revised.model_dump(), reviews=summary)
