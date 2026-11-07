"""Changes to a brand kit, as pure functions that return an updated copy.

`platform=None` is the channel's own kit; a platform is that account's overrides, which
Channel.brand_for applies on top of the channel's kit.
"""

from __future__ import annotations

from sreditor.assets import BrandSlot
from sreditor.channels import Asset, AssetKind, BrandKit, Channel, Color, Platform


class NoSuchAccount(LookupError):
    """The channel has no account on that platform."""


def own_brand(channel: Channel, platform: Platform | None) -> BrandKit:
    """The kit set at this level: the channel's, or the account's overrides."""
    if platform is None:
        return channel.brand
    account = channel.account(platform)
    if account is None:
        raise NoSuchAccount(platform)
    return account.brand


def with_brand(channel: Channel, platform: Platform | None, brand: BrandKit) -> Channel:
    """The channel with the kit at this level replaced."""
    if platform is None:
        return channel.model_copy(update={"brand": brand})
    if channel.account(platform) is None:
        raise NoSuchAccount(platform)
    accounts = [
        account.model_copy(update={"brand": brand}) if account.platform == platform else account
        for account in channel.accounts
    ]
    return channel.model_copy(update={"accounts": accounts})


def with_file(brand: BrandKit, slot: BrandSlot, asset: Asset | None) -> BrandKit:
    """The kit with a file set in the slot, or removed when `asset` is None."""
    if slot is BrandSlot.AVATAR:
        return brand.model_copy(update={"avatar": asset})
    kind = AssetKind(slot.value)
    assets = {key: value for key, value in brand.assets.items() if key is not kind}
    if asset is not None:
        assets[kind] = asset
    return brand.model_copy(update={"assets": assets})


def file_in(brand: BrandKit, slot: BrandSlot) -> Asset | None:
    """The file in a slot, if any."""
    if slot is BrandSlot.AVATAR:
        return brand.avatar
    return brand.assets.get(AssetKind(slot.value))


def with_color(brand: BrandKit, color: Color) -> BrandKit:
    """The kit with a color added, or recolored when one has the same name."""
    others = [item for item in brand.colors if item.name.casefold() != color.name.casefold()]
    return brand.model_copy(update={"colors": [*others, color]})


def without_color(brand: BrandKit, name: str) -> BrandKit:
    """The kit without the named color."""
    colors = [item for item in brand.colors if item.name.casefold() != name.casefold()]
    return brand.model_copy(update={"colors": colors})


def referenced_assets(channel: Channel) -> list[Asset]:
    """Every file the channel's kit or its accounts' kits point to."""
    kits = [channel.brand, *(account.brand for account in channel.accounts)]
    found: list[Asset] = []
    for kit in kits:
        if kit.avatar is not None:
            found.append(kit.avatar)
        found.extend(kit.assets.values())
    return found
