"""Experiment pages: A/B experiments between two episodes, with the computed result (project.experiments).

The result is computed from the numbers people entered and shown next to the stored reading, which
is the person's own conclusion. Owners and editors write; every member reads.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.channels import Platform
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_operations import list_episodes
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeNotFound
from sreditor.experiment_operations import (
    ExperimentChange,
    ExperimentQuery,
    create_experiment,
    delete_experiment,
    experiment_result,
    get_experiment,
    list_experiments,
    replace_experiment,
)
from sreditor.experiment_results import ExperimentResult, ResultNote, Side
from sreditor.experiments import (
    CHANGE_LENGTH,
    HYPOTHESIS_LENGTH,
    READING_LENGTH,
    Experiment,
    ExperimentInput,
    ExperimentNotFound,
    ExperimentStatus,
    Verdict,
)
from sreditor.field_types import TITLE_LENGTH
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.post_metrics import Metric
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.labels import platform_name
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import RevisionFields, revision_of
from sreditor.web.workspace.post_metrics import METRIC_LABELS
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    require,
)
from sreditor.web.workspace.what_worked import AGE_LABELS
from sreditor.what_worked import SnapshotAge

router = APIRouter()

EXPERIMENTS_SUFFIX = "/experiments"
SECTION = "experiments"
NO_CHOICE = ""

STATUS_LABELS: dict[ExperimentStatus, str] = {
    ExperimentStatus.PLANNED: N_("Planned"),
    ExperimentStatus.RUNNING: N_("Running"),
    ExperimentStatus.READ: N_("Read"),
    ExperimentStatus.ABANDONED: N_("Abandoned"),
}
# The badge tone of each status (a class of the badge component).
STATUS_TONES: dict[ExperimentStatus, str] = {
    ExperimentStatus.PLANNED: "",
    ExperimentStatus.RUNNING: "accent",
    ExperimentStatus.READ: "success",
    ExperimentStatus.ABANDONED: "warning",
}
VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.B_BETTER: N_("B did better"),
    Verdict.A_BETTER: N_("A did better"),
    Verdict.NO_DIFFERENCE: N_("No difference"),
    Verdict.INCONCLUSIVE: N_("Inconclusive"),
}
NOTE_LABELS: dict[ResultNote, str] = {
    ResultNote.A_WITHOUT_POSTS: N_(
        "Episode A has no post here yet. Record it in its delivery package."
    ),
    ResultNote.B_WITHOUT_POSTS: N_(
        "Episode B has no post here yet. Record it in its delivery package."
    ),
    ResultNote.A_WITHOUT_NUMBERS: N_("Episode A's posts have no number at this age yet."),
    ResultNote.B_WITHOUT_NUMBERS: N_("Episode B's posts have no number at this age yet."),
    ResultNote.A_IS_ZERO: N_("Episode A is at zero, so the ratio is not defined."),
}
EXPERIMENT_CHANGED = N_(
    "Someone changed this experiment after you opened the page. Reload it to see their changes, "
    "then try again."
)
EPISODE_GONE = N_("One of the episodes no longer exists. Reload the page and choose again.")


class ExperimentFields(SubmittedFields):
    """An experiment as the form posts it; empty choices mean none."""

    title: str = ""
    episode_a: str = ""
    episode_b: str = ""
    change: str = ""
    hypothesis: str = ""
    metric: str = Metric.VIEWS.value
    snapshot_age: str = SnapshotAge.LATEST.value
    platform: str = NO_CHOICE
    status: str = ""
    started_on: str = ""
    reading: str = ""
    verdict: str = NO_CHOICE
    revision: str = ""


def experiments_url(slug: str, experiment_id: UUID | None = None) -> str:
    """The experiment list, or one experiment's page."""
    suffix = (
        EXPERIMENTS_SUFFIX if experiment_id is None else f"{EXPERIMENTS_SUFFIX}/{experiment_id}"
    )
    return channel_url(slug, suffix)


def as_input(fields: ExperimentFields) -> ExperimentInput:
    """The experiment a form describes; a new one starts as planned."""
    return ExperimentInput.model_validate(
        {
            "title": fields.title,
            "episode_a": fields.episode_a,
            "episode_b": fields.episode_b,
            "change": fields.change,
            "hypothesis": fields.hypothesis,
            "metric": fields.metric,
            "snapshot_age": fields.snapshot_age,
            "platform": fields.platform or None,
            "status": fields.status or ExperimentStatus.PLANNED.value,
            "started_on": fields.started_on or None,
            "reading": fields.reading,
            "verdict": fields.verdict or None,
        }
    )


def found_experiment(app: Services, access: ChannelAccess, experiment_id: str) -> Experiment:
    """The experiment in the path; 404 when the channel has none with that id."""
    try:
        return get_experiment(app, access, UUID(experiment_id))
    except (ValueError, ExperimentNotFound) as missing:
        raise PageNotFound(experiment_id) from missing


def episode_choices(app: Services, access: ChannelAccess) -> list[tuple[str, str]]:
    """Every episode of the channel as (number, "#number title") options."""
    options: list[tuple[str, str]] = []
    cursor: str | None = None
    while True:
        found = list_episodes(app, access, cursor, MAX_LIMIT)
        options.extend((str(item.number), f"#{item.number} · {item.title}") for item in found.items)
        cursor = found.next_cursor
        if cursor is None:
            return options


def choices(
    labels: dict[Any, str], translate: Translator, empty: str = ""
) -> list[tuple[str, str]]:
    """(value, label) options for an enum's labels, with an optional empty first choice."""
    first = [(NO_CHOICE, translate(empty))] if empty else []
    return first + [(key.value, translate(label)) for key, label in labels.items()]


def form_context(app: Services, access: ChannelAccess, translate: Translator) -> dict[str, Any]:
    """The options and limits the experiment form needs, for the list and the detail page."""
    return {
        "episode_options": episode_choices(app, access),
        "metric_options": choices(METRIC_LABELS, translate),
        "age_options": choices(AGE_LABELS, translate),
        "platform_options": [(NO_CHOICE, translate(N_("Every platform")))]
        + [(platform.value, platform_name(platform)) for platform in Platform],
        "status_options": choices(STATUS_LABELS, translate),
        "verdict_options": choices(VERDICT_LABELS, translate, N_("No verdict yet")),
        "status_labels": STATUS_LABELS,
        "status_tones": STATUS_TONES,
        "verdict_labels": VERDICT_LABELS,
        "title_limit": TITLE_LENGTH,
        "change_limit": CHANGE_LENGTH,
        "hypothesis_limit": HYPOTHESIS_LENGTH,
        "reading_limit": READING_LENGTH,
    }


class ListView(BaseModel):
    """The experiment list, with its status filter and the new-experiment form as it was sent."""

    model_config = ConfigDict(frozen=True)

    status: str = ""
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: ExperimentFields | None = None


def chosen_status(value: str) -> ExperimentStatus | None:
    """The status a filter link names, or None for every status."""
    try:
        return ExperimentStatus(value)
    except ValueError:
        return None


def list_page(request: Request, app: Services, access: ChannelAccess, shown: ListView) -> Response:
    """The channel's experiments and, for those who may write, the form to plan one."""
    translate = translator(request)
    status = chosen_status(shown.status)
    query = ExperimentQuery(status=status, cursor=shown.cursor or None, limit=DEFAULT_LIMIT)
    found = list_experiments(app, access, query)
    base = experiments_url(access.channel.slug)
    filters = [(base, translate(N_("All")), "")] + [
        (f"{base}?status={value}", label, value)
        for value, label in choices(STATUS_LABELS, translate)
    ]
    context = {
        "experiments": found.items,
        "next_cursor": found.next_cursor,
        "status_filters": filters,
        "status_filter": status.value if status else "",
        "experiments_link": base,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        **form_context(app, access, translate),
    }
    view = View(
        template="workspace/experiments.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/experiments")
def experiments(
    request: Request, app: ServicesDep, access: PageAccess, status: str = "", cursor: str = ""
) -> Response:
    """The channel's experiments, oldest first, optionally of one status."""
    return list_page(request, app, access, ListView(status=status, cursor=cursor))


@router.post("/app/channels/{slug}/experiments")
def new_experiment(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[ExperimentFields, Form()],
) -> Response:
    """Plan an experiment and open it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    translate = translator(request)
    try:
        created = create_experiment(app, access, as_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except EpisodeNotFound:
        outcome = Outcome.invalid({"form": translate(EPISODE_GONE)})
    else:
        url = experiments_url(access.channel.slug, created.id)
        return done(request, app, url, Flash.EXPERIMENT_CREATED)
    return list_page(request, app, access, ListView(outcome=outcome, sent=fields))


class DetailView(BaseModel):
    """One experiment's page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    outcome: Outcome = Outcome()
    sent: ExperimentFields | None = None


def number_text(value: float | None, translate: Translator) -> str:
    """A number as people read it, or that it is missing."""
    return translate(N_("No number yet")) if value is None else f"{value:g}"


def side_rows(result: ExperimentResult, translate: Translator) -> list[dict[str, Any]]:
    """Each side as its label, episode, mean and one line per post, ready for the template."""
    sides = ((N_("A · control"), result.a), (N_("B · variant"), result.b))
    return [side_row(label, side, translate) for label, side in sides]


def side_row(label: str, side: Side, translate: Translator) -> dict[str, Any]:
    """One side of the pair as the template draws it."""
    posts = [
        f"{platform_name(post.platform)} ({post.cut}) · {number_text(post.value, translate)}"
        for post in side.posts
    ]
    return {
        "label": translate(label),
        "episode": side.episode,
        "title": side.title,
        "mean": number_text(side.value, translate),
        "posts": posts,
    }


def comparison_pairs(result: ExperimentResult, translate: Translator) -> list[tuple[str, str]]:
    """The metric, the age, the difference and the ratio as (term, value) pairs."""
    return [
        (translate(N_("Number")), translate(METRIC_LABELS[result.metric])),
        (translate(N_("Numbers taken")), translate(AGE_LABELS[result.snapshot_age])),
        (translate(N_("B minus A")), number_text(result.difference, translate)),
        (translate(N_("B over A")), number_text(result.ratio, translate)),
    ]


def experiment_values(experiment: Experiment) -> ExperimentFields:
    """The form's fields as the stored experiment fills them."""
    return ExperimentFields(
        title=experiment.title,
        episode_a=str(experiment.episode_a),
        episode_b=str(experiment.episode_b),
        change=experiment.change,
        hypothesis=experiment.hypothesis,
        metric=experiment.metric.value,
        snapshot_age=experiment.snapshot_age.value,
        platform=experiment.platform.value if experiment.platform else NO_CHOICE,
        status=experiment.status.value,
        started_on=experiment.started_on.isoformat() if experiment.started_on else "",
        reading=experiment.reading,
        verdict=experiment.verdict.value if experiment.verdict else NO_CHOICE,
    )


def detail_context(
    app: Services,
    access: ChannelAccess,
    experiment: Experiment,
    shown: DetailView,
    translate: Translator,
) -> dict[str, Any]:
    """The experiment, its computed result and the forms, for its page."""
    reading = experiment_result(app, access, experiment.id)
    link = experiments_url(access.channel.slug, experiment.id)
    return {
        "experiment": experiment,
        "values": shown.sent or experiment_values(experiment),
        "result_sides": side_rows(reading.result, translate),
        "comparison": comparison_pairs(reading.result, translate),
        "result_notes": [translate(NOTE_LABELS[note]) for note in reading.result.notes],
        "experiment_link": link,
        "list_link": experiments_url(access.channel.slug),
        "episodes_link": channel_url(access.channel.slug, "/episodes"),
        "errors": shown.outcome.errors,
        **form_context(app, access, translate),
    }


def detail_page(
    request: Request, app: Services, access: ChannelAccess, shown: DetailView
) -> Response:
    """An experiment with its computed result next to the person's reading."""
    experiment = found_experiment(app, access, shown.experiment_id)
    context = detail_context(app, access, experiment, shown, translator(request))
    view = View(
        template="workspace/experiment.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/experiments/{experiment_id}")
def experiment(
    request: Request, app: ServicesDep, access: PageAccess, experiment_id: str
) -> Response:
    """One experiment with its result."""
    return detail_page(request, app, access, DetailView(experiment_id=experiment_id))


@router.post("/app/channels/{slug}/experiments/{experiment_id}")
def save_experiment(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    experiment_id: str,
    fields: Annotated[ExperimentFields, Form()],
) -> Response:
    """Save everything the experiment holds, its reading included."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    current = found_experiment(app, access, experiment_id)
    translate = translator(request)
    try:
        change = ExperimentChange(
            experiment_id=current.id, expected_revision=revision_of(fields.revision)
        )
        replace_experiment(app, access, change, as_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"form": translate(EXPERIMENT_CHANGED)})
    except EpisodeNotFound:
        outcome = Outcome.invalid({"form": translate(EPISODE_GONE)})
    else:
        return done(request, app, experiments_url(access.channel.slug, current.id), Flash.SAVED)
    shown = DetailView(experiment_id=experiment_id, outcome=outcome, sent=fields)
    return detail_page(request, app, access, shown)


@router.post("/app/channels/{slug}/experiments/{experiment_id}/delete")
def remove_experiment(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    experiment_id: str,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Delete an experiment; its episodes stay."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    current = found_experiment(app, access, experiment_id)
    try:
        change = ExperimentChange(
            experiment_id=current.id, expected_revision=revision_of(fields.revision)
        )
        delete_experiment(app, access, change)
    except PartChanged:
        outcome = Outcome.conflict({"form": translator(request)(EXPERIMENT_CHANGED)})
        return detail_page(
            request, app, access, DetailView(experiment_id=experiment_id, outcome=outcome)
        )
    return done(request, app, experiments_url(access.channel.slug), Flash.EXPERIMENT_DELETED)
