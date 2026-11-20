"""MCP calls whose arguments do not fit the schema say what to send (rule agentic.mcp.errors)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_agentic import PUBLIC_URL, agent, call_tool, new_token, tool
from tests.test_api import FUNNY_MEMES

SLUG = FUNNY_MEMES["slug"]


@pytest.fixture
def mcp(app: FastAPI, sign_up: Callable[..., TestClient]) -> Iterator[TestClient]:
    owner = sign_up("owner@example.com", "Owner")
    assert owner.post("/channels", json=FUNNY_MEMES).status_code == 201
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def failure(result: dict[str, Any]) -> str:
    """The text of a tool error; fails the test when the call did not fail."""
    assert result["isError"], result
    text: str = result["content"][0]["text"]
    return text


def test_mcp_call_with_channel_instead_of_slug_names_both_arguments(mcp: TestClient) -> None:
    text = failure(tool(mcp, "get_editorial", {"channel": SLUG}))
    assert "`slug` is required" in text
    assert "`channel` is not an argument of get_editorial" in text


def test_mcp_call_missing_slug_points_at_list_channels(mcp: TestClient) -> None:
    assert "list_channels" in failure(tool(mcp, "list_pitches", {}))


def test_mcp_call_with_misfit_arguments_lists_what_the_operation_takes(mcp: TestClient) -> None:
    text = failure(tool(mcp, "list_episodes", {"channel": SLUG, "readiness": True}))
    assert "list_episodes takes `slug` (required)" in text and "`readiness`" in text


def test_mcp_call_with_misfit_arguments_hides_pydantic_report(mcp: TestClient) -> None:
    text = failure(tool(mcp, "get_editorial", {"channel": SLUG}))
    assert "validation error" not in text and "input_value" not in text


def test_mcp_call_with_wrong_value_names_the_argument(mcp: TestClient) -> None:
    text = failure(tool(mcp, "list_pitches", {"slug": SLUG, "status": "gone"}))
    assert text.startswith("list_pitches did not run: `status`:")


def test_mcp_call_missing_nested_field_names_its_path(mcp: TestClient) -> None:
    arguments = {"slug": SLUG, "guideline": {"kind": "pillar"}}
    assert "`guideline.text` is required" in failure(tool(mcp, "write_guideline", arguments))


def test_mcp_call_with_operation_of_another_tool_names_that_tool(mcp: TestClient) -> None:
    request = {"operation": "list_episodes", "slug": SLUG}
    text = failure(call_tool(mcp, "read_channel", {"request": request}))
    assert "list_episodes is an operation of read_episode" in text


def test_mcp_call_with_unknown_operation_lists_the_tools_operations(mcp: TestClient) -> None:
    text = failure(call_tool(mcp, "read_channel", {"request": {"operation": "nope"}}))
    assert "Operations of read_channel: list_channels" in text


def test_mcp_call_without_request_says_how_to_call_the_tool(mcp: TestClient) -> None:
    text = failure(call_tool(mcp, "read_channel", {"operation": "get_channel", "slug": SLUG}))
    assert "read_channel takes one argument, `request`" in text


def test_mcp_call_with_request_as_json_text_still_names_the_argument(mcp: TestClient) -> None:
    request = '{"operation": "get_channel", "channel": "x"}'
    assert "`slug` is required" in failure(call_tool(mcp, "read_channel", {"request": request}))


def test_mcp_call_with_request_as_plain_text_says_how_to_call_the_tool(mcp: TestClient) -> None:
    text = failure(call_tool(mcp, "read_channel", {"request": "get_channel"}))
    assert "read_channel takes one argument, `request`" in text
