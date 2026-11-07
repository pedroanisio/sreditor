"""A channel's Editorial: its purpose and guidelines, kept as statements in the stmt store.

Each channel owns the scope `<base>/<slug>`, and each of its platforms `<base>/<slug>/<platform>`.
Resolving at a platform scope inherits house rules from the base scope and the channel's rules,
and a platform may override the single-valued kinds (purpose, voice, audience).
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.channels import Channel, Platform
from sreditor.field_types import ChannelSlug
from sreditor.store import Owner, Statement, StmtClient

CATEGORY_PREFIX = "editorial."
# The longest guideline stmt stores, in code points.
GUIDELINE_LENGTH = 512
# stmt priorities run from 0 (lowest) to 4; guidelines sit in the middle unless told otherwise.
LOWEST_PRIORITY = 0
HIGHEST_PRIORITY = 4
DEFAULT_PRIORITY = 2


class GuidelineKind(StrEnum):
    """What a guideline is about; the name after `editorial.` in its stmt category."""

    PURPOSE = "purpose"
    VOICE = "voice"
    AUDIENCE = "audience"
    PILLAR = "pillar"
    AVOID = "avoid"


# Kinds with one winner per scope chain; the deepest scope overrides the others.
SINGLE_KINDS = frozenset({GuidelineKind.PURPOSE, GuidelineKind.VOICE, GuidelineKind.AUDIENCE})


class Guideline(BaseModel):
    """One editorial statement, with the scope that owns it."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: GuidelineKind
    text: str
    scope: str
    revision: int


class Editorial(BaseModel):
    """The guidelines that apply to a channel, or to one of its platforms."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    platform: Platform | None
    purpose: Guideline | None = None
    voice: Guideline | None = None
    audience: Guideline | None = None
    pillars: list[Guideline] = Field(default_factory=list)
    avoid: list[Guideline] = Field(default_factory=list)


class GuidelineInput(BaseModel):
    """A guideline to set or add, at the channel's level or a platform's."""

    model_config = ConfigDict(frozen=True)

    kind: GuidelineKind
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=GUIDELINE_LENGTH)
    ]
    platform: Platform | None = None
    priority: int = Field(default=DEFAULT_PRIORITY, ge=LOWEST_PRIORITY, le=HIGHEST_PRIORITY)


class GuidelineNotFound(LookupError):
    """No editorial statement with that id is owned by the requested scope."""


class GuidelineChanged(ValueError):
    """The guideline moved on from the revision the caller expected."""

    def __init__(self, statement_id: str, current: int) -> None:
        super().__init__(statement_id)
        self.current = current


def to_guideline(statement: Statement) -> Guideline | None:
    """The guideline a statement holds, or None when it is not an editorial statement."""
    if not statement.category.startswith(CATEGORY_PREFIX):
        return None
    try:
        kind = GuidelineKind(statement.category.removeprefix(CATEGORY_PREFIX))
    except ValueError:
        return None
    return Guideline(
        id=statement.id,
        kind=kind,
        text=statement.statement,
        scope=statement.scope,
        revision=statement.revision,
    )


def assemble(slug: str, platform: Platform | None, guidelines: list[Guideline]) -> Editorial:
    """The Editorial from resolved guidelines: lists for pillars and avoid, one of each other."""
    singles = {
        guideline.kind.value: guideline
        for guideline in guidelines
        if guideline.kind in SINGLE_KINDS
    }
    return Editorial.model_validate(
        {
            "channel": slug,
            "platform": platform,
            "pillars": [item for item in guidelines if item.kind is GuidelineKind.PILLAR],
            "avoid": [item for item in guidelines if item.kind is GuidelineKind.AVOID],
            **singles,
        }
    )


class EditorialService:
    """Reads and writes a channel's guidelines as statements in its stmt scopes."""

    def __init__(self, client: StmtClient, owner: Owner) -> None:
        self.client = client
        self.owner = owner

    def scope(self, slug: str, platform: Platform | None = None) -> str:
        """The stmt scope of a channel, or of one of its platforms."""
        parts = [self.client.settings.scope.rstrip("/"), slug]
        if platform is not None:
            parts.append(platform.value)
        return "/".join(parts)

    def register(self, channel: Channel) -> None:
        """Create the stmt scopes for a channel and its platforms. Safe to repeat."""
        self.client.add_scope(self.scope(channel.slug))
        for account in channel.accounts:
            self.client.add_scope(self.scope(channel.slug, account.platform))

    def editorial(self, channel: Channel, platform: Platform | None = None) -> Editorial:
        """The guidelines that apply at a level, after stmt resolves its overrides."""
        resolved = self.client.resolve(self.scope(channel.slug, platform))
        guidelines = [found for found in map(to_guideline, resolved) if found is not None]
        return assemble(channel.slug, platform, guidelines)

    def _id_prefix(self, slug: str, platform: Platform | None) -> str:
        return f"{slug}.{platform.value}" if platform else slug

    def write(
        self,
        channel: Channel,
        data: GuidelineInput,
        owner: Owner | None = None,
        expected_revision: int | None = None,
    ) -> Guideline:
        """Set a single-valued kind at the scope, or add a new pillar or avoid guideline.

        `owner` is recorded as the statement's author; it defaults to the service owner. With
        `expected_revision`, a single-valued kind is set only if it is still at that revision
        (0: not set yet), so nobody overwrites a text they have not read.
        """
        scope = self.scope(channel.slug, data.platform)
        self.register(channel)
        self.client.add_scope(scope)
        prefix = self._id_prefix(channel.slug, data.platform)
        document = {
            "statement": data.text,
            "owner": (owner or self.owner).model_dump(),
            "category": f"{CATEGORY_PREFIX}{data.kind.value}",
            "priority": data.priority,
        }
        revision: int | None = None
        if data.kind in SINGLE_KINDS:
            statement_id = f"{prefix}.{data.kind.value}"
            document["override_key"] = f"{CATEGORY_PREFIX}{data.kind.value}"
            existing = self.client.get(statement_id, scope)
            if existing is not None and existing.scope == scope:
                revision = existing.revision
            if expected_revision is not None and expected_revision != (revision or 0):
                raise GuidelineChanged(statement_id, revision or 0)
        else:
            statement_id = f"{prefix}.{data.kind.value}.{secrets.token_hex(4)}"
        new_revision = self.client.write(statement_id, document, scope, revision)
        return Guideline(
            id=statement_id, kind=data.kind, text=data.text, scope=scope, revision=new_revision
        )

    def retire(
        self,
        channel: Channel,
        statement_id: str,
        platform: Platform | None = None,
        owner: Owner | None = None,
        expected_revision: int | None = None,
    ) -> None:
        """Disable a guideline owned by the channel's (or platform's) scope.

        With `expected_revision`, refuse when the guideline changed since the caller read it.
        """
        scope = self.scope(channel.slug, platform)
        existing = self.client.get(statement_id, scope)
        if existing is None or existing.scope != scope or to_guideline(existing) is None:
            raise GuidelineNotFound(statement_id)
        if expected_revision is not None and expected_revision != existing.revision:
            raise GuidelineChanged(statement_id, existing.revision)
        if not existing.enabled:
            return
        document = {**existing.document(), "enabled": False}
        if owner is not None:
            document["owner"] = owner.model_dump()
        self.client.write(statement_id, document, scope, existing.revision)
