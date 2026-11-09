"""MCP tools that move files in and out through signed links (rule agentic.mcp.files).

An agent in a sandbox, such as claude.ai's, cannot use the MCP connection's OAuth token to call
the API, and a file never travels inside a tool result beyond 1 MB. So a tool answers with a
single-use link, valid for 15 minutes, that the sandbox opens with a plain GET or PUT.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from sreditor import file_operations as files
from sreditor.deps import Services
from sreditor.external_media_tools import external_reported
from sreditor.file_operations import ExportCheck, FileOffer, FileTarget, UploadOffer, UploadTarget
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, issue_link_once
from sreditor.mcp_server import Effect, tool_annotations
from sreditor.media_operations import RenderNotUploadable
from sreditor.pitch_tools import channel_access, pitch_reported
from sreditor.render_tools import render_reported


def file_reported[T](action: Callable[[], T]) -> T:
    """Run a file tool's action, turning its refusals into tool errors that name the fix."""

    def guarded() -> T:
        try:
            return action()
        except RenderNotUploadable as refusal:
            raise ToolError(
                f"{refusal}. Use request_render, then get_file_link with source render"
            ) from refusal

    return render_reported(lambda: external_reported(lambda: pitch_reported(guarded)))


def add_file_tools(server: MCPServer, services: Services) -> None:
    """Register the file tools."""

    @server.tool(annotations=tool_annotations("Get a download link for a file", Effect.CREATE))
    def get_file_link(
        context: Context, slug: str, target: FileTarget, inline: bool = False
    ) -> FileOffer:
        """Get a single-use link to download one file, which your sandbox then opens with a
        plain GET and no credentials: the channel's logo, avatar or another brand file, an
        episode's media file, a finished render's video, a third-party item's uploaded copy, an
        episode's srstudio export zip, or an export or book. Use it when you need a file's
        bytes; to see what exists first, call get_channel, list_media, list_renders or
        list_external_media. The link works once and expires in 15 minutes (`expires_at`); ask
        again after that. Pass `inline` true to also get a file of 1 MB or less as
        `content_base64` and skip the download. For source export, `target.export` has a
        `scope`: episode (the whole episode as data: research, sources, pitch, screenplay, short
        script, classification, style, third-party uses and credits) gives an episode snapshot in
        one call, instead of calling each get tool; channel (a summary with the profile and every
        episode's classification); or pitch_book, episode_book, library_book, channel_book or
        rights_book. Add the episode `number` or the `pitch_id`, the `format` (json, to read it
        as data; xlsx and pdf are for people), the `language` (en or pt-BR) and, for the library,
        channel and rights books, `options` with sections, episode statuses or a number range.
        An export is built when the link is opened, from the data as it is then, and never holds
        waivers. Needs channels:read, and for srstudio_export the right to change the channel
        (channels:write). Waivers cannot be fetched this way."""
        return file_reported(
            lambda: files.issue_download(
                services, channel_access(services, context, slug), target, inline
            )
        )

    @server.tool(annotations=tool_annotations("Get an upload link for a file", Effect.CREATE))
    def create_upload_link(
        context: Context,
        slug: str,
        upload: UploadTarget,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, UploadOffer]:
        """Get a single-use link to upload one file, which your sandbox sends with `method`
        (PUT) and the file's bytes as the body to `url`, with no credentials. `upload.target`
        says where it goes, and each target takes only its own fields:
        - episode_media (the default when `target` is left out, so the original arguments still
        work): a file of an episode, with the episode `number`, the `kind`, a `name` and an
        optional `note`. `kind` is voice_over, sound_effect, score, visual_effect (MP4 or
        WebM), cover or still (PNG, JPEG or WebP) or final_video (a finished MP4 or WebM master
        made outside sreditor, measured on arrival; use its id as a cut's `video_id` in
        write_delivery). A render is made with request_render, never uploaded. The answer is
        the new media file. Needs content:write.
        - external_copy: the copy of a third-party item, with its `item_id` from
        list_external_media, replacing any earlier copy. The accepted types follow the item's
        kind (a 3D model GLB, FBX or BLEND, a shader UTF-8 text, and so on; a model, texture
        or shader may be a ZIP package). The answer is the item. Remove a copy with
        update_external_media and remove_copy. Needs content:write.
        - brand_file: a file of the brand kit, with the `slot` (avatar, which must be square,
        logo, banner, watermark, thumbnail_template, intro for the opening sting, outro for the
        closing sting, or font) and, for a font, its `role` (heading, body or caption), whose
        family update_brand must have set first. Add `platform` to fill an account's
        overrides. It replaces the file in the slot; the answer is the brand kit. Empty a
        slot with update_brand and clear_files. Needs owner or editor role and channels:write.
        Waivers are never sent through links.
        The type is read from the file's content, and `accepted_types` and `max_bytes` say
        what is allowed; a refused file leaves the link usable. The link works once and
        expires in 15 minutes. Each call issues another link unless you pass an
        `idempotency_key`: a retry with the same key and arguments is marked replayed and
        returns a fresh link to the same place, since a link's secret is never stored."""
        call = IdempotentCall(
            operation=f"create_upload_link/{slug}", arguments=upload, key=idempotency_key
        )
        return file_reported(
            lambda: issue_link_once(
                services,
                context,
                call,
                lambda: files.issue_upload(
                    services, channel_access(services, context, slug), upload
                ),
                UploadOffer,
            )
        )

    @server.tool(annotations=tool_annotations("Check an srstudio export", Effect.CREATE))
    def check_srstudio_export(context: Context, slug: str, number: int) -> ExportCheck:
        """Check whether an episode can be exported for srstudio: `ready`, the `missing` pieces
        in plain words (such as a voice-over take or a visual style), the episode's srstudio
        `slug` and `seed` and, when ready, a download link `file_link` to the zip, built when
        you open it. Use it before rendering locally with srstudio; fix each missing piece (a
        character or place with no world or kind: save_library_sheet; the slug and seed:
        set_episode_slug) and call it again. Each ready check issues a new single-use link.
        Needs an owner or editor and, for a token, the channels:write scope."""
        return file_reported(
            lambda: files.check_export(services, channel_access(services, context, slug), number)
        )
