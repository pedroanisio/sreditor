"""The change feed route: what changed in a channel since a cursor (rule agentic.events)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from sreditor import event_operations
from sreditor.deps import Access, ServicesDep
from sreditor.events import EventPage
from sreditor.pagination import DEFAULT_LIMIT, Limit

router = APIRouter(tags=["events"])

Since = Annotated[
    str | None,
    Query(
        description="The `next_cursor` of the previous call; omit it to read the latest events "
        "and get a cursor to continue from."
    ),
]


@router.get("/channels/{slug}/events", summary="List what changed in a channel since a cursor")
def list_events(
    access: Access, app: ServicesDep, since: Since = None, limit: Limit = DEFAULT_LIMIT
) -> EventPage:
    """List the channel's events oldest first after `since`, to react to changes (a render
    finished, a script was revised) without polling every resource. Each event names its
    operation, the episode or item it concerns (`episode_number`, `item_id`, `status`), the
    actor and `read_with`, the MCP tool that reads the item; it never carries what people
    wrote. Keep `next_cursor`, which is always set, and pass it as `since` next time; call again
    at once while `has_more` is true. Without `since` you get the latest events. A cursor older
    than the retention period (30 days by default) answers 410: call without `since` and re-read
    what you track. Events about waivers need the waivers:read scope.
    """
    return event_operations.list_events(app, access, since, limit)
