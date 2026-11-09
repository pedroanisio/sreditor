"""What the overview and the readiness sources read about one episode, each part read once.

Every property calls the operation that already serves that part, so the overview never repeats
a query or a warning rule (rule agentic.mcp.results: one call reads the episode).
"""

from __future__ import annotations

from functools import cached_property

from sreditor import (
    claim_operations,
    context_operations,
    delivery_operations,
    episode_operations,
    external_media_operations,
    library_operations,
    pitch_operations,
    research_operations,
    review_operations,
    screenplay_operations,
    short_script_operations,
)
from sreditor.claims import ClaimWarnings
from sreditor.context import ItemContext, ItemKind, ItemRef
from sreditor.delivery import Delivery
from sreditor.deps import ChannelAccess, RoleMissing, ScopeMissing, Services
from sreditor.episodes import PartCounts, RevisedEpisode
from sreditor.external_media import EpisodeCredits
from sreditor.library import EpisodeStyle
from sreditor.library_operations import Cast
from sreditor.members import Permission
from sreditor.pitches import EpisodePitches
from sreditor.research import ResearchNotes
from sreditor.reviews import ReviewSummary
from sreditor.screenplay import Screenplay
from sreditor.short_script import ShortScript
from sreditor.srstudio_export import Missing, missing_pieces


class EpisodeFacts:
    """One episode as the caller may read it; each part is fetched on first use and kept."""

    def __init__(self, app: Services, access: ChannelAccess, number: int) -> None:
        self.app = app
        self.access = access
        self.number = number

    def may(self, permission: Permission) -> bool:
        """Whether the caller's role and token scopes allow this permission."""
        try:
            self.access.require(permission)
        except (RoleMissing, ScopeMissing):
            return False
        return True

    @cached_property
    def episode(self) -> RevisedEpisode:
        """The episode's title, summary and status with its revision."""
        return episode_operations.get_episode(self.app, self.access, self.number)

    @cached_property
    def counts(self) -> PartCounts:
        """How many sources, media files and third-party uses the episode has."""
        return episode_operations.count_episode_parts(self.app, self.access, self.number)

    @cached_property
    def notes(self) -> ResearchNotes:
        """The episode's research notes."""
        return research_operations.get_notes(self.app, self.access, self.number)

    @cached_property
    def pitches(self) -> EpisodePitches:
        """The pitches the episode is built on."""
        return pitch_operations.get_episode_pitches(self.app, self.access, self.number)

    @cached_property
    def screenplay(self) -> Screenplay:
        """The episode's screenplay."""
        return screenplay_operations.get_screenplay(self.app, self.access, self.number)

    @cached_property
    def cast(self) -> Cast:
        """The screenplay's characters and places matched against the channel's library."""
        return library_operations.episode_cast(self.app, self.access, self.number)

    @cached_property
    def short_script(self) -> ShortScript:
        """The episode's short script with its warnings."""
        return short_script_operations.get_short_script(self.app, self.access, self.number)

    @cached_property
    def style(self) -> EpisodeStyle:
        """The visual style the episode picked."""
        return library_operations.get_episode_style(self.app, self.access, self.number)

    @cached_property
    def credits(self) -> EpisodeCredits:
        """The credits and rights warnings of the third-party media the episode uses."""
        return external_media_operations.get_credits(self.app, self.access, self.number)

    @cached_property
    def claim_count(self) -> int:
        """How many claims the episode has."""
        return claim_operations.count_claims(self.app, self.access, self.number)

    @cached_property
    def unchecked(self) -> ClaimWarnings:
        """The claims that have no checked primary source."""
        return claim_operations.unchecked_claims(self.app, self.access, self.number)

    @cached_property
    def reviews(self) -> ReviewSummary:
        """How many reviews the episode has and the latest one."""
        return review_operations.review_summary(self.app, self.access, self.number)

    @cached_property
    def delivery(self) -> Delivery:
        """The delivery package with its warnings."""
        return delivery_operations.get_delivery(self.app, self.access, self.number)

    @cached_property
    def context(self) -> ItemContext:
        """The episode's answers to what, who, where, when, why and how."""
        item = ItemRef(kind=ItemKind.EPISODE, id=str(self.number))
        return context_operations.get_context(self.app, self.access, item)

    @cached_property
    def export_gaps(self) -> list[Missing]:
        """What keeps the episode from exporting to srstudio and from rendering."""
        return missing_pieces(self.app, self.access, self.number)
