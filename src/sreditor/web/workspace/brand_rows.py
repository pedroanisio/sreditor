"""What the brand page lists for fonts and color roles (rule project.brand)."""

from __future__ import annotations

from typing import Any

from sreditor.assets import BrandSlot, accept_attribute
from sreditor.assets import format_names as slot_formats
from sreditor.brand import own_brand
from sreditor.channels import (
    FONT_WEIGHT_MAX,
    FONT_WEIGHT_MIN,
    Channel,
    ColorRole,
    FontRole,
    FontSpec,
    Platform,
)
from sreditor.i18n import Translator
from sreditor.web.labels import COLOR_ROLE_LABELS, FONT_ROLE_LABELS

# The weights fonts come in, as CSS names them.
FONT_WEIGHT_STEP = 100


def font_rows(
    channel: Channel, platform: Platform | None, translate: Translator
) -> list[dict[str, Any]]:
    """Each font role with its own font and, on a platform, the channel's font it falls back to."""
    own = own_brand(channel, platform)
    return [
        {
            "role": role.value,
            "label": translate(FONT_ROLE_LABELS[role]),
            "own": own.fonts.get(role),
            "inherited": channel.brand.fonts.get(role) if platform else None,
            "accept": accept_attribute(BrandSlot.FONT),
            "formats": slot_formats(BrandSlot.FONT),
            "weights": weight_options(own.fonts.get(role)),
        }
        for role in FontRole
    ]


def weight_options(font: FontSpec | None) -> list[tuple[str, str]]:
    """(value, label) for the weight picker; a weight set through the API outside the steps stays."""
    weights = list(range(FONT_WEIGHT_MIN, FONT_WEIGHT_MAX + 1, FONT_WEIGHT_STEP))
    if font is not None and font.weight not in weights:
        weights = sorted([*weights, font.weight])
    return [(str(weight), str(weight)) for weight in weights]


def color_role_options(translate: Translator) -> list[tuple[str, str]]:
    """(value, label) for the color role picker."""
    return [(role.value, translate(COLOR_ROLE_LABELS[role])) for role in ColorRole]
