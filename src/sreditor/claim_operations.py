"""Claim operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Reading needs the read permission; writing a claim and checking its sources is content work,
open to contributors and the content:write scope. A check records who made it: the user, or the
API token an agent acted through (rule agentic.audit). Claims only warn (rule project.claims).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.claims import (
    CheckRecord,
    Claim,
    ClaimInput,
    ClaimWarnings,
    SourceNotAvailable,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.members import Permission
from sreditor.operations import gated, owner_of
from sreditor.pagination import Page
from sreditor.pitch_operations import next_cursor
from sreditor.research import SourceNotFound


class ClaimOperation:
    """stmt operation names an agent's claim writes are gated on."""

    CREATE = "claim.create"
    WRITE = "claim.write"
    CHECK = "claim.check"
    DELETE = "claim.delete"


class ClaimQuery(BaseModel):
    """Which page of an episode's claims to list."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str | None = None
    limit: int


class ClaimChange(BaseModel):
    """Which claim of which episode a change targets, and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    number: int
    claim_id: UUID
    expected_revision: int | None = None


class SourceCheck(BaseModel):
    """Which backing source of a claim to mark checked, or to take the mark back from."""

    model_config = ConfigDict(frozen=True)

    change: ClaimChange
    source_id: UUID
    is_checked: bool = True


def is_available(app: Services, channel: str, number: int, source_id: UUID) -> bool:
    """Whether the source is the episode's own or belongs to one of the pitches it uses."""
    try:
        app.research.source(channel, number, source_id)
    except SourceNotFound:
        pass
    else:
        return True
    for pitch_id in app.pitches.episode_pitches(channel, number).pitch_ids:
        try:
            app.pitches.source(pitch_id, source_id)
        except SourceNotFound:
            continue
        return True
    return False


def require_available(app: Services, channel: str, number: int, data: ClaimInput) -> None:
    """Refuse a claim backed by a source that is not the episode's or one of its pitches'."""
    for backing in data.sources:
        if not is_available(app, channel, number, backing.source_id):
            raise SourceNotAvailable(str(backing.source_id))


def list_claims(app: Services, access: ChannelAccess, query: ClaimQuery) -> Page[Claim]:
    """The episode's claims in the order they were added."""
    slug = episode_access(app, access, query.number, Permission.READ)
    rows = app.claims.page(slug, query.number, after_number(query.cursor), query.limit + 1)
    items = rows[: query.limit]
    last = app.claims.position(items[-1].id) if items else None
    return Page[Claim](items=items, next_cursor=next_cursor(rows, query.limit, last))


def episode_of_claim(app: Services, access: ChannelAccess, claim_id: UUID) -> int:
    """The number of the episode that holds the claim, for callers that only have its id."""
    access.require(Permission.READ)
    return app.claims.number_of(access.channel.slug, claim_id)


def get_claim(app: Services, access: ChannelAccess, number: int, claim_id: UUID) -> Claim:
    """One claim, with the revision to send back when changing it."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.claims.get(slug, number, claim_id)


def unchecked_claims(app: Services, access: ChannelAccess, number: int) -> ClaimWarnings:
    """The episode's claims that have no checked primary source: a warning, never a block."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.claims.unchecked(slug, number)


def count_claims(app: Services, access: ChannelAccess, number: int) -> int:
    """How many claims the episode has."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.claims.count(slug, number)


def add_claim(app: Services, access: ChannelAccess, number: int, data: ClaimInput) -> Claim:
    """Add a claim to the episode, tied to sources of the episode or of its pitches."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    require_available(app, slug, number, data)
    return gated(app, access, ClaimOperation.CREATE, lambda: app.claims.add(slug, number, data))


def replace_claim(
    app: Services, access: ChannelAccess, change: ClaimChange, data: ClaimInput
) -> Claim:
    """Replace a claim's quote, place and backing sources; kept sources stay as checked."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    claim = app.claims.get(slug, change.number, change.claim_id)
    require_available(app, slug, change.number, data)
    return gated(
        app,
        access,
        ClaimOperation.WRITE,
        lambda: app.claims.replace(claim, data, change.expected_revision),
    )


def check_claim_source(app: Services, access: ChannelAccess, check: SourceCheck) -> Claim:
    """Record that a backing source was checked, by this user or token, or take the mark back."""
    change = check.change
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    claim = app.claims.get(slug, change.number, change.claim_id)
    record = CheckRecord(
        is_checked=check.is_checked,
        checked_by=owner_of(access.user, access.token) if check.is_checked else None,
    )
    return gated(
        app,
        access,
        ClaimOperation.CHECK,
        lambda: app.claims.check(claim, check.source_id, record, change.expected_revision),
    )


def remove_claim(app: Services, access: ChannelAccess, change: ClaimChange) -> None:
    """Remove a claim and its backing sources."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    claim = app.claims.get(slug, change.number, change.claim_id)
    gated(
        app,
        access,
        ClaimOperation.DELETE,
        lambda: app.claims.delete(claim, change.expected_revision),
    )
