"""What worked: a channel's posts grouped by classification, hook or length (project.metrics.what-worked).

Pure functions and models. The numbers are the ones people entered by hand (post_metrics.py); a
group's median and mean come from one snapshot of each post, so a post counts once however many
days of numbers it has. Groups with few posts are flagged rather than hidden, because a median of
two posts says little but is still what the channel has.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sreditor.channels import Platform
from sreditor.classification import Dimension
from sreditor.post_metrics import Metric, Snapshot
from sreditor.short_script import HOOK_SECONDS

MIN_GROUP_POSTS = 3  # under this a group is too small to read
ROUND_DIGITS = 2
AGE_TOLERANCE_DAYS = 2  # a fixed-age snapshot may be this many days off the exact age
FAST_HOOK_SECONDS = 2.0

# Band names are what people and agents read in a group's key; the last one has no upper limit.
HOOK_BANDS: tuple[tuple[str, float], ...] = (
    ("up_to_2s", FAST_HOOK_SECONDS),
    ("2_to_3s", HOOK_SECONDS),
    ("over_3s", math.inf),
)
VIDEO_BANDS: tuple[tuple[str, float], ...] = (
    ("up_to_15s", 15.0),
    ("15_to_30s", 30.0),
    ("30_to_60s", 60.0),
    ("over_60s", math.inf),
)


class GroupBy(StrEnum):
    """What posts are grouped by."""

    CLASSIFICATION = "classification"
    HOOK_REACTION = "hook_reaction"
    HOOK_LENGTH = "hook_length"
    VIDEO_LENGTH = "video_length"


class SnapshotAge(StrEnum):
    """Which snapshot of a post is read: its latest, or the one at a fixed age after publishing."""

    LATEST = "latest"
    DAY_1 = "day_1"
    DAY_3 = "day_3"
    DAY_7 = "day_7"
    DAY_14 = "day_14"
    DAY_30 = "day_30"


AGE_DAYS: dict[SnapshotAge, int] = {
    SnapshotAge.DAY_1: 1,
    SnapshotAge.DAY_3: 3,
    SnapshotAge.DAY_7: 7,
    SnapshotAge.DAY_14: 14,
    SnapshotAge.DAY_30: 30,
}


class WhatWorkedQuery(BaseModel):
    """What to compare: the grouping, the number, the snapshot and which posts take part."""

    model_config = ConfigDict(frozen=True)

    group_by: GroupBy = Field(
        description="classification (needs classification_dimension), hook_reaction, hook_length "
        "or video_length."
    )
    classification_dimension: Dimension | None = Field(
        default=None, description="With group_by classification: the dimension to group by."
    )
    metric: Metric = Field(default=Metric.VIEWS, description="The number to summarise.")
    age: SnapshotAge = Field(
        default=SnapshotAge.LATEST,
        description="latest, or the snapshot nearest that many days after publishing "
        f"(within {AGE_TOLERANCE_DAYS} days); a younger post has none and is left out.",
    )
    platform: Platform | None = Field(default=None, description="Only posts on this platform.")
    published_from: date | None = Field(default=None, description="First publish day (UTC).")
    published_to: date | None = Field(default=None, description="Last publish day (UTC).")


class Entry(BaseModel):
    """One post ready to be grouped: its group (None when it has none) and its number."""

    model_config = ConfigDict(frozen=True)

    number: int
    key: str | None
    is_proposal: bool = False
    value: float | None = None


class Group(BaseModel):
    """Posts that share a value, with the median and mean of the chosen number."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The shared value: a vocabulary value, hook reaction or band.")
    posts: int
    episodes: list[int] = Field(description="Numbers of the episodes the posts belong to.")
    median: float
    mean: float
    is_too_small: bool = Field(description="True when the group has fewer than 3 posts.")


class Excluded(BaseModel):
    """Posts of the filter that no group holds, and why; each post is counted once."""

    model_config = ConfigDict(frozen=True)

    without_publish_time: int = Field(description="Posts with no publish time in the package.")
    proposals: int = Field(description="Posts of episodes whose classification nobody reviewed.")
    without_value: int = Field(description="Posts with no value in the dimension (not classified).")
    without_numbers: int = Field(
        description="Posts with no snapshot at that age or no such number."
    )


class WhatWorked(BaseModel):
    """The groups of a channel's posts with their numbers, and what was left out."""

    model_config = ConfigDict(frozen=True)

    group_by: GroupBy
    classification_dimension: Dimension | None
    metric: Metric
    age: SnapshotAge
    min_group_posts: int = MIN_GROUP_POSTS
    groups: list[Group]
    excluded: Excluded


def band_of(seconds: float, bands: tuple[tuple[str, float], ...]) -> str:
    """The name of the first band whose upper limit holds the length."""
    return next(name for name, limit in bands if seconds <= limit)


def snapshot_for(history: list[Snapshot], published_on: date, age: SnapshotAge) -> Snapshot | None:
    """A post's latest snapshot, or the one nearest `age` after publishing within the tolerance.

    `history` is oldest first. A post younger than the age has none, so it never skews a group.
    """
    if age is SnapshotAge.LATEST:
        return history[-1] if history else None
    target = published_on + timedelta(days=AGE_DAYS[age])
    near = [
        found for found in history if abs((found.captured_on - target).days) <= AGE_TOLERANCE_DAYS
    ]
    return min(near, key=lambda found: abs((found.captured_on - target).days), default=None)


def number_of(snapshot: Snapshot | None, metric: Metric) -> float | None:
    """The chosen number of a snapshot; None when there is no snapshot or it left it out."""
    return None if snapshot is None else getattr(snapshot, metric.value)


def group_of(key: str, values: list[tuple[int, float]]) -> Group:
    """A group's posts as (episode number, value) pairs, summarised."""
    figures = [value for _, value in values]
    return Group(
        key=key,
        posts=len(values),
        episodes=sorted({number for number, _ in values}),
        median=round(statistics.median(figures), ROUND_DIGITS),
        mean=round(statistics.fmean(figures), ROUND_DIGITS),
        is_too_small=len(values) < MIN_GROUP_POSTS,
    )


def summarise(
    entries: list[Entry], query: WhatWorkedQuery, without_publish_time: int
) -> WhatWorked:
    """Group the entries, best median first; every entry lands in a group or one exclusion."""
    grouped: dict[str, list[tuple[int, float]]] = defaultdict(list)
    proposals = without_value = without_numbers = 0
    for entry in entries:
        if entry.is_proposal:
            proposals += 1
        elif entry.key is None:
            without_value += 1
        elif entry.value is None:
            without_numbers += 1
        else:
            grouped[entry.key].append((entry.number, entry.value))
    groups = sorted(
        (group_of(key, values) for key, values in grouped.items()),
        key=lambda group: (-group.median, group.key),
    )
    return WhatWorked(
        group_by=query.group_by,
        classification_dimension=query.classification_dimension,
        metric=query.metric,
        age=query.age,
        groups=groups,
        excluded=Excluded(
            without_publish_time=without_publish_time,
            proposals=proposals,
            without_value=without_value,
            without_numbers=without_numbers,
        ),
    )
