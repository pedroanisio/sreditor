"""Channels: a brand published on several platforms, each with its own account."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from sreditor.field_types import ChannelSlug, LanguageTag, MediaType, Sha256, VoiceId
from sreditor.voice_tuning import VoiceTuning


class Platform(StrEnum):
    """A social platform a channel can have one account on."""

    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    X = "x"
    FACEBOOK = "facebook"
    KWAI = "kwai"


class Status(StrEnum):
    """Whether a channel, or one of its accounts, is ready to publish."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


TEXT_LENGTH = 200
ASSET_PATH_LENGTH = 512
DEFAULT_LANGUAGE = "pt-BR"
HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"
RELATIVE_PATH_PATTERN = r"^[^/].*$"

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TEXT_LENGTH)
]

# Profile description limits, in characters. Platforms change these; verify before relying on them.
DESCRIPTION_LIMITS: dict[Platform, int] = {
    Platform.TIKTOK: 80,
    Platform.INSTAGRAM: 150,
    Platform.X: 160,
    Platform.YOUTUBE: 1000,
}


HANDLE_SIGIL = "@"


def normalized_handle(handle: str) -> str:
    """A handle as platforms compare them: without the leading @ and ignoring case."""
    return handle.strip().removeprefix(HANDLE_SIGIL).strip().casefold()


class AssetKind(StrEnum):
    """The slot a brand file fills in a kit."""

    LOGO = "logo"
    BANNER = "banner"
    WATERMARK = "watermark"
    INTRO = "intro"
    OUTRO = "outro"
    THUMBNAIL_TEMPLATE = "thumbnail_template"
    FONT = "font"


class Asset(BaseModel):
    """A file in the assets root. Files stay out of git; the record pins their content."""

    model_config = ConfigDict(frozen=True)

    path: Annotated[
        str,
        StringConstraints(
            pattern=RELATIVE_PATH_PATTERN, min_length=1, max_length=ASSET_PATH_LENGTH
        ),
    ]
    media_type: MediaType
    sha256: Sha256
    width: PositiveInt | None = None
    height: PositiveInt | None = None
    duration_seconds: PositiveFloat | None = None

    @field_validator("path")
    @classmethod
    def path_stays_inside_root(cls, path: str) -> str:
        """Refuse `..` so a record can never point outside the assets root."""
        if ".." in path.split("/"):
            raise ValueError("Asset path must stay inside the assets root")
        return path


class ColorRole(StrEnum):
    """What a brand color is for; each role is named once, except accent."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    ACCENT = "accent"
    TEXT = "text"
    BACKGROUND = "background"


class FontRole(StrEnum):
    """Where a brand font is used."""

    HEADING = "heading"
    BODY = "body"
    CAPTION = "caption"


# Kits saved before colors had roles: the first color was the brand's main one, the second its
# partner and the rest accents, which is how the books export already read them.
LEGACY_ROLES = (ColorRole.PRIMARY, ColorRole.SECONDARY)
FONT_WEIGHT_MIN = 100
FONT_WEIGHT_MAX = 900
DEFAULT_FONT_WEIGHT = 400
FONT_MEDIA_PREFIX = "font/"


class Color(BaseModel):
    """A named brand color and the role it plays."""

    model_config = ConfigDict(frozen=True)

    name: Text
    hex: Annotated[str, StringConstraints(pattern=HEX_PATTERN)]
    role: ColorRole = ColorRole.ACCENT


class FontInput(BaseModel):
    """A font a person or agent chooses: a family and a weight, without any file."""

    model_config = ConfigDict(frozen=True)

    family: Text
    weight: Annotated[int, Field(ge=FONT_WEIGHT_MIN, le=FONT_WEIGHT_MAX)] = DEFAULT_FONT_WEIGHT


class FontSpec(FontInput):
    """A brand font for one role, with the uploaded font file when there is one."""

    file: Asset | None = None

    @field_validator("file")
    @classmethod
    def file_is_a_font(cls, file: Asset | None) -> Asset | None:
        """Only a font file (woff2, ttf or otf) can be the file behind a font."""
        if file is not None and not file.media_type.startswith(FONT_MEDIA_PREFIX):
            raise ValueError("A font file must be a font")
        return file


def with_legacy_roles(colors: object) -> object:
    """Give colors saved without a role the first free legacy role, then accent."""
    if not isinstance(colors, list):
        return colors
    taken = {item.get("role") for item in colors if isinstance(item, Mapping)}
    free = [role.value for role in LEGACY_ROLES if role.value not in taken]
    migrated: list[Any] = []
    for item in colors:
        if isinstance(item, Mapping) and "role" not in item:
            role = free.pop(0) if free else ColorRole.ACCENT.value
            item = {**item, "role": role}
        migrated.append(item)
    return migrated


def check_palette(colors: list[Color]) -> list[Color]:
    """Refuse a repeated name, or a role other than accent given to two colors."""
    names = [color.name.casefold() for color in colors]
    roles = [color.role for color in colors if color.role is not ColorRole.ACCENT]
    if len(set(names)) != len(names):
        raise ValueError("Each color needs its own name")
    if len(set(roles)) != len(roles):
        raise ValueError("Only accent colors may share a role; give each other role once")
    return colors


def primary_of(colors: list[Color]) -> Color | None:
    """The color with the primary role, or the first when none has it; None without colors."""
    primary = next((color for color in colors if color.role is ColorRole.PRIMARY), None)
    return primary or (colors[0] if colors else None)


class NarratorVoice(BaseModel):
    """The ElevenLabs voice that narrates when a short script names none (rule project.voice.estimate)."""

    model_config = ConfigDict(frozen=True)

    voice_id: VoiceId
    voice_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=TEXT_LENGTH)] = (
        ""
    )
    tuning: VoiceTuning | None = None


class BrandKit(BaseModel):
    """A brand's avatar, files, colors and fonts; a platform's kit overrides the channel's.

    The logo and the opening and closing stings are files in `assets` (logo, intro, outro).
    """

    model_config = ConfigDict(frozen=True)

    avatar: Asset | None = None
    assets: dict[AssetKind, Asset] = Field(default_factory=dict)
    colors: list[Color] = Field(default_factory=list)
    fonts: dict[FontRole, FontSpec] = Field(default_factory=dict)
    narrator_voice: NarratorVoice | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_colors(cls, data: object) -> object:
        """Read kits saved before colors had roles (rule project.brand)."""
        if isinstance(data, Mapping) and "colors" in data:
            return {**data, "colors": with_legacy_roles(data["colors"])}
        return data

    @field_validator("avatar")
    @classmethod
    def avatar_is_square(cls, avatar: Asset | None) -> Asset | None:
        """Platforms crop avatars to a square or circle, so anything else would be cut."""
        if avatar is not None and avatar.width is not None and avatar.width != avatar.height:
            raise ValueError("Avatar must be square")
        return avatar

    @field_validator("colors")
    @classmethod
    def colors_are_distinct(cls, colors: list[Color]) -> list[Color]:
        """Refuse a repeated name, or a role other than accent given to two colors."""
        return check_palette(colors)

    @property
    def primary_color(self) -> Color | None:
        """The primary color, or the first one when none is marked primary."""
        return primary_of(self.colors)


class PlatformAccount(BaseModel):
    """A channel's account on one platform, with its own description and brand overrides."""

    model_config = ConfigDict(frozen=True)

    platform: Platform
    handle: Text
    url: HttpUrl | None = None
    description: str = ""
    brand: BrandKit = Field(default_factory=BrandKit)
    status: Status = Status.ACTIVE

    @model_validator(mode="after")
    def check_description_length(self) -> Self:
        """Refuse a description the platform would cut off."""
        limit = DESCRIPTION_LIMITS.get(self.platform)
        if limit is not None and len(self.description) > limit:
            raise ValueError(
                f"{self.platform} description has {len(self.description)} characters; "
                f"the limit is {limit}"
            )
        return self


class ChannelRename(BaseModel):
    """A new display name for a channel; its slug never changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Text


class Channel(BaseModel):
    """A brand published on several platforms: its accounts and its brand kit."""

    model_config = ConfigDict(frozen=True)

    slug: ChannelSlug
    name: Text
    language: LanguageTag = DEFAULT_LANGUAGE
    status: Status = Status.DRAFT
    brand: BrandKit = Field(default_factory=BrandKit)
    accounts: list[PlatformAccount] = Field(default_factory=list)

    @field_validator("accounts")
    @classmethod
    def one_account_per_platform(cls, accounts: list[PlatformAccount]) -> list[PlatformAccount]:
        """Refuse two accounts on one platform, since pages and scopes are keyed by platform."""
        platforms = [account.platform for account in accounts]
        duplicates = sorted({p for p in platforms if platforms.count(p) > 1})
        if duplicates:
            raise ValueError(f"One account per platform; repeated: {', '.join(duplicates)}")
        return accounts

    def account(self, platform: Platform) -> PlatformAccount | None:
        """The channel's account on a platform, if it has one."""
        return next((a for a in self.accounts if a.platform == platform), None)

    def brand_for(self, platform: Platform | None = None) -> BrandKit:
        """The channel's brand with one platform's overrides applied."""
        account = self.account(platform) if platform else None
        override = account.brand if account else BrandKit()
        return BrandKit(
            avatar=override.avatar or self.brand.avatar,
            assets={**self.brand.assets, **override.assets},
            colors=override.colors or self.brand.colors,
            fonts={**self.brand.fonts, **override.fonts},
        )
