"""Screenplay operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Reading needs the read permission; writing the screenplay is content work, open to contributors
and the content:write scope.
"""

from __future__ import annotations

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.screenplay import Screenplay, ScreenplayInput

# The stmt operation an agent's screenplay writes are gated on.
SCREENPLAY_WRITE = "screenplay.write"


def get_screenplay(app: Services, access: ChannelAccess, number: int) -> Screenplay:
    """The episode's screenplay, with its scenes, characters and revision."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.screenplays.screenplay(slug, number)


def write_screenplay(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: ScreenplayInput,
    expected_revision: int | None,
) -> Screenplay:
    """Replace the episode's screenplay."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    return gated(
        app,
        access,
        SCREENPLAY_WRITE,
        lambda: app.screenplays.write(slug, number, data, expected_revision),
    )
