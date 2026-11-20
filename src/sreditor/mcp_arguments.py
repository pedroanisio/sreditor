"""Tool errors for arguments that do not fit a tool's input schema (rule agentic.mcp.errors).

The MCP SDK validates a call's arguments before sreditor's code runs and answers a misfit with
Pydantic's own report, which names neither the arguments the operation takes nor the call that
finds a valid value. ToolRequests writes that answer instead, from the same validation error.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails

REQUEST = "request"
OPERATION = "operation"

MISSING = "missing"
NOT_AN_ARGUMENT = "extra_forbidden"

# Where the value of an argument agents often leave out or misname comes from.
ARGUMENT_SOURCES = {
    "slug": "`slug` is the channel's slug, which list_channels lists",
    "number": "`number` is the episode's number, which list_episodes lists",
}


def as_object(value: Any) -> dict[str, Any] | None:
    """The request as an object; the SDK also accepts one sent as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def argument_path(error: ErrorDetails, operation: str) -> str:
    """Where an error is, as the caller wrote it: without `request` and the operation's name."""
    parts = [str(part) for part in error["loc"]]
    for prefix in (REQUEST, operation):
        if parts and parts[0] == prefix:
            parts = parts[1:]
    return ".".join(parts) or REQUEST


def problem(error: ErrorDetails, operation: str) -> str:
    """One thing wrong with the arguments, naming the argument."""
    path = argument_path(error, operation)
    if error["type"] == MISSING:
        return f"`{path}` is required"
    if error["type"] == NOT_AN_ARGUMENT:
        return f"`{path}` is not an argument of {operation}"
    return f"`{path}`: {error['msg']}"


def accepted_arguments(model: type[BaseModel]) -> str:
    """The arguments an operation takes, the required ones marked."""
    names = [
        f"`{name}` (required)" if field.is_required() else f"`{name}`"
        for name, field in model.model_fields.items()
        if name != OPERATION
    ]
    return ", ".join(names) or "no arguments"


class ToolRequests(BaseModel):
    """A served tool's operations with their request models, and every operation's tool."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool: str
    members: dict[str, type[BaseModel]]
    tool_of: dict[str, str]

    def explain(self, arguments: dict[str, Any], error: ValidationError) -> str:
        """What was wrong with a call's arguments and the call that fixes it."""
        request = as_object(arguments.get(REQUEST))
        if request is None:
            return (
                f"{self.tool} takes one argument, `request`: an object with the operation's "
                f"name in `operation` and that operation's arguments beside it. {self.listed()}"
            )
        operation = request.get(OPERATION)
        model = self.members.get(operation) if isinstance(operation, str) else None
        if model is None:
            return self.unknown_operation(operation)
        return self.misfit(str(operation), model, error)

    def listed(self) -> str:
        """The tool's operations by name."""
        return f"Operations of {self.tool}: {', '.join(self.members)}."

    def unknown_operation(self, operation: Any) -> str:
        """Which tool has the operation, or which operations this tool has."""
        home = self.tool_of.get(operation) if isinstance(operation, str) else None
        if home is not None:
            return (
                f"{operation} is an operation of {home}, not of {self.tool}: call {home} with "
                "the same `request`."
            )
        return f"`request.operation` must name an operation of the tool called. {self.listed()}"

    def misfit(self, operation: str, model: type[BaseModel], error: ValidationError) -> str:
        """Each argument at fault, what the operation takes and where missing values come from."""
        errors = error.errors(include_url=False, include_input=False)
        problems = "; ".join(problem(each, operation) for each in errors)
        absent = {argument_path(each, operation) for each in errors if each["type"] == MISSING}
        sources = [text for name, text in ARGUMENT_SOURCES.items() if name in absent]
        return " ".join(
            [
                f"{operation} did not run: {problems}.",
                f"{operation} takes {accepted_arguments(model)}.",
                *(f"{source}." for source in sources),
            ]
        )
