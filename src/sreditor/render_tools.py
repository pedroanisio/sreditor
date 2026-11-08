"""MCP tools for rendering an episode into video (rules agentic.render.mcp and project.render).

Task-sized tools (agentic.mcp.tools.outcomes): request a render and get an answer at once, then
follow it until the video is done. A video never travels in a tool result: the agent fetches it
through get_file_link, whose single-use link needs no credentials (rule agentic.mcp.files).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sreditor import render_operations as renders
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit
from sreditor.render_operations import RenderRequest, RenderView
from sreditor.renders import RenderFinished, RenderNotFound, RenderPreset
from sreditor.srstudio_export import SrstudioNotReady


def render_error_message(error: Exception) -> str | None:
    """How to fix a refused render call (rule agentic.mcp.errors)."""
    if isinstance(error, SrstudioNotReady):
        pieces = "; ".join(item.english() for item in error.missing)
        return (
            f"The episode cannot render yet; srstudio lacks: {pieces}. Fix each piece (for "
            "example write_short_script, generate_voice_over or set_episode_style), then "
            "call request_render again."
        )
    if isinstance(error, RenderNotFound):
        return f"Render {error} not found; call list_renders for the episode's render ids."
    if isinstance(error, RenderFinished):
        return f"{error}; only a queued or running render can be canceled."
    return None


def render_reported[T](action: Callable[[], T]) -> T:
    """Run a render tool, turning render refusals into tool errors with their fix."""
    try:
        return reported(action)
    except (SrstudioNotReady, RenderNotFound, RenderFinished) as refusal:
        raise ToolError(render_error_message(refusal) or str(refusal)) from refusal


def add_render_tools(server: MCPServer, services: Services) -> None:
    """Register the render tools."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Request a render", Effect.REPLACE))
    def request_render(
        context: Context, slug: str, number: int, preset: RenderPreset = RenderPreset.MASTER
    ) -> RenderView:
        """Queue a video render of the episode as it is now and answer at once with the render's
        id, status and place in the queue; a render takes minutes, so call get_render later to
        follow it. Use it once the short script, voice-over and visual style are final. The same
        episode content, preset and engine answers with the earlier render (is_reused true) and
        costs nothing, so it is idempotent by design and takes no `idempotency_key`: calling it
        again after a timeout never queues a second render. `preset` is master (the delivery) or preview (quicker, lower quality).
        Needs an owner or editor and, for a token, the render:write scope. Fails with the list
        of what srstudio still lacks when the episode is not ready."""
        request = RenderRequest(number=number, preset=preset)
        return render_reported(
            lambda: renders.request_render(services, access(context, slug), request)
        )

    @server.tool(annotations=tool_annotations("Follow a render", Effect.READ))
    def get_render(context: Context, slug: str, number: int, render_id: UUID) -> RenderView:
        """Read a render's status (queued, building, rendering, encoding, done, failed,
        canceled), progress from 0 to 1, place in the queue and scene-render's findings. When
        done, `video` is the episode's media file; fetch its bytes with get_file_link (source
        render and this render's id), since `download_path` is an API path that only an API
        token can open, not an MCP connection's. A person signed in can open it from the
        episode's media. Use the render id request_render or list_renders returned."""
        return render_reported(
            lambda: renders.get_render(services, access(context, slug), number, render_id)
        )

    @server.tool(annotations=tool_annotations("List renders", Effect.READ))
    def list_renders(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[RenderView]:
        """List the episode's renders, newest first, with each one's status and, once done, its
        video. Pass `next_cursor` as `cursor` for more."""
        return render_reported(
            lambda: renders.list_renders(services, access(context, slug), number, cursor, limit)
        )

    @server.tool(annotations=tool_annotations("Cancel a render", Effect.REMOVE))
    def cancel_render(context: Context, slug: str, number: int, render_id: UUID) -> RenderView:
        """Stop a queued or running render, for example one requested by mistake; its worker
        stops at its next report. Fails when the render already ended. Needs an owner or editor
        and, for a token, the render:write scope."""
        return render_reported(
            lambda: renders.cancel_render(services, access(context, slug), number, render_id)
        )
