"""Channel pages: the list, a new channel, its overview, its settings and deleting it."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.book_operations import CHANNEL_BOOK
from sreditor.channels import Channel, Status
from sreditor.deps import ChannelAccess, Services, ServicesDep, request_locale
from sreditor.editorial import Editorial
from sreditor.i18n import N_
from sreditor.members import Permission
from sreditor.operations import create_channel, delete_channel, list_channels, replace_channel
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.repository import ChannelExists, RevisionMismatch
from sreditor.users import User
from sreditor.web.forms import field_errors
from sreditor.web.labels import STATUS_LABELS, STATUS_TONES, status_options
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import (
    ChannelFields,
    DeleteFields,
    NewChannelFields,
    revision_of,
    slug_from,
)
from sreditor.web.workspace.shell import (
    APP_PATH,
    CHANGED_MEANWHILE,
    Flash,
    Outcome,
    PageAccess,
    PageUser,
    View,
    channel_page,
    channel_url,
    done,
    require,
    workspace_page,
)

router = APIRouter()

SLUG_TAKEN = N_("This address is taken. Choose another one.")
WRONG_SLUG = N_("Type the channel's address exactly as shown.")


class ChecklistItem(BaseModel):
    """One thing a channel needs, the page where it is set, and who may set it."""

    model_config = ConfigDict(frozen=True)

    is_done: bool
    label: str
    suffix: str
    permission: Permission


def checklist(channel: Channel, editorial: Editorial) -> list[ChecklistItem]:
    """What the channel still needs before it is ready to publish."""
    channel_write = Permission.WRITE_CHANNEL
    editorial_write = Permission.WRITE_EDITORIAL
    steps = [
        (bool(channel.accounts), N_("Add an account"), "/accounts", channel_write),
        (channel.brand.avatar is not None, N_("Upload an avatar"), "/brand", channel_write),
        (bool(channel.brand.colors), N_("Choose brand colors"), "/brand", channel_write),
        (editorial.purpose is not None, N_("Write the purpose"), "/editorial", editorial_write),
        (editorial.voice is not None, N_("Describe the voice"), "/editorial", editorial_write),
        (
            editorial.audience is not None,
            N_("Describe the audience"),
            "/editorial",
            editorial_write,
        ),
        (
            channel.status is Status.ACTIVE,
            N_("Set the channel to active"),
            "/settings",
            channel_write,
        ),
    ]
    return [
        ChecklistItem(is_done=done, label=label, suffix=suffix, permission=permission)
        for done, label, suffix, permission in steps
    ]


def checklist_entries(access: ChannelAccess, editorial: Editorial) -> list[tuple[bool, str, str]]:
    """(done, label msgid, href) for the overview; no link where the person cannot act."""
    slug = access.channel.slug
    return [
        (
            item.is_done,
            item.label,
            channel_url(slug, item.suffix) if access.can(item.permission) else "",
        )
        for item in checklist(access.channel, editorial)
    ]


@router.get(APP_PATH)
def channel_list(request: Request, app: ServicesDep, user: PageUser, cursor: str = "") -> Response:
    """The channels the person belongs to, with their status and role."""
    found = list_channels(app, user, cursor or None, DEFAULT_LIMIT)
    roles = app.members.roles_of(user.id)
    rows = [(channel, roles.get(channel.slug)) for channel in found.items]
    return workspace_page(
        request,
        app,
        user,
        "workspace/channels.html",
        rows=rows,
        next_cursor=found.next_cursor,
        status_labels=STATUS_LABELS,
        status_tones=STATUS_TONES,
    )


def new_channel_page(
    request: Request, app: Services, user: User, fields: NewChannelFields, outcome: Outcome
) -> Response:
    """The new channel form, with what was typed and any errors."""
    return workspace_page(
        request,
        app,
        user,
        "workspace/new_channel.html",
        status_code=outcome.status_code,
        values=fields,
        errors=outcome.errors,
    )


@router.get(f"{APP_PATH}/channels/new")
def new_channel_form(request: Request, app: ServicesDep, user: PageUser) -> Response:
    """Ask for the new channel's name, address and language."""
    fields = NewChannelFields(language=request_locale(request))
    return new_channel_page(request, app, user, fields, Outcome())


@router.post(f"{APP_PATH}/channels/new")
def new_channel(
    request: Request,
    app: ServicesDep,
    user: PageUser,
    fields: Annotated[NewChannelFields, Form()],
) -> Response:
    """Create the channel, with the person as its owner."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    translate = translator(request)
    slug = fields.slug.strip() or slug_from(fields.name)
    try:
        channel = Channel(slug=slug, name=fields.name, language=fields.language.strip())
        create_channel(app, user, None, channel)
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
        return new_channel_page(request, app, user, fields, outcome)
    except ChannelExists:
        outcome = Outcome.conflict({"slug": translate(SLUG_TAKEN)})
        return new_channel_page(request, app, user, fields, outcome)
    return done(request, app, channel_url(channel.slug, "/accounts"), Flash.CREATED)


@router.get(f"{APP_PATH}/channels/{{slug}}")
def overview(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """The channel at a glance: its status, accounts, members and what it still needs."""
    channel = access.channel
    translate = translator(request)
    editorial = app.editorials.editorial(channel)
    items = checklist_entries(access, editorial)
    context: dict[str, Any] = {
        "checklist_items": [(done, translate(label), href) for done, label, href in items],
        "remaining": sum(1 for item in items if not item[0]),
        "member_count": len(app.members.list(channel.slug)),
        "episode_count": app.episodes.count(channel.slug),
        "export_link": CHANNEL_BOOK.format(slug=channel.slug),
        "books_link": f"{APP_PATH}/channels/{channel.slug}/books",
        "status_labels": STATUS_LABELS,
        "status_tones": STATUS_TONES,
    }
    return channel_page(
        request,
        app,
        access,
        View(template="workspace/overview.html", section="overview", context=context),
    )


def settings_page(
    request: Request, app: Services, access: ChannelAccess, outcome: Outcome, values: Any = None
) -> Response:
    """The settings form and the danger zone, with any errors."""
    context = {
        "errors": outcome.errors,
        "values": values or access.channel,
        "status_options": status_options(translator(request)),
    }
    view = View(
        template="workspace/settings.html",
        section="settings",
        status_code=outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(f"{APP_PATH}/channels/{{slug}}/settings")
def settings_form(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """The channel's name, language and status."""
    require(access, Permission.WRITE_CHANNEL)
    return settings_page(request, app, access, Outcome())


@router.post(f"{APP_PATH}/channels/{{slug}}/settings")
def save_settings(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[ChannelFields, Form()],
) -> Response:
    """Save the channel's name, language and status."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    translate = translator(request)
    document = access.channel.model_dump()
    document.update(name=fields.name, language=fields.language.strip(), status=fields.status)
    try:
        updated = Channel.model_validate(document)
        replace_channel(app, access, updated, revision_of(fields.revision))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
        return settings_page(request, app, access, outcome, fields)
    except RevisionMismatch:
        outcome = Outcome.conflict({"form": translate(CHANGED_MEANWHILE)})
        return settings_page(request, app, access, outcome, fields)
    return done(request, app, channel_url(access.channel.slug, "/settings"), Flash.SAVED)


@router.post(f"{APP_PATH}/channels/{{slug}}/delete")
def delete(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[DeleteFields, Form()],
) -> Response:
    """Delete the channel once its address is typed again."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.DELETE_CHANNEL)
    translate = translator(request)
    if fields.confirm_slug.strip() != access.channel.slug:
        outcome = Outcome.invalid({"confirm_slug": translate(WRONG_SLUG)})
        return settings_page(request, app, access, outcome)
    try:
        delete_channel(app, access, revision_of(fields.revision))
    except RevisionMismatch:
        outcome = Outcome.conflict({"form": translate(CHANGED_MEANWHILE)})
        return settings_page(request, app, access, outcome)
    return done(request, app, APP_PATH, Flash.DELETED)
