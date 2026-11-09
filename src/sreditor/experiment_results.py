"""The computed result of an A/B experiment: each side's numbers, their difference and ratio.

Never stored (rule project.experiments): it is derived from the numbers people entered, with
what_worked's snapshot selection, so the two readings of a post agree. Pure functions and models.
"""

from __future__ import annotations

import statistics
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sreditor.channels import Platform
from sreditor.experiments import Experiment
from sreditor.post_metrics import Metric
from sreditor.what_worked import ROUND_DIGITS, SnapshotAge


class ResultNote(StrEnum):
    """Why a result is incomplete; each is one thing the person can fix by entering numbers."""

    A_WITHOUT_POSTS = "a_without_posts"
    B_WITHOUT_POSTS = "b_without_posts"
    A_WITHOUT_NUMBERS = "a_without_numbers"
    B_WITHOUT_NUMBERS = "b_without_numbers"
    A_IS_ZERO = "a_is_zero"


class PostValue(BaseModel):
    """One post of a side: where it went live and its number at the chosen age, if any."""

    model_config = ConfigDict(frozen=True)

    platform: Platform
    published_at: str | None = Field(description="When it went live (ISO 8601), if recorded.")
    captured_on: date | None = Field(description="The day of the snapshot that was read.")
    value: float | None = Field(description="The metric at the chosen age; null without one.")


class Side(BaseModel):
    """The posts of one episode of the pair and the mean of their numbers."""

    model_config = ConfigDict(frozen=True)

    episode: int
    title: str
    posts: list[PostValue]
    value: float | None = Field(description="Mean of the posts' numbers; null when none has one.")


class ExperimentResult(BaseModel):
    """What the numbers say about the pair: B against A, never a verdict."""

    model_config = ConfigDict(frozen=True)

    metric: Metric
    snapshot_age: SnapshotAge
    platform: Platform | None
    a: Side
    b: Side
    difference: float | None = Field(description="B minus A; null while a side has no number.")
    ratio: float | None = Field(description="B divided by A; null while undefined.")
    notes: list[ResultNote] = Field(description="What is missing, when the result is incomplete.")


def side_of(episode: int, title: str, posts: list[PostValue]) -> Side:
    """A side's posts with the mean of those that have a number."""
    figures = [post.value for post in posts if post.value is not None]
    mean = round(statistics.fmean(figures), ROUND_DIGITS) if figures else None
    return Side(episode=episode, title=title, posts=posts, value=mean)


def notes_of(a: Side, b: Side) -> list[ResultNote]:
    """What is missing on each side, in a fixed order."""
    notes = []
    if not a.posts:
        notes.append(ResultNote.A_WITHOUT_POSTS)
    elif a.value is None:
        notes.append(ResultNote.A_WITHOUT_NUMBERS)
    if not b.posts:
        notes.append(ResultNote.B_WITHOUT_POSTS)
    elif b.value is None:
        notes.append(ResultNote.B_WITHOUT_NUMBERS)
    if a.value == 0 and b.value is not None:
        notes.append(ResultNote.A_IS_ZERO)
    return notes


def result_of(experiment: Experiment, a: Side, b: Side) -> ExperimentResult:
    """The two sides compared; difference and ratio wait until both sides have a number."""
    difference = ratio = None
    if a.value is not None and b.value is not None:
        difference = round(b.value - a.value, ROUND_DIGITS)
        if a.value:
            ratio = round(b.value / a.value, ROUND_DIGITS)
    return ExperimentResult(
        metric=experiment.metric,
        snapshot_age=experiment.snapshot_age,
        platform=experiment.platform,
        a=a,
        b=b,
        difference=difference,
        ratio=ratio,
        notes=notes_of(a, b),
    )
