"""Pages for a post's numbers and for comparing a channel's posts (rule project.metrics).

Numbers are typed in or imported from a platform's retention CSV; sreditor never fetches them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.post_metrics import Metric, Numbers, SnapshotInput
from sreditor.post_metrics_operations import (
    PostNotRecorded,
    PostQuery,
    RetentionImport,
    compare_posts,
    import_retention,
    list_snapshots,
    post_of,
    record_metrics,
)
from sreditor.problems import ProblemError
from sreditor.retention_csv import MAX_CSV_CHARS, PROBLEM_MESSAGES, RetentionCsvError
from sreditor.web.forms import field_errors
from sreditor.web.labels import platform_name
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.delivery import PostFormPath, PostTarget, delivery_url
from sreditor.web.workspace.episodes import episode_frame, found_episode, labels
from sreditor.web.workspace.forms import RetentionFields, SnapshotFields
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    refused,
    require,
)

router = APIRouter()

PAGE = "/app/channels/{slug}/episodes/{number}/delivery/metrics/{platform}"
COMPARE_PAGE = "/app/channels/{slug}/metrics"
COMPARE_SUFFIX = "/metrics"
UTF8_MAX_BYTES = 4  # so the bytes read for a CSV can hold MAX_CSV_CHARS characters, plus one
NO_NUMBERS = N_("No numbers yet.")
NOT_RECORDED = N_("This post is not in the delivery package. Add it there first.")
METRIC_LABELS: dict[Metric, str] = {
    Metric.VIEWS: N_("Views"),
    Metric.REACH: N_("Reach"),
    Metric.LIKES: N_("Likes"),
    Metric.COMMENTS: N_("Comments"),
    Metric.SHARES: N_("Shares"),
    Metric.SAVES: N_("Saves"),
    Metric.AVG_WATCH_S: N_("Average watch time (s)"),
}


def metrics_url(slug: str, number: int, platform: str) -> str:
    """The numbers page of one post."""
    return f"{delivery_url(slug, number)}/metrics/{platform}"


def as_snapshot(fields: SnapshotFields) -> SnapshotInput:
    """What a form says about a day's numbers; an empty box is a number left out."""
    given = {
        name: value.strip() or None
        for name, value in fields.model_dump(exclude={"csrf_token"}).items()
    }
    return SnapshotInput.model_validate(given)


def numbers_text(numbers: Numbers | None, translate: Translator) -> str:
    """Numbers as one line, "Views 120 · Likes 7"; numbers left out are skipped."""
    parts = [
        f"{translate(label)} {getattr(numbers, metric.value):g}"
        for metric, label in METRIC_LABELS.items()
        if numbers is not None and getattr(numbers, metric.value) is not None
    ]
    return " · ".join(parts) or translate(NO_NUMBERS)


def csv_text(fields: RetentionFields) -> str | None:
    """The uploaded CSV as text, or None when no file was chosen."""
    if fields.file is None or not fields.file.filename:
        return None
    raw = fields.file.file.read(MAX_CSV_CHARS * UTF8_MAX_BYTES + 1)
    return raw.decode("utf-8-sig", errors="replace")


def csv_errors(refusal: RetentionCsvError, translate: Translator) -> dict[str, str]:
    """The first problem of a CSV in the person's language, next to the file field."""
    found = refusal.problems[0]
    return {"file": translate(PROBLEM_MESSAGES[found.kind], **found.values)}


def post_context(
    app: Services, access: ChannelAccess, target: PostFormPath, cursor: str
) -> dict[str, Any]:
    """The post's snapshots, newest first, for the template."""
    try:
        post = post_of(app, access, target.number, target.platform)
    except PostNotRecorded as missing:
        raise PageNotFound(target.platform.value) from missing
    found = list_snapshots(app, access, post, cursor or None, DEFAULT_LIMIT)
    return {"snapshots": found.items, "next_cursor": found.next_cursor}


class MetricsPageState(BaseModel):
    """What a post's numbers page shows besides its data: the sent forms and their errors."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    outcome: Outcome = Outcome()
    sent: SnapshotFields | None = None
    sent_csv: RetentionFields | None = None
    cursor: str = ""


def post_metrics_page(
    request: Request,
    app: Services,
    access: ChannelAccess,
    target: PostFormPath,
    state: MetricsPageState,
) -> Response:
    """A post's snapshots with the forms to add numbers or import a retention CSV."""
    require(access, Permission.READ)
    current = found_episode(app, access, target.number)
    translate = translator(request)
    slug = access.channel.slug
    data = post_context(app, access, target, state.cursor)
    context = {
        "episode": current.episode,
        "platform_name": platform_name(target.platform),
        "errors": state.outcome.errors,
        "sent": state.sent,
        "sent_csv": state.sent_csv,
        "back_link": delivery_url(slug, target.number),
        "post_link": metrics_url(slug, target.number, target.platform.value),
        "max_csv_chars": MAX_CSV_CHARS,
        **data,
        "lines": {str(item.id): numbers_text(item, translate) for item in data["snapshots"]},
        **episode_frame(app, access, target.number, translate),
        "episode_tab": "delivery",
        **labels(),
    }
    view = View(
        template="workspace/post_metrics.html",
        section="episodes",
        status_code=state.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def post_metrics(
    request: Request, app: ServicesDep, access: PageAccess, target: PostTarget, cursor: str = ""
) -> Response:
    """A post's numbers, one snapshot per day."""
    return post_metrics_page(request, app, access, target, MetricsPageState(cursor=cursor))


def recorded_outcome(request: Request, action: Callable[[], object]) -> Outcome | None:
    """Run a write of numbers; the errors to show, or None when it worked."""
    translate = translator(request)
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except RetentionCsvError as refusal:
        return Outcome.invalid(csv_errors(refusal, translate))
    except PostNotRecorded:
        return Outcome.conflict({"form": translate(NOT_RECORDED)})
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post(f"{PAGE}/snapshots")
def add_snapshot(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: PostTarget,
    fields: Annotated[SnapshotFields, Form()],
) -> Response:
    """Enter a post's numbers for one day; a second entry for the day merges into the first."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, target.number)
    outcome = recorded_outcome(
        request,
        lambda: record_metrics(app, access, target.number, target.platform, as_snapshot(fields)),
    )
    if outcome is None:
        return done(
            request,
            app,
            metrics_url(access.channel.slug, target.number, target.platform.value),
            Flash.METRICS_RECORDED,
        )
    state = MetricsPageState(outcome=outcome, sent=fields)
    return post_metrics_page(request, app, access, target, state)


def import_source(fields: RetentionFields) -> RetentionImport:
    """The CSV text and day a form sent; RetentionCsvError when no file came."""
    text = csv_text(fields)
    return RetentionImport.model_validate(
        {"csv": text if text is not None else "", "captured_on": fields.captured_on or None}
    )


@router.post(f"{PAGE}/retention")
def add_retention(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: PostTarget,
    fields: Annotated[RetentionFields, Form()],
) -> Response:
    """Import a platform's retention CSV into the day's snapshot."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, target.number)
    outcome = recorded_outcome(
        request,
        lambda: import_retention(
            app, access, target.number, target.platform, import_source(fields)
        ),
    )
    if outcome is None:
        return done(
            request,
            app,
            metrics_url(access.channel.slug, target.number, target.platform.value),
            Flash.METRICS_RECORDED,
        )
    state = MetricsPageState(outcome=outcome, sent_csv=fields)
    return post_metrics_page(request, app, access, target, state)


def compare_url(slug: str) -> str:
    """The channel's post comparison page."""
    return channel_url(slug, COMPARE_SUFFIX)


@router.get(COMPARE_PAGE)
def compare(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    sort: Metric = Metric.VIEWS,
    cursor: str = "",
) -> Response:
    """The channel's posts with their latest numbers, best first by the chosen number."""
    require(access, Permission.READ)
    translate = translator(request)
    slug = access.channel.slug
    query = PostQuery(sort_by=sort, cursor=cursor or None, limit=DEFAULT_LIMIT)
    found = compare_posts(app, access, query)
    base = compare_url(slug)
    context = {
        "rows": [
            (row, numbers_text(row.latest, translate), platform_name(row.platform))
            for row in found.items
        ],
        "next_cursor": found.next_cursor,
        "sort": sort.value,
        "sorts": [
            (f"{base}?sort={metric.value}", translate(METRIC_LABELS[metric]), metric.value)
            for metric in Metric
        ],
        "compare_link": base,
        "episodes_link": channel_url(slug, "/episodes"),
    }
    view = View(template="workspace/metrics.html", section="metrics", context=context)
    return channel_page(request, app, access, view)
