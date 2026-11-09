"""Readiness: what still holds an episode back, each gap with the call that fixes it (project.readiness).

Every area that already computes warnings has one function here that wraps it and turns each
finding into a ReadinessItem. The registry at the end lists them, so another area is one more
line. A blocker stops an srstudio export or a render; everything else only warns, as the rules
of each area say. fix_tool is always the name of a tool the MCP server registers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from sreditor.delivery import WARNING_MESSAGES as DELIVERY_MESSAGES
from sreditor.delivery import DeliveryWarningKind
from sreditor.episode_facts import EpisodeFacts
from sreditor.episodes import EpisodeStatus
from sreditor.external_media import Problem
from sreditor.i18n import N_
from sreditor.members import Permission
from sreditor.reviews import Verdict
from sreditor.short_script import WARNING_MESSAGES as SCRIPT_MESSAGES
from sreditor.short_script import WarningKind
from sreditor.srstudio_export import (
    NO_KIND,
    NO_SCRIPT,
    NO_STYLE,
    NO_TAKE,
    NO_WORLD,
    NOT_TOON,
    STALE_TAKE,
    TAKE_GONE,
    UNPUBLISHED,
)

NAME_SEPARATOR = ", "


class Severity(StrEnum):
    """Whether a gap stops work (blocker) or only deserves a look (warning)."""

    BLOCKER = "blocker"
    WARNING = "warning"


class Area(StrEnum):
    """The part of the episode a gap belongs to; pages link each area to the page that fixes it."""

    CLAIMS = "claims"
    CREDITS = "credits"
    DELIVERY = "delivery"
    SHORT_SCRIPT = "short_script"
    LIBRARY = "library"
    SRSTUDIO = "srstudio"
    REVIEWS = "reviews"


class Fix(BaseModel):
    """The tool that resolves a gap and what to do with it."""

    model_config = ConfigDict(frozen=True)

    tool: str
    hint: str


class Kind(BaseModel):
    """One sort of gap: its stable code, severity, area and fix."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    area: Area
    fix: Fix


class ReadinessItem(BaseModel):
    """One gap in an episode, written for an agent choosing what to call next."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="A stable code for this sort of gap.")
    severity: Severity = Field(description="blocker stops export and render; warning only warns.")
    area: Area
    message: str = Field(description="What is missing, in English.")
    fix_tool: str = Field(description="The MCP tool that resolves it.")
    fix_hint: str = Field(description="What to pass or check when calling that tool.")
    # The English template and its values, so pages translate the message; not part of the API.
    template: str = Field(default="", exclude=True)
    values: dict[str, str] = Field(default_factory=dict, exclude=True)


class ReadinessReport(BaseModel):
    """An episode's gaps, blockers first, and the areas the caller may not check."""

    model_config = ConfigDict(frozen=True)

    blockers: int
    warnings: int
    items: list[ReadinessItem]
    skipped_areas: list[Area] = Field(
        default_factory=list,
        description="Areas not checked because the token or role may not read them.",
    )


def item(kind: Kind, template: str, values: Mapping[str, object] | None = None) -> ReadinessItem:
    """The gap of this kind, its message filled from the template."""
    shown = {name: str(value) for name, value in (values or {}).items()}
    return ReadinessItem(
        code=kind.code,
        severity=kind.severity,
        area=kind.area,
        message=template % shown if shown else template,
        fix_tool=kind.fix.tool,
        fix_hint=kind.fix.hint,
        template=template,
        values=shown,
    )


def kind(code: str, area: Area, fix: Fix, severity: Severity = Severity.WARNING) -> Kind:
    """A sort of gap; warnings unless the code says it blocks export and render."""
    return Kind(code=code, severity=severity, area=area, fix=fix)


CLAIMS_UNCHECKED = kind(
    "claims_unchecked",
    Area.CLAIMS,
    Fix(
        tool="check_claim_source",
        hint="list_claims shows each claim's sources; call check_claim_source once a primary "
        "source was read, or update_claim to mark a source primary or add one.",
    ),
)
CLAIMS_MESSAGE = N_("Claims without a checked primary source: %(count)s.")

LIBRARY_FIX = Fix(
    tool="save_library_entry",
    hint="Add the name with save_library_entry (kind character or location), or fix the "
    "spelling in write_screenplay; list_library shows what the library holds.",
)
LIBRARY_CHARACTERS = kind("library_character_missing", Area.LIBRARY, LIBRARY_FIX)
LIBRARY_LOCATIONS = kind("library_location_missing", Area.LIBRARY, LIBRARY_FIX)
CHARACTERS_MESSAGE = N_("Characters with no library entry: %(names)s.")
LOCATIONS_MESSAGE = N_("Locations with no library entry: %(names)s.")

RIGHTS_ITEM_FIX = Fix(
    tool="update_external_media",
    hint="get_episode_credits names each item; call update_external_media with its rights, "
    "rights_note or attribution (list_external_media gives the revision).",
)
RIGHTS_USE_FIX = Fix(
    tool="update_external_media_use",
    hint="get_episode_credits gives each use's id; call update_external_media_use with the time "
    "range or pages the episode shows.",
)
RIGHTS_KINDS: dict[Problem, Kind] = {
    Problem.RIGHTS_UNKNOWN: kind("rights_unknown", Area.CREDITS, RIGHTS_ITEM_FIX),
    Problem.NO_REASON: kind("rights_no_reason", Area.CREDITS, RIGHTS_ITEM_FIX),
    Problem.NO_RANGE: kind("rights_no_range", Area.CREDITS, RIGHTS_USE_FIX),
    Problem.NO_CREDIT: kind("rights_no_credit", Area.CREDITS, RIGHTS_ITEM_FIX),
}
RIGHTS_MESSAGES: dict[Problem, str] = {
    Problem.RIGHTS_UNKNOWN: N_("Rights unknown for: %(titles)s."),
    Problem.NO_REASON: N_("Citation without a reason: %(titles)s."),
    Problem.NO_RANGE: N_("Citation without a time range: %(titles)s."),
    Problem.NO_CREDIT: N_("No credit line for: %(titles)s."),
}

WRITE_DELIVERY = Fix(
    tool="write_delivery",
    hint="get_delivery gives the package and its revision; change the cut and call "
    "write_delivery with that revision as expected_revision.",
)
RECORD_POST_TEXT = Fix(
    tool="record_post",
    hint="Give the post its own caption (with the channel's AI notice line) and at most 5 "
    "hashtags with record_post, or fix the cut's text with write_delivery.",
)
DELIVERY_KINDS: dict[DeliveryWarningKind, Kind] = {
    DeliveryWarningKind.NO_VIDEO: kind(
        "delivery_no_video",
        Area.DELIVERY,
        Fix(
            tool="write_delivery",
            hint="Give the cut a video_id from list_media (upload a master with "
            "create_upload_link as final_video) or an external_video, then call write_delivery.",
        ),
    ),
    DeliveryWarningKind.VIDEO_REMOVED: kind(
        "delivery_video_removed", Area.DELIVERY, WRITE_DELIVERY
    ),
    DeliveryWarningKind.NO_LINK: kind(
        "delivery_no_link",
        Area.DELIVERY,
        Fix(tool="record_post", hint="Call record_post with the post's url once it is live."),
    ),
    DeliveryWarningKind.TOO_LONG: kind("delivery_too_long", Area.DELIVERY, WRITE_DELIVERY),
    DeliveryWarningKind.TOO_MANY_HASHTAGS: kind(
        "delivery_too_many_hashtags", Area.DELIVERY, WRITE_DELIVERY
    ),
    DeliveryWarningKind.NO_AI_NOTICE: kind("delivery_no_ai_notice", Area.DELIVERY, WRITE_DELIVERY),
    DeliveryWarningKind.POST_TOO_MANY_HASHTAGS: kind(
        "delivery_post_too_many_hashtags", Area.DELIVERY, RECORD_POST_TEXT
    ),
    DeliveryWarningKind.POST_NO_AI_NOTICE: kind(
        "delivery_post_no_ai_notice", Area.DELIVERY, RECORD_POST_TEXT
    ),
}

REVIEW_FIX = Fix(
    tool="add_review",
    hint="Watch the render or final video, then call add_review with the verdict, notes and the "
    "time ranges seen; get_episode shows the latest review.",
)
REVIEW_MISSING = kind("review_missing", Area.REVIEWS, REVIEW_FIX)
REVIEW_NOT_APPROVED = kind("review_not_approved", Area.REVIEWS, REVIEW_FIX)
REVIEW_MISSING_MESSAGE = N_("The episode has no review yet.")
REVIEW_NOT_APPROVED_MESSAGE = N_("The latest review did not approve the episode.")

WRITE_SCRIPT = Fix(
    tool="write_short_script",
    hint="get_short_script gives the script and its revision; fix the blocks it names and call "
    "write_short_script with that revision as expected_revision.",
)
SCRIPT_KINDS: dict[WarningKind, Kind] = {
    found: kind(f"short_script_{found.value}", Area.SHORT_SCRIPT, WRITE_SCRIPT)
    for found in WarningKind
}

GENERATE_VOICE = Fix(
    tool="generate_voice_over",
    hint="Call estimate_voice_over to see the cost, then generate_voice_over; it spends "
    "ElevenLabs credits unless the same takes already exist.",
)
SRSTUDIO_SHEET = Fix(
    tool="save_library_sheet",
    hint="Call save_library_sheet for the entry (list_library gives its id) with the world, "
    "kind or rig srstudio needs.",
)


def blocker(code: str, fix: Fix) -> Kind:
    """A gap that stops the srstudio export and with it request_render."""
    return kind(code, Area.SRSTUDIO, fix, Severity.BLOCKER)


SRSTUDIO_KINDS: dict[str, Kind] = {
    NO_SCRIPT: blocker(
        "srstudio_no_script",
        Fix(
            tool="write_short_script",
            hint="Write at least one spoken block with write_short_script.",
        ),
    ),
    NO_TAKE: blocker("srstudio_no_take", GENERATE_VOICE),
    STALE_TAKE: blocker("srstudio_stale_take", GENERATE_VOICE),
    TAKE_GONE: blocker("srstudio_take_gone", GENERATE_VOICE),
    NO_WORLD: blocker("srstudio_no_world", SRSTUDIO_SHEET),
    NO_KIND: blocker("srstudio_no_kind", SRSTUDIO_SHEET),
    NOT_TOON: blocker("srstudio_not_toon", SRSTUDIO_SHEET),
    UNPUBLISHED: blocker("srstudio_unpublished_fact", SRSTUDIO_SHEET),
    NO_STYLE: blocker(
        "srstudio_no_style",
        Fix(
            tool="set_episode_style",
            hint="Pick a visual style with set_episode_style (list_library with kind style gives "
            "the ids); its srstudio style must be set.",
        ),
    ),
}
SRSTUDIO_OTHER = blocker(
    "srstudio_other",
    Fix(
        tool="check_srstudio_export",
        hint="Call check_srstudio_export: it lists each missing piece and the tool to fix it.",
    ),
)


def claim_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """Claims with no checked primary source (project.claims): one item, with the count."""
    count = facts.unchecked.count
    return [item(CLAIMS_UNCHECKED, CLAIMS_MESSAGE, {"count": count})] if count else []


def library_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """Screenplay names the library has no entry for (project.library.by-name)."""
    found = []
    for members, library_kind, message in (
        (facts.cast.characters, LIBRARY_CHARACTERS, CHARACTERS_MESSAGE),
        (facts.cast.locations, LIBRARY_LOCATIONS, LOCATIONS_MESSAGE),
    ):
        names = [member.name for member in members if member.entry is None]
        if names:
            found.append(item(library_kind, message, {"names": NAME_SEPARATOR.join(names)}))
    return found


def credit_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """Rights gaps of third-party media (project.external-media.use): one item per sort of gap."""
    found = []
    for problem, problem_kind in RIGHTS_KINDS.items():
        titles = sorted({warn.title for warn in facts.credits.warnings if warn.problem is problem})
        if titles:
            found.append(
                item(
                    problem_kind, RIGHTS_MESSAGES[problem], {"titles": NAME_SEPARATOR.join(titles)}
                )
            )
    return found


def delivery_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """Gaps in the delivery package (project.delivery), one item per warning."""
    return [
        item(DELIVERY_KINDS[found.kind], DELIVERY_MESSAGES[found.kind], found.values)
        for found in facts.delivery.warnings
    ]


def short_script_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """Weaknesses of the short script (project.short-script), one item per warning."""
    return [
        item(SCRIPT_KINDS[found.kind], SCRIPT_MESSAGES[found.kind], found.values)
        for found in facts.short_script.warnings
    ]


def export_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """What the srstudio export lacks, which is also what request_render refuses for.

    Before production these gaps are expected, so they only warn; from production on they block.
    """
    found = [
        item(SRSTUDIO_KINDS.get(missing.message, SRSTUDIO_OTHER), missing.message, missing.values)
        for missing in facts.export_gaps
    ]
    if facts.episode.episode.status in BLOCKING_STATUSES:
        return found
    return [gap.model_copy(update={"severity": Severity.WARNING}) for gap in found]


# Statuses from which a gap in the srstudio export holds the episode back.
BLOCKING_STATUSES = frozenset({EpisodeStatus.PRODUCTION, EpisodeStatus.PUBLISHED})


def review_items(facts: EpisodeFacts) -> list[ReadinessItem]:
    """From production on, an episode whose latest review is missing or not an approval."""
    if facts.episode.episode.status not in BLOCKING_STATUSES:
        return []
    latest = facts.reviews.latest
    if latest is None:
        return [item(REVIEW_MISSING, REVIEW_MISSING_MESSAGE)]
    if latest.verdict is not Verdict.APPROVED:
        return [item(REVIEW_NOT_APPROVED, REVIEW_NOT_APPROVED_MESSAGE)]
    return []


@dataclass(frozen=True)
class ReadinessSource:
    """One area's check: the function, the kinds it may report and the permission it needs."""

    area: Area
    check: Callable[[EpisodeFacts], list[ReadinessItem]]
    kinds: tuple[Kind, ...]
    permission: Permission = Permission.READ


# Adding an area is one more line here; its function wraps the module that computes the warnings.
READINESS_SOURCES: tuple[ReadinessSource, ...] = (
    ReadinessSource(Area.CLAIMS, claim_items, (CLAIMS_UNCHECKED,)),
    ReadinessSource(Area.CREDITS, credit_items, tuple(RIGHTS_KINDS.values())),
    ReadinessSource(Area.DELIVERY, delivery_items, tuple(DELIVERY_KINDS.values())),
    ReadinessSource(Area.SHORT_SCRIPT, short_script_items, tuple(SCRIPT_KINDS.values())),
    ReadinessSource(Area.LIBRARY, library_items, (LIBRARY_CHARACTERS, LIBRARY_LOCATIONS)),
    ReadinessSource(
        Area.SRSTUDIO,
        export_items,
        (*SRSTUDIO_KINDS.values(), SRSTUDIO_OTHER),
        Permission.WRITE_CHANNEL,
    ),
    ReadinessSource(Area.REVIEWS, review_items, (REVIEW_MISSING, REVIEW_NOT_APPROVED)),
)


def readiness_report(facts: EpisodeFacts) -> ReadinessReport:
    """Every gap the caller may see, blockers first, and the areas left unchecked."""
    found: list[ReadinessItem] = []
    skipped: list[Area] = []
    for source in READINESS_SOURCES:
        if facts.may(source.permission):
            found.extend(source.check(facts))
        else:
            skipped.append(source.area)
    ordered = sorted(found, key=lambda gap: gap.severity is not Severity.BLOCKER)
    blockers = sum(gap.severity is Severity.BLOCKER for gap in ordered)
    return ReadinessReport(
        blockers=blockers, warnings=len(ordered) - blockers, items=ordered, skipped_areas=skipped
    )
