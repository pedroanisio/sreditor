"""The episode overview and the channel's readiness summary (rule project.readiness).

One call reads an episode: a compact digest of each part with the revision to write it back
with, and the readiness list. Digests hold counts, names and short excerpts, never whole texts
(rule agentic.mcp.results); each says which tool reads the part in full. Every digest is built
from the operation that already serves the part, and waivers are never part of it.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sreditor.context import ContextInput
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_facts import EpisodeFacts
from sreditor.episode_operations import list_episodes
from sreditor.episodes import Episode, EpisodeStatus
from sreditor.members import Permission
from sreditor.pagination import Page
from sreditor.readiness import ReadinessReport, readiness_report

EXCERPT_CHARACTERS = 200


class Section(StrEnum):
    """The parts of an episode the overview can include."""

    RESEARCH = "research"
    PITCHES = "pitches"
    SCREENPLAY = "screenplay"
    SHORT_SCRIPT = "short_script"
    STYLE = "style"
    CREDITS = "credits"
    MEDIA = "media"
    CONTEXT = "context"
    DELIVERY = "delivery"
    CLAIMS = "claims"
    READINESS = "readiness"


# The parts an agent writes back to most and its readiness; the rest are asked for by name.
DEFAULT_SECTIONS = frozenset(
    {
        Section.RESEARCH,
        Section.SCREENPLAY,
        Section.SHORT_SCRIPT,
        Section.STYLE,
        Section.MEDIA,
        Section.CLAIMS,
        Section.READINESS,
    }
)


class ResearchDigest(BaseModel):
    """The research notes' size and revision, a short start of them and how many sources exist."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to write_research_notes.")
    notes_characters: int
    notes_excerpt: str
    source_count: int
    read_with: str = "get_research"


class PitchesDigest(BaseModel):
    """The pitches the episode is built on, main one first."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to set_episode_pitches.")
    pitch_ids: list[UUID]
    read_with: str = "get_research"


class ScreenplayDigest(BaseModel):
    """The screenplay's size and revision, its characters and places."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to write_screenplay.")
    text_characters: int
    scene_count: int
    characters: list[str]
    locations: list[str]
    read_with: str = "get_screenplay"


class ShortScriptDigest(BaseModel):
    """The short script's shape and revision."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to write_short_script.")
    duration_s: float
    language: str
    block_count: int
    warning_count: int
    read_with: str = "get_short_script"


class StyleDigest(BaseModel):
    """The visual style the episode picked, if any."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to set_episode_style.")
    style_id: UUID | None
    style_name: str | None
    read_with: str = "get_episode_style"


class CreditsDigest(BaseModel):
    """How many credits the third-party media owes and how many rights warnings it has."""

    model_config = ConfigDict(frozen=True)

    use_count: int
    credit_count: int
    warning_count: int
    read_with: str = "get_episode_credits"


class MediaDigest(BaseModel):
    """How many media files the episode has."""

    model_config = ConfigDict(frozen=True)

    count: int
    read_with: str = "list_media"


class ContextDigest(BaseModel):
    """Which of what, who, where, when, why and how are answered."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to write_context.")
    answered: list[str]
    read_with: str = "get_context"


class DeliveryDigest(BaseModel):
    """The delivery package's shape and warnings."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(description="Pass as expected_revision to write_delivery.")
    cut_count: int
    post_count: int
    warning_count: int
    read_with: str = "get_delivery"


class ClaimsDigest(BaseModel):
    """How many claims the episode has and how many lack a checked primary source."""

    model_config = ConfigDict(frozen=True)

    count: int
    unchecked: int
    read_with: str = "list_claims"


class EpisodeOverview(BaseModel):
    """An episode in one call: its details and the sections asked for; others are null."""

    model_config = ConfigDict(frozen=True)

    episode: Episode
    revision: int = Field(description="Pass as expected_revision to update_episode.")
    sections: list[Section] = Field(description="The sections this answer includes.")
    research: ResearchDigest | None = None
    pitches: PitchesDigest | None = None
    screenplay: ScreenplayDigest | None = None
    short_script: ShortScriptDigest | None = None
    style: StyleDigest | None = None
    credits: CreditsDigest | None = None
    media: MediaDigest | None = None
    context: ContextDigest | None = None
    delivery: DeliveryDigest | None = None
    claims: ClaimsDigest | None = None
    readiness: ReadinessReport | None = None


class EpisodeReadiness(BaseModel):
    """One episode's place in the channel's readiness: how many gaps stop work and how many warn."""

    model_config = ConfigDict(frozen=True)

    number: int
    title: str
    status: EpisodeStatus
    blockers: int
    warnings: int


def research_digest(facts: EpisodeFacts) -> ResearchDigest:
    """Research notes and source count."""
    text = facts.notes.text
    return ResearchDigest(
        revision=facts.notes.revision,
        notes_characters=len(text),
        notes_excerpt=text[:EXCERPT_CHARACTERS],
        source_count=facts.counts.sources,
    )


def pitches_digest(facts: EpisodeFacts) -> PitchesDigest:
    """The episode's pitches."""
    return PitchesDigest(revision=facts.pitches.revision, pitch_ids=facts.pitches.pitch_ids)


def screenplay_digest(facts: EpisodeFacts) -> ScreenplayDigest:
    """The screenplay's size, characters and places."""
    screenplay = facts.screenplay
    return ScreenplayDigest(
        revision=screenplay.revision,
        text_characters=len(screenplay.text),
        scene_count=len(screenplay.scenes),
        characters=screenplay.characters,
        locations=screenplay.locations,
    )


def short_script_digest(facts: EpisodeFacts) -> ShortScriptDigest:
    """The short script's shape."""
    script = facts.short_script
    return ShortScriptDigest(
        revision=script.revision,
        duration_s=script.duration_s,
        language=script.voice.language,
        block_count=len(script.blocks),
        warning_count=len(script.warnings),
    )


def style_digest(facts: EpisodeFacts) -> StyleDigest:
    """The picked visual style."""
    chosen = facts.style.style
    return StyleDigest(
        revision=facts.style.revision,
        style_id=chosen.id if chosen else None,
        style_name=chosen.name if chosen else None,
    )


def credits_digest(facts: EpisodeFacts) -> CreditsDigest:
    """Credits owed and rights warnings."""
    return CreditsDigest(
        use_count=facts.counts.uses,
        credit_count=len(facts.credits.credits),
        warning_count=len(facts.credits.warnings),
    )


def media_digest(facts: EpisodeFacts) -> MediaDigest:
    """The number of media files."""
    return MediaDigest(count=facts.counts.media)


def context_digest(facts: EpisodeFacts) -> ContextDigest:
    """Which questions of the context are answered."""
    context = facts.context
    return ContextDigest(
        revision=context.revision,
        answered=[name for name in ContextInput.model_fields if getattr(context, name)],
    )


def delivery_digest(facts: EpisodeFacts) -> DeliveryDigest:
    """The delivery package's shape."""
    package = facts.delivery
    return DeliveryDigest(
        revision=package.revision,
        cut_count=len(package.cuts),
        post_count=len(package.posts),
        warning_count=len(package.warnings),
    )


def claims_digest(facts: EpisodeFacts) -> ClaimsDigest:
    """Claim and unchecked claim counts."""
    return ClaimsDigest(count=facts.claim_count, unchecked=facts.unchecked.count)


# One builder per section; a new section is one more line here and in Section.
DIGESTS: dict[Section, Callable[[EpisodeFacts], BaseModel]] = {
    Section.RESEARCH: research_digest,
    Section.PITCHES: pitches_digest,
    Section.SCREENPLAY: screenplay_digest,
    Section.SHORT_SCRIPT: short_script_digest,
    Section.STYLE: style_digest,
    Section.CREDITS: credits_digest,
    Section.MEDIA: media_digest,
    Section.CONTEXT: context_digest,
    Section.DELIVERY: delivery_digest,
    Section.CLAIMS: claims_digest,
    Section.READINESS: readiness_report,
}


def episode_overview(
    app: Services, access: ChannelAccess, number: int, include: frozenset[Section] | None
) -> EpisodeOverview:
    """The episode with a digest of each section asked for, DEFAULT_SECTIONS when none is."""
    wanted = DEFAULT_SECTIONS if not include else include
    facts = EpisodeFacts(app, access, number)
    current = facts.episode  # refuses a missing episode before any part is read
    ordered = [section for section in Section if section in wanted]
    parts: dict[str, Any] = {section.value: DIGESTS[section](facts) for section in ordered}
    return EpisodeOverview(
        episode=current.episode, revision=current.revision, sections=ordered, **parts
    )


def summaries_for(
    app: Services, access: ChannelAccess, episodes: list[Episode]
) -> list[EpisodeReadiness]:
    """The readiness counts of each episode, in the order given."""
    found = []
    for episode in episodes:
        report = readiness_report(EpisodeFacts(app, access, episode.number))
        found.append(
            EpisodeReadiness(
                number=episode.number,
                title=episode.title,
                status=episode.status,
                blockers=report.blockers,
                warnings=report.warnings,
            )
        )
    return found


def list_readiness(
    app: Services, access: ChannelAccess, cursor: str | None, limit: int
) -> Page[EpisodeReadiness]:
    """A page of the channel's episodes by number, each with its blockers and warnings."""
    access.require(Permission.READ)
    episodes = list_episodes(app, access, cursor, limit)
    return Page[EpisodeReadiness](
        items=summaries_for(app, access, episodes.items), next_cursor=episodes.next_cursor
    )
