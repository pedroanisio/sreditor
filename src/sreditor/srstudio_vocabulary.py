"""srstudio's closed vocabularies, copied and pinned so sreditor can write files srstudio reads (rule project.srstudio).

sreditor never imports srstudio: when srstudio changes one of these lists, this copy is updated by hand
together with SRSTUDIO_REVISION, and tests/test_srstudio_vocabulary.py pins what the exporter relies on.
"""

from __future__ import annotations

import re
from enum import StrEnum

# The srstudio commit these lists were copied from (git log -1 in /home/pals/src/worskpace/srstudio).
SRSTUDIO_REVISION = "d289a03d"
BIBLE_SCHEMA = "1.0.0"  # srstudio/bible.py SCHEMA

# srstudio/bible.py SLUG: lowercase words joined by single hyphens.
SLUG_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
SLUG = re.compile(SLUG_PATTERN)
# A text field of the bible that nobody has settled yet starts with this mark (srstudio/bible.py MARKS).
OPEN_MARK = "open:"
MIN_BEAT_STEP_SECONDS = 0.15  # srstudio/sketch.py MIN_STEP
MIN_SPEED = 0.7  # srstudio/voice.py VoiceSettings
MAX_SPEED = 1.2


class Style(StrEnum):
    """srstudio/styles.py STYLES: each fixes the frame size and the frames per second."""

    CUTOUT = "cutout"
    INK = "ink"
    SOFT = "soft"
    CEL = "cel"
    STICK = "stick"
    TOON = "toon"
    SAGA = "saga"
    COLLAGE = "collage"
    GLOSS = "gloss"


# Width, height and frames per second of each style (srstudio/styles.py).
FRAMES: dict[Style, tuple[int, int, int]] = {
    Style.CUTOUT: (1080, 1920, 30),
    Style.INK: (1080, 1080, 24),
    Style.SOFT: (1080, 1920, 30),
    Style.CEL: (1080, 1920, 24),
    Style.STICK: (1080, 1920, 24),
    Style.TOON: (1080, 1920, 24),
    Style.SAGA: (1080, 1920, 24),
    Style.COLLAGE: (1080, 1920, 24),
    Style.GLOSS: (1080, 1920, 24),
}
# The styles whose sketches take beats (eyes, mouth, arms, wear) from the toon cast.
BEAT_STYLES = frozenset({Style.TOON})


class World(StrEnum):
    """srstudio/bible.py WORLDS."""

    OKAYVILLE = "okayville"
    SAGA = "saga"
    GLOSS = "gloss"
    NEWS = "news"
    HOST = "host"
    MIND = "mind"
    MAPS = "maps"


class CharacterStatus(StrEnum):
    """srstudio/bible.py STATUSES."""

    CANON = "canon"
    PLACEHOLDER = "placeholder"


class CharacterKind(StrEnum):
    """srstudio/bible.py KINDS."""

    PERSON = "person"
    ANIMAL = "animal"
    OBJECT = "object"
    ARCHETYPE = "archetype"


class ComicRole(StrEnum):
    """srstudio/bible.py ROLES."""

    STRAIGHT = "straight"
    INSTIGATOR = "instigator"
    VICTIM = "victim"
    CHAOS = "chaos"
    KNOW_IT_ALL = "know-it-all"


class FactKind(StrEnum):
    """srstudio/bible.py FACT_KINDS: how a published episode shows a fact."""

    LINE = "line"
    DRAWN = "drawn"
    VOICE = "voice"
    SEEN = "seen"


class FactField(StrEnum):
    """srstudio/bible.py FACT_FIELDS: the part of a bible entry a fact stands behind."""

    IDENTITY = "identity"
    LOOK_PALETTE = "look.palette"
    LOOK_OUTFITS = "look.outfits"
    LOOK_NEVER_CHANGES = "look.never_changes"
    VOICE_PROVIDER = "voice.provider"
    VOICE_VOICE = "voice.voice"
    VOICE_SETTINGS = "voice.settings"
    VOICE_PACE = "voice.pace"
    VOICE_TICS = "voice.tics"
    VOICE_USES = "voice.uses"
    VOICE_NEVER_USES = "voice.never_uses"
    WHO_AGE_RANGE = "who.age_range"
    WHO_JOB = "who.job"
    WHO_HOME = "who.home"
    WHO_NORMAL_DAY = "who.normal_day"
    WHO_WANTS = "who.wants"
    WHO_FLAW = "who.flaw"
    WHO_HABIT = "who.habit"
    WHO_POV = "who.pov"
    COMIC_ROLE = "comic.role"
    COMIC_UNDER_PRESSURE = "comic.under_pressure"
    COMIC_ALWAYS_GETS_THEM = "comic.always_gets_them"
    RELATIONSHIP = "relationship"
    WORLD = "world"


class PairStatus(StrEnum):
    """srstudio/bible.py PAIR_STATUSES."""

    CANON = "canon"
    PROPOSAL = "proposal"


class Eyes(StrEnum):
    """srstudio/toon.py EYES."""

    WIDE = "wide"
    HALF = "half"
    LEFT = "left"
    UP = "up"
    DOWN = "down"
    SIDE = "side"
    CLOSED = "closed"
    NARROW = "narrow"
    SHOCK = "shock"
    ROLL = "roll"
    TEARY = "teary"
    WINCE = "wince"
    HEARTS = "hearts"
    SLEEPY = "sleepy"


class Mouth(StrEnum):
    """srstudio/toon.py MOUTHS."""

    FLAT = "flat"
    OPEN = "open"
    ROUND = "round"
    O = "o"
    DROP = "drop"
    GRIN = "grin"
    WAVE = "wave"
    SMIRK = "smirk"
    NONE = "none"
    GRIMACE = "grimace"
    FROWN = "frown"
    TONGUE = "tongue"
    KISS = "kiss"
    YAWN = "yawn"
    SCREAM = "scream"
    PUFF = "puff"


class Arms(StrEnum):
    """srstudio/toon.py ARMS."""

    DESK = "desk"
    TYPE = "type"
    CROSSED = "crossed"
    SIGH = "sigh"
    LEAN = "lean"
    FACE = "face"
    FACE2 = "face2"


class Wear(StrEnum):
    """srstudio/toon.py WEAR."""

    GLASSES = "glasses"
    SWEAT = "sweat"
    FLUSH = "flush"


# srstudio/sketch.py CHARACTERS: who a toon beat may show; a rig named toon-<name> draws them.
TOON_CHARACTERS = frozenset(
    {
        "cat",
        "dog",
        "frog",
        "bear",
        "rabbit",
        "fox",
        "raccoon",
        "hedgehog",
        "sloth",
        "goat",
        "hamster",
        "bento",
        "lyra",
    }
)
TOON_RIG_PREFIX = "toon-"

# srstudio/direction.py AUDIO_TAGS: the Eleven v3 and v4 audio tags srstudio accepts in a direction.
AUDIO_TAGS = frozenset(
    {
        "laughs",
        "laughs harder",
        "starts laughing",
        "wheezing",
        "whispers",
        "sighs",
        "exhales",
        "sarcastic",
        "curious",
        "excited",
        "crying",
        "snorts",
        "mischievously",
        "shouts",
        "clears throat",
        "gunshot",
        "applause",
        "clapping",
        "explosion",
        "swallows",
        "gulps",
        "sings",
        "woo",
    }
)


def is_slug(text: str) -> bool:
    """Whether the text is a slug srstudio accepts."""
    return SLUG.fullmatch(text) is not None


def toon_character(rig: str) -> str | None:
    """The toon cast name a rig draws (toon-dog draws dog), or None when the rig is not a toon character."""
    name = rig.removeprefix(TOON_RIG_PREFIX)
    return name if rig.startswith(TOON_RIG_PREFIX) and name in TOON_CHARACTERS else None


def is_unknown_toon_rig(rig: str) -> bool:
    """Whether a rig claims srstudio's toon cast (toon-...) but names no one in it, so nothing draws it."""
    return rig.startswith(TOON_RIG_PREFIX) and toon_character(rig) is None


def toon_rigs() -> list[str]:
    """Every toon rig srstudio draws, sorted, for a message that lists the valid ones."""
    return sorted(TOON_RIG_PREFIX + name for name in TOON_CHARACTERS)
