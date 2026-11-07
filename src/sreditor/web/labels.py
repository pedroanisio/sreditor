"""How the pages name enum values: English msgids (N_) or names that are never translated."""

from __future__ import annotations

from sreditor.assets import BrandSlot
from sreditor.channels import Platform, Status
from sreditor.editorial import GuidelineKind
from sreditor.episodes import EpisodeStatus
from sreditor.i18n import N_, Translator
from sreditor.tokens import Scope

# Brand names, written the way each platform writes its own; never translated.
PLATFORM_NAMES: dict[Platform, str] = {
    Platform.TIKTOK: "TikTok",
    Platform.INSTAGRAM: "Instagram",
    Platform.YOUTUBE: "YouTube",
    Platform.X: "X",
    Platform.FACEBOOK: "Facebook",
    Platform.KWAI: "Kwai",
}

STATUS_LABELS: dict[Status, str] = {
    Status.DRAFT: N_("Draft"),
    Status.ACTIVE: N_("Active"),
    Status.PAUSED: N_("Paused"),
    Status.ARCHIVED: N_("Archived"),
}

# The badge tone of each status (a class of the badge component).
STATUS_TONES: dict[Status, str] = {
    Status.DRAFT: "",
    Status.ACTIVE: "success",
    Status.PAUSED: "warning",
    Status.ARCHIVED: "",
}

EPISODE_STATUS_LABELS: dict[EpisodeStatus, str] = {
    EpisodeStatus.IDEA: N_("Idea"),
    EpisodeStatus.RESEARCH: N_("Research"),
    EpisodeStatus.SCRIPT: N_("Script"),
    EpisodeStatus.PRODUCTION: N_("Production"),
    EpisodeStatus.PUBLISHED: N_("Published"),
}

# The badge tone of each episode status (a class of the badge component).
EPISODE_STATUS_TONES: dict[EpisodeStatus, str] = {
    EpisodeStatus.IDEA: "",
    EpisodeStatus.RESEARCH: "accent",
    EpisodeStatus.SCRIPT: "accent",
    EpisodeStatus.PRODUCTION: "warning",
    EpisodeStatus.PUBLISHED: "success",
}

SLOT_LABELS: dict[BrandSlot, str] = {
    BrandSlot.AVATAR: N_("Avatar"),
    BrandSlot.LOGO: N_("Logo"),
    BrandSlot.BANNER: N_("Banner"),
    BrandSlot.WATERMARK: N_("Watermark"),
    BrandSlot.INTRO: N_("Intro video"),
    BrandSlot.OUTRO: N_("Outro video"),
    BrandSlot.THUMBNAIL_TEMPLATE: N_("Thumbnail template"),
    BrandSlot.FONT: N_("Font"),
}

KIND_LABELS: dict[GuidelineKind, str] = {
    GuidelineKind.PURPOSE: N_("Purpose"),
    GuidelineKind.VOICE: N_("Voice"),
    GuidelineKind.AUDIENCE: N_("Audience"),
    GuidelineKind.PILLAR: N_("Content pillars"),
    GuidelineKind.AVOID: N_("Avoid"),
}

KIND_HINTS: dict[GuidelineKind, str] = {
    GuidelineKind.PURPOSE: N_("Why the channel exists, in one or two sentences."),
    GuidelineKind.VOICE: N_("How the channel sounds: tone, words it uses and words it doesn't."),
    GuidelineKind.AUDIENCE: N_("Who the channel speaks to."),
    GuidelineKind.PILLAR: N_("A theme the channel keeps coming back to."),
    GuidelineKind.AVOID: N_("Something the channel never does or says."),
}

SCOPE_HINTS: dict[Scope, str] = {
    Scope.CHANNELS_READ: N_("Read channels, their accounts, brand kits and editorial."),
    Scope.CHANNELS_WRITE: N_("Create channels and change their accounts and brand kits."),
    Scope.CHANNELS_DELETE: N_("Delete channels."),
    Scope.EDITORIAL_WRITE: N_("Write and retire editorial guidelines."),
    Scope.MEMBERS_WRITE: N_("Invite people and change their roles."),
    Scope.CONTENT_WRITE: N_("Create and edit episodes."),
    Scope.WAIVERS_READ: N_("Read episodes' signed waivers, which hold personal data."),
    Scope.RENDER_WRITE: N_("Render episodes into video on the render workers."),
}


def status_options(translate: Translator) -> list[tuple[str, str]]:
    """(value, label) for a status picker."""
    return [(status.value, translate(label)) for status, label in STATUS_LABELS.items()]


def episode_options(translate: Translator) -> list[tuple[str, str]]:
    """(value, label) for an episode status picker."""
    return [(status.value, translate(label)) for status, label in EPISODE_STATUS_LABELS.items()]


def platform_name(platform: Platform) -> str:
    """How a platform is written on the pages."""
    return PLATFORM_NAMES[platform]
