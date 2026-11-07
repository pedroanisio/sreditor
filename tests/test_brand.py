from __future__ import annotations

import pytest

from sreditor.assets import BrandSlot
from sreditor.brand import (
    NoSuchAccount,
    file_in,
    own_brand,
    referenced_assets,
    with_brand,
    with_color,
    with_file,
    without_color,
)
from sreditor.channels import Asset, AssetKind, BrandKit, Channel, Color, Platform, PlatformAccount

LOGO = Asset(path="memes/a.png", media_type="image/png", sha256="a" * 64)
AVATAR = Asset(path="memes/b.png", media_type="image/png", sha256="b" * 64)
PINK = Color(name="Pink", hex="#ff0066")


def channel() -> Channel:
    """A channel with a TikTok account whose kit sets an avatar."""
    tiktok = PlatformAccount(
        platform=Platform.TIKTOK, handle="memes", brand=BrandKit(avatar=AVATAR)
    )
    return Channel(slug="memes", name="Memes", brand=BrandKit(colors=[PINK]), accounts=[tiktok])


def test_own_brand_no_platform_is_channel_kit() -> None:
    assert own_brand(channel(), None).colors == [PINK]


def test_own_brand_platform_is_account_overrides() -> None:
    assert own_brand(channel(), Platform.TIKTOK).avatar == AVATAR


def test_own_brand_missing_account_raises() -> None:
    with pytest.raises(NoSuchAccount):
        own_brand(channel(), Platform.X)


def test_with_brand_platform_replaces_only_that_account_kit() -> None:
    updated = with_brand(channel(), Platform.TIKTOK, BrandKit())
    assert updated.accounts[0].brand == BrandKit() and updated.brand.colors == [PINK]


def test_with_brand_no_platform_replaces_channel_kit() -> None:
    assert with_brand(channel(), None, BrandKit()).brand == BrandKit()


def test_with_brand_missing_account_raises() -> None:
    with pytest.raises(NoSuchAccount):
        with_brand(channel(), Platform.X, BrandKit())


def test_with_file_logo_slot_sets_asset_kind() -> None:
    kit = with_file(BrandKit(), BrandSlot.LOGO, LOGO)
    assert kit.assets == {AssetKind.LOGO: LOGO} and file_in(kit, BrandSlot.LOGO) == LOGO


def test_with_file_none_empties_the_slot() -> None:
    kit = with_file(BrandKit(assets={AssetKind.LOGO: LOGO}), BrandSlot.LOGO, None)
    assert file_in(kit, BrandSlot.LOGO) is None


def test_with_file_avatar_slot_sets_avatar() -> None:
    kit = with_file(BrandKit(), BrandSlot.AVATAR, AVATAR)
    assert file_in(kit, BrandSlot.AVATAR) == AVATAR


def test_with_color_same_name_any_case_recolors() -> None:
    kit = with_color(BrandKit(colors=[PINK]), Color(name="pink", hex="#000000"))
    assert [color.hex for color in kit.colors] == ["#000000"]


def test_without_color_removes_by_name_any_case() -> None:
    assert without_color(BrandKit(colors=[PINK]), "PINK").colors == []


def test_referenced_assets_lists_channel_and_account_files() -> None:
    kit = BrandKit(assets={AssetKind.LOGO: LOGO})
    updated = with_brand(channel(), None, kit)
    assert referenced_assets(updated) == [LOGO, AVATAR]
