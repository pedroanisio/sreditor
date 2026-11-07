"""Waiver operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Waivers hold personal data (rule project.waivers): reading them needs a role that may read
waivers (owner or editor) and, for a token, the waivers:read scope; changing them needs that and
the editorial permission as well, since a change shows what the waiver says.
"""

from __future__ import annotations

from typing import BinaryIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor
from sreditor.waivers import NewWaiver, Waiver, WaiverInput


class WaiverOperation:
    """stmt operation names an agent's waiver writes are gated on."""

    CREATE = "waiver.create"
    WRITE = "waiver.write"
    DELETE = "waiver.delete"


class WaiverChange(BaseModel):
    """Which waiver a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    number: int
    waiver_id: UUID
    expected_revision: int | None = None


def writable(app: Services, access: ChannelAccess, number: int) -> str:
    """The channel's slug once the caller may both read and change the episode's waivers."""
    episode_access(app, access, number, Permission.READ_WAIVERS)
    access.require(Permission.WRITE_EDITORIAL)
    return access.channel.slug


def list_waivers(
    app: Services, access: ChannelAccess, number: int, cursor: str | None, limit: int
) -> Page[Waiver]:
    """The episode's waivers in the order they were added."""
    slug = episode_access(app, access, number, Permission.READ_WAIVERS)
    rows = app.waivers.waivers(slug, number, after_number(cursor), limit + 1)
    items = rows[:limit]
    has_more = len(rows) > limit
    cursor_out = encode_cursor(str(app.waivers.position(items[-1].id))) if has_more else None
    return Page[Waiver](items=items, next_cursor=cursor_out)


def get_waiver(app: Services, access: ChannelAccess, number: int, waiver_id: UUID) -> Waiver:
    """One waiver of the episode."""
    slug = episode_access(app, access, number, Permission.READ_WAIVERS)
    return app.waivers.get(slug, number, waiver_id)


def add_waiver(app: Services, access: ChannelAccess, new: NewWaiver, source: BinaryIO) -> Waiver:
    """Check, store and add a signed waiver; the gate decides before anything is stored."""
    slug = writable(app, access, new.number)
    placed = new.model_copy(update={"channel": slug})
    return gated(app, access, WaiverOperation.CREATE, lambda: app.waivers.add(placed, source))


def update_waiver(
    app: Services, access: ChannelAccess, change: WaiverChange, data: WaiverInput
) -> Waiver:
    """Change who signed a waiver, when, or what it covers."""
    slug = writable(app, access, change.number)
    waiver = app.waivers.get(slug, change.number, change.waiver_id)
    return gated(
        app,
        access,
        WaiverOperation.WRITE,
        lambda: app.waivers.replace(waiver, data, change.expected_revision),
    )


def remove_waiver(app: Services, access: ChannelAccess, change: WaiverChange) -> Waiver:
    """Take a waiver out of the episode; returns what was removed."""
    slug = writable(app, access, change.number)
    waiver = app.waivers.get(slug, change.number, change.waiver_id)
    gated(
        app,
        access,
        WaiverOperation.DELETE,
        lambda: app.waivers.delete(waiver, change.expected_revision),
    )
    return waiver
