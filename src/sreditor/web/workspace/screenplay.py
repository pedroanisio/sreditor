"""An episode's screenplay page: the Fountain text, and the scenes and characters read from it."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.fountain import blocks
from sreditor.i18n import N_
from sreditor.library_operations import episode_cast
from sreditor.members import Permission
from sreditor.screenplay import SCREENPLAY_LENGTH, ScreenplayInput
from sreditor.screenplay_operations import get_screenplay, write_screenplay
from sreditor.web.forms import field_errors
from sreditor.web.routes import page, rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import TextFields, revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    View,
    autosave_refused,
    autosaved,
    channel_page,
    channel_url,
    done,
    is_autosave,
    require,
)

router = APIRouter()

SCREENPLAY_SUFFIX = "/screenplay"
FOUNTAIN_GUIDE = "https://fountain.io/syntax"
SCREENPLAY_CHANGED = N_(
    "Someone changed the screenplay after you opened the page. Copy your text, reload the page to "
    "see their changes, then try again."
)


class ScreenplayView(BaseModel):
    """The screenplay page, with the text that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent: TextFields | None = None


def screenplay_url(slug: str, number: int) -> str:
    """The screenplay page of an episode."""
    return episodes_url(slug, number) + SCREENPLAY_SUFFIX


def screenplay_page(
    request: Request, app: Services, access: ChannelAccess, shown: ScreenplayView
) -> Response:
    """The screenplay, the form to change it, and its scenes and characters."""
    current = found_episode(app, access, shown.number)
    screenplay = get_screenplay(app, access, shown.number)
    context = {
        "episode": current.episode,
        "screenplay": screenplay,
        "cast": episode_cast(app, access, shown.number),
        "library_link": channel_url(access.channel.slug, "/library"),
        "screenplay_text": shown.sent.text if shown.sent else screenplay.text,
        "screenplay_blocks": blocks(screenplay.text),
        "errors": shown.outcome.errors,
        "screenplay_link": screenplay_url(access.channel.slug, shown.number),
        "fountain_guide": FOUNTAIN_GUIDE,
        **episode_frame(app, access, shown.number, translator(request)),
        "episode_tab": "screenplay",
        "screenplay_limit": SCREENPLAY_LENGTH,
        **labels(),
    }
    view = View(
        template="workspace/screenplay.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/episodes/{number}/screenplay")
def screenplay(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """An episode's screenplay."""
    return screenplay_page(request, app, access, ScreenplayView(number=number))


@router.get("/app/channels/{slug}/episodes/{number}/screenplay/preview")
def screenplay_preview(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """The saved screenplay formatted, alone, for the page to swap in after an autosave."""
    found_episode(app, access, number)
    screenplay = get_screenplay(app, access, number)
    template = "workspace/_screenplay_preview.html"
    return page(request, app, template, screenplay_blocks=blocks(screenplay.text))


@router.post("/app/channels/{slug}/episodes/{number}/screenplay")
def save_screenplay(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[TextFields, Form()],
) -> Response:
    """Save the screenplay."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    try:
        data = ScreenplayInput(text=fields.text)
        saved = write_screenplay(app, access, number, data, revision_of(fields.revision))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"text": translate(SCREENPLAY_CHANGED)})
    else:
        if is_autosave(request):
            return autosaved(saved.revision)
        return done(request, app, screenplay_url(access.channel.slug, number), Flash.SAVED)
    if is_autosave(request):
        return autosave_refused(outcome)
    shown = ScreenplayView(number=number, outcome=outcome, sent=fields)
    return screenplay_page(request, app, access, shown)
