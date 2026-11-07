"""How the library's characters relate: srstudio's relationships.json, kept as pages (rule project.srstudio.bible).

The list page names each pair and adds one from two characters and a relation; each pair has its
own page with every text and the history of facts that cite published episodes. Texts left empty
export as open questions. The page sits beside the library, not under it, so its addresses never
meet the library's own /library/<kind> forms.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import FormData

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.library import EntryNotFound, LibraryKind
from sreditor.members import Permission
from sreditor.srstudio import (
    TEXT_LENGTH,
    Relationship,
    RelationshipInput,
    RelationshipNotFound,
    SameCharacter,
)
from sreditor.srstudio_operations import (
    FactNotPublished,
    RelationshipChange,
    add_relationship,
    all_entries,
    delete_relationship,
    list_relationships,
    replace_relationship,
)
from sreditor.srstudio_vocabulary import FactField, PairStatus
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import revision_of
from sreditor.web.workspace.library import library_url
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
from sreditor.web.workspace.srstudio import (
    NOT_PUBLISHED,
    filled_rows,
    option_lists,
    published_options,
    sheet_errors,
)

router = APIRouter()

RELATIONSHIPS_SUFFIX = "/relationships"
PAGE = "/app/channels/{slug}/relationships"
HISTORY = "history"
TEXT_FIELDS = ("relation", "how_they_know", "dynamic", "tension", "meet")
SAME_CHARACTER = N_("Pick two different characters.")
CHARACTER_GONE = N_("This character is no longer in the library. Pick another.")
PAIR_CHANGED = N_("Someone changed this relationship after you opened it. Reload the page.")
STATUS_LABELS = {PairStatus.CANON: N_("Canon"), PairStatus.PROPOSAL: N_("Proposal")}


def relationships_url(slug: str, relationship_id: UUID | str | None = None) -> str:
    """The list of relationships, or one relationship's page."""
    suffix = (
        RELATIONSHIPS_SUFFIX
        if relationship_id is None
        else f"{RELATIONSHIPS_SUFFIX}/{relationship_id}"
    )
    return channel_url(slug, suffix)


def relationship_id_of(value: str) -> UUID:
    """The id in a path; a malformed one is a relationship that does not exist."""
    try:
        return UUID(value)
    except ValueError as malformed:
        raise PageNotFound(value) from malformed


def found_relationship(app: Services, access: ChannelAccess, relationship_id: UUID) -> Relationship:
    """The channel's relationship, or the not-found page."""
    try:
        return app.srstudio.relationship(access.channel.slug, relationship_id)
    except RelationshipNotFound as missing:
        raise PageNotFound(str(relationship_id)) from missing


def pair_data(form: FormData) -> dict[str, Any]:
    """What a form says about a pair: both characters, the texts, the status and the history."""
    data: dict[str, Any] = {
        name: str(form.get(name, "")) for name in ("first", "second", *TEXT_FIELDS)
    }
    data["status"] = str(form.get("status", "")) or PairStatus.PROPOSAL.value
    data[HISTORY] = filled_rows(form, HISTORY)
    return data


def pair_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run a create or save; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(sheet_errors(invalid, translate))
    except SameCharacter:
        return Outcome.invalid({"second": translate(SAME_CHARACTER)})
    except EntryNotFound:
        return Outcome.invalid({"first": translate(CHARACTER_GONE)})
    except FactNotPublished:
        return Outcome.invalid({"form": translate(NOT_PUBLISHED)})
    except (PartChanged, RelationshipNotFound):
        return Outcome.conflict({"form": translate(PAIR_CHANGED)})
    return None


class ListView(BaseModel):
    """The list page, with the add form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    sent: dict[str, Any] | None = None


def character_choices(app: Services, channel: str, translate: Translator) -> list[tuple[str, str]]:
    """The channel's characters, after an empty choice that asks for one."""
    characters = all_entries(app, channel, LibraryKind.CHARACTER)
    return [
        ("", translate(N_("Pick a character"))),
        *((str(entry.id), entry.name) for entry in characters),
    ]


def common_context(app: Services, access: ChannelAccess, translate: Translator) -> dict[str, Any]:
    """What both pages need: the characters by id, the choices and the way back."""
    characters = all_entries(app, access.channel.slug, LibraryKind.CHARACTER)
    return {
        "names": {str(entry.id): entry.name for entry in characters},
        "character_options": character_choices(app, access.channel.slug, translate),
        "status_options": [
            (status.value, translate(label)) for status, label in STATUS_LABELS.items()
        ],
        "status_labels": {status.value: label for status, label in STATUS_LABELS.items()},
        "library_link": library_url(access.channel.slug),
        "relationships_link": relationships_url(access.channel.slug),
        "text_limit": TEXT_LENGTH,
    }


def list_page(request: Request, app: Services, access: ChannelAccess, shown: ListView) -> Response:
    """Every pair, each linking to its page, and for writers the dialog that adds one."""
    translate = translator(request)
    context = {
        "relationships": list_relationships(app, access),
        "errors": shown.outcome.errors,
        "sent": shown.sent or {},
        **common_context(app, access, translate),
    }
    view = View(
        template="workspace/relationships.html",
        section="library",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def relationships(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """How the library's characters relate."""
    return list_page(request, app, access, ListView())


@router.post(PAGE)
async def create(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """Record how two characters relate, then open the new pair's page to fill in the rest."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    data = pair_data(form)
    created: list[Relationship] = []

    def store() -> None:
        created.append(add_relationship(app, access, RelationshipInput.model_validate(data)))

    outcome = pair_errors(store, translator(request))
    if outcome is None:
        return done(
            request, app, relationships_url(access.channel.slug, created[0].id), Flash.LIBRARY_ADDED
        )
    return list_page(request, app, access, ListView(outcome=outcome, sent=data))


class PairView(BaseModel):
    """One pair's page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    relationship_id: UUID
    outcome: Outcome = Outcome()
    sent: dict[str, Any] | None = None


def pair_page(request: Request, app: Services, access: ChannelAccess, shown: PairView) -> Response:
    """A pair with every text and its history, as a form for those who may write."""
    translate = translator(request)
    stored = found_relationship(app, access, shown.relationship_id)
    context = {
        "pair": stored,
        "shown": shown.sent or stored.model_dump(mode="json"),
        "errors": shown.outcome.errors,
        "action": relationships_url(access.channel.slug, stored.id),
        "episodes": published_options(app, access.channel.slug),
        "fact_fields": option_lists(translate)["fact_fields"],
        "fact_kinds": option_lists(translate)["fact_kinds"],
        "blank_field": FactField.RELATIONSHIP.value,
        **common_context(app, access, translate),
    }
    view = View(
        template="workspace/relationship.html",
        section="library",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(f"{PAGE}/{{relationship_id}}")
def relationship(
    request: Request, app: ServicesDep, access: PageAccess, relationship_id: str
) -> Response:
    """One pair: what they are to each other, how they met and what episodes showed it."""
    return pair_page(
        request, app, access, PairView(relationship_id=relationship_id_of(relationship_id))
    )


def change_of(relationship_id: UUID, form: FormData) -> RelationshipChange:
    """The pair a form targets and the revision it was showing."""
    return RelationshipChange(
        relationship_id=relationship_id,
        expected_revision=revision_of(str(form.get("revision", ""))),
    )


@router.post(f"{PAGE}/{{relationship_id}}")
async def save(
    request: Request, app: ServicesDep, access: PageAccess, relationship_id: str
) -> Response:
    """Save every text, the status and the history of a pair."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    target = relationship_id_of(relationship_id)
    found_relationship(app, access, target)
    data = pair_data(form)

    def store() -> None:
        replace_relationship(
            app, access, change_of(target, form), RelationshipInput.model_validate(data)
        )

    outcome = pair_errors(store, translator(request))
    if outcome is None:
        return done(request, app, relationships_url(access.channel.slug, target), Flash.SAVED)
    return pair_page(
        request, app, access, PairView(relationship_id=target, outcome=outcome, sent=data)
    )


@router.post(f"{PAGE}/{{relationship_id}}/remove")
async def remove(
    request: Request, app: ServicesDep, access: PageAccess, relationship_id: str
) -> Response:
    """Take a pair out of the library."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    target = relationship_id_of(relationship_id)
    found_relationship(app, access, target)
    outcome = pair_errors(
        lambda: delete_relationship(app, access, change_of(target, form)), translator(request)
    )
    if outcome is None:
        return done(request, app, relationships_url(access.channel.slug), Flash.REMOVED)
    return pair_page(request, app, access, PairView(relationship_id=target, outcome=outcome))
