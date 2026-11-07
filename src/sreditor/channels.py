"""Channels: a brand published on several platforms, each with its own account."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

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


SLUG_LENGTH = 64
TEXT_LENGTH = 200
ASSET_PATH_LENGTH = 512
DEFAULT_LANGUAGE = "pt-BR"
SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
LANGUAGE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"  # a BCP 47 tag such as pt-BR
HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MEDIA_TYPE_PATTERN = r"^[a-z]+/[a-z0-9.+-]+$"
RELATIVE_PATH_PATTERN = r"^[^/].*$"
VOICE_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, min_length=1, max_length=SLUG_LENGTH)]
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
    media_type: Annotated[str, StringConstraints(pattern=MEDIA_TYPE_PATTERN)]
    sha256: Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
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


class Color(BaseModel):
    """A named brand color."""

    model_config = ConfigDict(frozen=True)

    name: Text
    hex: Annotated[str, StringConstraints(pattern=HEX_PATTERN)]


class NarratorVoice(BaseModel):
    """The ElevenLabs voice that narrates when a short script names none (rule project.voice.estimate)."""

    model_config = ConfigDict(frozen=True)

    voice_id: Annotated[str, StringConstraints(pattern=VOICE_ID_PATTERN, min_length=1)]
    voice_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=TEXT_LENGTH)] = (
        ""
    )
    tuning: VoiceTuning | None = None


class BrandKit(BaseModel):
    """A brand's avatar, files and colors; a platform's kit overrides the channel's."""

    model_config = ConfigDict(frozen=True)

    avatar: Asset | None = None
    assets: dict[AssetKind, Asset] = Field(default_factory=dict)
    colors: list[Color] = Field(default_factory=list)
    narrator_voice: NarratorVoice | None = None

    @field_validator("avatar")
    @classmethod
    def avatar_is_square(cls, avatar: Asset | None) -> Asset | None:
        """Platforms crop avatars to a square or circle, so anything else would be cut."""
        if avatar is not None and avatar.width is not None and avatar.width != avatar.height:
            raise ValueError("Avatar must be square")
        return avatar


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


class Channel(BaseModel):
    """A brand published on several platforms: its accounts and its brand kit."""

    model_config = ConfigDict(frozen=True)

    slug: Slug
    name: Text
    language: Annotated[str, StringConstraints(pattern=LANGUAGE_PATTERN)] = DEFAULT_LANGUAGE
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
        )
