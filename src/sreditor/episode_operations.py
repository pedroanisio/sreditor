"""Episode operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Creating and editing an episode is content work, open to contributors and the content:write
scope; deleting one loses its history, so it needs a role that may change the channel itself.
"""

from __future__ import annotations

from fastapi import status

from sreditor.claims import warning_text as claims_warning
from sreditor.delivery import delivery_gaps
from sreditor.deps import ChannelAccess, Services
from sreditor.episodes import Episode, EpisodeInput, EpisodeStatus, PartCounts, RevisedEpisode
from sreditor.experiments import EpisodeInExperiment
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import INVALID_CURSOR, Page, decode_cursor, encode_cursor
from sreditor.problems import ProblemError
from sreditor.reviews import warning_text as review_warning


class EpisodeOperation:
    """stmt operation names an agent's episode writes are gated on."""

    CREATE = "episode.create"
    WRITE = "episode.write"
    DELETE = "episode.delete"


def after_number(cursor: str | None) -> int | None:
    """The episode number a cursor points past."""
    position = decode_cursor(cursor)
    if position is None:
        return None
    if not position.isdigit():
        raise ProblemError(status.HTTP_400_BAD_REQUEST, INVALID_CURSOR, "invalid-cursor")
    return int(position)


def episode_access(
    app: Services, access: ChannelAccess, number: int, permission: Permission
) -> str:
    """The channel's slug once the permission is checked and the episode is known to exist."""
    access.require(permission)
    app.episodes.get(access.channel.slug, number)
    access.episode_number = number  # for the change feed, if a write follows
    return access.channel.slug


def list_episodes(
    app: Services, access: ChannelAccess, cursor: str | None, limit: int
) -> Page[Episode]:
    """The channel's episodes by number."""
    access.require(Permission.READ)
    rows = app.episodes.list(access.channel.slug, after_number(cursor), limit + 1)
    items = rows[:limit]
    has_more = len(rows) > limit
    return Page[Episode](
        items=items, next_cursor=encode_cursor(str(items[-1].number)) if has_more else None
    )


def get_episode(app: Services, access: ChannelAccess, number: int) -> RevisedEpisode:
    """One episode with the revision to send back when changing it."""
    access.require(Permission.READ)
    return app.episodes.get(access.channel.slug, number)


def count_episode_parts(app: Services, access: ChannelAccess, number: int) -> PartCounts:
    """How many sources, media files and waivers the episode has; waivers only for their readers."""
    access.require(Permission.READ)
    counts = app.episodes.part_counts(access.channel.slug, number)
    if access.can(Permission.READ_WAIVERS):
        return counts
    return counts.model_copy(update={"waivers": 0})


def create_episode(app: Services, access: ChannelAccess, data: EpisodeInput) -> RevisedEpisode:
    """Add an episode with the channel's next number."""
    access.require(Permission.WRITE_CONTENT)
    slug = access.channel.slug

    def create() -> RevisedEpisode:
        created = app.episodes.create(slug, data)
        access.episode_number = created.episode.number
        return created

    return gated(app, access, EpisodeOperation.CREATE, create)


def replace_episode(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: EpisodeInput,
    expected_revision: int | None,
) -> RevisedEpisode:
    """Replace an episode's title, summary and status.

    Moving to published is never refused for unchecked claims or a missing approval (rules
    project.claims and project.reviews); the result carries a warning for each.
    """
    access.require(Permission.WRITE_CONTENT)
    slug = access.channel.slug
    access.episode_number = number
    revised = gated(
        app,
        access,
        EpisodeOperation.WRITE,
        lambda: app.episodes.replace(slug, number, data, expected_revision),
    )
    if data.status is not EpisodeStatus.PUBLISHED:
        return revised
    # Warn, never refuse: no delivery gap, unchecked claim or missing approval holds back a
    # status (rules project.delivery, project.claims and project.reviews).
    gaps = delivery_gaps(app.deliveries, app.media, app.settings.delivery, slug, number)
    return revised.model_copy(
        update={
            "warnings": gaps,
            "claims_warning": claims_warning(app.claims.unchecked(slug, number).count),
            "review_warning": review_warning(app.reviews.summary(slug, number).latest),
        }
    )


def refuse_while_compared(app: Services, slug: str, number: int) -> None:
    """Refuse to delete an episode some experiment compares, naming those experiments."""
    experiments = app.experiments.of_episode(slug, number)
    if experiments:
        raise EpisodeInExperiment(number, experiments)


def delete_episode(
    app: Services, access: ChannelAccess, number: int, expected_revision: int | None
) -> None:
    """Delete an episode; its number is never given to another one."""
    access.require(Permission.WRITE_CHANNEL)
    slug = access.channel.slug
    refuse_while_compared(app, slug, number)
    access.episode_number = number
    gated(
        app,
        access,
        EpisodeOperation.DELETE,
        lambda: app.episodes.delete(slug, number, expected_revision),
    )


def move_episode(
    app: Services, access: ChannelAccess, number: int, stage: EpisodeStatus
) -> RevisedEpisode:
    """Move an episode to another production stage, keeping its title and summary.

    The board shows no revisions, so the move checks against the revision it just read: a
    change made in between is never overwritten, it fails as EpisodeChanged instead.
    """
    current = get_episode(app, access, number)
    episode = current.episode
    data = EpisodeInput(title=episode.title, summary=episode.summary, status=stage)
    return replace_episode(app, access, number, data, current.revision)
