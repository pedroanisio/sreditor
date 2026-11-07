"""The rough language guess that warns when a speech and its voice disagree."""

from __future__ import annotations

from sreditor.language_guess import guess_language, primary_subtag


def test_guess_language_english_text_is_en() -> None:
    assert guess_language("This is what the dog would do when the boss was there.") == "en"


def test_guess_language_portuguese_text_is_pt() -> None:
    assert guess_language("Você não sabe o que ele fez com a cadeira da sala.") == "pt"


def test_guess_language_short_text_is_unsure() -> None:
    assert guess_language("Oi.") is None


def test_guess_language_mixed_text_is_unsure() -> None:
    assert guess_language("the and of is o os as não") is None


def test_primary_subtag_of_region_tag_is_language() -> None:
    assert primary_subtag("pt-BR") == "pt"
