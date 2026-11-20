"""Experiment operations shared by the REST routes, the MCP tools and the pages (rule project.experiments).

Reading needs the read permission. Writing, and deleting, belong to owners and editors with the
editorial:write scope: an experiment is an editorial judgement about which choice worked. Reading
the result only reads the numbers people entered, with what_worked's snapshot selection.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.delivery import post_key
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number
from sreditor.experiment_results import (
    ExperimentResult,
    PostValue,
    Side,
    result_of,
    side_of,
)
from sreditor.experiments import (
    Experiment,
    ExperimentInput,
    ExperimentStatus,
)
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.post_metrics import ChannelPost, PostKey, Snapshot
from sreditor.what_worked import number_of, snapshot_for


class ExperimentOperation:
    """stmt operation names an agent's experiment writes are gated on."""

    CREATE = "experiment.create"
    WRITE = "experiment.write"
    DELETE = "experiment.delete"


class ExperimentChange(BaseModel):
    """Which experiment a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    experiment_id: UUID
    expected_revision: int | None = None


class ExperimentQuery(BaseModel):
    """Which page of a channel's experiments to list, optionally of one status."""

    model_config = ConfigDict(frozen=True)

    status: ExperimentStatus | None = None
    cursor: str | None = None
    limit: int


class ExperimentReading(BaseModel):
    """An experiment together with its computed result, as the page and the MCP tool show them."""

    model_config = ConfigDict(frozen=True)

    experiment: Experiment
    result: ExperimentResult


def experiment_access(
    app: Services, access: ChannelAccess, experiment_id: UUID, permission: Permission
) -> Experiment:
    """The channel's experiment once the permission is checked; ExperimentNotFound otherwise."""
    access.require(permission)
    return app.experiments.get(access.channel.slug, experiment_id)


def check_episodes(app: Services, access: ChannelAccess, data: ExperimentInput) -> None:
    """Both episodes of the pair must be episodes of the channel (EpisodeNotFound otherwise)."""
    for number in (data.episode_a, data.episode_b):
        app.episodes.get(access.channel.slug, number)


def list_experiments(
    app: Services, access: ChannelAccess, query: ExperimentQuery
) -> Page[Experiment]:
    """The channel's experiments in the order they were added."""
    access.require(Permission.READ)
    rows = app.experiments.list(
        access.channel.slug, after_number(query.cursor), query.limit + 1, query.status
    )
    items = rows[: query.limit]
    cursor = None
    if len(rows) > query.limit:
        cursor = encode_cursor(str(app.experiments.position(items[-1].id)))
    return Page[Experiment](items=items, next_cursor=cursor)


def get_experiment(app: Services, access: ChannelAccess, experiment_id: UUID) -> Experiment:
    """One experiment, with the revision to send back when changing it."""
    return experiment_access(app, access, experiment_id, Permission.READ)


def create_experiment(app: Services, access: ChannelAccess, data: ExperimentInput) -> Experiment:
    """Add an experiment linking two episodes of the channel."""
    access.require(Permission.WRITE_EDITORIAL)
    check_episodes(app, access, data)
    slug = access.channel.slug
    return gated(
        app, access, ExperimentOperation.CREATE, lambda: app.experiments.create(slug, data)
    )


def replace_experiment(
    app: Services, access: ChannelAccess, change: ExperimentChange, data: ExperimentInput
) -> Experiment:
    """Replace everything an experiment holds, its reading included."""
    current = experiment_access(app, access, change.experiment_id, Permission.WRITE_EDITORIAL)
    check_episodes(app, access, data)
    return gated(
        app,
        access,
        ExperimentOperation.WRITE,
        lambda: app.experiments.replace(current, data, change.expected_revision),
    )


def delete_experiment(app: Services, access: ChannelAccess, change: ExperimentChange) -> None:
    """Delete an experiment; the episodes it compared are untouched."""
    current = experiment_access(app, access, change.experiment_id, Permission.WRITE_EDITORIAL)
    gated(
        app,
        access,
        ExperimentOperation.DELETE,
        lambda: app.experiments.delete(current, change.expected_revision),
    )


def post_value(
    item: ChannelPost, history: dict[PostKey, list[Snapshot]], experiment: Experiment
) -> PostValue:
    """A post's number at the experiment's age, picked the way what_worked picks it."""
    published = item.post.published_at
    found = snapshot_for(
        history.get((item.number, *post_key(item.post)), []),
        published.date() if published else None,
        experiment.snapshot_age,
    )
    return PostValue(
        platform=item.post.platform,
        cut=item.post.cut,
        published_at=published.isoformat() if published else None,
        captured_on=found.captured_on if found else None,
        value=number_of(found, experiment.metric),
    )


def sides_of(app: Services, experiment: Experiment) -> tuple[Side, Side]:
    """Both episodes' posts, on the experiment's platform if it names one, with their numbers."""
    slug = experiment.channel
    history = app.post_metrics.history(slug)
    posts = [
        item
        for item in app.post_metrics.channel_posts(slug)
        if experiment.platform is None or item.post.platform == experiment.platform
    ]

    def side(number: int) -> Side:
        values = [post_value(item, history, experiment) for item in posts if item.number == number]
        return side_of(number, app.episodes.get(slug, number).episode.title, values)

    return side(experiment.episode_a), side(experiment.episode_b)


def experiment_result(
    app: Services, access: ChannelAccess, experiment_id: UUID
) -> ExperimentReading:
    """The experiment with what the numbers say about its pair, computed now."""
    experiment = experiment_access(app, access, experiment_id, Permission.READ)
    a, b = sides_of(app, experiment)
    return ExperimentReading(experiment=experiment, result=result_of(experiment, a, b))
