"""Exports: everything an episode holds, or a summary of its channel, as one model that the
JSON, Excel and PDF files are all written from, so they never disagree (rule project.export).

They only read, with the same permission as reading each part; waivers and media files are
never part of an export.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor import classification_operations as classifying
from sreditor import episode_operations, library_operations, pitch_operations
from sreditor import external_media_operations as third_party
from sreditor import research_operations as research
from sreditor.classification import ChannelProfile, EpisodeClassification, ObservedProfile
from sreditor.deps import ChannelAccess, Services
from sreditor.editorial import Editorial
from sreditor.episodes import Episode
from sreditor.external_media import EpisodeCredits, ExternalMedia, MediaUse
from sreditor.library import LibraryEntry
from sreditor.members import Permission
from sreditor.operations import get_editorial
from sreditor.pagination import MAX_LIMIT, Page
from sreditor.pitches import Pitch, PitchSource
from sreditor.research import Source
from sreditor.screenplay import Screenplay
from sreditor.screenplay_operations import get_screenplay
from sreditor.short_script import ShortScript
from sreditor.short_script_operations import get_short_script

# Bumped when a field changes meaning or goes away, so whoever reads an export can tell.
FORMAT_VERSION = "1"
# Where the API serves exports; the workspace pages link to them for download.
EPISODE_EXPORT = "/channels/{slug}/episodes/{number}/export"
CHANNEL_EXPORT = "/channels/{slug}/export"


class ChannelSummary(BaseModel):
    """The channel an export comes from."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str


class PitchPart(BaseModel):
    """The pitch an episode refers to, with its own research, as it is."""

    model_config = ConfigDict(frozen=True)

    pitch: Pitch
    notes: str
    sources: list[PitchSource]


class ThirdPartyUse(BaseModel):
    """One use of a third-party item, with the item it uses."""

    model_config = ConfigDict(frozen=True)

    use: MediaUse
    item: ExternalMedia


class EpisodeExport(BaseModel):
    """Everything an episode holds, except its waivers and media files."""

    model_config = ConfigDict(frozen=True)

    format_version: str = FORMAT_VERSION
    exported_at: datetime
    channel: ChannelSummary
    episode: Episode
    research_notes: str
    sources: list[Source]
    pitch: PitchPart | None
    screenplay: Screenplay
    short_script: ShortScript | None
    classification: EpisodeClassification
    style: LibraryEntry | None
    third_party: list[ThirdPartyUse]
    credits: EpisodeCredits


class EpisodeRow(BaseModel):
    """One episode in a channel's summary."""

    model_config = ConfigDict(frozen=True)

    episode: Episode
    classification: EpisodeClassification


class ChannelExport(BaseModel):
    """A summary of a channel: its editorial, its profile and each episode with its class."""

    model_config = ConfigDict(frozen=True)

    format_version: str = FORMAT_VERSION
    exported_at: datetime
    channel: ChannelSummary
    editorial: Editorial
    declared_profile: ChannelProfile
    observed_profile: ObservedProfile
    episodes: list[EpisodeRow]


def every_item[T](fetch: Callable[[str | None], Page[T]]) -> list[T]:
    """Every item of a paged list, page by page."""
    items: list[T] = []
    cursor: str | None = None
    while True:
        page = fetch(cursor)
        items += page.items
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def channel_summary(access: ChannelAccess) -> ChannelSummary:
    """The channel's slug and name."""
    return ChannelSummary(slug=access.channel.slug, name=access.channel.name)


def pitch_part(app: Services, access: ChannelAccess, number: int) -> PitchPart | None:
    """The episode's pitch with its notes and sources, or None when it has none."""
    pitch_id: UUID | None = pitch_operations.get_episode_pitch(app, access, number).pitch_id
    if pitch_id is None:
        return None
    sources = every_item(
        lambda cursor: pitch_operations.list_sources(app, access, pitch_id, cursor, MAX_LIMIT)
    )
    return PitchPart(
        pitch=pitch_operations.get_pitch(app, access, pitch_id),
        notes=pitch_operations.get_notes(app, access, pitch_id).text,
        sources=sources,
    )


def third_party_uses(app: Services, access: ChannelAccess, number: int) -> list[ThirdPartyUse]:
    """Each use of a third-party item, with the item."""
    uses = every_item(
        lambda cursor: third_party.list_uses(
            app, access, third_party.UseQuery(number=number, cursor=cursor, limit=MAX_LIMIT)
        )
    )
    return [
        ThirdPartyUse(use=use, item=third_party.get_item(app, access, use.media_id)) for use in uses
    ]


def written_short_script(app: Services, access: ChannelAccess, number: int) -> ShortScript | None:
    """The short script, or None while nobody wrote one."""
    found = get_short_script(app, access, number)
    return found if found.revision else None


def export_episode(app: Services, access: ChannelAccess, number: int) -> EpisodeExport:
    """Everything an episode holds, read with the person's or token's own permissions."""
    access.require(Permission.READ)
    sources = every_item(
        lambda cursor: research.list_sources(app, access, number, cursor, MAX_LIMIT)
    )
    return EpisodeExport(
        exported_at=datetime.now(UTC),
        channel=channel_summary(access),
        episode=episode_operations.get_episode(app, access, number).episode,
        research_notes=research.get_notes(app, access, number).text,
        sources=sources,
        pitch=pitch_part(app, access, number),
        screenplay=get_screenplay(app, access, number),
        short_script=written_short_script(app, access, number),
        classification=classifying.get_classification(app, access, number),
        style=library_operations.get_episode_style(app, access, number).style,
        third_party=third_party_uses(app, access, number),
        credits=third_party.get_credits(app, access, number),
    )


def export_channel(app: Services, access: ChannelAccess) -> ChannelExport:
    """A summary of the channel, with each episode's classification."""
    access.require(Permission.READ)
    episodes = every_item(
        lambda cursor: episode_operations.list_episodes(app, access, cursor, MAX_LIMIT)
    )
    rows = [
        EpisodeRow(
            episode=episode,
            classification=classifying.get_classification(app, access, episode.number),
        )
        for episode in episodes
    ]
    return ChannelExport(
        exported_at=datetime.now(UTC),
        channel=channel_summary(access),
        editorial=get_editorial(app, access, None),
        declared_profile=classifying.get_profile(app, access),
        observed_profile=classifying.get_observed_profile(app, access),
        episodes=rows,
    )
