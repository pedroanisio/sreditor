from __future__ import annotations

from sreditor.voice_markup import (
    ProblemKind,
    SpeechPause,
    TtsModel,
    for_speech,
    leaks_in_v3,
    lost_in_v2,
    pause_seconds,
    plain_text,
    problems,
    timed_pauses,
)


def kinds(speech: str) -> list[ProblemKind]:
    return [found.kind for found in problems(speech)]


def test_problems_with_known_tags_finds_none() -> None:
    assert problems("Selling [pause:0.3] is a [emphasis]crime[/emphasis].") == []


def test_problems_with_unknown_tag_names_it() -> None:
    found = problems("A [shrug] here.")
    assert [item.kind for item in found] == [ProblemKind.UNKNOWN]
    assert found[0].message() == "[shrug] is not a voice tag; pick one from the list."


def test_problems_with_unclosed_span_reports_it() -> None:
    assert kinds("[irony]official term") == [ProblemKind.UNCLOSED]


def test_problems_with_closed_point_tag_reports_it() -> None:
    assert kinds("[laugh]ha[/laugh]") == [ProblemKind.POINT_CLOSED]


def test_problems_with_close_before_open_reports_it() -> None:
    assert kinds("word[/irony]") == [ProblemKind.CLOSED_UNOPENED]


def test_problems_with_pause_over_three_seconds_reports_it() -> None:
    assert kinds("[pause:4]") == [ProblemKind.SECONDS]


def test_problems_with_value_on_plain_tag_reports_it() -> None:
    assert kinds("[cut:now]") == [ProblemKind.VALUE_NOT_TAKEN]


def test_problems_without_required_value_reports_it() -> None:
    assert kinds("[pause]") == [ProblemKind.VALUE_MISSING]


def test_problems_with_unlisted_choice_reports_it() -> None:
    found = problems("[voice_sfx:siren]")
    assert found[0].kind == ProblemKind.VALUE_NOT_LISTED
    assert "gunshot, explosion, clapping" in found[0].message()


def test_problems_with_non_english_passthrough_reports_it() -> None:
    assert kinds("[tts:<b>]") == [ProblemKind.AUDIO_TAG]


def test_for_speech_in_v2_turns_pause_into_break_and_drops_voice_tags() -> None:
    spoken = for_speech("Wait [pause:0.5] for [irony]it[/irony].", TtsModel.V2)
    assert spoken == 'Wait <break time="0.5s" /> for it.'


def test_for_speech_in_v3_opens_audio_tag_and_capitalises_emphasis() -> None:
    spoken = for_speech("It is [irony]a deal[/irony], a [emphasis]crime[/emphasis].", TtsModel.V3)
    assert spoken == "It is [sarcastically] a deal, a CRIME."


def test_for_speech_drops_edit_cues_in_every_model() -> None:
    for model in TtsModel:
        assert for_speech("Look [cut] [sfx:siren] here.", model) == "Look here."


def test_for_speech_fills_value_into_audio_tag() -> None:
    assert for_speech("[accent:French]Bonjour[/accent]", TtsModel.V3) == "[French accent] Bonjour"


def test_plain_text_removes_every_tag() -> None:
    assert plain_text("Wait [pause:0.5] for [irony]it[/irony] .") == "Wait for it."


def test_pause_seconds_adds_pauses_and_silences() -> None:
    assert pause_seconds("[pause:0.5] a [silence:1] b [pause:9]") == 1.5


def test_lost_in_v2_lists_voice_tags_without_v2_form() -> None:
    assert lost_in_v2(["[irony]a[/irony] [pause:1] [cut]"]) == ["irony"]


def test_leaks_in_v3_flags_span_followed_by_plain_words() -> None:
    assert leaks_in_v3(["[irony]a[/irony] then normal words"]) == ["irony"]


def test_leaks_in_v3_with_next_tag_right_after_finds_none() -> None:
    assert leaks_in_v3(["[irony]a[/irony] [happy]b[/happy]", "[laugh]"]) == []


def test_for_speech_in_v3_leaves_timed_pauses_out() -> None:
    assert for_speech("Wait [pause:0.5] for it.", TtsModel.V3) == "Wait for it."


def test_timed_pauses_count_the_spoken_words_before_each() -> None:
    found = timed_pauses("Wait [pause:0.5] for [emphasis]it[/emphasis] [silence:1]")
    assert found == [
        SpeechPause(after_words=1, seconds=0.5),
        SpeechPause(after_words=3, seconds=1.0),
    ]
