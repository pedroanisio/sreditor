"""An episode's research page: its notes and the sources behind its claims."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.pitch_operations import (
    PitchQuery,
    episode_pitch_research,
    get_episode_pitch,
    list_pitches,
)
from sreditor.research import (
    CLAIM_LENGTH,
    NOTES_LENGTH,
    SOURCE_TITLE_LENGTH,
    NotesInput,
    SourceInput,
    SourceNotFound,
)
from sreditor.research_operations import (
    SourceChange,
    add_source,
    get_notes,
    list_sources,
    remove_source,
    write_notes,
)
from sreditor.web.forms import field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import (
    RemoveSourceFields,
    SourceFields,
    TextFields,
    revision_of,
)
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

RESEARCH_SUFFIX = "/research"
SOURCES_SUFFIX = "/sources"
REMOVE_SUFFIX = "/remove"
PITCH_SUFFIX = "/pitch"
NOTES_CHANGED = N_(
    "Someone changed the notes after you opened the page. Copy your text, reload the page to see "
    "their changes, then try again."
)
SOURCE_GONE = N_("This source was already removed or changed. Reload the page.")


class ResearchView(BaseModel):
    """The research page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str = ""
    outcome: Outcome = Outcome()
    notes: TextFields | None = None
    source: SourceFields | None = None


def research_url(slug: str, number: int) -> str:
    """The research page of an episode."""
    return episodes_url(slug, number) + RESEARCH_SUFFIX


def sources_url(slug: str, number: int) -> str:
    """Where an episode's source forms post."""
    return episodes_url(slug, number) + SOURCES_SUFFIX


def pitch_panel(app: Services, access: ChannelAccess, number: int) -> dict[str, Any]:
    """The pitch the episode uses, its research as it is now, and the pitches to choose from."""
    chosen = get_episode_pitch(app, access, number)
    query = PitchQuery(limit=MAX_LIMIT)
    return {
        "episode_pitch": chosen,
        "pitch_research": episode_pitch_research(app, access, number),
        "pitch_choices": [
            (str(item.id), item.title) for item in list_pitches(app, access, query).items
        ],
        "pitch_link": episodes_url(access.channel.slug, number) + PITCH_SUFFIX,
        "pitches_link": channel_url(access.channel.slug, "/pitches"),
    }


def research_page(
    request: Request, app: Services, access: ChannelAccess, shown: ResearchView
) -> Response:
    """The episode's notes and sources, with the forms to change them."""
    current = found_episode(app, access, shown.number)
    notes = get_notes(app, access, shown.number)
    sources = list_sources(app, access, shown.number, shown.cursor or None, DEFAULT_LIMIT)
    context = {
        "episode": current.episode,
        "notes": notes,
        "notes_text": shown.notes.text if shown.notes else notes.text,
        "sources": sources.items,
        "next_cursor": sources.next_cursor,
        "sent_source": shown.source,
        "errors": shown.outcome.errors,
        "research_link": research_url(access.channel.slug, shown.number),
        "sources_link": sources_url(access.channel.slug, shown.number),
        "remove_source_link": sources_url(access.channel.slug, shown.number) + REMOVE_SUFFIX,
        **episode_frame(app, access, shown.number, translator(request)),
        "episode_tab": "research",
        **pitch_panel(app, access, shown.number),
        "notes_limit": NOTES_LENGTH,
        "source_title_limit": SOURCE_TITLE_LENGTH,
        "claim_limit": CLAIM_LENGTH,
        **labels(),
    }
    view = View(
        template="workspace/research.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/episodes/{number}/research")
def research(
    request: Request, app: ServicesDep, access: PageAccess, number: int, cursor: str = ""
) -> Response:
    """An episode's research notes and sources."""
    return research_page(request, app, access, ResearchView(number=number, cursor=cursor))


@router.post("/app/channels/{slug}/episodes/{number}/research")
def save_notes(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[TextFields, Form()],
) -> Response:
    """Save the research notes."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    try:
        data = NotesInput(text=fields.text)
        saved = write_notes(app, access, number, data, revision_of(fields.revision))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"text": translate(NOTES_CHANGED)})
    else:
        if is_autosave(request):
            return autosaved(saved.revision)
        return done(request, app, research_url(access.channel.slug, number), Flash.SAVED)
    if is_autosave(request):
        return autosave_refused(outcome)
    shown = ResearchView(number=number, outcome=outcome, notes=fields)
    return research_page(request, app, access, shown)


def source_input(fields: SourceFields) -> SourceInput:
    """The source a form describes; an empty date means today."""
    return SourceInput.model_validate(
        {
            "url": fields.url.strip(),
            "title": fields.title,
            "claim": fields.claim,
            "accessed_on": fields.accessed_on.strip() or None,
        }
    )


@router.post("/app/channels/{slug}/episodes/{number}/sources")
def new_source(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[SourceFields, Form()],
) -> Response:
    """Cite a source."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    try:
        add_source(app, access, number, source_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        shown = ResearchView(number=number, outcome=outcome, source=fields)
        return research_page(request, app, access, shown)
    return done(request, app, research_url(access.channel.slug, number), Flash.SOURCE_ADDED)


def removal(number: int, fields: RemoveSourceFields) -> SourceChange | None:
    """The source a removal form names, or None when the form is malformed."""
    try:
        source_id = UUID(fields.source_id)
    except ValueError:
        return None
    return SourceChange(
        number=number, source_id=source_id, expected_revision=revision_of(fields.revision)
    )


@router.post("/app/channels/{slug}/episodes/{number}/sources/remove")
def drop_source(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[RemoveSourceFields, Form()],
) -> Response:
    """Stop citing a source."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    change = removal(number, fields)
    try:
        if change is None:
            raise SourceNotFound(fields.source_id)
        remove_source(app, access, change)
    except (SourceNotFound, PartChanged):
        outcome = Outcome.conflict({"form": translator(request)(SOURCE_GONE)})
        return research_page(request, app, access, ResearchView(number=number, outcome=outcome))
    return done(request, app, research_url(access.channel.slug, number), Flash.REMOVED)
