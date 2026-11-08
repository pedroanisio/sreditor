"""Changes to a brand kit, as pure functions that return an updated copy.

`platform=None` is the channel's own kit; a platform is that account's overrides, which
Channel.brand_for applies on top of the channel's kit.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sreditor.assets import BrandSlot
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
    NarratorVoice,
    Platform,
    check_palette,
)


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
    """The kit with a color added, or recolored when one has the same name.

    A role other than accent moves to this color: the color that held it becomes an accent.
    """
    others = [item for item in brand.colors if item.name.casefold() != color.name.casefold()]
    if color.role is not ColorRole.ACCENT:
        others = [
            item.model_copy(update={"role": ColorRole.ACCENT}) if item.role is color.role else item
            for item in others
        ]
    return brand.model_copy(update={"colors": [*others, color]})


def without_color(brand: BrandKit, name: str) -> BrandKit:
    """The kit without the named color."""
    colors = [item for item in brand.colors if item.name.casefold() != name.casefold()]
    return brand.model_copy(update={"colors": colors})


class BrandPatch(BaseModel):
    """A change to the colors and fonts of a kit; what is left out stays as it is.

    `colors` replaces the whole palette. `fonts` sets the fonts of the roles it names and
    removes the role whose value is null; other roles stay. `narrator_voice` sets the voice a
    short script speaks with when it names none, and an explicit null clears it (rule
    project.voice.estimate); left out, it stays.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    colors: list[Color] | None = Field(
        default=None,
        description="The whole palette: #RRGGBB colors with a role; accent may repeat.",
    )
    fonts: dict[FontRole, FontInput | None] | None = Field(
        default=None,
        description="Fonts by role (heading, body, caption); null removes a role's font.",
    )
    narrator_voice: NarratorVoice | None = Field(
        default=None,
        description="The default narrator voice; null clears it, leaving it out keeps it.",
    )

    @property
    def is_empty(self) -> bool:
        """Whether the patch changes nothing: no palette, no fonts and no narrator voice named."""
        return (
            self.colors is None
            and self.fonts is None
            and "narrator_voice" not in self.model_fields_set
        )

    @field_validator("colors")
    @classmethod
    def palette_is_valid(cls, colors: list[Color] | None) -> list[Color] | None:
        """Refuse a palette the kit would refuse, so the caller gets a field error."""
        return None if colors is None else check_palette(colors)


def with_fonts(brand: BrandKit, fonts: dict[FontRole, FontInput | None]) -> BrandKit:
    """The kit with fonts set or removed by role.

    A font keeps its uploaded file only while its family stays the same, since a file belongs
    to one family.
    """
    updated = dict(brand.fonts)
    for role, chosen in fonts.items():
        if chosen is None:
            updated.pop(role, None)
            continue
        current = updated.get(role)
        is_same_family = (
            current is not None and current.family.casefold() == chosen.family.casefold()
        )
        file = current.file if current is not None and is_same_family else None
        updated[role] = FontSpec(family=chosen.family, weight=chosen.weight, file=file)
    return brand.model_copy(update={"fonts": updated})


def with_font_file(brand: BrandKit, role: FontRole, file: Asset) -> BrandKit:
    """The kit with a font file set for a role whose family is already chosen."""
    spec = brand.fonts[role].model_copy(update={"file": file})
    return brand.model_copy(update={"fonts": {**brand.fonts, role: spec}})


def with_patch(brand: BrandKit, patch: BrandPatch) -> BrandKit:
    """The kit after a patch; BrandKit validates the palette, so a bad one raises."""
    updated = brand
    if patch.fonts is not None:
        updated = with_fonts(updated, patch.fonts)
    if patch.colors is not None:
        updated = BrandKit.model_validate({**updated.model_dump(), "colors": patch.colors})
    if "narrator_voice" in patch.model_fields_set:
        updated = updated.model_copy(update={"narrator_voice": patch.narrator_voice})
    return updated


def referenced_assets(channel: Channel) -> list[Asset]:
    """Every file the channel's kit or its accounts' kits point to."""
    kits = [channel.brand, *(account.brand for account in channel.accounts)]
    found: list[Asset] = []
    for kit in kits:
        if kit.avatar is not None:
            found.append(kit.avatar)
        found.extend(kit.assets.values())
        found.extend(font.file for font in kit.fonts.values() if font.file is not None)
    return found
