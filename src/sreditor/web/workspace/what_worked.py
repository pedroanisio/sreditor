"""The "What worked" tab of the metrics page: posts compared by classification, hook or length.

It reads the numbers people entered (rule project.metrics.what-worked); nothing is fetched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from sreditor.channels import Platform
from sreditor.classification import Dimension
from sreditor.deps import ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.post_metrics import Metric
from sreditor.web.classification_labels import DIMENSION_LABELS, VALUE_LABELS
from sreditor.web.labels import platform_name
from sreditor.web.routes import translator
from sreditor.web.workspace.post_metrics import (
    COMPARE_PAGE,
    METRIC_LABELS,
    metrics_tabs,
)
from sreditor.web.workspace.shell import PageAccess, View, channel_page, require
from sreditor.web.workspace.short_script import REACTION_LABELS
from sreditor.what_worked import (
    Excluded,
    GroupBy,
    SnapshotAge,
    WhatWorked,
    WhatWorkedQuery,
)
from sreditor.what_worked_operations import what_worked

router = APIRouter()

PAGE = f"{COMPARE_PAGE}/what-worked"
WHAT_WORKED_URL = "/app/channels/{slug}/metrics/what-worked"
CLASSIFICATION_PREFIX = "classification."
DEFAULT_CHOICE = f"{CLASSIFICATION_PREFIX}{Dimension.GENRES.value}"
ALL_PLATFORMS = ""
GROUP_LABELS: dict[GroupBy, str] = {
    GroupBy.HOOK_REACTION: N_("Hook: the reaction it aims for"),
    GroupBy.HOOK_LENGTH: N_("Hook: how long it runs"),
    GroupBy.VIDEO_LENGTH: N_("Length of the video"),
}
BAND_LABELS: dict[str, str] = {
    "up_to_2s": N_("Up to 2 s"),
    "2_to_3s": N_("2 to 3 s"),
    "over_3s": N_("Over 3 s"),
    "up_to_15s": N_("Up to 15 s"),
    "15_to_30s": N_("15 to 30 s"),
    "30_to_60s": N_("30 to 60 s"),
    "over_60s": N_("Over 60 s"),
}
AGE_LABELS: dict[SnapshotAge, str] = {
    SnapshotAge.LATEST: N_("Latest numbers"),
    SnapshotAge.DAY_1: N_("1 day after publishing"),
    SnapshotAge.DAY_3: N_("3 days after publishing"),
    SnapshotAge.DAY_7: N_("7 days after publishing"),
    SnapshotAge.DAY_14: N_("14 days after publishing"),
    SnapshotAge.DAY_30: N_("30 days after publishing"),
}
EXCLUDED_LABELS: dict[str, str] = {
    "without_publish_time": N_("%(count)s without a publish time"),
    "proposals": N_("%(count)s whose classification nobody reviewed"),
    "without_value": N_("%(count)s with no value for this grouping"),
    "without_numbers": N_("%(count)s with no numbers at that age"),
}


class WorkedFields(BaseModel):
    """The pickers of the page, as they come in the address."""

    model_config = ConfigDict(frozen=True)

    by: str = DEFAULT_CHOICE
    metric: Metric = Metric.VIEWS
    age: SnapshotAge = SnapshotAge.LATEST
    platform: str = ALL_PLATFORMS


def group_choices(translate: Translator) -> list[tuple[str, str]]:
    """What posts can be grouped by, as (value, label) options."""
    classified = [
        (
            f"{CLASSIFICATION_PREFIX}{dimension.value}",
            translate(N_("Classification: %(name)s"), name=translate(DIMENSION_LABELS[dimension])),
        )
        for dimension in Dimension
    ]
    other = [(group.value, translate(label)) for group, label in GROUP_LABELS.items()]
    return classified + other


def dimension_of(choice: str) -> Dimension | None:
    """The classification dimension a picker value names, if it names one."""
    if not choice.startswith(CLASSIFICATION_PREFIX):
        return None
    name = choice.removeprefix(CLASSIFICATION_PREFIX)
    return next((dimension for dimension in Dimension if dimension.value == name), None)


def query_of(fields: WorkedFields) -> WhatWorkedQuery:
    """The operation's query for the pickers; an unknown choice falls back to the default."""
    other = next((group for group in GROUP_LABELS if group.value == fields.by), None)
    dimension = None if other else dimension_of(fields.by) or Dimension.GENRES
    return WhatWorkedQuery(
        group_by=other or GroupBy.CLASSIFICATION,
        classification_dimension=dimension,
        metric=fields.metric,
        age=fields.age,
        platform=next((found for found in Platform if found.value == fields.platform), None),
    )


def key_label(key: str, translate: Translator) -> str:
    """A group's key in the person's language; an unlisted key shows as it is."""
    reactions = {reaction.value: label for reaction, label in REACTION_LABELS.items()}
    label = {**VALUE_LABELS, **reactions, **BAND_LABELS}.get(key)
    return translate(label) if label else key


def excluded_text(excluded: Excluded, translate: Translator) -> str:
    """The posts no group holds, as one line; empty when every post is in a group."""
    parts = [
        translate(label, count=count)
        for name, label in EXCLUDED_LABELS.items()
        if (count := getattr(excluded, name))
    ]
    return " · ".join(parts)


def rows_of(found: WhatWorked, translate: Translator) -> list[tuple[str, str, bool, str]]:
    """Each group as (title, numbers line, is_too_small, episodes line)."""
    return [
        (
            key_label(group.key, translate),
            translate(
                N_("Posts %(count)s · Median %(median)g · Mean %(mean)g"),
                count=group.posts,
                median=group.median,
                mean=group.mean,
            ),
            group.is_too_small,
            ", ".join(f"#{number}" for number in group.episodes),
        )
        for group in found.groups
    ]


@router.get(PAGE)
def what_worked_page(
    request: Request, app: ServicesDep, access: PageAccess, fields: Annotated[WorkedFields, Query()]
) -> Response:
    """Posts grouped by the chosen dimension, with the median and mean of the chosen number."""
    require(access, Permission.READ)
    translate = translator(request)
    found = what_worked(app, access, query_of(fields))
    context = {
        "tabs": metrics_tabs(access.channel.slug, translate),
        "rows": rows_of(found, translate),
        "excluded_line": excluded_text(found.excluded, translate),
        "min_posts": found.min_group_posts,
        "action": WHAT_WORKED_URL.format(slug=access.channel.slug),
        "sent": fields,
        "group_options": group_choices(translate),
        "metric_options": [
            (metric.value, translate(label)) for metric, label in METRIC_LABELS.items()
        ],
        "age_options": [(age.value, translate(label)) for age, label in AGE_LABELS.items()],
        "platform_options": [(ALL_PLATFORMS, translate(N_("All platforms")))]
        + [(platform.value, platform_name(platform)) for platform in Platform],
    }
    view = View(template="workspace/what_worked.html", section="metrics", context=context)
    return channel_page(request, app, access, view)
