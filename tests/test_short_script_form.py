from __future__ import annotations

from sreditor.i18n import Translator
from sreditor.web.workspace.script_form import parse_script, seconds

ENGLISH = Translator("en")
PORTUGUESE = Translator("pt-BR")


def form(**fields: str) -> dict[str, str]:
    base = {
        "duration_s": "15",
        "model": "v3",
        "voice_id": "abc",
        "language": "pt-BR",
        "block_count": "2",
        "text_count": "1",
        "block-0-start_s": "0",
        "block-0-end_s": "3",
        "block-0-function": "hook",
        "block-0-speech": "Hook [pause:0.3] here.",
        "block-1-start_s": "3",
        "block-1-end_s": "15",
        "block-1-function": "punch",
        "block-1-speech": "",
        "text-0-text": "",
    }
    return {**base, **fields}


def test_parse_script_drops_rows_without_speech_or_text() -> None:
    parsed = parse_script(form(), ENGLISH)
    assert parsed.errors == {}
    assert parsed.script is not None and len(parsed.script.blocks) == 1
    assert parsed.script.screen_texts == []


def test_parse_script_reads_impact_and_screen_text() -> None:
    parsed = parse_script(
        form(
            **{
                "block-1-speech": "Punch.",
                "block-1-reaction": "laughter",
                "block-1-intensity": "4",
                "block-1-note": "They laugh",
                "text-0-text": "R$ 12 bi",
                "text-0-shown": "throughout",
            }
        ),
        ENGLISH,
    )
    assert parsed.script is not None
    impact = parsed.script.blocks[1].impact
    assert impact is not None and impact.intensity == 4
    assert parsed.script.screen_texts[0].is_persistent


def test_parse_script_accepts_comma_decimals() -> None:
    parsed = parse_script(form(**{"block-0-end_s": "2,5"}), ENGLISH)
    assert parsed.script is not None and parsed.script.blocks[0].end_s == 2.5


def test_parse_script_with_bad_tag_shows_translated_error_on_speech() -> None:
    parsed = parse_script(form(**{"block-0-speech": "A [shrug]."}), PORTUGUESE)
    assert parsed.script is None
    assert "[shrug]" in parsed.errors["block-0-speech"]
    assert parsed.errors["block-0-speech"] != "[shrug] is not a voice tag; pick one from the list."


def test_parse_script_with_text_for_seconds_shows_error_on_field() -> None:
    parsed = parse_script(form(**{"block-0-start_s": "soon"}), ENGLISH)
    assert parsed.errors == {"block-0-start_s": "Enter a number of seconds, such as 2.5."}


def test_parse_script_with_block_ending_first_shows_error_on_end() -> None:
    parsed = parse_script(form(**{"block-0-end_s": "0"}), ENGLISH)
    assert parsed.errors == {"block-0-end_s": "A block must end after it starts."}


def test_parse_script_with_reaction_but_no_intensity_asks_for_it() -> None:
    parsed = parse_script(form(**{"block-0-reaction": "surprise"}), ENGLISH)
    assert parsed.errors == {"block-0-intensity": "Pick how strong the reaction is."}


def test_parse_script_with_missing_start_shows_error_on_start() -> None:
    parsed = parse_script(form(**{"block-0-start_s": ""}), ENGLISH)
    assert set(parsed.errors) == {"block-0-start_s"}


def test_parse_script_with_bad_function_shows_error_on_function() -> None:
    parsed = parse_script(form(**{"block-0-function": "climax"}), ENGLISH)
    assert set(parsed.errors) == {"block-0-function"}


def test_parse_script_with_bad_length_shows_error_on_length() -> None:
    assert set(parse_script(form(duration_s="long"), ENGLISH).errors) == {"duration_s"}


def test_parse_script_with_zero_length_shows_error_on_length() -> None:
    assert set(parse_script(form(duration_s="0"), ENGLISH).errors) == {"duration_s"}


def test_parse_script_with_bad_language_shows_error_on_language() -> None:
    assert set(parse_script(form(language="Portuguese"), ENGLISH).errors) == {"language"}


def test_parse_script_with_screen_text_ending_first_shows_error() -> None:
    fields = {"text-0-text": "Hi", "text-0-start_s": "5", "text-0-end_s": "2"}
    assert set(parse_script(form(**fields), ENGLISH).errors) == {"text-0-end_s"}


def test_parse_script_with_bad_screen_text_time_shows_error() -> None:
    fields = {"text-0-text": "Hi", "text-0-start_s": "x"}
    assert set(parse_script(form(**fields), ENGLISH).errors) == {"text-0-start_s"}


def test_seconds_of_blank_is_none() -> None:
    assert seconds("  ") is None
