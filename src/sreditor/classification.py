"""Editorial classification of episodes and the channel's profile (rules project.classification,
project.classification.checks and project.classification.profile).

The vocabulary is Pedro's video-classification 1.0.0 taxonomy: twelve dimensions with closed
lists of values. Each dimension is present, absent, unknown or not applicable, and a present one
holds one primary value and up to two secondary ones. An episode's classification is a text part
of its own (JSON), so writing it never conflicts with the screenplay or the research; the
channel's declared profile is one row per channel. The observed profile is counted from the
episodes on each read, never stored, so it cannot drift from them.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.database import Database
from sreditor.episode_texts import UNWRITTEN, EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.i18n import N_

SCHEMA_VERSION = "1.0.0"  # the taxonomy version this vocabulary copies
MAX_ITEMS = 3  # one primary and up to two secondary values
MAX_SECONDARY = 2
MAX_REFERENCES = 5
SATIRE_LENGTH = 300
REFERENCE_LENGTH = 300
# A declared primary value seen in fewer episodes than this share is drift (rule ...profile).
DRIFT_BELOW = 0.5
PROFILE_LABEL = "Channel profile"
CLASSIFICATION_LABEL = "Classification"

SatireText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SATIRE_LENGTH)
]
Reference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REFERENCE_LENGTH)
]


class Dimension(StrEnum):
    """An editorial dimension with a closed list of values."""

    AUDIENCE = "audience"
    DOMAINS = "domains"
    PURPOSES = "purposes"
    GENRES = "genres"
    APPROACHES = "approaches"
    FORMATS = "formats"
    VISUAL_TECHNIQUES = "visual_techniques"
    VISUAL_STYLES = "visual_styles"
    TONES = "tones"
    EMOTIONAL_TARGETS = "emotional_targets"
    CREATIVE_DEVICES = "creative_devices"
    HUMOR_DEVICES = "humor_devices"


# taxonomy.json of video-classification 1.0.0; a new value is a new revision of the taxonomy.
VOCABULARY: dict[Dimension, tuple[str, ...]] = {
    Dimension.AUDIENCE: ("preschool", "children", "teen", "adult", "general", "specialist"),
    Dimension.DOMAINS: (
        "science",
        "science.physics",
        "science.cosmology",
        "mathematics",
        "politics",
        "history",
        "society",
        "economy",
        "economy.banking",
        "engineering",
        "military_technology",
        "aviation",
        "daily_life",
        "relationships",
        "religion",
        "childhood",
        "music",
        "technology",
        "biology",
        "psychology",
    ),
    Dimension.PURPOSES: (
        "explain",
        "teach",
        "entertain",
        "inform",
        "persuade",
        "sell",
        "provoke_reflection",
    ),
    Dimension.GENRES: (
        "comedy",
        "documentary",
        "educational",
        "journalistic",
        "musical",
        "drama",
        "suspense",
    ),
    Dimension.APPROACHES: (
        "satirical",
        "parodic",
        "analytical",
        "investigative",
        "demonstrative",
        "observational",
        "explanatory",
    ),
    Dimension.FORMATS: (
        "sketch",
        "visual_essay",
        "presenter_explainer",
        "interview",
        "tutorial",
        "music_video",
        "narrated_documentary",
        "demonstration",
        "list",
        "performance",
    ),
    Dimension.VISUAL_TECHNIQUES: (
        "live_action",
        "animation_2d",
        "animation_3d",
        "stop_motion",
        "screen_capture",
    ),
    Dimension.VISUAL_STYLES: (
        "photorealistic",
        "cartoon",
        "pixel_art",
        "collage",
        "diagrammatic",
        "commercial_parody",
    ),
    Dimension.TONES: ("serious", "ironic", "playful", "absurd", "warm", "confrontational"),
    Dimension.EMOTIONAL_TARGETS: (
        "curiosity",
        "wonder",
        "amusement",
        "indignation",
        "empathy",
        "tension",
        "calm",
    ),
    Dimension.CREATIVE_DEVICES: (
        "visual_metaphor",
        "contrast",
        "inversion",
        "personification",
        "repetition",
        "analogy",
    ),
    Dimension.HUMOR_DEVICES: (
        "incongruity",
        "exaggeration",
        "escalation",
        "deadpan",
        "wordplay",
        "anticlimax",
        "callback",
        "slapstick",
    ),
}
COMEDY = "comedy"
SATIRICAL = "satirical"
PARODIC = "parodic"


class State(StrEnum):
    """Whether a dimension holds values; only `present` may."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Role(StrEnum):
    """How much a value defines the episode: editorially, not by screen time."""

    PRIMARY = "primary"
    SECONDARY = "secondary"


class Method(StrEnum):
    """Who classified it: a person, an AI, or an AI with a person."""

    HUMAN = "human"
    AI = "ai"
    MIXED = "mixed"


class ClassifiedValue(BaseModel):
    """One value of a dimension, its role and, from an AI, how sure it is."""

    model_config = ConfigDict(frozen=True)

    value: str = Field(description="A value from the dimension's vocabulary.")
    role: Role = Role.PRIMARY
    confidence: float | None = Field(default=None, ge=0, le=1)


class DimensionClass(BaseModel):
    """A dimension's state and, when present, its values."""

    model_config = ConfigDict(frozen=True)

    state: State = State.UNKNOWN
    items: list[ClassifiedValue] = Field(default_factory=list, max_length=MAX_ITEMS)


class Satire(BaseModel):
    """What a satire aims at and the behaviour it criticises."""

    model_config = ConfigDict(frozen=True)

    target: SatireText
    criticized_behavior: SatireText


class Classification(BaseModel):
    """An editorial classification: each dimension, satire and parody details, and the method.

    A dimension left out is unknown.
    """

    model_config = ConfigDict(frozen=True)

    dimensions: dict[Dimension, DimensionClass] = Field(default_factory=dict)
    satire: Satire | None = None
    parody_references: list[Reference] = Field(default_factory=list, max_length=MAX_REFERENCES)
    method: Method = Method.HUMAN

    def of(self, dimension: Dimension) -> DimensionClass:
        """The dimension as classified; unknown when left out."""
        return self.dimensions.get(dimension, DimensionClass())

    def values(self, dimension: Dimension) -> list[str]:
        """The values a present dimension holds, primary first."""
        found = self.of(dimension)
        ordered = sorted(found.items, key=lambda item: item.role != Role.PRIMARY)
        return [item.value for item in ordered] if found.state is State.PRESENT else []

    def primary(self, dimension: Dimension) -> str | None:
        """The dimension's primary value, if present."""
        values = self.values(dimension)
        return values[0] if values else None


class Refusal(BaseModel):
    """Why a classification cannot be saved: the field at fault and what to fix."""

    model_config = ConfigDict(frozen=True)

    field: str
    message: str


class ClassificationRefused(ValueError):
    """A classification that breaks the rules; every refusal names its field."""

    def __init__(self, refusals: list[Refusal]) -> None:
        super().__init__("; ".join(f"{found.field}: {found.message}" for found in refusals))
        self.refusals = refusals


def dimension_refusals(
    dimension: Dimension, found: DimensionClass, method: Method
) -> list[Refusal]:
    """The rules of one dimension: states, one primary, at most two secondary, the vocabulary."""
    field = f"dimensions.{dimension.value}"
    if found.state is not State.PRESENT:
        if found.items:
            return [Refusal(field=field, message=N_("Only a present dimension holds values"))]
        return []
    if not found.items:
        return [Refusal(field=field, message=N_("A present dimension needs at least one value"))]
    roles = [item.role for item in found.items]
    values = [item.value for item in found.items]
    checks = (
        (roles.count(Role.PRIMARY) != 1, N_("A present dimension needs exactly one primary value")),
        (roles.count(Role.SECONDARY) > MAX_SECONDARY, N_("At most two secondary values")),
        (len(set(values)) != len(values), N_("A value appears twice")),
        (
            method is not Method.HUMAN and any(item.confidence is None for item in found.items),
            N_("An AI or mixed classification gives every value a confidence"),
        ),
    )
    refusals = [Refusal(field=field, message=message) for is_broken, message in checks if is_broken]
    unknown = [value for value in values if value not in VOCABULARY[dimension]]
    if unknown:
        allowed = ", ".join(VOCABULARY[dimension])
        message = f"Unknown value {unknown[0]}; use one of {allowed}"
        refusals.append(Refusal(field=field, message=message))
    return refusals


def editorial_refusals(data: Classification) -> list[Refusal]:
    """Comedy names a humor device, satire its target, parody its reference."""
    checks = (
        (
            COMEDY in data.values(Dimension.GENRES) and not data.values(Dimension.HUMOR_DEVICES),
            "dimensions.humor_devices",
            N_("Comedy needs at least one humor device"),
        ),
        (
            SATIRICAL in data.values(Dimension.APPROACHES) and data.satire is None,
            "satire",
            N_("A satirical approach needs its target and the behaviour it criticises"),
        ),
        (
            data.satire is not None and SATIRICAL not in data.values(Dimension.APPROACHES),
            "satire",
            N_("Satire details need a satirical approach"),
        ),
        (
            PARODIC in data.values(Dimension.APPROACHES) and not data.parody_references,
            "parody_references",
            N_("A parodic approach needs the work it parodies"),
        ),
        (
            bool(data.parody_references) and PARODIC not in data.values(Dimension.APPROACHES),
            "parody_references",
            N_("Parody references need a parodic approach"),
        ),
    )
    return [
        Refusal(field=field, message=message) for is_broken, field, message in checks if is_broken
    ]


def refusals_of(data: Classification) -> list[Refusal]:
    """Every rule the classification breaks; empty when it may be saved."""
    found = [
        refusal
        for dimension, classified in data.dimensions.items()
        for refusal in dimension_refusals(dimension, classified, data.method)
    ]
    return found + editorial_refusals(data)


def check(data: Classification) -> None:
    """Refuse a classification that breaks the rules."""
    refusals = refusals_of(data)
    if refusals:
        raise ClassificationRefused(refusals)


class EpisodeClassification(BaseModel):
    """An episode's classification; `is_reviewed` is false for an agent's proposal."""

    model_config = ConfigDict(frozen=True)

    number: int
    classification: Classification = Field(default_factory=Classification)
    is_reviewed: bool = False
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class ChannelProfile(BaseModel):
    """The profile a channel declares for itself."""

    model_config = ConfigDict(frozen=True)

    classification: Classification = Field(default_factory=Classification)
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class Observation(BaseModel):
    """How often a value appears among the channel's classified episodes."""

    model_config = ConfigDict(frozen=True)

    dimension: Dimension
    value: str
    primary_count: int
    episode_count: int  # primary or secondary
    prevalence: float  # episode_count over all classified episodes, unknown included


class Drift(BaseModel):
    """A value the channel declares as primary that few of its episodes hold."""

    model_config = ConfigDict(frozen=True)

    dimension: Dimension
    value: str
    prevalence: float


class ObservedProfile(BaseModel):
    """The profile counted from the classified episodes, and where it drifts from the declared."""

    model_config = ConfigDict(frozen=True)

    sample_size: int
    unreviewed: int
    observations: list[Observation]
    drift: list[Drift]


def observe(episodes: list[EpisodeClassification]) -> list[Observation]:
    """Count each value over the episodes, in vocabulary order."""
    size = len(episodes)
    observations = []
    for dimension, vocabulary in VOCABULARY.items():
        for value in vocabulary:
            held = [found.classification.values(dimension) for found in episodes]
            count = sum(value in values for values in held)
            if count:
                primary = sum(bool(values) and values[0] == value for values in held)
                observations.append(
                    Observation(
                        dimension=dimension,
                        value=value,
                        primary_count=primary,
                        episode_count=count,
                        prevalence=count / size,
                    )
                )
    return observations


def drift_of(declared: Classification, observations: list[Observation], size: int) -> list[Drift]:
    """Declared primary values held by fewer than half the classified episodes."""
    if not size:
        return []
    shares = {(found.dimension, found.value): found.prevalence for found in observations}
    drift = []
    for dimension in Dimension:
        value = declared.primary(dimension)
        share = shares.get((dimension, value), 0.0) if value else None
        if value and share is not None and share < DRIFT_BELOW:
            drift.append(Drift(dimension=dimension, value=value, prevalence=share))
    return drift


def observed_profile(
    declared: Classification, episodes: list[EpisodeClassification]
) -> ObservedProfile:
    """The observed profile of the episodes, compared with the declared one."""
    observations = observe(episodes)
    return ObservedProfile(
        sample_size=len(episodes),
        unreviewed=sum(not found.is_reviewed for found in episodes),
        observations=observations,
        drift=drift_of(declared, observations, len(episodes)),
    )


class StoredClassification(BaseModel):
    """The JSON an episode's classification part holds."""

    model_config = ConfigDict(frozen=True)

    classification: Classification
    is_reviewed: bool


class ClassificationStore:
    """Episode classifications as a text part; the channel profile in its own table."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.texts = EpisodeTexts(database)

    def episode(self, channel: str, number: int) -> EpisodeClassification:
        """An episode's classification; all unknown at revision 0 when never written."""
        key = TextKey(part=TextPart.CLASSIFICATION, channel=channel, number=number)
        stored = self.texts.read(key)
        if not stored.text:
            return EpisodeClassification(number=number)
        parsed = StoredClassification.model_validate_json(stored.text)
        return EpisodeClassification(
            number=number,
            classification=parsed.classification,
            is_reviewed=parsed.is_reviewed,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )

    def write_episode(
        self, key: TextKey, stored: StoredClassification, expected_revision: int | None
    ) -> EpisodeClassification:
        """Replace an episode's classification; PartChanged when it moved on."""
        revision = self.texts.write(key, stored.model_dump_json(), expected_revision)
        if revision is None:
            current = self.episode(key.channel, key.number)
            raise PartChanged(f"{CLASSIFICATION_LABEL} of episode {key.number}", current.revision)
        return self.episode(key.channel, key.number)

    def classified(self, channel: str) -> list[EpisodeClassification]:
        """Every classified episode of the channel, by number."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT number, text, revision, updated_at FROM {TextPart.CLASSIFICATION}"
                " WHERE channel_slug = ? AND text != '' ORDER BY number",
                (channel,),
            ).fetchall()
        found = []
        for row in rows:
            parsed = StoredClassification.model_validate_json(row["text"])
            found.append(
                EpisodeClassification(
                    number=row["number"],
                    classification=parsed.classification,
                    is_reviewed=parsed.is_reviewed,
                    revision=row["revision"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
            )
        return found

    def profile(self, channel: str) -> ChannelProfile:
        """The channel's declared profile; all unknown at revision 0 when never written."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT document, revision, updated_at FROM channel_profiles WHERE channel_slug = ?",
                (channel,),
            ).fetchone()
        if row is None:
            return ChannelProfile()
        return ChannelProfile(
            classification=Classification.model_validate(json.loads(row["document"])),
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def write_profile(
        self, channel: str, data: Classification, expected_revision: int | None
    ) -> ChannelProfile:
        """Replace the declared profile; PartChanged when it moved on."""
        document = data.model_dump_json()
        now = self.database.now()
        if expected_revision is None:
            sql = (
                "INSERT INTO channel_profiles (channel_slug, document, revision, updated_at)"
                " VALUES (?, ?, 1, ?) ON CONFLICT (channel_slug) DO UPDATE SET"
                " document = excluded.document, revision = revision + 1,"
                " updated_at = excluded.updated_at RETURNING revision"
            )
            parameters: tuple[object, ...] = (channel, document, now)
        elif expected_revision == UNWRITTEN:
            sql = (
                "INSERT OR IGNORE INTO channel_profiles (channel_slug, document, revision,"
                " updated_at) VALUES (?, ?, 1, ?) RETURNING revision"
            )
            parameters = (channel, document, now)
        else:
            sql = (
                "UPDATE channel_profiles SET document = ?, revision = revision + 1,"
                " updated_at = ? WHERE channel_slug = ? AND revision = ? RETURNING revision"
            )
            parameters = (document, now, channel, expected_revision)
        with self.database.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise PartChanged(PROFILE_LABEL, self.profile(channel).revision)
        return self.profile(channel)
