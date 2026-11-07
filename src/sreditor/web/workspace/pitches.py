"""Pitch pages: a channel's pautas (story ideas), one pitch with its research, and deleting it."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor.book_operations import PITCH_BOOK
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.pitch_operations import (
    PitchChange,
    PitchQuery,
    PitchSourceChange,
    add_source,
    create_pitch,
    delete_pitch,
    get_notes,
    get_pitch,
    list_pitches,
    list_sources,
    pitch_episodes,
    remove_source,
    replace_pitch,
    set_episode_pitches,
    write_notes,
)
from sreditor.pitches import (
    SUMMARY_LENGTH,
    TITLE_LENGTH,
    Pitch,
    PitchInput,
    PitchInUse,
    PitchList,
    PitchNotesInput,
    PitchNotFound,
    PitchStatus,
)
from sreditor.research import (
    CLAIM_LENGTH,
    NOTES_LENGTH,
    SOURCE_TITLE_LENGTH,
    SourceInput,
    SourceNotFound,
)
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import found_episode
from sreditor.web.workspace.forms import (
    RemoveSourceFields,
    RevisionFields,
    SourceFields,
    TextFields,
    revision_of,
)
from sreditor.web.workspace.research import ResearchView, research_page, research_url
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

router = APIRouter()

PITCHES_SUFFIX = "/pitches"
SECTION = "pitches"

PITCH_STATUS_LABELS: dict[PitchStatus, str] = {
    PitchStatus.IDEA: N_("Idea"),
    PitchStatus.RESEARCH: N_("Research"),
    PitchStatus.APPROVED: N_("Approved"),
    PitchStatus.DISCARDED: N_("Discarded"),
}

# The badge tone of each pitch status (a class of the badge component).
PITCH_STATUS_TONES: dict[PitchStatus, str] = {
    PitchStatus.IDEA: "",
    PitchStatus.RESEARCH: "accent",
    PitchStatus.APPROVED: "success",
    PitchStatus.DISCARDED: "warning",
}

PITCH_CHANGED = N_(
    "Someone changed this pitch after you opened the page. Reload it to see their changes, "
    "then try again."
)
NOTES_CHANGED = N_(
    "Someone changed the notes after you opened the page. Copy your text, reload the page to see "
    "their changes, then try again."
)
SOURCE_GONE = N_("This source was already removed or changed. Reload the page.")
PITCH_GONE = N_(
    "A chosen pitch no longer exists, or is chosen twice. Reload the page and choose again."
)
EPISODE_PITCH_CHANGED = N_(
    "Someone changed this episode's pitches after you opened the page. Reload it and try again."
)
PITCH_IN_USE = N_(
    "Episodes still use this pitch. Choose another pitch, or none, on their research pages first."
)


class PitchFields(SubmittedFields):
    """A pitch's title, summary and status; a new pitch leaves the status empty."""

    title: str = ""
    summary: str = ""
    status: str = ""
    revision: str = ""


def pitches_url(slug: str, pitch_id: UUID | None = None) -> str:
    """The pitch list, or one pitch's page."""
    suffix = PITCHES_SUFFIX if pitch_id is None else f"{PITCHES_SUFFIX}/{pitch_id}"
    return channel_url(slug, suffix)


def status_options(translate: Translator) -> list[tuple[str, str]]:
    """(value, label) for a pitch status picker."""
    return [(status.value, translate(label)) for status, label in PITCH_STATUS_LABELS.items()]


def labels() -> dict[str, Any]:
    """What every pitch template needs to name statuses and size its fields."""
    return {
        "pitch_status_labels": PITCH_STATUS_LABELS,
        "pitch_status_tones": PITCH_STATUS_TONES,
        "title_limit": TITLE_LENGTH,
        "summary_limit": SUMMARY_LENGTH,
    }


def as_input(fields: PitchFields) -> PitchInput:
    """The pitch a form describes; a new pitch starts as an idea."""
    status = fields.status or PitchStatus.IDEA.value
    return PitchInput.model_validate(
        {"title": fields.title, "summary": fields.summary, "status": status}
    )


def found_pitch(app: Services, access: ChannelAccess, pitch_id: str) -> Pitch:
    """The pitch in the path; 404 when the channel has none with that id."""
    try:
        return get_pitch(app, access, UUID(pitch_id))
    except (ValueError, PitchNotFound) as missing:
        raise PageNotFound(pitch_id) from missing


class ListView(BaseModel):
    """The pitch list, with its status filter and the new-pitch form as it was sent."""

    model_config = ConfigDict(frozen=True)

    status: str = ""
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: PitchFields | None = None


def chosen_status(value: str) -> PitchStatus | None:
    """The status a filter link names, or None for every status."""
    try:
        return PitchStatus(value)
    except ValueError:
        return None


def list_page(request: Request, app: Services, access: ChannelAccess, shown: ListView) -> Response:
    """The channel's pitches and, for those who may write, the form to add one."""
    status = chosen_status(shown.status)
    query = PitchQuery(status=status, cursor=shown.cursor or None, limit=DEFAULT_LIMIT)
    found = list_pitches(app, access, query)
    base = pitches_url(access.channel.slug)
    translate = translator(request)
    filters = [(base, translate(N_("All")), "")] + [
        (f"{base}?status={value}", label, value) for value, label in status_options(translate)
    ]
    context = {
        "pitches": found.items,
        "next_cursor": found.next_cursor,
        "status_filters": filters,
        "status_filter": status.value if status else "",
        "pitches_link": base,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        **labels(),
    }
    view = View(
        template="workspace/pitches.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/pitches")
def pitches(
    request: Request, app: ServicesDep, access: PageAccess, status: str = "", cursor: str = ""
) -> Response:
    """The channel's pitches, oldest first, optionally of one status."""
    return list_page(request, app, access, ListView(status=status, cursor=cursor))


@router.post("/app/channels/{slug}/pitches")
def new_pitch(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[PitchFields, Form()],
) -> Response:
    """Add a pitch and open it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    try:
        created = create_pitch(app, access, as_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        return list_page(request, app, access, ListView(outcome=outcome, sent=fields))
    return done(request, app, pitches_url(access.channel.slug, created.id), Flash.PITCH_CREATED)


class PitchView(BaseModel):
    """One pitch's page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    pitch_id: str
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: PitchFields | None = None
    notes: TextFields | None = None
    source: SourceFields | None = None


def pitch_context(
    app: Services, access: ChannelAccess, pitch: Pitch, shown: PitchView
) -> dict[str, Any]:
    """The pitch's research and the episodes that use it, for its page."""
    notes = get_notes(app, access, pitch.id)
    sources = list_sources(app, access, pitch.id, shown.cursor or None, DEFAULT_LIMIT)
    link = pitches_url(access.channel.slug, pitch.id)
    return {
        "pitch": pitch,
        "values": shown.sent or pitch,
        "notes": notes,
        "notes_text": shown.notes.text if shown.notes else notes.text,
        "sources": sources.items,
        "next_cursor": sources.next_cursor,
        "sent_source": shown.source,
        "used_by": pitch_episodes(app, access, pitch.id),
        "pitch_link": link,
        "list_link": pitches_url(access.channel.slug),
        "episodes_link": channel_url(access.channel.slug, "/episodes"),
        "notes_limit": NOTES_LENGTH,
        "source_title_limit": SOURCE_TITLE_LENGTH,
        "claim_limit": CLAIM_LENGTH,
        **labels(),
    }


def pitch_page(
    request: Request, app: Services, access: ChannelAccess, shown: PitchView
) -> Response:
    """A pitch's details, notes and sources, with the forms to change them."""
    pitch = found_pitch(app, access, shown.pitch_id)
    context = pitch_context(app, access, pitch, shown)
    context["status_options"] = status_options(translator(request))
    context["errors"] = shown.outcome.errors
    context["book_link"] = PITCH_BOOK.format(slug=access.channel.slug, pitch_id=pitch.id)
    view = View(
        template="workspace/pitch.html",
        section=SECTION,
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/pitches/{pitch_id}")
def pitch(
    request: Request, app: ServicesDep, access: PageAccess, pitch_id: str, cursor: str = ""
) -> Response:
    """One pitch with its research."""
    return pitch_page(request, app, access, PitchView(pitch_id=pitch_id, cursor=cursor))


@router.post("/app/channels/{slug}/pitches/{pitch_id}")
def save_pitch(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    pitch_id: str,
    fields: Annotated[PitchFields, Form()],
) -> Response:
    """Save a pitch's title, summary and status."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    current = found_pitch(app, access, pitch_id)
    translate = translator(request)
    try:
        change = PitchChange(pitch_id=current.id, expected_revision=revision_of(fields.revision))
        replace_pitch(app, access, change, as_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"form": translate(PITCH_CHANGED)})
    else:
        return done(request, app, pitches_url(access.channel.slug, current.id), Flash.SAVED)
    shown = PitchView(pitch_id=pitch_id, outcome=outcome, sent=fields)
    return pitch_page(request, app, access, shown)


@router.post("/app/channels/{slug}/pitches/{pitch_id}/research")
def save_notes(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    pitch_id: str,
    fields: Annotated[TextFields, Form()],
) -> Response:
    """Save the pitch's research notes."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    current = found_pitch(app, access, pitch_id)
    translate = translator(request)
    try:
        change = PitchChange(pitch_id=current.id, expected_revision=revision_of(fields.revision))
        write_notes(app, access, change, PitchNotesInput(text=fields.text))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"text": translate(NOTES_CHANGED)})
    else:
        return done(request, app, pitches_url(access.channel.slug, current.id), Flash.SAVED)
    shown = PitchView(pitch_id=pitch_id, outcome=outcome, notes=fields)
    return pitch_page(request, app, access, shown)


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


@router.post("/app/channels/{slug}/pitches/{pitch_id}/sources")
def new_source(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    pitch_id: str,
    fields: Annotated[SourceFields, Form()],
) -> Response:
    """Cite a source for the pitch."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    current = found_pitch(app, access, pitch_id)
    try:
        add_source(app, access, current.id, source_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        shown = PitchView(pitch_id=pitch_id, outcome=outcome, source=fields)
        return pitch_page(request, app, access, shown)
    return done(request, app, pitches_url(access.channel.slug, current.id), Flash.SOURCE_ADDED)


def removal(pitch: Pitch, fields: RemoveSourceFields) -> PitchSourceChange | None:
    """The source a removal form names, or None when the form is malformed."""
    try:
        source_id = UUID(fields.source_id)
    except ValueError:
        return None
    return PitchSourceChange(
        pitch_id=pitch.id, source_id=source_id, expected_revision=revision_of(fields.revision)
    )


@router.post("/app/channels/{slug}/pitches/{pitch_id}/sources/remove")
def drop_source(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    pitch_id: str,
    fields: Annotated[RemoveSourceFields, Form()],
) -> Response:
    """Stop citing a source for the pitch."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    current = found_pitch(app, access, pitch_id)
    change = removal(current, fields)
    try:
        if change is None:
            raise SourceNotFound(fields.source_id)
        remove_source(app, access, change)
    except (SourceNotFound, PartChanged):
        outcome = Outcome.conflict({"form": translator(request)(SOURCE_GONE)})
        return pitch_page(request, app, access, PitchView(pitch_id=pitch_id, outcome=outcome))
    return done(request, app, pitches_url(access.channel.slug, current.id), Flash.REMOVED)


@router.post("/app/channels/{slug}/pitches/{pitch_id}/delete")
def remove_pitch(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    pitch_id: str,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Delete a pitch with its research, unless episodes still use it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    current = found_pitch(app, access, pitch_id)
    translate = translator(request)
    try:
        change = PitchChange(pitch_id=current.id, expected_revision=revision_of(fields.revision))
        delete_pitch(app, access, change)
    except PitchInUse:
        outcome = Outcome.conflict({"form": translate(PITCH_IN_USE)})
    except PartChanged:
        outcome = Outcome.conflict({"form": translate(PITCH_CHANGED)})
    else:
        return done(request, app, pitches_url(access.channel.slug), Flash.PITCH_DELETED)
    return pitch_page(request, app, access, PitchView(pitch_id=pitch_id, outcome=outcome))


class PitchChoiceFields(SubmittedFields):
    """The pitches an episode uses, in order, one per picker; empty pickers mean none."""

    pitch_id: list[str] = Field(default_factory=list)
    revision: str = ""


def list_of(fields: PitchChoiceFields) -> PitchList | None:
    """The pitches a form describes, or None when an id is malformed or repeated."""
    try:
        return PitchList(pitch_ids=[UUID(value) for value in fields.pitch_id if value])
    except ValueError:  # a malformed id; pydantic's ValidationError is a ValueError too
        return None


@router.post("/app/channels/{slug}/episodes/{number}/pitch")
def choose_pitch(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[PitchChoiceFields, Form()],
) -> Response:
    """Point an episode at its pitches, main one first, or at none."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    chosen = list_of(fields)
    try:
        if chosen is None:
            raise PitchNotFound(", ".join(fields.pitch_id))
        set_episode_pitches(app, access, number, chosen, revision_of(fields.revision))
    except PitchNotFound:
        outcome = Outcome.invalid({"pitch_id": translate(PITCH_GONE)})
    except PartChanged:
        outcome = Outcome.conflict({"pitch_id": translate(EPISODE_PITCH_CHANGED)})
    else:
        return done(request, app, research_url(access.channel.slug, number), Flash.SAVED)
    return research_page(request, app, access, ResearchView(number=number, outcome=outcome))
