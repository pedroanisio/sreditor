"""Post metrics operations shared by the REST routes, the MCP tools and the pages (project.metrics).

Reading needs the read permission; entering numbers is content work, open to contributors and the
content:write scope. Numbers belong to a post, so the episode's delivery package must already
have a post on that platform. Nothing here calls a platform: people and agents bring the numbers.
"""

from __future__ import annotations

from datetime import date

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from sreditor.channels import Platform
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, decode_cursor, encode_cursor
from sreditor.post_metrics import (
    ChannelPost,
    Metric,
    Numbers,
    PostMetric,
    PostRef,
    Snapshot,
    SnapshotInput,
)
from sreditor.problems import ProblemError
from sreditor.retention_csv import MAX_CSV_CHARS, parse_retention_csv

INVALID_CURSOR = "Invalid cursor"


class PostMetricsOperation:
    """stmt operation names an agent's metrics writes are gated on."""

    WRITE = "post_metrics.write"


class PostNotRecorded(LookupError):
    """The episode's delivery package has no post on that platform to attach numbers to."""

    def __init__(self, platform: Platform) -> None:
        super().__init__(platform.value)
        self.platform = platform


class RetentionImport(BaseModel):
    """A platform's retention CSV and the day its numbers are from."""

    model_config = ConfigDict(frozen=True)

    csv: str = Field(
        max_length=MAX_CSV_CHARS,
        description="Two columns with a header: the second and the percent still watching.",
    )
    captured_on: date | None = Field(default=None, description="The day; today when left out.")


class PostQuery(BaseModel):
    """How to compare a channel's posts: by which number, and which page."""

    model_config = ConfigDict(frozen=True)

    sort_by: Metric = Metric.VIEWS
    cursor: str | None = None
    limit: int


def post_of(app: Services, access: ChannelAccess, number: int, platform: Platform) -> PostRef:
    """The episode's post on a platform; PostNotRecorded when its package has none."""
    slug = episode_access(app, access, number, Permission.READ)
    if all(post.platform != platform for post in app.deliveries.delivery(slug, number).posts):
        raise PostNotRecorded(platform)
    return PostRef(channel=slug, number=number, platform=platform)


def record_metrics(
    app: Services, access: ChannelAccess, number: int, platform: Platform, data: SnapshotInput
) -> Snapshot:
    """Add the day's numbers for a post, or merge them into that day's snapshot."""
    access.require(Permission.WRITE_CONTENT)
    post = post_of(app, access, number, platform)
    day = data.captured_on or app.post_metrics.today()
    return gated(
        app,
        access,
        PostMetricsOperation.WRITE,
        lambda: app.post_metrics.record(post, day, data),
    )


def import_retention(
    app: Services, access: ChannelAccess, number: int, platform: Platform, source: RetentionImport
) -> Snapshot:
    """Read a retention CSV (RetentionCsvError when it is not one) into the day's snapshot."""
    access.require(Permission.WRITE_CONTENT)
    points = parse_retention_csv(source.csv)
    data = SnapshotInput(captured_on=source.captured_on, retention=points)
    return record_metrics(app, access, number, platform, data)


def day_after(cursor: str | None) -> date | None:
    """The day a snapshot cursor points past."""
    position = decode_cursor(cursor)
    if position is None:
        return None
    try:
        return date.fromisoformat(position)
    except ValueError as invalid:
        raise ProblemError(
            status.HTTP_400_BAD_REQUEST, INVALID_CURSOR, "invalid-cursor"
        ) from invalid


def list_snapshots(
    app: Services,
    access: ChannelAccess,
    post: PostRef,
    cursor: str | None,
    limit: int,
) -> Page[Snapshot]:
    """The post's snapshots, newest day first."""
    access.require(Permission.READ)
    rows = app.post_metrics.snapshots(post, day_after(cursor), limit + 1)
    items = rows[:limit]
    more = len(rows) > limit
    return Page[Snapshot](
        items=items,
        next_cursor=encode_cursor(items[-1].captured_on.isoformat()) if more else None,
    )


def row_of(
    item: ChannelPost,
    latest: dict[tuple[int, Platform], Snapshot],
    counts: dict[tuple[int, Platform], int],
) -> PostMetric:
    """A post with its latest snapshot's numbers, or none when nobody entered any."""
    key = (item.number, item.post.platform)
    found = latest.get(key)
    return PostMetric(
        number=item.number,
        episode_title=item.title,
        platform=item.post.platform,
        cut=item.post.cut,
        url=str(item.post.url) if item.post.url else None,
        published_at=item.post.published_at,
        captured_on=found.captured_on if found else None,
        snapshots=counts.get(key, 0),
        retention_points=len(found.retention) if found else 0,
        latest=Numbers.model_validate(found.model_dump()) if found else None,
    )


def value_of(row: PostMetric, metric: Metric) -> float | None:
    """The number a post is compared by; None when it has no snapshot or left it out."""
    return None if row.latest is None else getattr(row.latest, metric.value)


def ranked(rows: list[PostMetric], metric: Metric) -> list[PostMetric]:
    """Posts from the highest number down; posts without it come last, in episode order."""

    def order(row: PostMetric) -> tuple[bool, float, int, str]:
        value = value_of(row, metric)
        return (value is None, -(value or 0), row.number, row.platform.value)

    return sorted(rows, key=order)


def offset_of(cursor: str | None) -> int:
    """The position in the ranking a cursor points to; 0 for the first page."""
    position = decode_cursor(cursor)
    if position is None:
        return 0
    if not position.isdigit():
        raise ProblemError(status.HTTP_400_BAD_REQUEST, INVALID_CURSOR, "invalid-cursor")
    return int(position)


def compare_posts(app: Services, access: ChannelAccess, query: PostQuery) -> Page[PostMetric]:
    """The channel's posts with their latest numbers, from the best by the chosen number."""
    access.require(Permission.READ)
    slug = access.channel.slug
    latest = app.post_metrics.latest(slug)
    counts = app.post_metrics.counts(slug)
    rows = [row_of(item, latest, counts) for item in app.post_metrics.channel_posts(slug)]
    ordered = ranked(rows, query.sort_by)
    start = offset_of(query.cursor)
    items = ordered[start : start + query.limit]
    end = start + query.limit
    return Page[PostMetric](
        items=items, next_cursor=encode_cursor(str(end)) if end < len(ordered) else None
    )
