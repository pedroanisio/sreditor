"""Context routes: an item's answers to what, who, where, when, why and how, and its links."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Path, Request, Response, status
from fastapi.responses import JSONResponse

from sreditor import context_operations as contexts
from sreditor.context import (
    ContextInput,
    ItemContext,
    ItemKind,
    ItemNotFound,
    ItemRef,
    Link,
    LinkDirection,
    LinkedItem,
    LinkExists,
    LinkInput,
    LinkNotFound,
    SelfLink,
    TargetNotFound,
)
from sreditor.context_operations import CatalogQuery, ContextChange, LinkQuery
from sreditor.deps import Access, ServicesDep
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.idempotency import IdempotencyDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.problems import problem_response, problem_type

router = APIRouter(tags=["context"])

ITEM = "/channels/{slug}/items/{kind}/{item_id}"
LINK = "/channels/{slug}/links/{link_id}"

Kind = Annotated[
    ItemKind,
    Path(
        description="episode, pitch, pitch_source, source (an episode's), media (an episode's "
        "file), external_media, character, location, prop or style."
    ),
]
ItemIdPath = Annotated[
    str, Path(description="The item's id; for an episode, its number.", max_length=64)
]
LinkId = Annotated[UUID, Path(description="The link's id, from list_item_links.")]


def item_ref(kind: Kind, item_id: ItemIdPath) -> ItemRef:
    """The item in the path; 404 when its id has the wrong shape for its kind."""
    try:
        return ItemRef(kind=kind, id=item_id)
    except ValueError as malformed:
        raise ItemNotFound(ItemRef.model_construct(kind=kind, id=item_id)) from malformed


def context_change(
    item: Annotated[ItemRef, Depends(item_ref)], if_match: IfMatch = None
) -> ContextChange:
    """The item a context write targets and the revision its If-Match names."""
    return ContextChange(item=item, expected_revision=expected_revision(if_match))


def link_query(
    item: Annotated[ItemRef, Depends(item_ref)],
    direction: LinkDirection = LinkDirection.OUT,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> LinkQuery:
    """Which page of the item's links, made by it or pointing to it."""
    return LinkQuery(item=item, direction=direction, cursor=cursor, limit=limit)


Item = Annotated[ItemRef, Depends(item_ref)]
ContextTarget = Annotated[ContextChange, Depends(context_change)]
LinkListing = Annotated[LinkQuery, Depends(link_query)]


def tagged(found: ItemContext, response: Response) -> ItemContext:
    """The context, with its revision as the response's ETag."""
    response.headers["ETag"] = etag(found.revision)
    return found


@router.get("/channels/{slug}/items", summary="List linkable items")
def list_linkable_items(
    access: Access,
    app: ServicesDep,
    kind: ItemKind | None = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[LinkedItem]:
    """List the channel's items that keep a context and can be linked, kind by kind, with their
    names: episodes, pitches and their sources, episode sources and media files, third-party
    media and library entries. Use it to find the `kind` and `id` a link points to; pass
    `kind` to see only one kind."""
    return contexts.catalog(app, access, CatalogQuery(kind=kind, cursor=cursor, limit=limit))


@router.get(f"{ITEM}/context", summary="Read an item's context")
def get_item_context(
    item: Item, access: Access, app: ServicesDep, response: Response
) -> ItemContext:
    """Read an item's answers to what, who, where, when, why and how. Revision 0 (ETag "0")
    means nobody wrote them yet. Its links are listed by list_item_links."""
    return tagged(contexts.get_context(app, access, item), response)


@router.put(f"{ITEM}/context", summary="Write an item's context")
def write_item_context(
    target: ContextTarget, data: ContextInput, access: Access, app: ServicesDep, response: Response
) -> ItemContext:
    """Replace an item's six answers; an empty answer means it does not apply. Needs
    content:write. Send If-Match so you never overwrite answers you have not read."""
    return tagged(contexts.write_context(app, access, target, data), response)


@router.get(f"{ITEM}/links", summary="List an item's links")
def list_item_links(query: LinkListing, access: Access, app: ServicesDep) -> Page[Link]:
    """List the links an item makes (`direction=out`, the default), or the links other items
    make to it (`direction=in`: where it is mentioned), oldest first."""
    return contexts.list_links(app, access, query)


@router.post(
    f"{ITEM}/links",
    status_code=status.HTTP_201_CREATED,
    response_model=Link,
    summary="Link an item to another",
)
def link_item(
    item: Item, data: LinkInput, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Link an item to another of the same channel, answering one question about it: a pitch
    whose `who` is a character, a media file whose `where` is a location. Needs content:write.
    The same link twice is refused with 409. Accepts an Idempotency-Key header."""
    return idempotency.run(
        lambda: contexts.add_link(app, access, item, data), status.HTTP_201_CREATED
    )


@router.get(LINK, summary="Read a link")
def get_link(link_id: LinkId, access: Access, app: ServicesDep) -> Link:
    """Read one link of the channel, with both items named."""
    return contexts.get_link(app, access, link_id)


@router.delete(LINK, status_code=status.HTTP_204_NO_CONTENT, summary="Remove a link")
def remove_link(link_id: LinkId, access: Access, app: ServicesDep) -> Response:
    """Remove a link between two items; neither item changes. Needs content:write."""
    contexts.remove_link(app, access, link_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def field_problem(request: Request, error: Exception, kind: str) -> JSONResponse:
    """A 422 that names the link's `to` field."""
    return problem_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        str(error),
        problem_type(kind),
        errors=[{"location": ["body", "to"], "message": str(error), "type": kind}],
    )


def install_error_handlers(app: FastAPI) -> None:
    """Context errors as problem details: missing items 404, a repeated link 409."""

    @app.exception_handler(TargetNotFound)
    async def target_not_found(request: Request, missing: TargetNotFound) -> JSONResponse:
        message = (
            f"No {missing.item.kind} {missing.item.id} in this channel; list_linkable_items "
            "shows the items a link can point to"
        )
        return field_problem(request, ValueError(message), "link-target-not-found")

    @app.exception_handler(SelfLink)
    async def self_link(request: Request, refused: SelfLink) -> JSONResponse:
        return field_problem(request, refused, "self-link")

    @app.exception_handler(ItemNotFound)
    async def item_not_found(request: Request, missing: ItemNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"No {missing.item.kind} {missing.item.id} in this channel; list_linkable_items shows them",
        )

    @app.exception_handler(LinkNotFound)
    async def link_not_found(request: Request, missing: LinkNotFound) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_404_NOT_FOUND,
            f"Link {missing} not found; list_item_links shows an item's links",
        )

    @app.exception_handler(LinkExists)
    async def link_exists(request: Request, taken: LinkExists) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(taken), problem_type("link-exists")
        )
