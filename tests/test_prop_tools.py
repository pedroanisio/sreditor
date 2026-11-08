"""The save_prop MCP tool: a prop added or changed with its srstudio rig in one call."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_agentic import tool
from tests.test_brand_identity import mcp, owner  # noqa: F401  # shared fixtures

SLUG = "funny-memes"
LAPTOP = {"name": "Laptop", "description": "Ana's old laptop", "rig": "laptop"}


def save_prop(client: TestClient, **arguments: Any) -> dict[str, Any]:
    return tool(client, "save_prop", {"slug": SLUG, **arguments})


def test_save_prop_new_prop_has_rig_and_slug(mcp: TestClient) -> None:  # noqa: F811
    saved = save_prop(mcp, prop=LAPTOP)["structuredContent"]
    assert saved["entry"]["name"] == "Laptop"
    assert (saved["sheet"]["slug"], saved["sheet"]["sheet"]["rig"]) == ("laptop", "laptop")


def test_save_prop_new_prop_is_listed_as_prop(mcp: TestClient) -> None:  # noqa: F811
    save_prop(mcp, prop=LAPTOP)
    listed = tool(mcp, "list_library", {"slug": SLUG, "kind": "prop"})["structuredContent"]
    assert [item["name"] for item in listed["items"]] == ["Laptop"]


def test_save_prop_existing_prop_changes_rig_and_slug(mcp: TestClient) -> None:  # noqa: F811
    first = save_prop(mcp, prop=LAPTOP)["structuredContent"]["entry"]
    change = {
        **LAPTOP,
        "rig": "papers",
        "prop_slug": "notes",
        "prop_id": first["id"],
        "expected_revision": first["revision"],
    }
    saved = save_prop(mcp, prop=change)["structuredContent"]
    assert (saved["sheet"]["slug"], saved["sheet"]["sheet"]["rig"]) == ("notes", "papers")


def test_save_prop_same_idempotency_key_returns_first_prop(mcp: TestClient) -> None:  # noqa: F811
    first = save_prop(mcp, prop=LAPTOP, idempotency_key="laptop-1")["structuredContent"]
    again = save_prop(mcp, prop=LAPTOP, idempotency_key="laptop-1")["structuredContent"]
    assert again["entry"]["id"] == first["entry"]["id"]


def test_save_prop_taken_slug_is_a_tool_error(mcp: TestClient) -> None:  # noqa: F811
    save_prop(mcp, prop=LAPTOP)
    other = {"name": "Papers", "rig": "papers", "prop_slug": "laptop"}
    done = save_prop(mcp, prop=other)
    assert done["isError"] and "list_library with kind prop" in done["content"][0]["text"]


def test_save_prop_taken_slug_adds_no_prop(mcp: TestClient) -> None:  # noqa: F811
    save_prop(mcp, prop=LAPTOP)
    save_prop(mcp, prop={"name": "Papers", "rig": "papers", "prop_slug": "laptop"})
    listed = tool(mcp, "list_library", {"slug": SLUG, "kind": "prop"})["structuredContent"]
    assert [item["name"] for item in listed["items"]] == ["Laptop"]
