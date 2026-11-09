"""A channel's Editorial: its purpose and guidelines, kept as statements in the stmt store.

Each channel owns the scope `<base>/<slug>`, and each of its platforms `<base>/<slug>/<platform>`.
Resolving at a platform scope inherits house rules from the base scope and the channel's rules,
and a platform may override the single-valued kinds (purpose, voice, audience).
"""

from __future__ import annotations

import re
import secrets
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.channels import Channel, Platform
from sreditor.field_types import ChannelSlug
from sreditor.store import Owner, Statement, StmtClient, StoreError

CATEGORY_PREFIX = "editorial."
# The longest guideline stmt stores, in code points.
GUIDELINE_LENGTH = 512
# stmt priorities run from 0 (lowest) to 4; guidelines sit in the middle unless told otherwise.
LOWEST_PRIORITY = 0
HIGHEST_PRIORITY = 4
DEFAULT_PRIORITY = 2
# A guideline cites the statement it copies as one stmt evidence item: ref `stmt:<id>`, locator
# `revision <n>` (the revision the copy follows), so stmt keeps both with the guideline itself.
CITATION_PREFIX = "stmt:"
REVISION_LOCATOR = "revision "
REVISION_PATTERN = re.compile(rf"^{REVISION_LOCATOR}(\d+)$")
# The shape of a stmt id (stmt's own rule); the cited statement must also exist and be enabled.
STATEMENT_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$"


class GuidelineKind(StrEnum):
    """What a guideline is about; the name after `editorial.` in its stmt category."""

    PURPOSE = "purpose"
    VOICE = "voice"
    AUDIENCE = "audience"
    PILLAR = "pillar"
    AVOID = "avoid"


# Kinds with one winner per scope chain; the deepest scope overrides the others.
SINGLE_KINDS = frozenset({GuidelineKind.PURPOSE, GuidelineKind.VOICE, GuidelineKind.AUDIENCE})


class Citation(BaseModel):
    """The statement a guideline copies and the revision of it that the copy follows."""

    model_config = ConfigDict(frozen=True)

    statement_id: str
    revision: int


class Guideline(BaseModel):
    """One editorial statement, with the scope that owns it."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: GuidelineKind
    text: str
    scope: str
    revision: int
    citation: Citation | None = None


class DriftKind(StrEnum):
    """Why a copied guideline needs a look: the statement changed, is gone or cannot be read."""

    CHANGED = "changed"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class DriftWarning(BaseModel):
    """A guideline whose cited statement no longer matches its copy (rule project.editorial.drift)."""

    model_config = ConfigDict(frozen=True)

    guideline_id: str
    kind: DriftKind
    statement_id: str
    copy_revision: int
    statement_revision: int | None
    copy_text: str
    statement_text: str | None
    detail: str


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
    warnings: list[DriftWarning] = Field(default_factory=list)

    def all_guidelines(self) -> list[Guideline]:
        """Every guideline held, whatever its kind."""
        singles = (self.purpose, self.voice, self.audience)
        return [*(item for item in singles if item is not None), *self.pillars, *self.avoid]


class GuidelineInput(BaseModel):
    """A guideline to set or add, at the channel's level or a platform's."""

    model_config = ConfigDict(frozen=True)

    kind: GuidelineKind
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=GUIDELINE_LENGTH)
    ]
    platform: Platform | None = None
    priority: int = Field(default=DEFAULT_PRIORITY, ge=LOWEST_PRIORITY, le=HIGHEST_PRIORITY)
    cited_statement_id: Annotated[str, Field(pattern=STATEMENT_ID_PATTERN)] | None = None


class GuidelineNotFound(LookupError):
    """No editorial statement with that id is owned by the requested scope."""


class GuidelineChanged(ValueError):
    """The guideline moved on from the revision the caller expected."""

    def __init__(self, statement_id: str, current: int) -> None:
        super().__init__(statement_id)
        self.current = current


class CitedStatementNotFound(LookupError):
    """The statement a guideline cites does not exist, or has been retired."""


class GuidelineNotCited(ValueError):
    """The guideline cites no statement, so there is no text to take."""


def citation_of(evidence: list[Any]) -> Citation | None:
    """The citation among a statement's evidence items, or None when it has none."""
    for item in evidence:
        ref = item.get("ref", "") if isinstance(item, dict) else ""
        if not ref.startswith(CITATION_PREFIX):
            continue
        match = REVISION_PATTERN.match(item.get("locator", ""))
        if match:
            return Citation(
                statement_id=ref.removeprefix(CITATION_PREFIX), revision=int(match.group(1))
            )
    return None


def with_citation(evidence: list[Any], citation: Citation | None) -> list[Any]:
    """The evidence with its citation replaced by `citation` (or dropped when None)."""
    kept = [
        item
        for item in evidence
        if not (isinstance(item, dict) and str(item.get("ref", "")).startswith(CITATION_PREFIX))
    ]
    if citation is None:
        return kept
    item = {
        "ref": f"{CITATION_PREFIX}{citation.statement_id}",
        "locator": f"{REVISION_LOCATOR}{citation.revision}",
    }
    return [*kept, item]


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
        citation=citation_of(statement.evidence),
    )


def drift_warning(
    guideline: Guideline, found: Statement | StoreError | None
) -> DriftWarning | None:
    """The warning for a guideline against what stmt answered for its cited statement, if any."""
    citation = guideline.citation
    if citation is None:
        return None
    statement = found if isinstance(found, Statement) and found.enabled else None
    kind, detail = DriftKind.CHANGED, ""
    if isinstance(found, StoreError):
        kind = DriftKind.UNAVAILABLE
        detail = f"Statement {citation.statement_id} could not be read: stmt is unavailable."
    elif statement is None:
        kind = DriftKind.MISSING
        detail = f"Statement {citation.statement_id} no longer exists or was retired."
    elif statement.statement.strip() == guideline.text.strip():
        return None
    else:
        detail = (
            f"Statement {citation.statement_id} is at revision {statement.revision}, "
            f"this copy follows revision {citation.revision}."
        )
    return DriftWarning(
        guideline_id=guideline.id,
        kind=kind,
        statement_id=citation.statement_id,
        copy_revision=citation.revision,
        statement_revision=statement.revision if statement else None,
        copy_text=guideline.text,
        statement_text=statement.statement if statement else None,
        detail=detail,
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

    def cited(self, statement_id: str, scope: str) -> Statement:
        """The enabled statement a guideline cites, as seen from the scope that owns it."""
        found = self.client.get(statement_id, scope)
        if found is None or not found.enabled:
            raise CitedStatementNotFound(statement_id)
        return found

    def with_drift(self, editorial: Editorial, channel: Channel) -> Editorial:
        """The Editorial with a warning for each guideline whose cited statement differs.

        An unreadable stmt never breaks the read: it becomes an `unavailable` warning.
        """
        answers: dict[str, Statement | StoreError | None] = {}
        warnings = []
        for guideline in editorial.all_guidelines():
            if guideline.citation is None:
                continue
            statement_id = guideline.citation.statement_id
            if statement_id not in answers:
                answers[statement_id] = self._answer(statement_id, channel, editorial.platform)
            found = drift_warning(guideline, answers[statement_id])
            if found is not None:
                warnings.append(found)
        return editorial.model_copy(update={"warnings": warnings})

    def _answer(
        self, statement_id: str, channel: Channel, platform: Platform | None
    ) -> Statement | StoreError | None:
        try:
            return self.client.get(statement_id, self.scope(channel.slug, platform))
        except StoreError as unavailable:
            return unavailable

    def _document(self, data: GuidelineInput, owner: Owner, scope: str) -> dict[str, Any]:
        """The stmt document for a guideline, citing its statement at the revision read now."""
        citation = None
        if data.cited_statement_id is not None:
            cited = self.cited(data.cited_statement_id, scope)
            citation = Citation(statement_id=cited.id, revision=cited.revision)
        return {
            "statement": data.text,
            "owner": owner.model_dump(),
            "category": f"{CATEGORY_PREFIX}{data.kind.value}",
            "priority": data.priority,
            "evidence": with_citation([], citation),
        }

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
        document = self._document(data, owner or self.owner, scope)
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
            id=statement_id,
            kind=data.kind,
            text=data.text,
            scope=scope,
            revision=new_revision,
            citation=citation_of(document["evidence"]),
        )

    def take_cited_text(
        self,
        channel: Channel,
        statement_id: str,
        platform: Platform | None = None,
        owner: Owner | None = None,
        expected_revision: int | None = None,
    ) -> Guideline:
        """Revise a guideline to the current text of the statement it cites.

        An ordinary revision by the person (or token) that asks, so the history keeps the old
        copy; nothing ever does this on its own.
        """
        scope = self.scope(channel.slug, platform)
        existing = self.client.get(statement_id, scope)
        guideline = to_guideline(existing) if existing is not None else None
        if existing is None or existing.scope != scope or guideline is None:
            raise GuidelineNotFound(statement_id)
        if expected_revision is not None and expected_revision != existing.revision:
            raise GuidelineChanged(statement_id, existing.revision)
        if guideline.citation is None:
            raise GuidelineNotCited(statement_id)
        cited = self.cited(guideline.citation.statement_id, scope)
        citation = Citation(statement_id=cited.id, revision=cited.revision)
        document = {
            **existing.document(),
            "statement": cited.statement,
            "evidence": with_citation(existing.evidence, citation),
        }
        if owner is not None:
            document["owner"] = owner.model_dump()
        new_revision = self.client.write(statement_id, document, scope, existing.revision)
        return guideline.model_copy(
            update={"text": cited.statement, "revision": new_revision, "citation": citation}
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
