"""The Editorial: house rules, the channel's guidelines and each platform's overrides.

Each guideline says where it comes from, so a person sees what they can change here and what is
inherited from the house or the channel.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.channels import Channel, Platform
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.editorial import (
    GUIDELINE_LENGTH,
    SINGLE_KINDS,
    CitedStatementNotFound,
    Editorial,
    Guideline,
    GuidelineInput,
    GuidelineKind,
    GuidelineNotCited,
    GuidelineNotFound,
)
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.operations import retire_guideline, take_cited_text, write_guideline
from sreditor.problems import ProblemError
from sreditor.store import StoreError
from sreditor.web.forms import field_errors
from sreditor.web.labels import KIND_HINTS, KIND_LABELS, PLATFORM_NAMES
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import GuidelineFields, RetireFields, platform_or_none
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    level_tabs,
    refused,
    require,
)

router = APIRouter()

EDITORIAL_SUFFIX = "/editorial"
FROM_HOUSE = N_("House rule")
FROM_CHANNEL = N_("From the channel")
HERE = N_("Set here")
CHOOSE_KIND = N_("Choose what this guideline is about.")
NOT_SAVED = N_("The guideline could not be saved. Try again in a moment.")
BAD_GATEWAY = status.HTTP_502_BAD_GATEWAY
ALREADY_RETIRED = N_("This guideline was already retired. Reload the page.")
NOT_CITED = N_("This guideline cites no statement, so there is no text to take.")
CITED_GONE = N_("The statement this guideline cites no longer exists, so its text cannot be taken.")
CITED_UNKNOWN = N_("No enabled statement has this id.")


def editorial_url(slug: str, platform: Platform | None) -> str:
    """The editorial page at a level."""
    query = f"?platform={platform.value}" if platform else ""
    return channel_url(slug, EDITORIAL_SUFFIX) + query


def origin(app: Services, channel: Channel, platform: Platform | None, guideline: Guideline) -> str:
    """Where a guideline comes from, as a msgid: here, the channel, or the house."""
    if guideline.scope == app.editorials.scope(channel.slug, platform):
        return HERE
    if guideline.scope == app.editorials.scope(channel.slug):
        return FROM_CHANNEL
    return FROM_HOUSE


def kind_guidelines(editorial: Editorial, kind: GuidelineKind) -> list[Guideline]:
    """The guidelines of one kind: one at most for purpose, voice and audience."""
    if kind is GuidelineKind.PILLAR:
        return editorial.pillars
    if kind is GuidelineKind.AVOID:
        return editorial.avoid
    single: Guideline | None = getattr(editorial, kind.value)
    return [single] if single else []


def kind_rows(
    app: Services, channel: Channel, platform: Platform | None, editorial: Editorial
) -> list[dict[str, Any]]:
    """Each kind with its guidelines, where each comes from, and whether it is set here."""
    rows = []
    for kind in GuidelineKind:
        found = kind_guidelines(editorial, kind)
        entries = [(item, origin(app, channel, platform, item)) for item in found]
        here = [item for item, source in entries if source == HERE]
        rows.append(
            {
                "kind": kind,
                "label": KIND_LABELS[kind],
                "hint": KIND_HINTS[kind],
                "is_single": kind in SINGLE_KINDS,
                "entries": entries,
                "current": here[0].text if here and kind in SINGLE_KINDS else "",
            }
        )
    return rows


class EditorialView(BaseModel):
    """What the editorial page shows at one level, with the errors of the form that was sent."""

    model_config = ConfigDict(frozen=True)

    platform: Platform | None
    outcome: Outcome
    sent: GuidelineFields | None = None


def editorial_page(
    request: Request, app: Services, access: ChannelAccess, shown: EditorialView
) -> Response:
    """The guidelines at one level, with the tabs for the others."""
    channel = access.channel
    editorial = app.editorials.with_drift(
        app.editorials.editorial(channel, shown.platform), channel
    )
    context = {
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "platform": shown.platform,
        "platform_names": PLATFORM_NAMES,
        "tabs": level_tabs(channel, EDITORIAL_SUFFIX, translator(request)),
        "kinds": kind_rows(app, channel, shown.platform, editorial),
        "limit": GUIDELINE_LENGTH,
        "here": HERE,
        "drift": {warning.guideline_id: warning for warning in editorial.warnings},
        "cited": {
            item.id: item.citation.statement_id
            for item in editorial.all_guidelines()
            if item.citation is not None
        },
    }
    view = View(
        template="workspace/editorial.html",
        section="editorial",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


def level(access: ChannelAccess, platform: str) -> Platform | None:
    """The level a page or form names; 404 when the channel has no such account."""
    found = platform_or_none(platform)
    if found is not None and access.channel.account(found) is None:
        raise PageNotFound(platform)
    return found


@router.get("/app/channels/{slug}/editorial")
def editorial(
    request: Request, app: ServicesDep, access: PageAccess, platform: str = ""
) -> Response:
    """The Editorial at the channel's level, or a platform's overrides."""
    shown = EditorialView(platform=level(access, platform), outcome=Outcome())
    return editorial_page(request, app, access, shown)


def write_outcome(
    app: Services, access: ChannelAccess, fields: GuidelineFields, translate: Translator
) -> Outcome | None:
    """Write the guideline the form describes; the errors to show, or None when it is saved."""
    if fields.kind not in set(GuidelineKind):
        return Outcome.invalid({"kind": translate(CHOOSE_KIND)})
    platform = platform_or_none(fields.platform)
    try:
        data = GuidelineInput(
            kind=GuidelineKind(fields.kind),
            text=fields.text,
            platform=platform,
            cited_statement_id=fields.cited_statement_id.strip() or None,
        )
        write_guideline(app, access, data)
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except CitedStatementNotFound:
        return Outcome.invalid({"cited_statement_id": translate(CITED_UNKNOWN)})
    except StoreError:
        return Outcome.refused({"form": translate(NOT_SAVED)}, BAD_GATEWAY)
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post("/app/channels/{slug}/editorial")
def write(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[GuidelineFields, Form()],
) -> Response:
    """Set the purpose, voice or audience here, or add a pillar or an avoid guideline."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    platform = level(access, fields.platform)
    outcome = write_outcome(app, access, fields, translator(request))
    if outcome is not None:
        shown = EditorialView(platform=platform, outcome=outcome, sent=fields)
        return editorial_page(request, app, access, shown)
    return done(request, app, editorial_url(access.channel.slug, platform), Flash.SAVED)


def retire_outcome(
    app: Services, access: ChannelAccess, fields: RetireFields, translate: Translator
) -> Outcome | None:
    """Retire the guideline; the error to show, or None when it is retired."""
    try:
        retire_guideline(app, access, fields.statement_id, platform_or_none(fields.platform))
    except GuidelineNotFound:
        return Outcome.conflict({"form": translate(ALREADY_RETIRED)})
    except StoreError:
        return Outcome.refused({"form": translate(NOT_SAVED)}, BAD_GATEWAY)
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post("/app/channels/{slug}/editorial/retire")
def retire(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[RetireFields, Form()],
) -> Response:
    """Retire a guideline set at this level."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    platform = level(access, fields.platform)
    outcome = retire_outcome(app, access, fields, translator(request))
    if outcome is not None:
        shown = EditorialView(platform=platform, outcome=outcome)
        return editorial_page(request, app, access, shown)
    return done(request, app, editorial_url(access.channel.slug, platform), Flash.REMOVED)


def take_outcome(
    app: Services, access: ChannelAccess, fields: RetireFields, translate: Translator
) -> Outcome | None:
    """Take the cited statement's text; the error to show, or None when the guideline is revised."""
    try:
        take_cited_text(app, access, fields.statement_id, platform_or_none(fields.platform))
    except GuidelineNotFound:
        return Outcome.conflict({"form": translate(ALREADY_RETIRED)})
    except GuidelineNotCited:
        return Outcome.conflict({"form": translate(NOT_CITED)})
    except CitedStatementNotFound:
        return Outcome.conflict({"form": translate(CITED_GONE)})
    except StoreError:
        return Outcome.refused({"form": translate(NOT_SAVED)}, BAD_GATEWAY)
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post("/app/channels/{slug}/editorial/take-cited-text")
def take_text(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[RetireFields, Form()],
) -> Response:
    """Revise a guideline to the current text of the statement it cites."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    platform = level(access, fields.platform)
    outcome = take_outcome(app, access, fields, translator(request))
    if outcome is not None:
        shown = EditorialView(platform=platform, outcome=outcome)
        return editorial_page(request, app, access, shown)
    return done(request, app, editorial_url(access.channel.slug, platform), Flash.SAVED)
