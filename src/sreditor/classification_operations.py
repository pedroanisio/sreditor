"""Classification operations shared by the REST routes, the MCP tools and the pages.

Reading needs the read permission. Classifying an episode is content work, open to contributors
and the content:write scope. The channel's declared profile says what the channel is, like its
Editorial, so it needs the Editorial permission. A classification written with an API token is
an agent's proposal until a person saves or confirms it (rule project.classification.checks).
"""

from __future__ import annotations

from sreditor.classification import (
    ChannelProfile,
    Classification,
    EpisodeClassification,
    ObservedProfile,
    StoredClassification,
    check,
    observed_profile,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.episode_texts import TextKey, TextPart
from sreditor.members import Permission
from sreditor.operations import gated


class ClassificationOperation:
    """stmt operation names an agent's classification writes are gated on."""

    WRITE = "classification.write"
    PROFILE_WRITE = "channel_profile.write"


def get_classification(app: Services, access: ChannelAccess, number: int) -> EpisodeClassification:
    """An episode's classification; all unknown at revision 0 when never written."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.classifications.episode(slug, number)


def write_classification(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: Classification,
    expected_revision: int | None,
) -> EpisodeClassification:
    """Replace an episode's classification; ClassificationRefused names what breaks the rules.

    A person's write is reviewed; an agent's is a proposal.
    """
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    check(data)
    stored = StoredClassification(classification=data, is_reviewed=access.token is None)
    key = TextKey(part=TextPart.CLASSIFICATION, channel=slug, number=number)
    return gated(
        app,
        access,
        ClassificationOperation.WRITE,
        lambda: app.classifications.write_episode(key, stored, expected_revision),
    )


def get_profile(app: Services, access: ChannelAccess) -> ChannelProfile:
    """The profile the channel declares."""
    access.require(Permission.READ)
    return app.classifications.profile(access.channel.slug)


def write_profile(
    app: Services, access: ChannelAccess, data: Classification, expected_revision: int | None
) -> ChannelProfile:
    """Replace the channel's declared profile; same rules as an episode's classification."""
    access.require(Permission.WRITE_EDITORIAL)
    check(data)
    slug = access.channel.slug
    return gated(
        app,
        access,
        ClassificationOperation.PROFILE_WRITE,
        lambda: app.classifications.write_profile(slug, data, expected_revision),
    )


def get_observed_profile(app: Services, access: ChannelAccess) -> ObservedProfile:
    """What the classified episodes show, and where it drifts from the declared profile."""
    access.require(Permission.READ)
    slug = access.channel.slug
    declared = app.classifications.profile(slug).classification
    return observed_profile(declared, app.classifications.classified(slug))
