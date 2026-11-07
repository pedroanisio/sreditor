"""Read statements from a stmt store through its JSON command protocol."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL = 1


class Owner(BaseModel):
    """Who a stmt write is recorded as: an owner type and id."""

    model_config = ConfigDict(frozen=True)

    type: str
    id: str


class Trigger(BaseModel):
    """When a rule fires: an event kind, and optionally its phase and operation."""

    model_config = ConfigDict(frozen=True, extra="allow")

    kind: str
    phase: str | None = None
    operation: str | None = None


class Rule(BaseModel):
    """A stmt rule: its effect, the trigger that fires it and an optional action."""

    model_config = ConfigDict(frozen=True, extra="allow")

    effect: str
    trigger: Trigger
    action: dict[str, Any] | None = None


class Statement(BaseModel):
    """The enabled head revision of one statement, as `stmt list` returns it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    revision: int
    scope: str
    statement: str
    owner: Owner
    category: str
    priority: int
    conditionals: list[str] = Field(default_factory=list)
    rule: Rule | None = None
    override_key: str | None = None
    ttl_seconds: int | None = None
    evidence: list[Any] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    enabled: bool = True

    def document(self) -> dict[str, Any]:
        """The authoring document that recreates this revision with `create` or `revise`."""
        return self.model_dump(mode="json", exclude={"id", "revision", "scope"}, exclude_none=True)


class StmtError(BaseModel):
    """The error stmt reports when a command fails."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class Envelope(BaseModel):
    """The JSON envelope every stmt command answers with."""

    model_config = ConfigDict(frozen=True)

    protocol: int
    ok: bool
    result: Any = None
    error: StmtError | None = None


class Decision(BaseModel):
    """The outcome of `stmt gate` or `stmt emit` for one event."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str
    decision: str  # ALLOW, BLOCK or INDETERMINATE for a gate; ACCEPT for an emit
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class StoreSettings(BaseModel):
    """Where the store lives and which boundary to read."""

    model_config = ConfigDict(frozen=True)

    stmt: str = "stmt"
    db: str
    domain: str
    scope: str = "/"


class StoreError(RuntimeError):
    """A stmt command failed. `code` is stmt's error code when it returned one."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


Runner = Callable[[Sequence[str], dict[str, str]], "subprocess.CompletedProcess[str]"]


def run_process(args: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its output without raising, so callers read the envelope."""
    return subprocess.run(list(args), env=env, capture_output=True, text=True, check=False)


class StmtClient:
    """Calls the `stmt` command line, which owns the rule store and decides what agents may do."""

    def __init__(self, settings: StoreSettings, runner: Runner = run_process) -> None:
        self.settings = settings
        self._runner = runner

    def _call(self, *args: str, scope: str | None = None) -> Any:
        env = {
            **os.environ,
            "STMT_DB": self.settings.db,
            "STMT_DOMAIN": self.settings.domain,
            "STMT_SCOPE": scope or self.settings.scope,
        }
        try:
            done = self._runner([self.settings.stmt, *args], env)
        except OSError as failure:
            raise StoreError(f"Cannot run {self.settings.stmt!r}: {failure}") from failure
        try:
            envelope = Envelope.model_validate_json(done.stdout)
        except ValidationError as invalid:
            detail = done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}"
            raise StoreError(f"stmt {args[0]} returned no valid envelope: {detail}") from invalid
        if envelope.protocol != PROTOCOL:
            raise StoreError(f"Unsupported stmt protocol {envelope.protocol}")
        if not envelope.ok:
            error = envelope.error or StmtError(code="UNKNOWN", message="no error detail")
            raise StoreError(f"stmt {args[0]} failed: {error.code}: {error.message}", error.code)
        return envelope.result

    def statements(self) -> list[Statement]:
        """Return the enabled, unexpired head of every statement visible from the scope."""
        result = self._call("list")
        try:
            return [Statement.model_validate(item) for item in result]
        except (TypeError, ValidationError) as invalid:
            raise StoreError(f"Unexpected statement list: {invalid}") from invalid

    def resolve(self, scope: str, context: dict[str, Any] | None = None) -> list[Statement]:
        """Return the statements that apply at a scope, after overrides."""
        result = self._call("resolve", "--context", json.dumps(context or {}), scope=scope)
        try:
            return [Statement.model_validate(item) for item in result["matched"]]
        except (TypeError, KeyError, ValidationError) as invalid:
            raise StoreError(f"Unexpected resolve result: {invalid}") from invalid

    def add_scope(self, scope: str) -> None:
        """Register a scope. Adding an existing scope is a no-op."""
        self._call("scope-add", "--scope", scope)

    def get(self, statement_id: str, scope: str) -> Statement | None:
        """Return a statement's head as seen from a scope, or None when it is not visible."""
        try:
            result = self._call("get", statement_id, scope=scope)
        except StoreError as error:
            if error.code == "NOT_FOUND":
                return None
            raise
        return Statement.model_validate(result)

    def write(
        self, statement_id: str, document: dict[str, Any], scope: str, revision: int | None = None
    ) -> int:
        """Create a statement, or revise it when `revision` names the head it replaces.

        Returns the new revision number.
        """
        payload = json.dumps(document)
        if revision is None:
            result = self._call("create", statement_id, "--json", payload, scope=scope)
        else:
            args = ("revise", statement_id, "--if-revision", str(revision), "--json", payload)
            result = self._call(*args, scope=scope)
        return int(result["revision"])

    def gate(self, scope: str, request: dict[str, Any]) -> Decision:
        """Ask for a decision before an operation (`stmt gate`). BLOCK is a result, not an error."""
        return Decision.model_validate(
            self._call("gate", "--json", json.dumps(request), scope=scope)
        )

    def emit(self, scope: str, event: dict[str, Any]) -> Decision:
        """Record what happened after an operation (`stmt emit`)."""
        return Decision.model_validate(self._call("emit", "--json", json.dumps(event), scope=scope))
