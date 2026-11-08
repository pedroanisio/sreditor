"""Media operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Adding, renaming and removing an episode's media is content work, open to contributors and the
content:write scope, like its sources: it changes only that episode. Every call first checks
that the episode exists.
"""

from __future__ import annotations

from typing import BinaryIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.assets import AssetMissing
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.media import MediaFile, MediaInput, MediaKind, NewMedia
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.video_probe import probe_video

VIDEO_PREFIX = "video/"


class MediaOperation:
    """stmt operation names an agent's media writes are gated on."""

    CREATE = "media.create"
    WRITE = "media.write"
    DELETE = "media.delete"


class MediaQuery(BaseModel):
    """Which of an episode's files to list, and the page."""

    model_config = ConfigDict(frozen=True)

    number: int
    kind: MediaKind | None = None
    cursor: str | None = None
    limit: int


class MediaChange(BaseModel):
    """Which file a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    number: int
    media_id: UUID
    expected_revision: int | None = None


def list_media(app: Services, access: ChannelAccess, query: MediaQuery) -> Page[MediaFile]:
    """The episode's media, of one kind or all, in the order they were added."""
    slug = episode_access(app, access, query.number, Permission.READ)
    after = after_number(query.cursor)
    rows = app.media.files(slug, query.number, query.kind, after, query.limit + 1)
    items = [with_stored_measures(app, row) for row in rows[: query.limit]]
    has_more = len(rows) > query.limit
    cursor_out = encode_cursor(str(app.media.position(items[-1].id))) if has_more else None
    return Page[MediaFile](items=items, next_cursor=cursor_out)


def get_media(app: Services, access: ChannelAccess, number: int, media_id: UUID) -> MediaFile:
    """One media file of the episode."""
    slug = episode_access(app, access, number, Permission.READ)
    return measured(app, app.media.get(slug, number, media_id))


def with_stored_measures(app: Services, media: MediaFile) -> MediaFile:
    """The file with the measures kept for its content, if any."""
    return media.model_copy(update={"measures": app.media.measures_of(media.sha256)})


def measured(app: Services, media: MediaFile) -> MediaFile:
    """The file with its measures; a video not measured yet is measured now and kept."""
    known = with_stored_measures(app, media)
    if known.measures is not None or not media.media_type.startswith(VIDEO_PREFIX):
        return known
    try:
        path = app.assets.path_of(media.stored())
    except AssetMissing:
        return known
    found = probe_video(path)
    if found is None:
        return known
    app.media.save_measures(media.sha256, found)
    return known.model_copy(update={"measures": found})


class RenderNotUploadable(ValueError):
    """A render arrives only from a render worker (rule project.render), never by upload."""

    def __init__(self) -> None:
        super().__init__(
            "A render cannot be uploaded; request one with request_render (POST .../renders)"
        )


def add_media(app: Services, access: ChannelAccess, new: NewMedia, source: BinaryIO) -> MediaFile:
    """Check, store and add a file to the episode; the gate decides before anything is stored."""
    if new.kind is MediaKind.RENDER:
        raise RenderNotUploadable
    episode_access(app, access, new.number, Permission.WRITE_CONTENT)
    placed = new.model_copy(update={"channel": access.channel.slug})
    added = gated(app, access, MediaOperation.CREATE, lambda: app.media.add(placed, source))
    return measured(app, added)


def rename_media(
    app: Services, access: ChannelAccess, change: MediaChange, data: MediaInput
) -> MediaFile:
    """Rename a file or change its note."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    media = app.media.get(slug, change.number, change.media_id)
    renamed = gated(
        app,
        access,
        MediaOperation.WRITE,
        lambda: app.media.replace(media, data, change.expected_revision),
    )
    return with_stored_measures(app, renamed)


def remove_media(app: Services, access: ChannelAccess, change: MediaChange) -> MediaFile:
    """Take a file out of the episode; returns what was removed."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    media = app.media.get(slug, change.number, change.media_id)
    gated(
        app,
        access,
        MediaOperation.DELETE,
        lambda: app.media.delete(media, change.expected_revision),
    )
    return media
