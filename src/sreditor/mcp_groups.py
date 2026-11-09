"""MCP tools grouped by outcome (rule agentic.mcp.tools.outcomes).

Each tool groups the operations of one task: reading an episode, editing its research, managing
a channel and so on. An operation keeps the name, arguments and result it had as a tool of its
own, so every message, readiness item or search hit that names one still points at the right
call: the agent calls the tool whose `operations` list holds that name, with the operation's
arguments inside `request`. The operations are registered on a RecordingServer that is never
served; build() wraps them in the grouped tools the clients see.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from typing import Annotated, Any, Literal, Union, get_args, get_origin, override

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, create_model

REQUEST = "request"
OPERATION = "operation"
RESULT = "result"


class AddedTool(BaseModel):
    """What an operation was registered with, kept so its group can call and describe it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    function: Callable[..., Any]
    title: str | None
    annotations: ToolAnnotations | None


class RecordingServer(MCPServer):
    """A server that only records the tools added to it; their groups are what clients see."""

    def __init__(self) -> None:
        super().__init__(name="sreditor-operations")
        self.added: dict[str, AddedTool] = {}

    @override
    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        """Remember the operation under its name instead of serving it."""
        self.added[name or fn.__name__] = AddedTool(
            function=fn, title=title, annotations=annotations
        )


class ToolGroup(BaseModel):
    """One served tool: its name, title, what it is for and the operations it groups."""

    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    purpose: str
    operations: tuple[str, ...]

    def description(self) -> str:
        """The tool's description: its purpose, how to call it and its operations by name."""
        return (
            f"{self.purpose} Put the operation's name in `request.operation` and its arguments "
            "beside it in `request`; each operation's description in the input schema says what "
            "it does, what it needs and which scope. The result is under `result`. Operations: "
            f"{', '.join(self.operations)}."
        )


def camel(name: str) -> str:
    """snake_case to CamelCase, for the request model of an operation."""
    return "".join(part.capitalize() for part in name.split("_"))


def takes_context(parameter: inspect.Parameter) -> bool:
    """Whether a parameter receives the MCP Context rather than an argument."""
    return inspect.isclass(parameter.annotation) and issubclass(parameter.annotation, Context)


def signature_of(added: AddedTool) -> inspect.Signature:
    """The operation's signature with its annotations evaluated."""
    return inspect.signature(added.function, eval_str=True)


def request_model(name: str, added: AddedTool) -> type[BaseModel]:
    """A model of one operation's arguments, told apart from the others by `operation`."""
    fields: dict[str, Any] = {OPERATION: (Literal[name], ...)}
    for parameter in signature_of(added).parameters.values():
        if takes_context(parameter):
            continue
        required = parameter.default is inspect.Parameter.empty
        fields[parameter.name] = (parameter.annotation, ... if required else parameter.default)
    model: type[BaseModel] = create_model(
        f"{camel(name)}Request",
        __config__=ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True),
        __doc__=inspect.cleandoc(added.function.__doc__ or name),
        **fields,
    )
    return model


def result_type(added: AddedTool) -> Any:
    """The type an operation's result validates against; unwraps Annotated[CallToolResult, X]."""
    annotation = signature_of(added).return_annotation
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        if isinstance(base, type) and issubclass(base, CallToolResult) and metadata:
            return metadata[0]
    return annotation


def union_of(types: Iterable[Any]) -> Any:
    """One type for several, or the type itself when there is one."""
    unique = tuple(dict.fromkeys(types))
    # The members are only known when the operations are registered, so the union is built here.
    return unique[0] if len(unique) == 1 else Union[unique]  # noqa: UP007


def combined_annotations(title: str, hints: Sequence[ToolAnnotations | None]) -> ToolAnnotations:
    """The worst case of the operations' hints (rule agentic.mcp.annotations)."""
    known = [hint for hint in hints if hint is not None]
    return ToolAnnotations(
        title=title,
        readOnlyHint=all(hint.read_only_hint for hint in known),
        destructiveHint=any(hint.destructive_hint for hint in known),
        idempotentHint=all(hint.idempotent_hint for hint in known),
        openWorldHint=any(hint.open_world_hint for hint in known),
    )


def as_result(value: Any) -> CallToolResult:
    """An operation's result under `result`, keeping a replay mark in `_meta`."""
    if isinstance(value, CallToolResult):
        return value.model_copy(update={"structured_content": {RESULT: value.structured_content}})
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    text = value.model_dump_json() if isinstance(value, BaseModel) else str(value)
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content={RESULT: data}
    )


def call_operation(added: AddedTool, context: Context, request: BaseModel) -> Any:
    """Call the operation with the request's arguments and, when it takes one, the context."""
    arguments: dict[str, Any] = {
        field: getattr(request, field) for field in type(request).model_fields if field != OPERATION
    }
    for parameter in signature_of(added).parameters.values():
        if takes_context(parameter):
            arguments[parameter.name] = context
    return added.function(**arguments)


def group_function(group: ToolGroup, added: dict[str, AddedTool]) -> Callable[..., CallToolResult]:
    """The served function of a group: it validates `request` and runs that operation."""
    members = {name: request_model(name, added[name]) for name in group.operations}
    request_type: Any = union_of(members.values())
    if len(members) > 1:
        request_type = Annotated[request_type, Field(discriminator=OPERATION)]
    # A model of its own, so one result type and several come back the same way, under `result`.
    output: type[BaseModel] = create_model(
        f"{camel(group.name)}Result",
        __config__=ConfigDict(frozen=True, arbitrary_types_allowed=True),
        result=(union_of(result_type(added[name]) for name in group.operations), ...),
    )

    def run(context: Context, request: BaseModel) -> CallToolResult:
        operation = added[getattr(request, OPERATION)]
        return as_result(call_operation(operation, context, request))

    run.__name__ = group.name
    run.__doc__ = group.description()
    returned = Annotated[CallToolResult, output]
    # The request and result types are built from the operations, so the signature is set here
    # for the MCP server to read; Context is found through the annotations.
    run.__annotations__ = {"context": Context, REQUEST: request_type, "return": returned}
    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "context", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context
            ),
            inspect.Parameter(
                REQUEST, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=request_type
            ),
        ],
        return_annotation=returned,
    )
    return run


def add_groups(server: MCPServer, groups: Sequence[ToolGroup], added: dict[str, AddedTool]) -> None:
    """Serve each group as one tool built from its recorded operations."""
    for group in groups:
        server.add_tool(
            group_function(group, added),
            name=group.name,
            title=group.title,
            description=group.description(),
            annotations=combined_annotations(
                group.title, [added[name].annotations for name in group.operations]
            ),
        )
