"""An episode's claims page: each figure or fact it states and the sources that back it (rule
project.claims). Claims only warn; nothing here blocks a status."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor.claim_operations import (
    ClaimChange,
    ClaimQuery,
    SourceCheck,
    add_claim,
    check_claim_source,
    list_claims,
    remove_claim,
    replace_claim,
    unchecked_claims,
)
from sreditor.claims import (
    PLACE_LENGTH,
    QUOTE_LENGTH,
    BackingNotFound,
    Claim,
    ClaimInput,
    ClaimNotFound,
    PlaceKind,
    SourceNotAvailable,
)
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.operations import TOKEN_OWNER
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.pitch_operations import get_episode_pitches, get_pitch
from sreditor.pitch_operations import list_sources as list_pitch_sources
from sreditor.research_operations import list_sources as list_episode_sources
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

CLAIMS_SUFFIX = "/claims"
CLAIM_GONE = N_("This claim was already removed or changed. Reload the page.")
CLAIM_CHANGED = N_(
    "Someone changed this claim after you opened the page. Reload it to see their changes, "
    "then try again."
)
SOURCE_UNAVAILABLE = N_(
    "Choose sources of this episode or of its pitches; one you picked is no longer there."
)
AN_API_TOKEN = N_("an API token")

PLACE_LABELS: dict[PlaceKind, str] = {
    PlaceKind.SCENE: N_("Screenplay scene"),
    PlaceKind.BLOCK: N_("Short-script block"),
    PlaceKind.TEXT: N_("Somewhere else"),
}


class ClaimFields(SubmittedFields):
    """A claim as posted: quote, place and the sources that back it; `revision` when editing."""

    quote: str = ""
    place_kind: str = PlaceKind.SCENE.value
    place_ref: str = ""
    source: list[str] = Field(default_factory=list)
    primary: list[str] = Field(default_factory=list)
    claim_id: str = ""
    revision: str = ""


class CheckFields(SubmittedFields):
    """Mark one source of a claim checked, or take the mark back."""

    claim_id: str = ""
    source_id: str = ""
    is_checked: str = ""
    revision: str = ""


class ClaimRemovalFields(SubmittedFields):
    """Remove one claim."""

    claim_id: str = ""
    revision: str = ""


class ClaimsView(BaseModel):
    """The claims page, with the form that was sent, which claim it edited and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: ClaimFields | None = None
    edited: str = ""  # the id of the claim whose form was sent; empty for the new-claim form


def claims_url(slug: str, number: int) -> str:
    """The claims page of an episode."""
    return episodes_url(slug, number) + CLAIMS_SUFFIX


def claim_input(fields: ClaimFields) -> ClaimInput:
    """The claim a form describes; a source ticked as primary is also one of its sources."""
    chosen = set(fields.source) | set(fields.primary)
    return ClaimInput.model_validate(
        {
            "quote": fields.quote,
            "place": {"kind": fields.place_kind, "ref": fields.place_ref},
            "sources": [
                {"source_id": source_id, "is_primary": source_id in fields.primary}
                for source_id in sorted(chosen)
            ],
        }
    )


def form_errors(invalid: ValidationError, translate: Translator) -> dict[str, str]:
    """The form's errors, with nested ones shown next to the field that holds them."""
    names = {"place": "place_ref", "sources": "source"}
    return {names.get(name, name): text for name, text in field_errors(invalid, translate).items()}


def source_options(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> list[tuple[str, str, str]]:
    """(id, title, whose) of every source a claim of the episode may cite: its own, then each
    pitch's."""
    options = [
        (str(source.id), source.title, translate(N_("Episode source")))
        for source in list_episode_sources(app, access, number, None, MAX_LIMIT).items
    ]
    for pitch_id in get_episode_pitches(app, access, number).pitch_ids:
        pitch = get_pitch(app, access, pitch_id)
        whose = f"{translate(N_('Pitch'))}: {pitch.title}"
        options += [
            (str(source.id), source.title, whose)
            for source in list_pitch_sources(app, access, pitch_id, None, MAX_LIMIT).items
        ]
    return options


def checker_names(app: Services, found: list[Claim], translate: Translator) -> dict[str, str]:
    """Who checked what, by id: a person's name, or a note that an API token did."""
    names: dict[str, str] = {}
    for claim in found:
        for source in claim.sources:
            owner = source.checked_by
            if owner is None or owner.id in names:
                continue
            person = None if owner.type == TOKEN_OWNER else app.users.get(UUID(owner.id))
            names[owner.id] = person.display_name if person else translate(AN_API_TOKEN)
    return names


def claims_context(
    app: Services, access: ChannelAccess, shown: ClaimsView, translate: Translator
) -> dict[str, Any]:
    """The claims, the sources they may cite and who checked what."""
    query = ClaimQuery(number=shown.number, cursor=shown.cursor or None, limit=DEFAULT_LIMIT)
    found = list_claims(app, access, query)
    unchecked = unchecked_claims(app, access, shown.number)
    base = claims_url(access.channel.slug, shown.number)
    return {
        "claims": found.items,
        "next_cursor": found.next_cursor,
        "unchecked_count": unchecked.count,
        "unchecked_ids": {str(item.claim_id) for item in unchecked.claims},
        "source_options": source_options(app, access, shown.number, translate),
        "checkers": checker_names(app, found.items, translate),
        "place_options": [(kind.value, translate(label)) for kind, label in PLACE_LABELS.items()],
        "place_labels": {kind.value: label for kind, label in PLACE_LABELS.items()},
        "claims_link": base,
        "sent_claim": shown.sent,
        "edited_claim": shown.edited,
        "errors": shown.outcome.errors,
        "quote_limit": QUOTE_LENGTH,
        "place_limit": PLACE_LENGTH,
    }


def claims_page(
    request: Request, app: Services, access: ChannelAccess, shown: ClaimsView
) -> Response:
    """The episode's claims with the forms to add, change and check them."""
    current = found_episode(app, access, shown.number)
    translate = translator(request)
    context = {
        "episode": current.episode,
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": "claims",
        **claims_context(app, access, shown, translate),
        **labels(),
    }
    view = View(
        template="workspace/claims.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/episodes/{number}/claims")
def claims(
    request: Request, app: ServicesDep, access: PageAccess, number: int, cursor: str = ""
) -> Response:
    """An episode's claims."""
    return claims_page(request, app, access, ClaimsView(number=number, cursor=cursor))


def refusal_outcome(refusal: Exception, translate: Translator) -> Outcome:
    """How a refused claim write shows: a stale revision conflicts, a lost source is invalid."""
    if isinstance(refusal, PartChanged):
        return Outcome.conflict({"form": translate(CLAIM_CHANGED)})
    if isinstance(refusal, SourceNotAvailable):
        return Outcome.invalid({"source": translate(SOURCE_UNAVAILABLE)})
    return Outcome.conflict({"form": translate(CLAIM_GONE)})


@router.post("/app/channels/{slug}/episodes/{number}/claims")
def new_claim(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[ClaimFields, Form()],
) -> Response:
    """Add a claim."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    try:
        add_claim(app, access, number, claim_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(form_errors(invalid, translate))
    except SourceNotAvailable as refusal:
        outcome = refusal_outcome(refusal, translate)
    else:
        return done(request, app, claims_url(access.channel.slug, number), Flash.CLAIM_ADDED)
    return claims_page(
        request, app, access, ClaimsView(number=number, outcome=outcome, sent=fields)
    )


def change_of(number: int, claim_id: str, revision: str) -> ClaimChange | None:
    """The claim a form names, or None when its id is malformed."""
    try:
        return ClaimChange(
            number=number, claim_id=UUID(claim_id), expected_revision=revision_of(revision)
        )
    except ValueError:
        return None


@router.post("/app/channels/{slug}/episodes/{number}/claims/save")
def save_claim(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[ClaimFields, Form()],
) -> Response:
    """Save a claim's quote, place and backing sources."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    change = change_of(number, fields.claim_id, fields.revision)
    try:
        if change is None:
            raise ClaimNotFound(fields.claim_id)
        replace_claim(app, access, change, claim_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(form_errors(invalid, translate))
    except (PartChanged, ClaimNotFound, SourceNotAvailable) as refusal:
        outcome = refusal_outcome(refusal, translate)
    else:
        return done(request, app, claims_url(access.channel.slug, number), Flash.SAVED)
    shown = ClaimsView(number=number, outcome=outcome, sent=fields, edited=fields.claim_id)
    return claims_page(request, app, access, shown)


@router.post("/app/channels/{slug}/episodes/{number}/claims/check")
def check_source(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[CheckFields, Form()],
) -> Response:
    """Mark one source of a claim checked, or take the mark back."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    change = change_of(number, fields.claim_id, fields.revision)
    try:
        if change is None:
            raise ClaimNotFound(fields.claim_id)
        check = SourceCheck(
            change=change, source_id=UUID(fields.source_id), is_checked=bool(fields.is_checked)
        )
        check_claim_source(app, access, check)
    except (ValueError, PartChanged, ClaimNotFound, BackingNotFound) as refusal:
        outcome = refusal_outcome(refusal, translator(request))
        return claims_page(request, app, access, ClaimsView(number=number, outcome=outcome))
    return done(request, app, claims_url(access.channel.slug, number), Flash.SAVED)


@router.post("/app/channels/{slug}/episodes/{number}/claims/delete")
def drop_claim(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[ClaimRemovalFields, Form()],
) -> Response:
    """Remove a claim."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    change = change_of(number, fields.claim_id, fields.revision)
    try:
        if change is None:
            raise ClaimNotFound(fields.claim_id)
        remove_claim(app, access, change)
    except (PartChanged, ClaimNotFound) as refusal:
        outcome = refusal_outcome(refusal, translator(request))
        return claims_page(request, app, access, ClaimsView(number=number, outcome=outcome))
    return done(request, app, claims_url(access.channel.slug, number), Flash.REMOVED)
