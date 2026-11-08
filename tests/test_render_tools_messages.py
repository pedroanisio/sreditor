"""Texts that tell an agent how to fix a refused render (rule agentic.mcp.errors)."""

from __future__ import annotations

from sreditor.render_tools import render_error_message
from sreditor.srstudio_export import NO_STYLE, Missing, SrstudioNotReady


def test_render_error_message_not_ready_names_registered_style_tool() -> None:
    message = render_error_message(SrstudioNotReady([Missing(message=NO_STYLE)])) or ""
    assert "set_episode_style" in message
    assert "choose_episode_style" not in message
