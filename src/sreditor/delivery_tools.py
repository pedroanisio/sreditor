"""MCP tools for an episode's delivery package and its posts' metrics (rule agentic.mcp).

Task-sized tools (agentic.mcp.tools.outcomes): read or write the package of cuts and posts, add a
post's link after publishing, then enter its numbers or a retention CSV and compare the channel's
posts. Numbers are brought by the agent or person, never fetched from a platform.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict

from sreditor import delivery_operations as deliveries
from sreditor import post_metrics_operations as metrics
from sreditor.channels import Platform
from sreditor.delivery import Delivery, DeliveryInput
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit
from sreditor.post_metrics import (
    Metric,
    PostMetric,
    Snapshot,
    SnapshotInput,
)
from sreditor.retention_csv import RetentionCsvError
from sreditor.what_worked import WhatWorked, WhatWorkedQuery
from sreditor.what_worked_operations import what_worked as run_what_worked

CSV_SHAPE = "Send two columns with a header, such as 'second,percent'."
CUT_HINT = (
    " Add the cut to `cuts` in the same write_delivery call, or use a cut name from get_delivery."
)


def delivery_error_message(error: Exception) -> str | None:
    """How to fix a refused delivery or metrics call (agentic.mcp.errors); None for others."""
    if isinstance(error, deliveries.DeliveryRefused):
        where = ".".join(str(part) for part in error.location)
        prefix = f"{where}: " if where else ""
        hint = CUT_HINT if "cut" in str(error) and not where else ""
        return f"{prefix}{error}{hint}"
    if isinstance(error, metrics.PostNotRecorded):
        return (
            f"The episode has no post on {error}; call record_post for that platform first, "
            "then enter its numbers."
        )
    if isinstance(error, RetentionCsvError):
        found = "; ".join(problem.message for problem in error.problems)
        return f"The CSV is not a retention export: {found} {CSV_SHAPE}"
    return None


def delivery_reported[T](action: Callable[[], T]) -> T:
    """Run a delivery tool's action; its refusals name their fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except (
            deliveries.DeliveryRefused,
            metrics.PostNotRecorded,
            RetentionCsvError,
        ) as refusal:
            raise ToolError(delivery_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def add_delivery_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write a delivery package, record posts and handle post metrics."""
    add_package_tools(server, services)
    add_metrics_tools(server, services)


def channel_access(services: Services, context: Context, slug: str) -> ChannelAccess:
    """The caller's access to the channel, from the token behind the MCP request."""
    who = principal(context)
    return access_for(services, slug, who.user, who.token)


class PlatformPost(deliveries.PostInput):
    """A post on one platform account of the channel."""

    platform: Platform


class PlatformSnapshot(SnapshotInput):
    """A post's numbers for one day, naming the platform of the post."""

    platform: Platform


class PostOnPlatform(BaseModel):
    """One post: the episode's number and the platform it was published on."""

    model_config = ConfigDict(frozen=True)

    number: int
    platform: Platform


class PlatformCsv(metrics.RetentionImport):
    """A retention CSV for the post on one platform."""

    platform: Platform


def add_package_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write an episode's delivery package."""

    @server.tool(annotations=tool_annotations("Read a delivery package", Effect.READ))
    def get_delivery(context: Context, slug: str, number: int) -> Delivery:
        """Read an episode's delivery package: its cuts (final video, cover, caption, alt text,
        hashtags) and the post on each platform account (cut, link, publish time, and its own
        caption, alt_text and hashtags, which replace the cut's when not empty), plus
        `warnings` for a cut without a video, a video over the length limit (24 s by default),
        more than 5 hashtags, a caption with no AI notice line, a post without a link and a post
        whose text, its own or its cut's, has no AI notice or more than 5 hashtags. They
        never block a status. `files` lists the media files the cuts use, each with its `sha256`. Revision 0 means nobody wrote it yet. For the files themselves use list_media."""
        return delivery_reported(
            lambda: deliveries.get_delivery(
                services, channel_access(services, context, slug), number
            )
        )

    @server.tool(annotations=tool_annotations("Write a delivery package", Effect.REPLACE))
    def write_delivery(
        context: Context,
        slug: str,
        number: int,
        delivery: DeliveryInput,
        expected_revision: int | None = None,
    ) -> Delivery:
        """Replace an episode's whole delivery package. Needs content:write.

        A cut is a slug name (main, vertical, teaser) with `video_id` (a video file of the
        episode from list_media: a final_video, a render...) or `external_video` (a master kept
        outside sreditor, registered by `location` such as a Drive path, with its `sha256` and,
        as you declare them, duration_seconds, width, height and loudness_lufs; sreditor never
        opens the location; give one of the two, not both), `cover_id` (an image of kind cover),
        caption and alt_text up to 1000 characters and hashtags (typed with or without #).
        A post names a platform the channel has an account on and a cut of the same package,
        with the post's link and publish time and, optionally, its own `caption`, `alt_text`
        and `hashtags` (plain text; at most 5 hashtags; the caption must match the channel's
        AI notice pattern): the call is refused naming the field otherwise. A post with none
        of its own text goes out with its cut's. To add only a link, use record_post. Pass
        `revision` from get_delivery as `expected_revision` (0 when empty)."""
        return delivery_reported(
            lambda: deliveries.write_delivery(
                services,
                channel_access(services, context, slug),
                number,
                delivery,
                expected_revision,
            )
        )

    @server.tool(annotations=tool_annotations("Record a post", Effect.REPLACE))
    def record_post(
        context: Context,
        slug: str,
        number: int,
        post: PlatformPost,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Delivery]:
        """Set the post on one platform account: which cut went there, its link, when it went
        live and, optionally, its own `caption` (must carry the AI notice), `alt_text` and
        `hashtags` (at most 5), refused naming the field otherwise. A text you leave out keeps
        the earlier post's; an empty one goes back to the cut's. Use it after publishing; it
        keeps the cuts and the other posts. The cut must
        already be in the package (write_delivery) and the channel must have an account on the
        platform. Needs content:write. A retry with the same `idempotency_key` returns the
        first result."""
        call = IdempotentCall(operation=f"record_post/{slug}/{number}", arguments=post)
        return delivery_reported(
            lambda: run_once(
                services,
                context,
                call.with_key(idempotency_key),
                lambda: deliveries.record_post(
                    services, channel_access(services, context, slug), number, post.platform, post
                ),
                Delivery,
            )
        )


def add_metrics_tools(server: MCPServer, services: Services) -> None:
    """Tools to enter a post's numbers and to compare the channel's posts."""

    @server.tool(annotations=tool_annotations("Record post numbers", Effect.REPLACE))
    def record_post_metrics(
        context: Context,
        slug: str,
        number: int,
        snapshot: PlatformSnapshot,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Snapshot]:
        """Enter a post's numbers for one day (captured_on, today when left out): views, reach,
        likes, comments, shares, saves, avg_watch_s and retention as {second, percent} points,
        for the post on `platform`. Every number is optional and non-negative; a second entry
        for the same day merges into the first, and a number you leave out keeps its value. The
        post must be in the delivery package (record_post). Take numbers from the platform's
        analytics page: this never fetches them. Needs content:write."""
        call = IdempotentCall(operation=f"record_post_metrics/{slug}/{number}", arguments=snapshot)
        return delivery_reported(
            lambda: run_once(
                services,
                context,
                call.with_key(idempotency_key),
                lambda: metrics.record_metrics(
                    services,
                    channel_access(services, context, slug),
                    number,
                    snapshot.platform,
                    snapshot,
                ),
                Snapshot,
            )
        )

    @server.tool(annotations=tool_annotations("Import a retention CSV", Effect.REPLACE))
    def import_retention_csv(
        context: Context,
        slug: str,
        number: int,
        source: PlatformCsv,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Snapshot]:
        """Import a platform's retention export, passed as CSV text in `csv`, into the snapshot
        of the post on `platform` for one day. It needs a header and two columns, the second and
        the percent still watching ('second,percent', or the names an Instagram or YouTube
        export uses), at most 200000 characters; anything else is refused with the rows at
        fault. It replaces that day's retention and keeps its other numbers. The post must be
        in the delivery package. Needs content:write."""
        call = IdempotentCall(operation=f"import_retention_csv/{slug}/{number}", arguments=source)
        return delivery_reported(
            lambda: run_once(
                services,
                context,
                call.with_key(idempotency_key),
                lambda: metrics.import_retention(
                    services,
                    channel_access(services, context, slug),
                    number,
                    source.platform,
                    source,
                ),
                Snapshot,
            )
        )

    @server.tool(annotations=tool_annotations("List post snapshots", Effect.READ))
    def list_post_snapshots(
        context: Context,
        slug: str,
        post: PostOnPlatform,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[Snapshot]:
        """List one post's dated snapshots, newest day first, each with its numbers and
        retention curve, to see how a post grew; compare_posts shows only the latest of each
        post. `post` names the episode `number` and the `platform` of its post (record_post adds it). Enter
        numbers with record_post_metrics. Pass `next_cursor` as `cursor` for more."""

        def read() -> Page[Snapshot]:
            found = channel_access(services, context, slug)
            target = metrics.post_of(services, found, post.number, post.platform)
            return metrics.list_snapshots(services, found, target, cursor, limit)

        return delivery_reported(read)

    @server.tool(annotations=tool_annotations("Compare posts", Effect.READ))
    def compare_posts(
        context: Context,
        slug: str,
        sort_by: Metric = Metric.VIEWS,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[PostMetric]:
        """Compare the channel's posts by their latest numbers, best first by `sort_by` (views,
        reach, likes, comments, shares, saves or avg_watch_s), to see which pitches and hooks
        worked. Each row is one post with its latest snapshot and the number of snapshots it
        has; posts nobody measured come last. To compare groups of posts by classification, hook or
        length use what_worked. Pass `next_cursor` as `cursor` for more."""
        query = metrics.PostQuery(sort_by=sort_by, cursor=cursor, limit=limit)
        return delivery_reported(
            lambda: metrics.compare_posts(services, channel_access(services, context, slug), query)
        )

    @server.tool(annotations=tool_annotations("See what worked", Effect.READ))
    def what_worked(context: Context, slug: str, query: WhatWorkedQuery) -> WhatWorked:
        """Compare the channel's published posts by classification, hook or length, to see which
        kind of episode did better, so the next pitch follows it. `query.group_by` is
        classification (with `classification_dimension`, see get_classification_vocabulary; only
        reviewed classifications count, proposals are left out and counted in `excluded`),
        hook_reaction or hook_length (from the short script's hook block) or video_length. Each
        group gives its posts and the median and mean of `query.metric`, from every post's latest
        snapshot or, with `query.age` such as day_7, the one nearest that many days after
        publishing, so young posts do not skew it. Groups under 3 posts are flagged
        `is_too_small`: do not conclude from them. Filter by `platform` and publish days. Numbers
        are the ones entered with record_post_metrics. To rank single posts use compare_posts;
        for one post's days use list_post_snapshots."""
        return delivery_reported(
            lambda: run_what_worked(services, channel_access(services, context, slug), query)
        )
