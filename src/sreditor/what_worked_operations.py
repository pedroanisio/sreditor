"""What worked: the operation shared by the REST route, the MCP tool and the page (project.metrics.what-worked).

Reading needs the read permission. It only reads: the classification, the short script, the
delivery package and the numbers people entered, and never writes or fetches anything.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import status

from sreditor.classification import Dimension
from sreditor.delivery import VIDEO_FAMILY, duration_of, post_key
from sreditor.deps import ChannelAccess, Services
from sreditor.members import Permission
from sreditor.post_metrics import ChannelPost
from sreditor.problems import ProblemError
from sreditor.short_script import Block, BlockFunction
from sreditor.what_worked import (
    HOOK_BANDS,
    VIDEO_BANDS,
    Entry,
    GroupBy,
    WhatWorked,
    WhatWorkedQuery,
    band_of,
    number_of,
    snapshot_for,
    summarise,
)

INVALID_QUERY = "invalid-what-worked-query"

# A keyer names the post's group, and says whether its classification is only a proposal.
Keyer = Callable[[ChannelPost], tuple[str | None, bool]]


def refuse(detail: str) -> ProblemError:
    """An unprocessable query, with what to change."""
    return ProblemError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail, INVALID_QUERY)


def check_query(query: WhatWorkedQuery) -> None:
    """Refuse a query whose grouping or dates make no sense; the detail says what to change."""
    is_classification = query.group_by is GroupBy.CLASSIFICATION
    if is_classification and query.classification_dimension is None:
        allowed = ", ".join(dimension.value for dimension in Dimension)
        raise refuse(
            f"group_by classification needs classification_dimension, one of {allowed}; "
            "get_classification_vocabulary lists their values"
        )
    if not is_classification and query.classification_dimension is not None:
        raise refuse("classification_dimension only applies when group_by is classification")
    if query.published_from and query.published_to and query.published_from > query.published_to:
        raise refuse("published_from must not be after published_to")


def classification_keyer(app: Services, slug: str, dimension: Dimension) -> Keyer:
    """Group by a dimension's primary value; an agent's proposal is set apart until reviewed."""
    classified = {found.number: found for found in app.classifications.classified(slug)}

    def key_of(item: ChannelPost) -> tuple[str | None, bool]:
        found = classified.get(item.number)
        if found is None:
            return None, False
        if not found.is_reviewed:
            return None, True
        return found.classification.primary(dimension), False

    return key_of


def hook_of(app: Services, slug: str, number: int) -> Block | None:
    """The first hook block of the episode's short script, if it has one."""
    script = app.short_scripts.short_script(slug, number)
    return next((block for block in script.blocks if block.function is BlockFunction.HOOK), None)


def hook_keyer(app: Services, slug: str, group_by: GroupBy) -> Keyer:
    """Group by the reaction the hook aims for, or by how long the hook runs."""

    def key_of(item: ChannelPost) -> tuple[str | None, bool]:
        hook = hook_of(app, slug, item.number)
        if hook is None:
            return None, False
        if group_by is GroupBy.HOOK_REACTION:
            return (hook.impact.reaction.value if hook.impact else None), False
        return band_of(hook.end_s - hook.start_s, HOOK_BANDS), False

    return key_of


def video_seconds(app: Services, slug: str, item: ChannelPost) -> float | None:
    """How long the posted cut's video runs, from its stored measures or declared length."""
    cut = next(
        (
            found
            for found in app.deliveries.delivery(slug, item.number).cuts
            if found.name == item.post.cut
        ),
        None,
    )
    if cut is None:
        return None
    durations = {
        video.id: (
            found.duration_seconds if (found := app.media.measures_of(video.sha256)) else None
        )
        for video in app.media.of_family(slug, item.number, VIDEO_FAMILY)
    }
    return duration_of(cut, durations)


def length_keyer(app: Services, slug: str) -> Keyer:
    """Group by the length band of the video that was posted."""

    def key_of(item: ChannelPost) -> tuple[str | None, bool]:
        seconds = video_seconds(app, slug, item)
        return (None if seconds is None else band_of(seconds, VIDEO_BANDS)), False

    return key_of


def keyer_for(app: Services, slug: str, query: WhatWorkedQuery) -> Keyer:
    """The way of naming a post's group for the query's dimension."""
    if query.group_by is GroupBy.CLASSIFICATION and query.classification_dimension:
        return classification_keyer(app, slug, query.classification_dimension)
    if query.group_by is GroupBy.VIDEO_LENGTH:
        return length_keyer(app, slug)
    return hook_keyer(app, slug, query.group_by)


def is_in_dates(published_at: datetime, query: WhatWorkedQuery) -> bool:
    """Whether the post went live inside the query's dates, both ends included."""
    day = published_at.date()
    return (query.published_from is None or day >= query.published_from) and (
        query.published_to is None or day <= query.published_to
    )


def entries_of(
    app: Services, slug: str, query: WhatWorkedQuery, posts: list[tuple[ChannelPost, datetime]]
) -> list[Entry]:
    """Each post with its group and its number at the query's age."""
    key_of = keyer_for(app, slug, query)
    history = app.post_metrics.history(slug)
    entries = []
    for item, published_at in posts:
        key, is_proposal = key_of(item)
        found = snapshot_for(
            history.get((item.number, *post_key(item.post)), []), published_at.date(), query.age
        )
        entries.append(
            Entry(
                number=item.number,
                key=key,
                is_proposal=is_proposal,
                value=number_of(found, query.metric),
            )
        )
    return entries


def what_worked(app: Services, access: ChannelAccess, query: WhatWorkedQuery) -> WhatWorked:
    """Group the channel's published posts and summarise one number per group."""
    access.require(Permission.READ)
    check_query(query)
    slug = access.channel.slug
    posts = [
        item
        for item in app.post_metrics.channel_posts(slug)
        if query.platform is None or item.post.platform == query.platform
    ]
    published = [
        (item, item.post.published_at) for item in posts if item.post.published_at is not None
    ]
    in_range = [(item, moment) for item, moment in published if is_in_dates(moment, query)]
    return summarise(entries_of(app, slug, query, in_range), query, len(posts) - len(published))
