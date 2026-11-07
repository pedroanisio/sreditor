"""An episode's render page: request a video, follow it and play it (rules project.render*).

Everyone in the channel sees the renders and their videos; only owners and editors, who hold the
render permission, may request or cancel one. While a render runs the list reloads itself with
htmx, so the page shows progress without a manual reload.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.render_operations import (
    RenderRequest,
    RenderView,
    cancel_render,
    list_renders,
    request_render,
)
from sreditor.renders import FINISHED, RenderFinished, RenderNotFound, RenderPreset, RenderStatus
from sreditor.srstudio_export import SrstudioNotReady, missing_pieces
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episode_srstudio import checklist, srstudio_url
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

RENDER_SUFFIX = "/render"
PAGE = "/app/channels/{slug}/episodes/{number}/render"
CANCEL = PAGE + "/{render_id}/cancel"
NOT_READY = N_("The episode is not ready to render yet. Fix what the list below shows first.")
ALREADY_ENDED = N_("This render already ended, so it can no longer be canceled.")
STATUS_LABELS: dict[RenderStatus, str] = {
    RenderStatus.QUEUED: N_("Queued"),
    RenderStatus.BUILDING: N_("Building the scene"),
    RenderStatus.RENDERING: N_("Rendering frames"),
    RenderStatus.ENCODING: N_("Encoding the video"),
    RenderStatus.DONE: N_("Done"),
    RenderStatus.FAILED: N_("Failed"),
    RenderStatus.CANCELED: N_("Canceled"),
}
STATUS_TONES: dict[RenderStatus, str] = {
    RenderStatus.QUEUED: "",
    RenderStatus.BUILDING: "accent",
    RenderStatus.RENDERING: "accent",
    RenderStatus.ENCODING: "accent",
    RenderStatus.DONE: "success",
    RenderStatus.FAILED: "warning",
    RenderStatus.CANCELED: "",
}
PRESET_LABELS: dict[RenderPreset, str] = {
    RenderPreset.MASTER: N_("Master: the video to publish"),
    RenderPreset.PREVIEW: N_("Preview: quicker, lower quality"),
}
PERCENT = 100


class RenderFields(SubmittedFields):
    """The request form: which preset to render."""

    preset: str = RenderPreset.MASTER.value


class CancelFields(SubmittedFields):
    """The cancel form carries only its CSRF token."""


class RenderPageView(BaseModel):
    """The render page, with the outcome of a form that was sent."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str | None = None
    outcome: Outcome = Outcome()
    preset: str = RenderPreset.MASTER.value


def render_url(slug: str, number: int) -> str:
    """The render page of an episode."""
    return episodes_url(slug, number) + RENDER_SUFFIX


def render_row(shown: RenderView, translate: Translator) -> dict[str, Any]:
    """What the template shows for one render."""
    job = shown.job
    return {
        "id": job.id,
        "status": translate(STATUS_LABELS[job.status]),
        "tone": STATUS_TONES[job.status],
        "preset": translate(PRESET_LABELS[job.preset]),
        "percent": round(job.progress * PERCENT),
        "is_running": job.status not in FINISHED,
        "queue_place": None if shown.queue_place is None else shown.queue_place + 1,
        "requested_on": translate.day(job.created_at),
        "failure": job.failure,
        "findings": job.findings,
        "video": shown.video,
    }


def render_page(
    request: Request, app: Services, access: ChannelAccess, shown: RenderPageView
) -> Response:
    """The episode's renders, newest first, and the form that asks for another."""
    current = found_episode(app, access, shown.number)
    translate = translator(request)
    page = list_renders(app, access, shown.number, shown.cursor, DEFAULT_LIMIT)
    rows = [render_row(item, translate) for item in page.items]
    may_render = access.can(Permission.RENDER)
    missing = missing_pieces(app, access, shown.number) if may_render else []
    action = render_url(access.channel.slug, shown.number)
    context: dict[str, Any] = {
        "episode": current.episode,
        "renders": rows,
        "is_any_running": any(row["is_running"] for row in rows),
        "next_cursor": page.next_cursor,
        "may_render": may_render,
        "missing": checklist(access, shown.number, missing, translate),
        "srstudio_link": srstudio_url(access.channel.slug, shown.number),
        "preset_options": [
            (preset.value, translate(label)) for preset, label in PRESET_LABELS.items()
        ],
        "preset_value": shown.preset,
        "errors": shown.outcome.errors,
        "action": action,
        "media_link": episodes_url(access.channel.slug, shown.number) + "/media",
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": "render",
        **labels(),
    }
    view = View(
        template="workspace/episode_render.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def render_tab(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    cursor: str | None = None,
) -> Response:
    """The episode's renders and, for owners and editors, the form that requests one."""
    return render_page(request, app, access, RenderPageView(number=number, cursor=cursor))


def request_outcome(
    app: Services, access: ChannelAccess, number: int, fields: RenderFields, translate: Translator
) -> Outcome | Flash:
    """Queue the render; the flash to show, or the errors when it was refused."""
    try:
        wanted = RenderRequest.model_validate({"number": number, "preset": fields.preset})
        shown = request_render(app, access, wanted)
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except SrstudioNotReady:
        return Outcome.conflict({"form": translate(NOT_READY)})
    return Flash.RENDER_REUSED if shown.is_reused else Flash.RENDER_QUEUED


@router.post(PAGE)
def ask_render(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[RenderFields, Form()],
) -> Response:
    """Queue a render of the episode as it is now, or point to the earlier one of the same export."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.RENDER)
    found_episode(app, access, number)
    outcome = request_outcome(app, access, number, fields, translator(request))
    if isinstance(outcome, Flash):
        return done(request, app, render_url(access.channel.slug, number), outcome)
    shown = RenderPageView(number=number, outcome=outcome, preset=fields.preset)
    return render_page(request, app, access, shown)


class RenderPath(BaseModel):
    """The episode and render a form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    render_id: UUID


def render_path(number: int, render_id: UUID) -> RenderPath:
    """The episode and render in the address."""
    return RenderPath(number=number, render_id=render_id)


# Path values in one parameter, so the handler keeps to five (code.functions.args).
RenderTarget = Annotated[RenderPath, Depends(render_path)]


@router.post(CANCEL)
def cancel(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: RenderTarget,
    fields: Annotated[CancelFields, Form()],
) -> Response:
    """Stop a queued or running render."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.RENDER)
    number, render_id = target.number, target.render_id
    try:
        cancel_render(app, access, number, render_id)
    except RenderNotFound as unknown:
        raise PageNotFound(str(render_id)) from unknown
    except RenderFinished:
        outcome = Outcome.conflict({"form": translator(request)(ALREADY_ENDED)})
        return render_page(request, app, access, RenderPageView(number=number, outcome=outcome))
    return done(request, app, render_url(access.channel.slug, number), Flash.RENDER_CANCELED)
