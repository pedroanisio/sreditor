"""The workspace's quick search (Ctrl+K): channels, the current channel's sections, episodes and pitches."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from sreditor.context import ItemKind, ItemRef
from sreditor.deps import Services, ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.library import LibraryKind
from sreditor.operations import list_channels, search_channels
from sreditor.pagination import MAX_LIMIT
from sreditor.search import HitKind, SearchHit, folded
from sreditor.search_index import SearchKind
from sreditor.search_operations import ContentQuery, SearchResult
from sreditor.search_operations import search_channel as search_channel_text
from sreditor.users import User
from sreditor.web.routes import page, translator
from sreditor.web.workspace.episodes import episodes_url
from sreditor.web.workspace.external_media import item_url
from sreditor.web.workspace.item_context import context_url
from sreditor.web.workspace.library import library_url
from sreditor.web.workspace.pitches import pitches_url
from sreditor.web.workspace.research import research_url
from sreditor.web.workspace.screenplay import screenplay_url
from sreditor.web.workspace.shell import (
    NavLink,
    PageUser,
    channel_url,
    page_access,
    section_links,
    workspace_page,
)
from sreditor.web.workspace.short_script import short_script_url

router = APIRouter()

SEARCH_PATH = "/app/search"
SEARCH_RESULTS_TEMPLATE = "workspace/_search_results.html"
# The dialog's input asks for the results alone; htmx names the element it swaps in this header.
# Boosted links send HX-Request too, so that header alone cannot tell the two apart.
TARGET_HEADER = "hx-target"
RESULTS_TARGET = "search-results"
RESULTS_PER_GROUP = 8
QUERY_LENGTH = 100

CHANNELS_GROUP = N_("Channels")
SECTIONS_GROUP = N_("Sections")
EPISODES_GROUP = N_("Episodes")
PITCHES_GROUP = N_("Pitches")
TEXT_GROUP = N_("In the text")
# What a text hit is, shown before its snippet so a research note is told from an episode title.
KIND_LABELS = {
    SearchKind.EPISODE: N_("Episode"),
    SearchKind.RESEARCH: N_("Research notes"),
    SearchKind.SOURCE: N_("Source"),
    SearchKind.SCREENPLAY: N_("Screenplay"),
    SearchKind.SHORT_SCRIPT: N_("Short script"),
    SearchKind.PITCH: N_("Pitch"),
    SearchKind.PITCH_NOTES: N_("Pitch notes"),
    SearchKind.PITCH_SOURCE: N_("Pitch source"),
    SearchKind.CHARACTER: N_("Character"),
    SearchKind.LOCATION: N_("Location"),
    SearchKind.PROP: N_("Prop"),
    SearchKind.STYLE: N_("Visual style"),
    SearchKind.EXTERNAL_MEDIA: N_("Third-party media"),
    SearchKind.CONTEXT: N_("Context"),
}


class SearchRequest(BaseModel):
    """What the person typed, and the channel they were in when they opened the search."""

    model_config = ConfigDict(frozen=True)

    text: str
    channel: str


ResultGroup = tuple[str, list[NavLink]]


def hit_link(hit: SearchHit, names: dict[str, str]) -> NavLink:
    """(href, title, channel name) of an episode or pitch."""
    if hit.kind is HitKind.EPISODE:
        href = episodes_url(hit.channel, int(hit.key))
        return href, f"#{hit.key} · {hit.title}", names[hit.channel]
    return pitches_url(hit.channel, UUID(hit.key)), hit.title, names[hit.channel]


def text_href(slug: str, result: SearchResult) -> str:
    """The workspace page where a text hit lives."""
    number = result.episode or 0
    kind = result.kind
    if kind is SearchKind.EPISODE:
        return episodes_url(slug, number)
    if kind in (SearchKind.RESEARCH, SearchKind.SOURCE):
        return research_url(slug, number)
    if kind is SearchKind.SCREENPLAY:
        return screenplay_url(slug, number)
    if kind is SearchKind.SHORT_SCRIPT:
        return short_script_url(slug, number)
    if result.pitch_id is not None and kind is not SearchKind.CONTEXT:
        return pitches_url(slug, result.pitch_id)
    return text_item_href(slug, result)


def text_item_href(slug: str, result: SearchResult) -> str:
    """The page of a library entry, third-party item or context hit."""
    if result.kind is SearchKind.EXTERNAL_MEDIA:
        return item_url(slug, result.id)
    if result.kind is SearchKind.CONTEXT:
        item_kind, _, item_id = result.id.partition(":")
        return context_url(slug, ItemRef(kind=ItemKind(item_kind), id=item_id))
    return library_url(slug, LibraryKind(result.kind.value))


def text_link(slug: str, result: SearchResult, translate: Translator) -> NavLink:
    """(href, title, snippet) of a text hit, the snippet led by what kind of text it is."""
    title = f"#{result.episode} · {result.title}" if result.episode else result.title
    detail = f"{translate(KIND_LABELS[result.kind])}: {result.snippet}"
    return text_href(slug, result), title, detail


def text_results(
    app: Services, user: User, search: SearchRequest, translate: Translator
) -> list[NavLink]:
    """Matches in the writing of the current channel; none outside a channel or without text."""
    if not search.channel or not search.text:
        return []
    access = page_access(search.channel, user, app)
    query = ContentQuery(text=search.text, limit=RESULTS_PER_GROUP)
    found = search_channel_text(app, access, query).items
    return [text_link(search.channel, result, translate) for result in found]


def channel_links(names: dict[str, str], text: str) -> list[NavLink]:
    """The person's channels whose name or address holds the text."""
    wanted = folded(text)
    found = [
        (channel_url(slug), name, slug)
        for slug, name in names.items()
        if wanted in folded(name) or wanted in slug
    ]
    return found[:RESULTS_PER_GROUP]


def section_results(
    app: Services, user: User, search: SearchRequest, translate: Translator
) -> list[NavLink]:
    """The current channel's sections whose name holds the text; none outside a channel."""
    if not search.channel:
        return []
    access = page_access(search.channel, user, app)
    wanted = folded(search.text)
    links = [link for _, group in section_links(access, translate) for link in group]
    return [link for link in links if wanted in folded(link[1])]


def search_results(
    app: Services, user: User, search: SearchRequest, translate: Translator
) -> list[ResultGroup]:
    """Every group with at least one result, in the order the dialog lists them."""
    channels = list_channels(app, user, None, MAX_LIMIT).items
    names = {channel.slug: channel.name for channel in channels}
    hits = search_channels(app, list(names), search.text, MAX_LIMIT) if search.text else []
    groups = [
        (translate(SECTIONS_GROUP), section_results(app, user, search, translate)),
        (translate(CHANNELS_GROUP), channel_links(names, search.text)),
        (
            translate(EPISODES_GROUP),
            [hit_link(hit, names) for hit in hits if hit.kind is HitKind.EPISODE],
        ),
        (
            translate(PITCHES_GROUP),
            [hit_link(hit, names) for hit in hits if hit.kind is HitKind.PITCH],
        ),
        (translate(TEXT_GROUP), text_results(app, user, search, translate)),
    ]
    return [(heading, links[:RESULTS_PER_GROUP]) for heading, links in groups if links]


@router.get(SEARCH_PATH)
def search_page(
    request: Request,
    app: ServicesDep,
    user: PageUser,
    q: Annotated[str, Query(max_length=QUERY_LENGTH)] = "",
    channel: str = "",
) -> Response:
    """The results alone for the search dialog, or a whole page when JavaScript is off."""
    search = SearchRequest(text=q.strip(), channel=channel)
    results = search_results(app, user, search, translator(request))
    context: dict[str, Any] = {
        "results": results,
        "query": search.text,
        "search_channel": search.channel,
    }
    if request.headers.get(TARGET_HEADER) == RESULTS_TARGET:
        return page(request, app, SEARCH_RESULTS_TEMPLATE, **context)
    return workspace_page(request, app, user, "workspace/search.html", **context)
