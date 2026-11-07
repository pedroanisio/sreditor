"""Context pages: an item's answers to what, who, where, when, why and how, and its links.

One page serves every kind of item (rule project.context); each item's own page links to it.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.context import (
    ANSWER_LENGTH,
    CATALOG_LIMIT,
    NOTE_LENGTH,
    ContextInput,
    ItemKind,
    ItemNotFound,
    ItemRef,
    LinkDirection,
    LinkedItem,
    LinkExists,
    LinkInput,
    LinkNotFound,
    Question,
    SelfLink,
)
from sreditor.context_operations import (
    CatalogQuery,
    ContextChange,
    LinkQuery,
    add_link,
    catalog,
    get_context,
    item_access,
    list_links,
    remove_link,
    write_context,
)
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import revision_of
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

PAGE = "/app/channels/{slug}/items/{kind}/{item_id}"

QUESTION_LABELS: dict[Question, str] = {
    Question.WHAT: N_("What"),
    Question.WHO: N_("Who"),
    Question.WHERE: N_("Where"),
    Question.WHEN: N_("When"),
    Question.WHY: N_("Why"),
    Question.HOW: N_("How"),
}

QUESTION_HINTS: dict[Question, str] = {
    Question.WHAT: N_("What happens, or what this is about."),
    Question.WHO: N_("The people involved. Link characters of the library below."),
    Question.WHERE: N_("The places. Link locations of the library below."),
    Question.WHEN: N_("A date, a period or a moment, such as 2024 or the 90s."),
    Question.WHY: N_("Why it matters, or why it is used."),
    Question.HOW: N_("How it happens, or how it is made."),
}

KIND_LABELS: dict[ItemKind, str] = {
    ItemKind.EPISODE: N_("Episode"),
    ItemKind.PITCH: N_("Pitch"),
    ItemKind.PITCH_SOURCE: N_("Pitch source"),
    ItemKind.SOURCE: N_("Source"),
    ItemKind.MEDIA: N_("Media file"),
    ItemKind.EXTERNAL_MEDIA: N_("Third-party media"),
    ItemKind.CHARACTER: N_("Character"),
    ItemKind.LOCATION: N_("Location"),
    ItemKind.PROP: N_("Prop"),
    ItemKind.STYLE: N_("Visual style"),
}

# The section of the channel menu each kind belongs to.
KIND_SECTIONS: dict[ItemKind, str] = {
    ItemKind.EPISODE: "episodes",
    ItemKind.SOURCE: "episodes",
    ItemKind.MEDIA: "episodes",
    ItemKind.PITCH: "pitches",
    ItemKind.PITCH_SOURCE: "pitches",
    ItemKind.EXTERNAL_MEDIA: "external-media",
    ItemKind.CHARACTER: "library",
    ItemKind.LOCATION: "library",
    ItemKind.PROP: "library",
    ItemKind.STYLE: "library",
}

CONTEXT_CHANGED = N_(
    "Someone changed these answers after you opened the page. Copy your text, reload the page "
    "to see their changes, then try again."
)
LINK_EXISTS = N_("This item already has that link.")
LINK_TO_ITSELF = N_("Choose another item: an item cannot link to itself.")
LINK_TARGET_GONE = N_("Choose an item from the list.")
LINK_GONE = N_("This link was already removed. Reload the page.")


def context_url(slug: str, item: LinkedItem | ItemRef) -> str:
    """The context page of an item."""
    return channel_url(slug, f"/items/{item.kind}/{item.id}")


def own_url(slug: str, item: LinkedItem) -> str:
    """The item's own page, where its other parts are edited."""
    suffixes: dict[ItemKind, str] = {
        ItemKind.EPISODE: f"/episodes/{item.episode}",
        ItemKind.SOURCE: f"/episodes/{item.episode}/research",
        ItemKind.MEDIA: f"/episodes/{item.episode}/media",
        ItemKind.PITCH: f"/pitches/{item.id}",
        ItemKind.PITCH_SOURCE: f"/pitches/{item.parent}",
        ItemKind.EXTERNAL_MEDIA: f"/external-media/{item.id}",
    }
    return channel_url(slug, suffixes.get(item.kind, "/library"))


class ContextFields(SubmittedFields):
    """The six answers as posted."""

    what: str = ""
    who: str = ""
    where: str = ""
    when: str = ""
    why: str = ""
    how: str = ""
    revision: str = ""


class LinkFields(SubmittedFields):
    """A link to add: the question it answers, the item as "kind:id", and a note."""

    question: str = ""
    target: str = ""
    note: str = ""


class RemoveLinkFields(SubmittedFields):
    """The link to remove."""

    link_id: str = ""


class ContextView(BaseModel):
    """The context page of one item, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    kind: str
    item_id: str
    links_cursor: str = ""
    mentions_cursor: str = ""
    outcome: Outcome = Outcome()
    answers: ContextFields | None = None
    link: LinkFields | None = None


def found_item(app: Services, access: ChannelAccess, kind: str, item_id: str) -> LinkedItem:
    """The item in the path; 404 when the channel has none of that kind with that id."""
    try:
        reference = ItemRef(kind=ItemKind(kind), id=item_id)
        return item_access(app, access, reference, Permission.READ)
    except (ValueError, ItemNotFound) as missing:
        raise PageNotFound(item_id) from missing


def target_options(
    app: Services, access: ChannelAccess, item: LinkedItem, translate: Translator
) -> list[tuple[str, str]]:
    """(value, label) for the link picker: every other item, named with its kind."""
    found = catalog(app, access, CatalogQuery(limit=CATALOG_LIMIT)).items
    return [
        (f"{other.kind}:{other.id}", f"{translate(KIND_LABELS[other.kind])} · {other.label}")
        for other in found
        if (other.kind, other.id) != (item.kind, item.id)
    ]


def page_context(
    app: Services, access: ChannelAccess, item: LinkedItem, shown: ContextView
) -> dict[str, Any]:
    """The item's answers, links and mentions, for its page."""
    reference = ItemRef(kind=item.kind, id=item.id)
    slug = access.channel.slug

    def links(direction: LinkDirection, cursor: str) -> Any:
        query = LinkQuery(
            item=reference, direction=direction, cursor=cursor or None, limit=DEFAULT_LIMIT
        )
        return list_links(app, access, query)

    answers = get_context(app, access, reference)
    out, mentions = (
        links(LinkDirection.OUT, shown.links_cursor),
        links(LinkDirection.IN, shown.mentions_cursor),
    )
    return {
        "item": item,
        "answers": answers,
        "values": shown.answers or answers,
        "links": out.items,
        "links_next": out.next_cursor,
        "mentions": mentions.items,
        "mentions_next": mentions.next_cursor,
        "sent_link": shown.link,
        "page_link": context_url(slug, item),
        "own_link": own_url(slug, item),
        "item_links": {
            (other.kind, other.id): context_url(slug, other)
            for link in out.items + mentions.items
            for other in (link.source, link.target)
        },
        "questions": list(Question),
        "question_labels": QUESTION_LABELS,
        "question_hints": QUESTION_HINTS,
        "kind_labels": KIND_LABELS,
        "answer_limit": ANSWER_LENGTH,
        "note_limit": NOTE_LENGTH,
    }


def context_page(
    request: Request, app: Services, access: ChannelAccess, shown: ContextView
) -> Response:
    """An item's context, links and mentions, with the forms to change them."""
    item = found_item(app, access, shown.kind, shown.item_id)
    translate = translator(request)
    context = page_context(app, access, item, shown)
    context["errors"] = shown.outcome.errors
    context["question_options"] = [
        (question.value, translate(label)) for question, label in QUESTION_LABELS.items()
    ]
    if access.can(Permission.WRITE_CONTENT):
        context["target_options"] = target_options(app, access, item, translate)
    view = View(
        template="workspace/item_context.html",
        section=KIND_SECTIONS[item.kind],
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def item_context(
    request: Request, app: ServicesDep, access: PageAccess, kind: str, item_id: str
) -> Response:
    """An item's context: its six answers, what it links to and where it is mentioned.

    ?links_cursor= and ?mentions_cursor= page the two lists.
    """
    shown = ContextView(
        kind=kind,
        item_id=item_id,
        links_cursor=request.query_params.get("links_cursor", ""),
        mentions_cursor=request.query_params.get("mentions_cursor", ""),
    )
    return context_page(request, app, access, shown)


def answers_of(fields: ContextFields) -> ContextInput:
    """The answers a form describes."""
    return ContextInput.model_validate(
        fields.model_dump(include={question.value for question in Question})
    )


@router.post(f"{PAGE}/context")
def save_context(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    item_id: str,
    fields: Annotated[ContextFields, Form()],
) -> Response:
    """Save the item's six answers."""
    kind = request.path_params["kind"]
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    item = found_item(app, access, kind, item_id)
    translate = translator(request)
    try:
        change = ContextChange(
            item=ItemRef(kind=item.kind, id=item.id),
            expected_revision=revision_of(fields.revision),
        )
        write_context(app, access, change, answers_of(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except PartChanged:
        outcome = Outcome.conflict({"form": translate(CONTEXT_CHANGED)})
    else:
        return done(request, app, context_url(access.channel.slug, item), Flash.SAVED)
    shown = ContextView(kind=kind, item_id=item_id, outcome=outcome, answers=fields)
    return context_page(request, app, access, shown)


def link_input(fields: LinkFields) -> LinkInput:
    """The link a form describes; a malformed target reads as an empty one."""
    kind, _, target_id = fields.target.partition(":")
    return LinkInput.model_validate(
        {
            "to": {"kind": kind, "id": target_id},
            "question": fields.question,
            "note": fields.note,
        }
    )


def link_errors(error: Exception, translate: Translator) -> dict[str, str]:
    """Where a refused link's error shows on the form."""
    if isinstance(error, ValidationError):
        found = field_errors(error, translate)
        if "to" in found or any(key.startswith("to") for key in found):
            found = {key: value for key, value in found.items() if not key.startswith("to")}
            found["target"] = translate(LINK_TARGET_GONE)
        return found
    if isinstance(error, SelfLink):
        return {"target": translate(LINK_TO_ITSELF)}
    if isinstance(error, LinkExists):
        return {"target": translate(LINK_EXISTS)}
    return {"target": translate(LINK_TARGET_GONE)}


@router.post(f"{PAGE}/links")
def new_link(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    item_id: str,
    fields: Annotated[LinkFields, Form()],
) -> Response:
    """Link the item to another of the channel."""
    kind = request.path_params["kind"]
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    item = found_item(app, access, kind, item_id)
    try:
        add_link(app, access, ItemRef(kind=item.kind, id=item.id), link_input(fields))
    except (ValidationError, SelfLink, LinkExists, ItemNotFound) as refused:
        outcome = Outcome.invalid(link_errors(refused, translator(request)))
        shown = ContextView(kind=kind, item_id=item_id, outcome=outcome, link=fields)
        return context_page(request, app, access, shown)
    return done(request, app, context_url(access.channel.slug, item), Flash.LINK_ADDED)


@router.post(f"{PAGE}/links/remove")
def drop_link(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    item_id: str,
    fields: Annotated[RemoveLinkFields, Form()],
) -> Response:
    """Remove one of the item's links, or a link that mentions it."""
    kind = request.path_params["kind"]
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    item = found_item(app, access, kind, item_id)
    try:
        remove_link(app, access, UUID(fields.link_id))
    except (ValueError, LinkNotFound):
        outcome = Outcome.conflict({"form": translator(request)(LINK_GONE)})
        return context_page(
            request, app, access, ContextView(kind=kind, item_id=item_id, outcome=outcome)
        )
    return done(request, app, context_url(access.channel.slug, item), Flash.REMOVED)
