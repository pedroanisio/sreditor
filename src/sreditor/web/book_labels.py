"""The names people read for books and their sections, apart from the code that writes books,
so the books page can show them without loading the PDF and Excel writers."""

from __future__ import annotations

from sreditor.book_operations import BookKind, BookSection
from sreditor.i18n import N_
from sreditor.srstudio_vocabulary import PairStatus

BOOK_LABELS: dict[BookKind, str] = {
    BookKind.PITCH: N_("Pitch book"),
    BookKind.EPISODE: N_("Episode book"),
    BookKind.LIBRARY: N_("Library book"),
    BookKind.CHANNEL: N_("Channel book"),
    BookKind.RIGHTS: N_("Rights and credits"),
}
SECTION_LABELS: dict[BookSection, str] = {
    BookSection.EDITORIAL: N_("Editorial"),
    BookSection.PROFILE: N_("Profile"),
    BookSection.PITCHES: N_("Pitches"),
    BookSection.EPISODES: N_("Episodes"),
    BookSection.CHARACTERS: N_("Characters"),
    BookSection.PLACES: N_("Places"),
    BookSection.STYLES: N_("Visual styles"),
    BookSection.PROPS: N_("Props"),
    BookSection.RELATIONSHIPS: N_("Relationships"),
    BookSection.THIRD_PARTY: N_("Third-party media"),
    BookSection.CREDITS: N_("Credits"),
}
PAIR_LABELS: dict[PairStatus, str] = {
    PairStatus.CANON: N_("Canon"),
    PairStatus.PROPOSAL: N_("Proposal"),
}
FONT_ROLE_LABELS: dict[str, str] = {
    "heading": N_("Heading"),
    "body": N_("Body"),
    "caption": N_("Caption"),
}
