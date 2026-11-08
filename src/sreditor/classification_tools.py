"""MCP tools to classify episodes and compare the channel with its profile (agentic.mcp).

A few task-sized tools (agentic.mcp.tools.outcomes): read the vocabulary, read and propose an
episode's classification, and read the channel's declared and observed profile in one call.
What an agent writes stays a proposal until a person confirms it.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from sreditor import classification_operations as classifying
from sreditor.classification import (
    ChannelProfile,
    Classification,
    ClassificationRefused,
    EpisodeClassification,
    ObservedProfile,
)
from sreditor.classification_routes import Vocabulary
from sreditor.classification_routes import get_classification_vocabulary as vocabulary
from sreditor.deps import ChannelAccess, Services
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pitch_tools import channel_access


class ProfileComparison(BaseModel):
    """The profile a channel declares next to the one its episodes show."""

    model_config = ConfigDict(frozen=True)

    declared: ChannelProfile
    observed: ObservedProfile


def classification_reported[T](action: Callable[[], T], retry: str = "classify_episode") -> T:
    """Run a tool's action; a rule break lists each field at fault and how to fix it.

    `retry` is the tool to call again once the fields are fixed.
    """

    def guarded() -> T:
        try:
            return action()
        except ClassificationRefused as refusal:
            raise ToolError(
                f"{refusal}. Fix those fields and call {retry} again; "
                "get_classification_vocabulary lists the allowed values"
            ) from refusal

    return reported(guarded)


def add_classification_tools(server: MCPServer, services: Services) -> None:
    """Tools to classify episodes and read the channel's profile."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Read the classification vocabulary", Effect.READ))
    def get_classification_vocabulary() -> Vocabulary:
        """List the values each of the twelve classification dimensions accepts (audience,
        domains, purposes, genres, approaches, formats, visual techniques and styles, tones,
        emotional targets, creative and humor devices). Read it before classify_episode."""
        return vocabulary()

    @server.tool(annotations=tool_annotations("Read an episode's classification", Effect.READ))
    def get_episode_classification(
        context: Context, slug: str, number: int
    ) -> EpisodeClassification:
        """Read an episode's editorial classification and its revision, before changing it.
        Revision 0 means nobody classified it; `is_reviewed` false marks an agent's proposal."""
        return classification_reported(
            lambda: classifying.get_classification(services, access(context, slug), number)
        )

    @server.tool(annotations=tool_annotations("Classify an episode", Effect.REPLACE))
    def classify_episode(
        context: Context,
        slug: str,
        number: int,
        classification: Classification,
        expected_revision: int | None = None,
    ) -> EpisodeClassification:
        """Propose an episode's classification from its screenplay and research: for each
        dimension a state (present, absent, unknown, not_applicable) and, when present, one
        primary and up to two secondary values with a confidence; set method to ai. Comedy
        needs a humor device, a satirical approach `satire`, a parodic one
        `parody_references`. It stays a proposal until a person confirms it. Needs
        content:write. Pass the `revision` from get_episode_classification (0 when new)."""
        return classification_reported(
            lambda: classifying.write_classification(
                services, access(context, slug), number, classification, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Declare the channel's profile", Effect.REPLACE))
    def declare_channel_profile(
        context: Context,
        slug: str,
        profile: Classification,
        expected_revision: int | None = None,
    ) -> ChannelProfile:
        """Replace the profile a channel declares for itself: what it is meant to be, in the
        same dimensions and rules as an episode's classification (each dimension a state and,
        when present, one primary and up to two secondary values; comedy needs a humor device,
        a satirical approach `satire`, a parodic one `parody_references`), from the values
        get_classification_vocabulary lists. get_channel_profile then compares it with what
        the channel's classified episodes show. Not for one episode: use classify_episode.
        Needs owner or editor role and the editorial:write scope. Pass the `revision` of
        `declared` from get_channel_profile (0 when never declared) as `expected_revision`."""
        return classification_reported(
            lambda: classifying.write_profile(
                services, access(context, slug), profile, expected_revision
            ),
            retry="declare_channel_profile",
        )

    @server.tool(annotations=tool_annotations("Read the channel's profile", Effect.READ))
    def get_channel_profile(context: Context, slug: str) -> ProfileComparison:
        """Read the profile a channel declares and the one its classified episodes show: each
        value's count and share, and `drift`, the declared primary values fewer than half of
        the episodes hold. Use it to check an idea or screenplay fits the channel."""

        def read() -> ProfileComparison:
            channel = access(context, slug)
            return ProfileComparison(
                declared=classifying.get_profile(services, channel),
                observed=classifying.get_observed_profile(services, channel),
            )

        return classification_reported(read)
