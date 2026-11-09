"""MCP tools for A/B experiments between two episodes (rules agentic.mcp and project.experiments).

Two task-sized tools (agentic.mcp.tools.outcomes): one reads, listing the channel's experiments or
opening one with its computed result, and one saves, creating an experiment or replacing one.
Deleting an experiment is left to the pages and the API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from sreditor import experiment_operations as experiments
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.experiment_operations import ExperimentChange, ExperimentQuery, ExperimentReading
from sreditor.experiments import Experiment, ExperimentInput, ExperimentNotFound
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit

EDIT_FIELDS = {"experiment_id", "expected_revision"}


class ExperimentEdit(ExperimentInput):
    """An experiment to save: a new one, or one to replace with the revision its caller read."""

    experiment_id: UUID | None = Field(
        default=None, description="Omit to create; an id from get_experiment replaces it."
    )
    expected_revision: int | None = Field(
        default=None, description="The experiment's `revision` from get_experiment."
    )


class ExperimentLookup(BaseModel):
    """What get_experiment returns: a page of experiments, or one experiment with its result."""

    model_config = ConfigDict(frozen=True)

    experiments: Page[Experiment] | None = None
    reading: ExperimentReading | None = None


def experiment_reported[T](action: Callable[[], T]) -> T:
    """Run an experiment tool's action; a missing experiment names the call that lists them."""

    def guarded() -> T:
        try:
            return action()
        except ExperimentNotFound as missing:
            raise ToolError(
                f"Experiment {missing} not found; call get_experiment without experiment_id "
                "for the channel's experiment ids"
            ) from missing

    return reported(guarded)


def channel_access(services: Services, context: Context, slug: str) -> ChannelAccess:
    """The caller's access to the channel, from the token behind the MCP request."""
    who = principal(context)
    return access_for(services, slug, who.user, who.token)


def add_experiment_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and save A/B experiments."""

    @server.tool(annotations=tool_annotations("Read experiments", Effect.READ))
    def get_experiment(
        context: Context,
        slug: str,
        experiment_id: UUID | None = None,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> ExperimentLookup:
        """Without `experiment_id`, list the channel's A/B experiments (two episodes compared:
        A the control, B the variant), oldest first; pass `next_cursor` as `cursor` for more. With it, read that
        experiment with its result, computed now and never stored: each side's posts with the
        chosen metric at the chosen snapshot age, the mean per side, `difference` (B minus A),
        `ratio` (B over A) and `notes` naming what is still missing (enter it with
        record_post_metrics). The result never decides: the person's conclusion is the
        experiment's `reading` (Markdown) and `verdict`. To compare many posts by
        classification, hook or length instead of one pair, use what_worked."""

        def read() -> ExperimentLookup:
            channel = channel_access(services, context, slug)
            if experiment_id is not None:
                return ExperimentLookup(
                    reading=experiments.experiment_result(services, channel, experiment_id)
                )
            query = ExperimentQuery(cursor=cursor, limit=limit)
            return ExperimentLookup(
                experiments=experiments.list_experiments(services, channel, query)
            )

        return experiment_reported(read)

    @server.tool(annotations=tool_annotations("Save an experiment", Effect.CREATE))
    def save_experiment(
        context: Context,
        slug: str,
        experiment: ExperimentEdit,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Experiment]:
        """Plan an A/B experiment between two episodes of the channel (no
        `experiment_id`), or replace one (its `experiment_id` from get_experiment, with its
        `revision` as `expected_revision`) to start it, write the reading or abandon it.
        `experiment` holds title, episode_a (the
        control) and episode_b (the variant) as episode numbers from list_episodes, `change`
        (what differs), `hypothesis` and `reading` (all Markdown), `metric`, `snapshot_age`,
        optional `platform`, `status` and `started_on`. A replace sets every field, so send
        them all. Status read needs a `verdict` (b_better, a_better, no_difference or
        inconclusive) and no other status takes one. Needs editorial:write and the owner or
        editor role. Pass an `idempotency_key` when creating, so a retry returns the first
        experiment. It stores your words only; the numbers come from get_experiment."""

        def save() -> Experiment:
            channel = channel_access(services, context, slug)
            data = ExperimentInput.model_validate(experiment.model_dump(exclude=EDIT_FIELDS))
            if experiment.experiment_id is None:
                return experiments.create_experiment(services, channel, data)
            change = ExperimentChange(
                experiment_id=experiment.experiment_id,
                expected_revision=experiment.expected_revision,
            )
            return experiments.replace_experiment(services, channel, change, data)

        call = IdempotentCall(
            operation=f"save_experiment/{slug}", arguments=experiment, key=idempotency_key
        )
        return experiment_reported(lambda: run_once(services, context, call, save, Experiment))
