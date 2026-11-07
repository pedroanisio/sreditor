"""A channel's identity as data (rule project.brand): colors with roles, fonts, a display name,
through the models, the API and MCP."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.assets import BrandSlot, UnsupportedFile, accepted_formats, inspect
from sreditor.book_operations import BookBrand
from sreditor.brand import (
    BrandPatch,
    referenced_assets,
    with_color,
    with_font_file,
    with_fonts,
    with_patch,
)
from sreditor.channels import (
    Asset,
    AssetKind,
    BrandKit,
    Channel,
    Color,
    ColorRole,
    FontInput,
    FontRole,
    FontSpec,
    Platform,
    PlatformAccount,
)
from tests.test_agentic import PUBLIC_URL, agent, mcp_call, new_token, tool
from tests.test_api import FUNNY_MEMES

CHANNEL = "/channels/funny-memes"
BRAND = f"{CHANNEL}/brand"
SHA = "a" * 64
PINK = Color(name="Pink", hex="#ff0066", role=ColorRole.PRIMARY)
INK = Color(name="Ink", hex="#111111", role=ColorRole.TEXT)
FONT_FILE = Asset(path="memes/f.woff2", media_type="font/woff2", sha256=SHA)
PALETTE = [
    {"name": "Pink", "hex": "#ff0066", "role": "primary"},
    {"name": "Ink", "hex": "#111111", "role": "text"},
]
FONTS = {"heading": {"family": "Bebas Neue", "weight": 700}, "body": {"family": "Inter"}}


# The models


def test_color_without_role_is_an_accent() -> None:
    assert Color(name="Pink", hex="#ff0066").role is ColorRole.ACCENT


def test_color_with_bad_hex_is_refused() -> None:
    with pytest.raises(ValidationError):
        Color(name="Pink", hex="ff0066")


def test_brand_kit_saved_without_roles_reads_first_color_as_primary() -> None:
    kit = BrandKit.model_validate(
        {
            "colors": [
                {"name": "Pink", "hex": "#ff0066"},
                {"name": "Ink", "hex": "#111111"},
                {"name": "Gold", "hex": "#ffcc00"},
                {"name": "Sky", "hex": "#00ccff"},
            ]
        }
    )
    assert [color.role for color in kit.colors] == [
        ColorRole.PRIMARY,
        ColorRole.SECONDARY,
        ColorRole.ACCENT,
        ColorRole.ACCENT,
    ]


def test_brand_kit_saved_without_roles_keeps_a_role_that_is_already_taken() -> None:
    kit = BrandKit.model_validate(
        {
            "colors": [
                {"name": "Pink", "hex": "#ff0066", "role": "primary"},
                {"name": "Ink", "hex": "#111111"},
            ]
        }
    )
    assert [color.role for color in kit.colors] == [ColorRole.PRIMARY, ColorRole.SECONDARY]


def test_channel_json_saved_before_roles_and_fonts_still_validates() -> None:
    saved = (
        '{"slug": "memes", "name": "Memes", "brand": {"colors": [{"name": "Pink", '
        '"hex": "#ff0066"}]}, "accounts": [{"platform": "tiktok", "handle": "memes", '
        '"brand": {"colors": [{"name": "Ink", "hex": "#111111"}]}}]}'
    )
    channel = Channel.model_validate_json(saved)
    assert channel.brand.colors[0].role is ColorRole.PRIMARY
    assert channel.accounts[0].brand.colors[0].role is ColorRole.PRIMARY
    assert channel.brand.fonts == {}


def test_brand_kit_two_colors_with_one_role_are_refused() -> None:
    twin = Color(name="Rose", hex="#ff3388", role=ColorRole.PRIMARY)
    with pytest.raises(ValidationError, match="Only accent colors may share a role"):
        BrandKit(colors=[PINK, twin])


def test_brand_kit_accents_may_share_the_role() -> None:
    kit = BrandKit(colors=[Color(name="A", hex="#000000"), Color(name="B", hex="#ffffff")])
    assert len(kit.colors) == 2


def test_brand_kit_repeated_color_name_is_refused() -> None:
    with pytest.raises(ValidationError, match="its own name"):
        BrandKit(colors=[Color(name="Pink", hex="#000000"), Color(name="pink", hex="#ffffff")])


@pytest.mark.parametrize("weight", [99, 901, 0])
def test_font_weight_outside_100_to_900_is_refused(weight: int) -> None:
    with pytest.raises(ValidationError):
        FontSpec(family="Inter", weight=weight)


def test_font_weight_900_is_accepted() -> None:
    assert FontSpec(family="Inter", weight=900).weight == 900


def test_font_file_that_is_an_image_is_refused() -> None:
    picture = Asset(path="memes/p.png", media_type="image/png", sha256=SHA)
    with pytest.raises(ValidationError, match="must be a font"):
        FontSpec(family="Inter", file=picture)


def test_brand_kit_primary_color_falls_back_to_the_first() -> None:
    kit = BrandKit(colors=[Color(name="A", hex="#000000")])
    assert kit.primary_color == kit.colors[0] and BrandKit().primary_color is None


def test_brand_kit_primary_color_is_the_one_with_the_role() -> None:
    kit = BrandKit(colors=[INK, PINK])
    assert kit.primary_color == PINK


def test_brand_for_platform_overrides_fonts_by_role() -> None:
    channel = Channel(
        slug="memes",
        name="Memes",
        brand=BrandKit(
            fonts={
                FontRole.HEADING: FontSpec(family="Bebas"),
                FontRole.BODY: FontSpec(family="Inter"),
            }
        ),
        accounts=[
            PlatformAccount(
                platform=Platform.TIKTOK,
                handle="memes",
                brand=BrandKit(fonts={FontRole.BODY: FontSpec(family="Roboto")}),
            )
        ],
    )
    fonts = channel.brand_for(Platform.TIKTOK).fonts
    assert fonts[FontRole.HEADING].family == "Bebas" and fonts[FontRole.BODY].family == "Roboto"


def test_book_brand_cover_color_is_the_primary_one() -> None:
    assert BookBrand(colors=[INK, PINK]).cover_color == PINK


def test_book_brand_cover_color_without_colors_is_none() -> None:
    assert BookBrand(colors=[]).cover_color is None


# Changing a kit


def test_with_color_role_moves_from_the_color_that_held_it() -> None:
    kit = with_color(BrandKit(colors=[PINK]), Color(name="Blue", hex="#0000ff", role="primary"))
    roles = {color.name: color.role for color in kit.colors}
    assert roles == {"Pink": ColorRole.ACCENT, "Blue": ColorRole.PRIMARY}


def test_with_fonts_same_family_keeps_the_file() -> None:
    kit = BrandKit(fonts={FontRole.BODY: FontSpec(family="Inter", file=FONT_FILE)})
    updated = with_fonts(kit, {FontRole.BODY: FontInput(family="inter", weight=600)})
    assert updated.fonts[FontRole.BODY].file == FONT_FILE
    assert updated.fonts[FontRole.BODY].weight == 600


def test_with_fonts_other_family_drops_the_file() -> None:
    kit = BrandKit(fonts={FontRole.BODY: FontSpec(family="Inter", file=FONT_FILE)})
    updated = with_fonts(kit, {FontRole.BODY: FontInput(family="Roboto")})
    assert updated.fonts[FontRole.BODY].file is None


def test_with_fonts_null_removes_the_role_and_keeps_others() -> None:
    kit = BrandKit(
        fonts={FontRole.BODY: FontSpec(family="Inter"), FontRole.HEADING: FontSpec(family="Bebas")}
    )
    assert list(with_fonts(kit, {FontRole.BODY: None}).fonts) == [FontRole.HEADING]


def test_with_font_file_sets_the_file_of_a_chosen_font() -> None:
    kit = BrandKit(fonts={FontRole.BODY: FontSpec(family="Inter")})
    assert with_font_file(kit, FontRole.BODY, FONT_FILE).fonts[FontRole.BODY].file == FONT_FILE


def test_with_patch_replaces_the_palette_and_keeps_the_fonts() -> None:
    kit = BrandKit(colors=[PINK], fonts={FontRole.BODY: FontSpec(family="Inter")})
    patch = BrandPatch(colors=[INK])
    updated = with_patch(kit, patch)
    assert updated.colors == [INK] and FontRole.BODY in updated.fonts


def test_brand_patch_with_a_repeated_role_is_refused() -> None:
    twin = Color(name="Rose", hex="#ff3388", role=ColorRole.PRIMARY)
    with pytest.raises(ValidationError, match="Only accent colors may share a role"):
        BrandPatch(colors=[PINK, twin])


def test_referenced_assets_include_font_files() -> None:
    kit = BrandKit(fonts={FontRole.BODY: FontSpec(family="Inter", file=FONT_FILE)})
    assert referenced_assets(Channel(slug="memes", name="Memes", brand=kit)) == [FONT_FILE]


# Files


def test_sting_slots_take_mp4_and_webm_only() -> None:
    names = [item.media_type for item in accepted_formats(BrandSlot.INTRO)]
    assert names == ["video/mp4", "video/webm"]


def test_sting_in_quicktime_is_refused() -> None:
    quicktime = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"
    with pytest.raises(UnsupportedFile, match="MP4, WEBM"):
        inspect(quicktime, BrandSlot.OUTRO, 1024)


def test_font_slot_takes_woff2_ttf_and_otf() -> None:
    assert [item.extension for item in accepted_formats(BrandSlot.FONT)] == [
        ".woff2",
        ".ttf",
        ".otf",
    ]


def test_logo_and_stings_live_in_asset_kinds() -> None:
    assert {AssetKind.LOGO, AssetKind.INTRO, AssetKind.OUTRO} <= set(AssetKind)


# The API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    return client


def patch_brand(client: TestClient, body: dict[str, Any], **kwargs: Any) -> Any:
    return client.patch(BRAND, json=body, **kwargs)


def test_api_get_brand_returns_the_etag_of_the_channel(owner: TestClient) -> None:
    response = owner.get(BRAND)
    assert response.status_code == 200 and response.headers["ETag"] == '"1"'


def test_api_patch_brand_sets_colors_and_fonts(owner: TestClient) -> None:
    response = patch_brand(owner, {"colors": PALETTE, "fonts": FONTS})
    body = response.json()
    assert response.status_code == 200
    assert [color["role"] for color in body["colors"]] == ["primary", "text"]
    assert body["fonts"]["heading"] == {"family": "Bebas Neue", "weight": 700, "file": None}
    assert body["fonts"]["body"]["weight"] == 400


def test_api_patch_brand_is_stored_on_the_channel(owner: TestClient) -> None:
    patch_brand(owner, {"colors": PALETTE})
    assert owner.get(BRAND).json()["colors"][0]["name"] == "Pink"
    assert owner.get(CHANNEL).json()["brand"]["colors"][0]["role"] == "primary"


def test_api_patch_brand_returns_a_new_etag(owner: TestClient) -> None:
    response = patch_brand(owner, {"colors": PALETTE})
    assert response.headers["ETag"] == '"2"'


def test_api_patch_brand_without_colors_keeps_the_palette(owner: TestClient) -> None:
    patch_brand(owner, {"colors": PALETTE})
    patch_brand(owner, {"fonts": FONTS})
    assert len(owner.get(BRAND).json()["colors"]) == 2


def test_api_patch_brand_null_font_removes_the_role(owner: TestClient) -> None:
    patch_brand(owner, {"fonts": FONTS})
    patch_brand(owner, {"fonts": {"heading": None}})
    assert list(owner.get(BRAND).json()["fonts"]) == ["body"]


def test_api_patch_brand_with_stale_if_match_answers_412(owner: TestClient) -> None:
    patch_brand(owner, {"colors": PALETTE})
    response = patch_brand(owner, {"colors": []}, headers={"If-Match": '"1"'})
    assert response.status_code == 412


def test_api_patch_brand_with_current_if_match_succeeds(owner: TestClient) -> None:
    response = patch_brand(owner, {"colors": PALETTE}, headers={"If-Match": '"1"'})
    assert response.status_code == 200


def test_api_patch_brand_with_repeated_role_answers_422_problem(owner: TestClient) -> None:
    twin = {"name": "Rose", "hex": "#ff3388", "role": "primary"}
    response = patch_brand(owner, {"colors": [*PALETTE, twin]})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"][0]["location"][:2] == ["body", "colors"]


@pytest.mark.parametrize("weight", [50, 950])
def test_api_patch_brand_with_weight_out_of_range_answers_422(
    owner: TestClient, weight: int
) -> None:
    response = patch_brand(owner, {"fonts": {"body": {"family": "Inter", "weight": weight}}})
    assert response.status_code == 422


def test_api_patch_brand_with_unknown_field_answers_422(owner: TestClient) -> None:
    assert patch_brand(owner, {"logo": "x"}).status_code == 422


def test_api_patch_brand_with_bad_hex_answers_422(owner: TestClient) -> None:
    bad = [{"name": "Pink", "hex": "pink", "role": "primary"}]
    assert patch_brand(owner, {"colors": bad}).status_code == 422


def test_api_patch_brand_for_platform_changes_only_that_account(owner: TestClient) -> None:
    response = patch_brand(owner, {"colors": PALETTE}, params={"platform": "tiktok"})
    assert response.status_code == 200
    assert owner.get(BRAND).json()["colors"] == []
    assert owner.get(BRAND, params={"platform": "tiktok"}).json()["colors"][0]["name"] == "Pink"


def test_api_patch_brand_for_platform_without_account_answers_422(owner: TestClient) -> None:
    response = patch_brand(owner, {"colors": PALETTE}, params={"platform": "x"})
    assert response.status_code == 422


def test_api_patch_brand_with_read_only_token_answers_403(app: FastAPI, owner: TestClient) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert patch_brand(reader, {"colors": PALETTE}).status_code == 403


def test_api_rename_channel_changes_the_name_and_keeps_the_slug(owner: TestClient) -> None:
    response = owner.patch(CHANNEL, json={"name": "Bater pra Cima"})
    assert response.status_code == 200
    assert response.json()["name"] == "Bater pra Cima" and response.json()["slug"] == "funny-memes"
    assert response.headers["ETag"] == '"2"'


def test_api_rename_channel_with_stale_if_match_answers_412(owner: TestClient) -> None:
    owner.patch(CHANNEL, json={"name": "One"})
    response = owner.patch(CHANNEL, json={"name": "Two"}, headers={"If-Match": '"1"'})
    assert response.status_code == 412


def test_api_rename_channel_with_blank_name_answers_422(owner: TestClient) -> None:
    assert owner.patch(CHANNEL, json={"name": "  "}).status_code == 422


def test_api_rename_channel_with_slug_in_body_answers_422(owner: TestClient) -> None:
    assert owner.patch(CHANNEL, json={"name": "New", "slug": "other"}).status_code == 422


def test_api_rename_channel_with_read_only_token_answers_403(
    app: FastAPI, owner: TestClient
) -> None:
    reader = agent(app, new_token(owner, ["channels:read"]))
    assert reader.patch(CHANNEL, json={"name": "New"}).status_code == 403


def test_api_openapi_describes_the_brand_operations(owner: TestClient) -> None:
    paths = owner.get("/openapi.json").json()["paths"]
    assert paths[BRAND.replace("funny-memes", "{slug}")]["patch"]["operationId"] == "patch_brand"
    assert paths["/channels/{slug}"]["patch"]["operationId"] == "rename_channel"


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def update_brand(mcp: TestClient, **arguments: Any) -> dict[str, Any]:
    brand = {name: arguments.pop(name) for name in ("colors", "fonts") if name in arguments}
    return tool(mcp, "update_brand", {"slug": "funny-memes", "brand": brand, **arguments})


def test_mcp_update_channel_renames_the_channel(mcp: TestClient) -> None:
    done = tool(mcp, "update_channel", {"slug": "funny-memes", "name": "Bater pra Cima"})
    view = done["structuredContent"]
    assert view["channel"]["name"] == "Bater pra Cima" and view["channel"]["slug"] == "funny-memes"
    assert view["revision"] == 2


def test_mcp_update_channel_with_stale_revision_is_a_tool_error(mcp: TestClient) -> None:
    tool(mcp, "update_channel", {"slug": "funny-memes", "name": "One"})
    done = tool(
        mcp, "update_channel", {"slug": "funny-memes", "name": "Two", "expected_revision": 1}
    )
    assert done["isError"] and "revision 2" in done["content"][0]["text"]


def test_mcp_update_channel_with_blank_name_is_a_tool_error(mcp: TestClient) -> None:
    assert tool(mcp, "update_channel", {"slug": "funny-memes", "name": " "})["isError"]


def test_mcp_update_brand_sets_colors_and_fonts(mcp: TestClient) -> None:
    done = update_brand(mcp, colors=PALETTE, fonts=FONTS)
    view = done["structuredContent"]
    assert not done.get("isError")
    assert [color["role"] for color in view["brand"]["colors"]] == ["primary", "text"]
    assert view["brand"]["fonts"]["heading"]["family"] == "Bebas Neue"
    assert view["revision"] == 2 and view["name"] == "Funny Memes"


def test_mcp_get_brand_shows_what_update_brand_set(mcp: TestClient) -> None:
    update_brand(mcp, colors=PALETTE)
    view = tool(mcp, "get_brand", {"slug": "funny-memes"})["structuredContent"]
    assert view["brand"]["colors"][0]["name"] == "Pink" and view["revision"] == 2


def test_mcp_get_brand_for_platform_names_the_platform(mcp: TestClient) -> None:
    view = tool(mcp, "get_brand", {"slug": "funny-memes", "platform": "tiktok"})
    assert view["structuredContent"]["platform"] == "tiktok"


def test_mcp_update_brand_for_platform_changes_only_that_account(mcp: TestClient) -> None:
    update_brand(mcp, platform="tiktok", colors=PALETTE)
    channel_kit = tool(mcp, "get_brand", {"slug": "funny-memes"})["structuredContent"]["brand"]
    assert channel_kit["colors"] == []


def test_mcp_update_brand_without_changes_names_the_fix(mcp: TestClient) -> None:
    done = update_brand(mcp)
    assert done["isError"] and "get_brand" in done["content"][0]["text"]


def test_mcp_update_brand_with_repeated_role_names_the_fix(mcp: TestClient) -> None:
    twin = {"name": "Rose", "hex": "#ff3388", "role": "primary"}
    done = update_brand(mcp, colors=[*PALETTE, twin])
    text = done["content"][0]["text"]
    assert done["isError"] and "share a role" in text and "get_brand" in text


def test_mcp_update_brand_with_stale_revision_is_a_tool_error(mcp: TestClient) -> None:
    update_brand(mcp, colors=PALETTE)
    done = update_brand(mcp, colors=[], expected_revision=1)
    assert done["isError"] and "changed" in done["content"][0]["text"]


def test_mcp_update_brand_for_missing_account_is_a_tool_error(mcp: TestClient) -> None:
    assert update_brand(mcp, platform="x", colors=PALETTE)["isError"]


def test_mcp_update_brand_with_read_only_token_is_a_tool_error(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["channels:read"]), PUBLIC_URL) as reader:
        assert update_brand(reader, colors=PALETTE)["isError"]


def test_mcp_brand_tool_descriptions_are_fixed_in_code(mcp: TestClient) -> None:
    listed = {item["name"]: item for item in mcp_call(mcp, "tools/list", {})["result"]["tools"]}
    assert "never changes" in listed["update_channel"]["description"]
    assert "WHOLE palette" in listed["update_brand"]["description"]
    assert listed["update_brand"]["annotations"]["idempotentHint"] is True
