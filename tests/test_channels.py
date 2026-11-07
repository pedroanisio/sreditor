from __future__ import annotations

import pytest
from pydantic import ValidationError

from sreditor.channels import Asset, AssetKind, BrandKit, Channel, Color, Platform, PlatformAccount

SHA = "a" * 64


def asset(path: str, **extra: object) -> Asset:
    return Asset(path=path, media_type="image/png", sha256=SHA, **extra)


def test_channel_slug_not_kebab_case_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        Channel(slug="Funny Memes", name="Funny Memes")


def test_channel_language_tag_is_accepted() -> None:
    assert Channel(slug="a", name="A", language="en").language == "en"


def test_channel_language_not_a_tag_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        Channel(slug="a", name="A", language="english please")


def test_channel_repeated_platform_account_raises_validation_error() -> None:
    account = PlatformAccount(platform=Platform.TIKTOK, handle="@a")
    with pytest.raises(ValidationError, match="repeated: tiktok"):
        Channel(slug="a", name="A", accounts=[account, account])


@pytest.mark.parametrize(("platform", "length"), [(Platform.TIKTOK, 80), (Platform.KWAI, 5000)])
def test_account_description_at_platform_limit_is_accepted(platform: Platform, length: int) -> None:
    PlatformAccount(platform=platform, handle="@a", description="x" * length)


def test_account_description_over_platform_limit_raises_validation_error() -> None:
    with pytest.raises(
        ValidationError, match="tiktok description has 81 characters; the limit is 80"
    ):
        PlatformAccount(platform=Platform.TIKTOK, handle="@a", description="x" * 81)


@pytest.mark.parametrize("dimensions", [{"width": 200, "height": 200}, {}])
def test_brand_avatar_square_or_unsized_is_accepted(dimensions: dict[str, int]) -> None:
    BrandKit(avatar=asset("a.png", **dimensions))


def test_brand_avatar_not_square_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="square"):
        BrandKit(avatar=asset("a.png", width=200, height=100))


@pytest.mark.parametrize("path", ["../secret.png", "/etc/passwd", "a/../../b.png"])
def test_asset_path_outside_root_raises_validation_error(path: str) -> None:
    with pytest.raises(ValidationError):
        asset(path)


def test_asset_sha256_not_hex_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        Asset(path="a.png", media_type="image/png", sha256="nothex")


def test_color_hex_not_a_hex_code_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        Color(name="primary", hex="red")


CHANNEL_AVATAR = asset("avatar.png")
TIKTOK_AVATAR = asset("tiktok/avatar.png")
LOGO = asset("logo.png")
BANNER = asset("banner.png")
TIKTOK_BANNER = asset("tiktok/banner.png")
PRIMARY = Color(name="primary", hex="#112233")
NEON = Color(name="primary", hex="#00FF88")


@pytest.fixture
def branded_channel() -> Channel:
    return Channel(
        slug="funny-memes",
        name="Funny Memes",
        brand=BrandKit(
            avatar=CHANNEL_AVATAR,
            assets={AssetKind.LOGO: LOGO, AssetKind.BANNER: BANNER},
            colors=[PRIMARY],
        ),
        accounts=[
            PlatformAccount(
                platform=Platform.TIKTOK,
                handle="@fm",
                brand=BrandKit(
                    avatar=TIKTOK_AVATAR, assets={AssetKind.BANNER: TIKTOK_BANNER}, colors=[NEON]
                ),
            ),
            PlatformAccount(platform=Platform.YOUTUBE, handle="@fm"),
        ],
    )


def test_brand_for_platform_with_override_replaces_avatar_and_colors(
    branded_channel: Channel,
) -> None:
    tiktok = branded_channel.brand_for(Platform.TIKTOK)
    assert tiktok.avatar == TIKTOK_AVATAR
    assert tiktok.colors == [NEON]


def test_brand_for_platform_with_override_merges_assets_per_kind(
    branded_channel: Channel,
) -> None:
    tiktok = branded_channel.brand_for(Platform.TIKTOK)
    assert tiktok.assets == {AssetKind.LOGO: LOGO, AssetKind.BANNER: TIKTOK_BANNER}


def test_brand_for_platform_without_override_inherits_channel_brand(
    branded_channel: Channel,
) -> None:
    youtube = branded_channel.brand_for(Platform.YOUTUBE)
    assert (youtube.avatar, youtube.colors) == (CHANNEL_AVATAR, [PRIMARY])


def test_brand_for_platform_without_account_or_none_returns_channel_brand(
    branded_channel: Channel,
) -> None:
    assert (
        branded_channel.brand_for(Platform.X)
        == branded_channel.brand_for()
        == branded_channel.brand
    )
