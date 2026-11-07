"""Old tool names that keep answering for 90 days after a rename (rule agentic.mcp.renames).

An agent or a saved prompt may still call a tool by the name it learned. When a tool is renamed,
add a ToolRename here: the old name is registered as an alias with the same arguments, whose
description names the new tool and the day the alias ends. After that day build() no longer
registers it, so the tool list shrinks on its own. The server instructions never mention aliases.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any, override

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon, ToolAnnotations
from pydantic import BaseModel, ConfigDict

ALIAS_DAYS = 90
ALIAS_TITLE_SUFFIX = " (old name)"


class ToolRename(BaseModel):
    """A tool that was renamed: its old and new names and the day the rename shipped."""

    model_config = ConfigDict(frozen=True)

    old_name: str
    new_name: str
    renamed_on: date

    @property
    def ends_on(self) -> date:
        """The last day the old name answers."""
        return self.renamed_on + timedelta(days=ALIAS_DAYS)

    def is_active(self, today: date) -> bool:
        """Whether the old name is still registered on this day."""
        return today <= self.ends_on

    def description(self) -> str:
        """What the alias tells an agent: it is an alias, for which tool and until when."""
        return (
            f"Old name of {self.new_name}, with the same arguments and result. It stops "
            f"answering after {self.ends_on.isoformat()}: call {self.new_name} instead."
        )


# No tool has been renamed since the MCP server shipped. The renames go here, newest last, as
# ToolRename(old_name="...", new_name="...", renamed_on=date(...)).
RENAMED_TOOLS: tuple[ToolRename, ...] = ()


class AddedTool(BaseModel):
    """What a tool was registered with, kept so an alias can reuse it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    function: Callable[..., Any]
    title: str | None
    annotations: ToolAnnotations | None


class AliasingServer(MCPServer):
    """An MCP server that remembers its tools, so build() can register aliases of them."""

    def __init__(self, *, name: str, instructions: str) -> None:
        super().__init__(name=name, instructions=instructions)
        self.added: dict[str, AddedTool] = {}
        self.alias_names: list[str] = []

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
        """Register the tool and remember it under its name."""
        self.added[name or fn.__name__] = AddedTool(
            function=fn, title=title, annotations=annotations
        )
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )

    def add_aliases(self, renames: Sequence[ToolRename], today: date) -> None:
        """Register the old name of each rename that is still active."""
        for rename in renames:
            if rename.is_active(today):
                self.add_alias(rename)

    def add_alias(self, rename: ToolRename) -> None:
        """Register one old name with the new tool's function, arguments and hints."""
        target = self.added[rename.new_name]
        annotations = target.annotations
        title = annotations.title if annotations and annotations.title else rename.new_name
        title += ALIAS_TITLE_SUFFIX
        if annotations is not None:
            annotations = annotations.model_copy(update={"title": title})
        self.add_tool(
            target.function,
            name=rename.old_name,
            title=title,
            description=rename.description(),
            annotations=annotations,
        )
        self.alias_names.append(rename.old_name)
