"""A/B experiments: two episodes of a channel linked with the change between them (rule project.experiments).

An experiment is stored and revised on its own, so writing a reading never conflicts with an
episode's parts. Its result is computed from the numbers people entered and never stored
(experiment_results.py). The two episodes are the control (A) and the variant (B).
"""

from __future__ import annotations

import builtins
import sqlite3
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from sreditor.channels import Platform
from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.field_types import ChannelSlug, Title
from sreditor.i18n import N_
from sreditor.post_metrics import Metric
from sreditor.what_worked import SnapshotAge

CHANGE_LENGTH = 1000
HYPOTHESIS_LENGTH = 2000
READING_LENGTH = 4000
FIRST_REVISION = 1

ChangeText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=CHANGE_LENGTH)]
HypothesisText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=HYPOTHESIS_LENGTH)
]
ReadingText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=READING_LENGTH)]
EpisodeNumber = Annotated[int, Field(ge=1)]
SAME_EPISODE = N_("Choose two different episodes.")
VERDICT_NEEDED = N_("A read experiment needs a verdict.")
VERDICT_ONLY_WHEN_READ = N_("Only a read experiment has a verdict.")


class ExperimentStatus(StrEnum):
    """Where an experiment stands: planned, running, read (concluded) or abandoned."""

    PLANNED = "planned"
    RUNNING = "running"
    READ = "read"
    ABANDONED = "abandoned"


class Verdict(StrEnum):
    """The person's conclusion once the experiment is read."""

    B_BETTER = "b_better"
    A_BETTER = "a_better"
    NO_DIFFERENCE = "no_difference"
    INCONCLUSIVE = "inconclusive"


class ExperimentInput(BaseModel):
    """What a person or an agent writes about an experiment; a save replaces all of it."""

    model_config = ConfigDict(frozen=True)

    title: Title
    episode_a: EpisodeNumber = Field(description="Number of the control episode.")
    episode_b: EpisodeNumber = Field(description="Number of the variant episode.")
    change: ChangeText = Field(
        default="", description="What differs between A and B (Markdown, short)."
    )
    hypothesis: HypothesisText = Field(
        default="", description="What you expect and why (Markdown)."
    )
    metric: Metric = Field(default=Metric.VIEWS, description="The number the pair is judged by.")
    snapshot_age: SnapshotAge = Field(
        default=SnapshotAge.LATEST, description="Which snapshot of each post is read."
    )
    platform: Platform | None = Field(
        default=None, description="Only posts on this platform; every platform when left out."
    )
    status: ExperimentStatus = ExperimentStatus.PLANNED
    started_on: date | None = Field(default=None, description="The day the experiment began.")
    reading: ReadingText = Field(
        default="", description="The person's conclusion (Markdown); the computed result is apart."
    )
    verdict: Verdict | None = Field(
        default=None,
        validate_default=True,
        description="Needed when the status is read, and only then.",
    )

    @field_validator("episode_b")
    @classmethod
    def differs_from_a(cls, number: int, info: ValidationInfo) -> int:
        """A pair names two different episodes."""
        if number == info.data.get("episode_a"):
            raise ValueError(SAME_EPISODE)
        return number

    @field_validator("verdict")
    @classmethod
    def goes_with_the_read_status(
        cls, verdict: Verdict | None, info: ValidationInfo
    ) -> Verdict | None:
        """A read experiment states its verdict, and no other status holds one."""
        status = info.data.get("status")
        if status is None:  # the status itself was refused, which the caller already hears
            return verdict
        if status is ExperimentStatus.READ and verdict is None:
            raise ValueError(VERDICT_NEEDED)
        if status is not ExperimentStatus.READ and verdict is not None:
            raise ValueError(VERDICT_ONLY_WHEN_READ)
        return verdict


class Experiment(ExperimentInput):
    """A stored experiment, with the revision to send back when changing it."""

    id: UUID
    channel: ChannelSlug
    revision: int
    created_at: datetime
    updated_at: datetime


class ExperimentNotFound(LookupError):
    """The channel has no experiment with that id."""


class EpisodeInExperiment(ValueError):
    """Experiments still compare the episode, so it cannot be deleted."""

    def __init__(self, number: int, experiments: list[UUID]) -> None:
        super().__init__(str(number))
        self.experiments = experiments


COLUMNS = (
    "id, channel_slug, title, episode_a, episode_b, change, hypothesis, metric, snapshot_age,"
    " platform, status, started_on, reading, verdict, revision, created_at, updated_at"
)
WRITABLE = (
    "title",
    "episode_a",
    "episode_b",
    "change",
    "hypothesis",
    "metric",
    "snapshot_age",
    "platform",
    "status",
    "started_on",
    "reading",
    "verdict",
)


def to_experiment(row: sqlite3.Row) -> Experiment:
    """An experiment row as a model."""
    started = row["started_on"]
    return Experiment(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        title=row["title"],
        episode_a=row["episode_a"],
        episode_b=row["episode_b"],
        change=row["change"],
        hypothesis=row["hypothesis"],
        metric=Metric(row["metric"]),
        snapshot_age=SnapshotAge(row["snapshot_age"]),
        platform=Platform(row["platform"]) if row["platform"] else None,
        status=ExperimentStatus(row["status"]),
        started_on=date.fromisoformat(started) if started else None,
        reading=row["reading"],
        verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        revision=row["revision"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def values_of(data: ExperimentInput) -> tuple[object, ...]:
    """The input's fields in the order of WRITABLE, as the database stores them."""
    return (
        data.title,
        data.episode_a,
        data.episode_b,
        data.change,
        data.hypothesis,
        data.metric.value,
        data.snapshot_age.value,
        data.platform.value if data.platform else None,
        data.status.value,
        data.started_on.isoformat() if data.started_on else None,
        data.reading,
        data.verdict.value if data.verdict else None,
    )


def experiment_label(experiment_id: UUID) -> str:
    """How errors name an experiment."""
    return f"Experiment {experiment_id}"


class ExperimentStore:
    """Experiments in the application database; deleting the channel deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self, channel: str, after: int | None, limit: int, status: ExperimentStatus | None = None
    ) -> builtins.list[Experiment]:
        """The channel's experiments in the order they were added, optionally of one status."""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {COLUMNS} FROM experiments WHERE channel_slug = ? AND position > ?"
                " AND (? IS NULL OR status = ?) ORDER BY position LIMIT ?",
                (channel, after or 0, status, status, limit),
            ).fetchall()
        return [to_experiment(row) for row in rows]

    def position(self, experiment_id: UUID) -> int:
        """Where an experiment sits in its channel's list; the list's cursor."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT position FROM experiments WHERE id = ?", (str(experiment_id),)
            ).fetchone()
        if row is None:
            raise ExperimentNotFound(str(experiment_id))
        return int(row["position"])

    def get(self, channel: str, experiment_id: UUID) -> Experiment:
        """One experiment of the channel."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {COLUMNS} FROM experiments WHERE channel_slug = ? AND id = ?",
                (channel, str(experiment_id)),
            ).fetchone()
        if row is None:
            raise ExperimentNotFound(str(experiment_id))
        return to_experiment(row)

    def create(self, channel: str, data: ExperimentInput) -> Experiment:
        """Add an experiment to the channel."""
        now = self.database.now()
        marks = ", ".join("?" for _ in WRITABLE)
        with self.database.connect() as connection:
            row = connection.execute(
                f"INSERT INTO experiments (id, channel_slug, {', '.join(WRITABLE)}, revision,"
                f" created_at, updated_at) VALUES (?, ?, {marks}, ?, ?, ?) RETURNING {COLUMNS}",
                (str(uuid4()), channel, *values_of(data), FIRST_REVISION, now, now),
            ).fetchone()
        return to_experiment(row)

    def replace(
        self, experiment: Experiment, data: ExperimentInput, expected_revision: int | None
    ) -> Experiment:
        """Replace every field; with `expected_revision`, only if still at it."""
        assignments = ", ".join(f"{name} = ?" for name in WRITABLE)
        with self.database.connect() as connection:
            row = connection.execute(
                f"UPDATE experiments SET {assignments}, revision = revision + 1, updated_at = ?"
                " WHERE id = ? AND (? IS NULL OR revision = ?) RETURNING " + COLUMNS,
                (
                    *values_of(data),
                    self.database.now(),
                    str(experiment.id),
                    expected_revision,
                    expected_revision,
                ),
            ).fetchone()
        if row is None:
            current = self.get(experiment.channel, experiment.id).revision
            raise PartChanged(experiment_label(experiment.id), current)
        return to_experiment(row)

    def delete(self, experiment: Experiment, expected_revision: int | None) -> None:
        """Delete an experiment; with `expected_revision`, only if nobody changed it since."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM experiments WHERE id = ? AND (? IS NULL OR revision = ?)",
                (str(experiment.id), expected_revision, expected_revision),
            )
        if cursor.rowcount == 0:
            current = self.get(experiment.channel, experiment.id).revision
            raise PartChanged(experiment_label(experiment.id), current)

    def of_episode(self, channel: str, number: int) -> builtins.list[UUID]:
        """The experiments that compare the episode, as control or as variant."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM experiments WHERE channel_slug = ?"
                " AND (episode_a = ? OR episode_b = ?) ORDER BY position",
                (channel, number, number),
            ).fetchall()
        return [UUID(row["id"]) for row in rows]
