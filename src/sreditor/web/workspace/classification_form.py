"""The classification as people see and edit it: five groups of dimensions, each shown as tags
and edited on its own as chips (rules project.classification and ...checks).

The groups keep every rule that ties dimensions together (comedy needs a humor device, satire
and parody their details) inside one group, so a group's save never breaks another's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import FormData

from sreditor.classification import (
    MAX_REFERENCES,
    MAX_SECONDARY,
    PARODIC,
    REFERENCE_LENGTH,
    SATIRE_LENGTH,
    SATIRICAL,
    VOCABULARY,
    Classification,
    ClassificationRefused,
    ClassifiedValue,
    Dimension,
    Method,
    Refusal,
    State,
    ValueRole,
)
from sreditor.i18n import N_, Translator
from sreditor.web.classification_labels import DIMENSION_LABELS, STATE_LABELS, VALUE_LABELS
from sreditor.web.forms import message_for

STATE_PREFIX = "state:"
ALSO_SUFFIX = "_also"
PERCENT = 100
SATIRE_TARGET = "satire_target"
SATIRE_BEHAVIOR = "satire_behavior"
PARODY_REFERENCES = "parody_references"
PRIMARY_TAG = ""
SECONDARY_TAG = "outline"
TOO_MANY_SECONDARY = N_("At most two secondary values")


class Group(BaseModel):
    """Dimensions edited together; `has_details` also holds the satire and parody fields."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    dimensions: tuple[Dimension, ...]
    has_details: bool = False


GROUPS = (
    Group(
        key="purpose",
        title=N_("Who it is for and why"),
        dimensions=(Dimension.AUDIENCE, Dimension.PURPOSES),
    ),
    Group(key="topics", title=N_("What it is about"), dimensions=(Dimension.DOMAINS,)),
    Group(
        key="genre",
        title=N_("Genre and approach"),
        dimensions=(Dimension.GENRES, Dimension.APPROACHES, Dimension.HUMOR_DEVICES),
        has_details=True,
    ),
    Group(
        key="tone",
        title=N_("Format and tone"),
        dimensions=(
            Dimension.FORMATS,
            Dimension.TONES,
            Dimension.EMOTIONAL_TARGETS,
            Dimension.CREATIVE_DEVICES,
        ),
    ),
    Group(
        key="look",
        title=N_("Look"),
        dimensions=(Dimension.VISUAL_TECHNIQUES, Dimension.VISUAL_STYLES),
    ),
)
GROUP_OF = {group.key: group for group in GROUPS}
DETAILS_GROUP = next(group for group in GROUPS if group.has_details)


def group_of(key: str) -> Group:
    """The group a form names; an unknown name is the first group, which saves nothing else."""
    return GROUP_OF.get(key, GROUPS[0])


# Reading a group's form


def field_of(location: Sequence[str | int]) -> str:
    """The form field an API location shows next to: dimensions.genres is genres."""
    parts = [str(part) for part in location]
    if parts[:1] == ["dimensions"] and len(parts) > 1:
        return parts[1]
    if parts[:1] == ["satire"]:
        return SATIRE_BEHAVIOR if "criticized_behavior" in parts else SATIRE_TARGET
    return parts[0] if parts else "form"


def model_errors(invalid: ValidationError, translate: Translator) -> dict[str, str]:
    """The first error for each form field, from a model that refused the form."""
    errors: dict[str, str] = {}
    for error in invalid.errors():
        errors.setdefault(field_of(error["loc"]), message_for(dict(error), translate))
    return errors


def refusal_errors(refusal: ClassificationRefused, translate: Translator) -> dict[str, str]:
    """The first rule each form field breaks, in the person's language."""
    errors: dict[str, str] = {}
    for found in refusal.refusals:
        errors.setdefault(field_of(found.field.split(".")), translate(found.message))
    return errors


def dimension_from_form(
    form: FormData, dimension: Dimension, kept: dict[str, float | None]
) -> dict[str, Any]:
    """One dimension from its chips; a value already classified keeps its confidence."""
    main = str(form.get(dimension.value, f"{STATE_PREFIX}{State.UNKNOWN}"))
    has_main = not main.startswith(STATE_PREFIX)
    # The main value ticked again under "also" counts once.
    others = [str(value) for value in form.getlist(dimension.value + ALSO_SUFFIX) if value != main]
    if len(others) > MAX_SECONDARY:
        refusal = Refusal(field=f"dimensions.{dimension.value}", message=TOO_MANY_SECONDARY)
        raise ClassificationRefused([refusal])
    chosen = [(main, ValueRole.PRIMARY)] if has_main else []
    chosen += [(value, ValueRole.SECONDARY) for value in others]
    items = [
        {"value": value, "role": role, "confidence": kept.get(value)} for value, role in chosen
    ]
    # A state the form never offers is left to the model, which refuses it as a field error.
    state = State.PRESENT if items else main.removeprefix(STATE_PREFIX)
    return {"state": state, "items": items}


def details_from_form(form: FormData) -> dict[str, Any]:
    """The satire and parody fields."""
    target = str(form.get(SATIRE_TARGET, "")).strip()
    behavior = str(form.get(SATIRE_BEHAVIOR, "")).strip()
    lines = str(form.get(PARODY_REFERENCES, "")).splitlines()
    satire = {"target": target, "criticized_behavior": behavior} if target or behavior else None
    return {"satire": satire, "parody_references": [line.strip() for line in lines if line.strip()]}


def group_from_form(form: FormData, group: Group, before: Classification) -> Classification:
    """The classification with one group replaced from its form, saved by a person."""
    document = before.model_dump(mode="json")
    for dimension in group.dimensions:
        kept = {item.value: item.confidence for item in before.of(dimension).items}
        document["dimensions"][dimension.value] = dimension_from_form(form, dimension, kept)
    if group.has_details:
        document.update(details_from_form(form))
    document["method"] = Method.HUMAN
    return Classification.model_validate(document)


# Showing it


def value_label(item: ClassifiedValue, translate: Translator) -> str:
    """A value as people read it, with an agent's confidence when there is one."""
    label = translate(VALUE_LABELS[item.value])
    if item.confidence is None:
        return label
    return f"{label} · {round(item.confidence * PERCENT)}%"


def dimension_tags(data: Classification, dimension: Dimension, translate: Translator) -> list[Any]:
    """A dimension's values as tags, main first; a state as one quiet tag; unknown as none."""
    found = data.of(dimension)
    if found.state is State.UNKNOWN:
        return []
    if found.state is not State.PRESENT:
        return [(translate(STATE_LABELS[found.state]), SECONDARY_TAG)]
    ordered = sorted(found.items, key=lambda item: item.role is not ValueRole.PRIMARY)
    return [
        (
            value_label(item, translate),
            PRIMARY_TAG if item.role is ValueRole.PRIMARY else SECONDARY_TAG,
        )
        for item in ordered
    ]


def detail_entries(data: Classification, translate: Translator) -> list[Any]:
    """The satire and parody details, when there are any."""
    entries: list[Any] = []
    if data.satire:
        entries.append((translate("Satire target"), [(data.satire.target, SECONDARY_TAG)]))
        behavior = [(data.satire.criticized_behavior, SECONDARY_TAG)]
        entries.append((translate("Behaviour it criticises"), behavior))
    if data.parody_references:
        references = [(reference, SECONDARY_TAG) for reference in data.parody_references]
        entries.append((translate("Works it parodies"), references))
    return entries


def summary(data: Classification, group: Group, translate: Translator) -> list[Any]:
    """A group as (dimension, tags) pairs, for reading."""
    entries = [
        (translate(DIMENSION_LABELS[dimension]), dimension_tags(data, dimension, translate))
        for dimension in group.dimensions
    ]
    return entries + (detail_entries(data, translate) if group.has_details else [])


def chips(data: Classification, dimension: Dimension, translate: Translator) -> dict[str, Any]:
    """A dimension's two chip sets: the main value or a state, and up to two more."""
    values = [(value, translate(VALUE_LABELS[value])) for value in VOCABULARY[dimension]]
    states = [(STATE_PREFIX + state, translate(label)) for state, label in STATE_LABELS.items()]
    found = data.of(dimension)
    main = data.primary(dimension) or STATE_PREFIX + found.state
    return {
        "name": dimension.value,
        "label": translate(DIMENSION_LABELS[dimension]),
        "main_options": states + values,
        "muted": [value for value, _label in states],
        "main": [main],
        "also_name": dimension.value + ALSO_SUFFIX,
        "also_options": values,
        "also": data.values(dimension)[1:],
    }


def shows_details(data: Classification) -> dict[str, bool]:
    """Which detail fields the form shows: only those the approach asks for, or already set."""
    approaches = data.values(Dimension.APPROACHES)
    return {
        "satire": SATIRICAL in approaches or data.satire is not None,
        "parody": PARODIC in approaches or bool(data.parody_references),
    }


def details(data: Classification) -> dict[str, Any]:
    """The satire and parody fields' values and limits."""
    return {
        "satire_target": data.satire.target if data.satire else "",
        "satire_behavior": data.satire.criticized_behavior if data.satire else "",
        "parody_references": "\n".join(data.parody_references),
        "satire_limit": SATIRE_LENGTH,
        "reference_limit": REFERENCE_LENGTH * MAX_REFERENCES,
        **shows_details(data),
    }


class GroupsShown(BaseModel):
    """What the page shows: the saved classification, and the group being edited, as sent."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    saved: Classification
    editing: str = ""
    sent: Classification | None = None
    hints: dict[str, str] = {}


def groups_view(shown: GroupsShown, translate: Translator) -> list[dict[str, Any]]:
    """Each group's summary from what is saved, and its form from what was sent, if anything."""
    views = []
    for group in GROUPS:
        is_open = group.key == shown.editing
        form_data = shown.sent if is_open and shown.sent else shown.saved
        dimensions = []
        for dimension in group.dimensions:
            found = chips(form_data, dimension, translate)
            dimensions.append({**found, "hint": shown.hints.get(dimension.value, "")})
        views.append(
            {
                "key": group.key,
                "title": translate(group.title),
                "summary": summary(shown.saved, group, translate),
                "dimensions": dimensions,
                "has_details": group.has_details,
                "details": details(form_data),
                "is_open": is_open,
            }
        )
    return views
