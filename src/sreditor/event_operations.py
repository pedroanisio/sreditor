"""Reading the change feed, shared by the REST route and the MCP tool (rule agentic.events)."""

from __future__ import annotations

from fastapi import status

from sreditor.deps import ChannelAccess, Services
from sreditor.events import CursorExpired, EventPage, EventQuery
from sreditor.members import Permission
from sreditor.pagination import INVALID_CURSOR, decode_cursor
from sreditor.problems import ProblemError
from sreditor.tokens import Scope

EXPIRED_CURSOR = (
    "That cursor is older than the %(days)d days of events kept, so some events were removed: "
    "call again without since to read the latest events and continue from its next_cursor, and "
    "re-read what you track, since changes in between are not listed"
)


def position_of(since: str | None) -> int | None:
    """The event id a cursor points past; an unreadable one is a 400 problem."""
    position = decode_cursor(since)
    if position is None:
        return None
    if not position.isdigit():
        raise ProblemError(status.HTTP_400_BAD_REQUEST, INVALID_CURSOR, "invalid-cursor")
    return int(position)


def may_see_waivers(access: ChannelAccess) -> bool:
    """Whether events about waivers, which are personal data, are for this caller too."""
    if not access.can(Permission.READ_WAIVERS):
        return False
    return access.token is None or access.token.allows(Scope.WAIVERS_READ)


def list_events(app: Services, access: ChannelAccess, since: str | None, limit: int) -> EventPage:
    """The channel's events after a cursor, oldest first; without one, the latest ones.

    Events about waivers are left out unless the caller may read waivers.
    """
    access.require(Permission.READ)
    query = EventQuery(after=position_of(since), limit=limit, has_waivers=may_see_waivers(access))
    try:
        return app.events.page(access.channel.slug, query)
    except CursorExpired as expired:
        detail = EXPIRED_CURSOR % {"days": expired.retention.days}
        raise ProblemError(status.HTTP_410_GONE, detail, "cursor-expired") from expired
