"""Classification pages: an episode's editorial classification, and the channel's profile with
what its episodes show (rules project.classification, ...checks and ...profile).

Both pages read as five groups of tags; each group opens in a dialog of its own, so a person
edits a few dimensions at a time. A person's save is reviewed; an agent's proposal shows until
someone edits or confirms it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import FormData

from sreditor.classification import (
    Classification,
    ClassificationRefused,
    Dimension,
    ObservedProfile,
)
from sreditor.classification_operations import (
    get_classification,
    get_observed_profile,
    get_profile,
    write_classification,
    write_profile,
)
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.web.classification_labels import DIMENSION_LABELS, VALUE_LABELS
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.classification_form import (
    GROUPS,
    PERCENT,
    GroupsShown,
    group_from_form,
    group_of,
    groups_view,
    model_errors,
    refusal_errors,
)
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    View,
    channel_page,
    channel_url,
    done,
    require,
)

router = APIRouter()

EPISODE_PAGE = "/app/channels/{slug}/episodes/{number}/classification"
CONFIRM_PAGE = EPISODE_PAGE + "/confirm"
PROFILE_PAGE = "/app/channels/{slug}/profile"
SUFFIX = "/classification"
PROFILE_SUFFIX = "/profile"
PART_GONE = N_("Someone changed this after you opened the page. Reload the page.")
DRIFT_TEXT = N_("%(dimension)s: the channel says %(value)s, but only %(share)s%% of episodes are.")


def classification_url(slug: str, number: int) -> str:
    """The episode's classification tab."""
    return episodes_url(slug, number) + SUFFIX


def profile_url(slug: str) -> str:
    """The channel's profile page."""
    return channel_url(slug, PROFILE_SUFFIX)


def value_names(values: list[str], translate: Translator) -> str:
    """Values as people read them, joined."""
    return ", ".join(translate(VALUE_LABELS[value]) for value in values)


def channel_hints(declared: Classification, translate: Translator) -> dict[str, str]:
    """What the channel declares for each dimension, as a hint next to the episode's."""
    hints = {}
    for dimension in Dimension:
        values = declared.values(dimension)
        if values:
            hints[dimension.value] = translate(
                "Channel: %(values)s", values=value_names(values, translate)
            )
    return hints


def save_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run a save; the errors to show by field, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(model_errors(invalid, translate))
    except ClassificationRefused as refusal:
        return Outcome.invalid(refusal_errors(refusal, translate))
    except PartChanged:
        return Outcome.conflict({"form": translate(PART_GONE)})
    return None


class Saved(BaseModel):
    """What a group's save did: the errors, the group and the classification as sent."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome | None
    group: str
    sent: Classification | None


def saved_group(
    form: FormData,
    before: Classification,
    write: Callable[[Classification, int | None], object],
    translate: Translator,
) -> Saved:
    """Replace one group from its form and write the result with the form's revision."""
    group = group_of(str(form.get("group", "")))
    sent: list[Classification] = []

    def run() -> None:
        sent.append(group_from_form(form, group, before))
        write(sent[0], revision_of(str(form.get("revision", ""))))

    outcome = save_errors(run, translate)
    return Saved(outcome=outcome, group=group.key, sent=sent[0] if sent else None)


# The episode tab


class ClassificationView(BaseModel):
    """The classification tab, with the group being edited as sent when it failed."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    editing: str = ""
    sent: Classification | None = None


def episode_page(
    request: Request, app: Services, access: ChannelAccess, shown: ClassificationView
) -> Response:
    """The episode's classification as tags by group, next to what the channel declares."""
    translate = translator(request)
    current = found_episode(app, access, shown.number)
    found = get_classification(app, access, shown.number)
    declared = get_profile(app, access).classification
    groups = GroupsShown(
        saved=found.classification,
        editing=shown.editing,
        sent=shown.sent,
        hints=channel_hints(declared, translate),
    )
    context = {
        "episode": current.episode,
        "found": found,
        "is_proposal": found.revision > 0 and not found.is_reviewed,
        "groups": groups_view(groups, translate),
        "errors": shown.outcome.errors,
        "form_link": classification_url(access.channel.slug, shown.number),
        "episode_tab": "classification",
        **episode_frame(app, access, shown.number, translate),
        **labels(),
    }
    view = View(
        template="workspace/classification.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(EPISODE_PAGE)
def episode_classification(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """An episode's editorial classification."""
    return episode_page(request, app, access, ClassificationView(number=number))


@router.post(EPISODE_PAGE)
async def save_episode_classification(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """Save one group; a person's save also confirms an agent's proposal."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    before = get_classification(app, access, number).classification

    def write(data: Classification, revision: int | None) -> None:
        write_classification(app, access, number, data, revision)

    saved = saved_group(form, before, write, translator(request))
    if saved.outcome is None:
        return done(request, app, classification_url(access.channel.slug, number), Flash.SAVED)
    shown = ClassificationView(
        number=number, outcome=saved.outcome, editing=saved.group, sent=saved.sent
    )
    return episode_page(request, app, access, shown)


@router.post(CONFIRM_PAGE)
async def confirm_episode_classification(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """Confirm an agent's proposal as it is, so it counts as reviewed."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found = get_classification(app, access, number).classification
    revision = revision_of(str(form.get("revision", "")))
    outcome = save_errors(
        lambda: write_classification(app, access, number, found, revision), translator(request)
    )
    if outcome is None:
        return done(request, app, classification_url(access.channel.slug, number), Flash.SAVED)
    return episode_page(request, app, access, ClassificationView(number=number, outcome=outcome))


# The channel's profile


class ProfileView(BaseModel):
    """The profile page, with the group being edited as sent when it failed."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    editing: str = ""
    sent: Classification | None = None


def observed_text(observed: ObservedProfile, dimension: Dimension, translate: Translator) -> str:
    """What the episodes hold for a dimension, most common first, with each value's share."""
    seen = sorted(
        (found for found in observed.observations if found.dimension is dimension),
        key=lambda found: -found.episode_count,
    )
    return ", ".join(
        f"{translate(VALUE_LABELS[found.value])} {round(found.prevalence * PERCENT)}%"
        for found in seen
    )


def comparison(
    declared: Classification, observed: ObservedProfile, translate: Translator
) -> dict[str, list[dict[str, str]]]:
    """Per group, each dimension: what the channel declares and what its episodes show."""
    return {
        group.key: [
            {
                "label": translate(DIMENSION_LABELS[dimension]),
                "declared": value_names(declared.values(dimension), translate),
                "observed": observed_text(observed, dimension, translate),
            }
            for dimension in group.dimensions
        ]
        for group in GROUPS
    }


def drift_lines(observed: ObservedProfile, translate: Translator) -> list[str]:
    """Each drift as a sentence."""
    return [
        translate(
            DRIFT_TEXT,
            dimension=translate(DIMENSION_LABELS[found.dimension]),
            value=translate(VALUE_LABELS[found.value]),
            share=round(found.prevalence * PERCENT),
        )
        for found in observed.drift
    ]


def profile_page(
    request: Request, app: Services, access: ChannelAccess, shown: ProfileView
) -> Response:
    """The declared profile next to the observed one, by group, and for editors, the dialogs."""
    translate = translator(request)
    declared = get_profile(app, access)
    observed = get_observed_profile(app, access)
    groups = GroupsShown(saved=declared.classification, editing=shown.editing, sent=shown.sent)
    context: dict[str, Any] = {
        "profile": declared,
        "observed": observed,
        "comparison": comparison(declared.classification, observed, translate),
        "drift": drift_lines(observed, translate),
        "groups": groups_view(groups, translate),
        "errors": shown.outcome.errors,
        "form_link": profile_url(access.channel.slug),
    }
    view = View(
        template="workspace/profile.html",
        section="profile",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PROFILE_PAGE)
def profile(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """The channel's profile: what it declares and what its episodes show."""
    return profile_page(request, app, access, ProfileView())


@router.post(PROFILE_PAGE)
async def save_profile(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """Save one group of the profile the channel declares."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    before = get_profile(app, access).classification

    def write(data: Classification, revision: int | None) -> None:
        write_profile(app, access, data, revision)

    saved = saved_group(form, before, write, translator(request))
    if saved.outcome is None:
        return done(request, app, profile_url(access.channel.slug), Flash.SAVED)
    shown = ProfileView(outcome=saved.outcome, editing=saved.group, sent=saved.sent)
    return profile_page(request, app, access, shown)
