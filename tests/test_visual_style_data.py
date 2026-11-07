"""A library visual style as data: palette, fonts, cut rhythm, smallest text, safe zone (rule project.visual-style)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.srstudio import CutRhythm, SafeZone, StyleSheet
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES

STYLES = "/channels/funny-memes/styles"
STYLE = "/channels/funny-memes/episodes/1/style"
PALETTE = {"ink": "#111111", "white": "#ffffff", "shade": "#888888", "background": "#eeeeee"}
FONTS = {
    "heading": {"family": "Inter", "weight": 700},
    "body": {"family": "Inter", "weight": 400},
}
STRUCTURED: dict[str, Any] = {
    "kind": "style",
    "style": "toon",
    "palette": PALETTE,
    "fonts": FONTS,
    "cut_rhythm": {"shortest_shot_s": 0.4, "average_shot_s": 1.5},
    "min_text_px": 48,
    "safe_zone": {"top": 10, "right": 5, "bottom": 20, "left": 5},
}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def style_id(owner: TestClient) -> str:
    response = owner.post(STYLES, json={"name": "Office"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def save_sheet(owner: TestClient, style_id: str, sheet: dict[str, Any]) -> Any:
    return owner.put(
        f"{STYLES}/{style_id}/srstudio",
        json={"slug": "office", "sheet": sheet},
        headers={"If-Match": '"0"'},
    )


# The model


def test_style_sheet_with_only_the_old_fields_has_no_structured_data() -> None:
    old = {"kind": "style", "style": "toon", "palette": PALETTE}
    sheet = StyleSheet.model_validate(old)
    assert (sheet.fonts, sheet.cut_rhythm, sheet.min_text_px, sheet.safe_zone) == (
        None,
        None,
        None,
        None,
    )


def test_style_sheet_stored_json_without_new_keys_still_loads() -> None:
    stored = '{"kind":"style","style":"cel","palette":null}'
    assert StyleSheet.model_validate_json(stored).style is not None


def test_cut_rhythm_with_shortest_above_average_is_refused() -> None:
    with pytest.raises(ValidationError, match="shortest shot cannot be longer"):
        CutRhythm(shortest_shot_s=2, average_shot_s=1)


def test_cut_rhythm_with_equal_shots_is_accepted() -> None:
    assert CutRhythm(shortest_shot_s=1, average_shot_s=1).average_shot_s == 1


@pytest.mark.parametrize("seconds", [0, -1])
def test_cut_rhythm_with_non_positive_shot_is_refused(seconds: float) -> None:
    with pytest.raises(ValidationError):
        CutRhythm(shortest_shot_s=seconds, average_shot_s=1)


def test_safe_zone_with_edge_above_fifty_is_refused() -> None:
    with pytest.raises(ValidationError):
        SafeZone(top=51, right=0, bottom=0, left=0)


def test_safe_zone_with_opposite_edges_covering_the_frame_is_refused() -> None:
    with pytest.raises(ValidationError, match="Opposite margins"):
        SafeZone(top=50, right=0, bottom=50, left=0)


def test_style_sheet_with_font_weight_off_the_scale_is_refused() -> None:
    with pytest.raises(ValidationError):
        StyleSheet.model_validate({"fonts": {"body": {"family": "Inter", "weight": 450}}})


def test_style_sheet_with_zero_smallest_text_is_refused() -> None:
    with pytest.raises(ValidationError):
        StyleSheet.model_validate({"min_text_px": 0})


def test_style_sheet_with_bad_palette_color_is_refused() -> None:
    with pytest.raises(ValidationError):
        StyleSheet.model_validate({"palette": {**PALETTE, "ink": "red"}})


# The API


def test_style_sheet_put_returns_every_structured_field(owner: TestClient, style_id: str) -> None:
    response = save_sheet(owner, style_id, STRUCTURED)
    assert response.status_code == 200, response.text
    assert response.json()["sheet"] == {**STRUCTURED, "fonts": {**FONTS, "caption": None}}


def test_style_sheet_get_after_put_returns_the_saved_data(owner: TestClient, style_id: str) -> None:
    save_sheet(owner, style_id, STRUCTURED)
    read = owner.get(f"{STYLES}/{style_id}/srstudio").json()["sheet"]
    assert read["safe_zone"]["bottom"] == 20 and read["min_text_px"] == 48


def test_style_sheet_put_with_inverted_cut_rhythm_names_the_field(
    owner: TestClient, style_id: str
) -> None:
    bad = {**STRUCTURED, "cut_rhythm": {"shortest_shot_s": 3, "average_shot_s": 1}}
    response = save_sheet(owner, style_id, bad)
    assert response.status_code == 422 and "cut_rhythm" in response.text


def test_episode_style_get_returns_the_structured_sheet(owner: TestClient, style_id: str) -> None:
    save_sheet(owner, style_id, STRUCTURED)
    owner.put(STYLE, json={"style_id": style_id}, headers={"If-Match": '"0"'})
    sheet = owner.get(STYLE).json()["sheet"]
    assert sheet["slug"] == "office" and sheet["sheet"]["fonts"]["heading"]["weight"] == 700


def test_episode_style_get_without_a_pick_has_no_sheet(owner: TestClient) -> None:
    assert owner.get(STYLE).json()["sheet"] is None


# MCP


def test_mcp_save_visual_style_then_get_returns_the_data(mcp: TestClient, style_id: str) -> None:
    data = {key: value for key, value in STRUCTURED.items() if key != "kind"}
    arguments = {"slug": "funny-memes", "style_id": style_id, "expected_revision": 0}
    saved = tool(mcp, "save_visual_style", {**arguments, "data": {**data, "style_slug": "office"}})
    assert not saved.get("isError"), saved
    read = tool(mcp, "get_visual_style", {"slug": "funny-memes", "style_id": style_id})
    sheet = read["structuredContent"]["sheet"]
    assert sheet["slug"] == "office" and sheet["sheet"]["cut_rhythm"]["average_shot_s"] == 1.5


def test_mcp_save_visual_style_without_slug_keeps_the_proposed_one(
    mcp: TestClient, style_id: str
) -> None:
    arguments = {"slug": "funny-memes", "style_id": style_id, "data": {"style": "cel"}}
    saved = tool(mcp, "save_visual_style", arguments)
    assert saved["structuredContent"]["sheet"]["slug"] == "office"


def test_mcp_save_visual_style_with_taken_slug_names_the_fix(
    mcp: TestClient, owner: TestClient, style_id: str
) -> None:
    other = owner.post(STYLES, json={"name": "Other"}).json()["id"]
    arguments = {"slug": "funny-memes", "style_id": style_id, "data": {"style_slug": "other"}}
    tool(mcp, "save_visual_style", {**arguments, "style_id": other})
    refused = tool(mcp, "save_visual_style", arguments)
    assert refused["isError"] and "style_slug" in refused["content"][0]["text"]


def test_mcp_save_visual_style_with_stale_revision_says_how_to_retry(
    mcp: TestClient, style_id: str
) -> None:
    arguments = {"slug": "funny-memes", "style_id": style_id, "data": {"style": "cel"}}
    tool(mcp, "save_visual_style", arguments)
    stale = tool(mcp, "save_visual_style", {**arguments, "expected_revision": 0})
    assert stale["isError"] and "expected_revision=1" in stale["content"][0]["text"]


def test_mcp_get_visual_style_unknown_id_names_the_listing_call(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "style_id": "00000000-0000-4000-8000-000000000000"}
    missing = tool(mcp, "get_visual_style", arguments)
    assert missing["isError"] and "list_library" in missing["content"][0]["text"]


def test_mcp_get_episode_style_returns_the_full_structured_style(
    mcp: TestClient, owner: TestClient, style_id: str
) -> None:
    save_sheet(owner, style_id, STRUCTURED)
    arguments = {"slug": "funny-memes", "number": 1, "style_id": style_id}
    tool(mcp, "set_episode_style", {**arguments, "expected_revision": 0})
    read = tool(mcp, "get_episode_style", {"slug": "funny-memes", "number": 1})
    sheet = read["structuredContent"]["sheet"]["sheet"]
    assert sheet["palette"] == PALETTE and sheet["safe_zone"]["top"] == 10
