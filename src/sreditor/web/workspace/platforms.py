"""The channel's accounts on each platform: add, edit and remove them."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from pydantic import ValidationError

from sreditor.channels import DESCRIPTION_LIMITS, BrandKit, Platform, PlatformAccount, Status
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.operations import delete_account, put_account
from sreditor.problems import ProblemError
from sreditor.repository import AccountTaken, RevisionMismatch
from sreditor.web.forms import field_errors
from sreditor.web.labels import PLATFORM_NAMES, STATUS_LABELS, STATUS_TONES, status_options
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import AccountFields, RevisionFields, revision_of
from sreditor.web.workspace.shell import (
    CHANGED_MEANWHILE,
    Flash,
    Outcome,
    PageAccess,
    View,
    channel_page,
    channel_url,
    done,
    refused,
    require,
)

router = APIRouter()

CHOOSE_PLATFORM = N_("Choose a platform.")
CHOOSE_STATUS = N_("Choose a status.")
DESCRIPTION_TOO_LONG = N_("%(platform)s allows %(limit)s characters; this has %(count)s.")
ACCOUNTS_SUFFIX = "/accounts"
LIMITS_HINT = N_("Character limits: %(limits)s.")
ACCOUNT_TAKEN = N_("This account already belongs to another channel.")


def accounts_page(
    request: Request, app: Services, access: ChannelAccess, outcome: Outcome, editing: Any = None
) -> Response:
    """Every account with its form, and a form to add one; errors show on the form sent."""
    taken = {account.platform for account in access.channel.accounts}
    translate = translator(request)
    limits = ", ".join(
        f"{PLATFORM_NAMES[key]} {limit}" for key, limit in DESCRIPTION_LIMITS.items()
    )
    context = {
        "errors": outcome.errors,
        "editing": editing,
        "available": [platform for platform in Platform if platform not in taken],
        "platform_options": [
            (platform.value, PLATFORM_NAMES[platform])
            for platform in Platform
            if platform not in taken
        ],
        "limits_hint": translate(LIMITS_HINT, limits=limits),
        "platform_names": PLATFORM_NAMES,
        "limits": DESCRIPTION_LIMITS,
        "status_options": status_options(translate),
        "status_labels": STATUS_LABELS,
        "status_tones": STATUS_TONES,
    }
    view = View(
        template="workspace/accounts.html",
        section="accounts",
        status_code=outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/accounts")
def accounts(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """The channel's accounts."""
    return accounts_page(request, app, access, Outcome())


def description_error(fields: AccountFields, platform: Platform, translate: Translator) -> str:
    """Why the description is too long for the platform, or an empty string."""
    limit = DESCRIPTION_LIMITS.get(platform)
    count = len(fields.description)
    if limit is None or count <= limit:
        return ""
    name = PLATFORM_NAMES[platform]
    return translate(DESCRIPTION_TOO_LONG, platform=name, limit=limit, count=count)


def account_from(access: ChannelAccess, fields: AccountFields) -> PlatformAccount:
    """The account the form describes, keeping its brand overrides; ValueError when invalid."""
    platform = Platform(fields.platform)
    current = access.channel.account(platform)
    brand = current.brand if current else BrandKit()
    return PlatformAccount(
        platform=platform,
        handle=fields.handle,
        url=fields.url.strip() or None,
        description=fields.description,
        status=Status(fields.status or Status.ACTIVE),
        brand=brand,
    )


def form_errors(fields: AccountFields, translate: Translator) -> dict[str, str]:
    """Errors the domain model would report in English, worded for the page."""
    try:
        platform = Platform(fields.platform)
    except ValueError:
        return {"platform": translate(CHOOSE_PLATFORM)}
    if fields.status and fields.status not in set(Status):
        return {"status": translate(CHOOSE_STATUS)}
    if message := description_error(fields, platform, translate):
        return {"description": message}
    return {}


@router.post("/app/channels/{slug}/accounts")
def save_account(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[AccountFields, Form()],
) -> Response:
    """Add the account, or save its changes."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    translate = translator(request)
    if errors := form_errors(fields, translate):
        return accounts_page(request, app, access, Outcome.invalid(errors), fields)
    try:
        account = account_from(access, fields)
        put_account(app, access, account, revision_of(fields.revision))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
        return accounts_page(request, app, access, outcome, fields)
    except RevisionMismatch:
        outcome = Outcome.conflict({"form": translate(CHANGED_MEANWHILE)})
        return accounts_page(request, app, access, outcome, fields)
    except AccountTaken:
        outcome = Outcome.conflict({"handle": translate(ACCOUNT_TAKEN)})
        return accounts_page(request, app, access, outcome, fields)
    return done(request, app, channel_url(access.channel.slug, ACCOUNTS_SUFFIX), Flash.SAVED)


@router.post("/app/channels/{slug}/accounts/{platform}/remove")
def remove_account(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    platform: Platform,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Remove the channel's account on a platform, with its brand overrides."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    translate = translator(request)
    try:
        delete_account(app, access, platform, revision_of(fields.revision))
    except RevisionMismatch:
        outcome = Outcome.conflict({"form": translate(CHANGED_MEANWHILE)})
        return accounts_page(request, app, access, outcome)
    except ProblemError as problem:
        return accounts_page(request, app, access, refused(problem, translate))
    return done(request, app, channel_url(access.channel.slug, ACCOUNTS_SUFFIX), Flash.REMOVED)
