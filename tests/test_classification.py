"""Classification rules (project.classification, ...checks) and the observed profile (...profile)."""

from __future__ import annotations

from typing import Any

import pytest

from sreditor.classification import (
    VOCABULARY,
    Classification,
    ClassificationRefused,
    Dimension,
    EpisodeClassification,
    Method,
    State,
    check,
    observed_profile,
    refusals_of,
)


def present(*values: str, confidence: float | None = None) -> dict[str, Any]:
    """A present dimension: the first value primary, the rest secondary."""
    items = [
        {"value": value, "role": "primary" if index == 0 else "secondary", "confidence": confidence}
        for index, value in enumerate(values)
    ]
    return {"state": "present", "items": items}


def classified(method: Method = Method.HUMAN, **dimensions: Any) -> Classification:
    """A classification from dimension names to dimension documents."""
    return Classification.model_validate({"dimensions": dimensions, "method": method})


def fields(data: Classification) -> list[str]:
    """The fields a classification is refused for."""
    return [found.field for found in refusals_of(data)]


def episode(number: int, data: Classification, is_reviewed: bool = True) -> EpisodeClassification:
    """A classified episode."""
    return EpisodeClassification(
        number=number, classification=data, is_reviewed=is_reviewed, revision=1
    )


def test_vocabulary_every_dimension_has_values() -> None:
    assert set(VOCABULARY) == set(Dimension)
    assert all(VOCABULARY.values())


def test_classification_left_out_dimension_is_unknown() -> None:
    assert Classification().of(Dimension.GENRES).state is State.UNKNOWN


def test_classification_values_primary_first() -> None:
    data = Classification.model_validate(
        {
            "dimensions": {
                "tones": {
                    "state": "present",
                    "items": [
                        {"value": "ironic", "role": "secondary"},
                        {"value": "serious", "role": "primary"},
                    ],
                }
            }
        }
    )
    assert data.values(Dimension.TONES) == ["serious", "ironic"]
    assert data.primary(Dimension.TONES) == "serious"


def test_classification_valid_passes_check() -> None:
    check(classified(genres=present("educational", "documentary"), tones=present("warm")))


def test_dimension_absent_with_values_refused() -> None:
    data = classified(tones={"state": "absent", "items": [{"value": "warm"}]})
    assert fields(data) == ["dimensions.tones"]


def test_dimension_present_without_values_refused() -> None:
    assert fields(classified(tones={"state": "present", "items": []})) == ["dimensions.tones"]


def test_dimension_two_primary_values_refused() -> None:
    data = classified(
        tones={"state": "present", "items": [{"value": "warm"}, {"value": "serious"}]}
    )
    assert "exactly one primary" in refusals_of(data)[0].message


def test_dimension_three_secondary_values_refused() -> None:
    with pytest.raises(ValueError, match="at most 3 items"):
        classified(tones=present("warm", "serious", "ironic", "playful"))


def test_dimension_duplicate_value_refused() -> None:
    data = classified(tones=present("warm", "warm"))
    assert "appears twice" in refusals_of(data)[0].message


def test_dimension_unknown_value_refused_listing_allowed() -> None:
    message = refusals_of(classified(tones=present("grumpy")))[0].message
    assert "Unknown value grumpy" in message
    assert "serious" in message


def test_dimension_ai_without_confidence_refused() -> None:
    assert fields(classified(Method.AI, tones=present("warm"))) == ["dimensions.tones"]


def test_dimension_ai_with_confidence_passes() -> None:
    check(classified(Method.AI, tones=present("warm", confidence=0.8)))


def test_comedy_without_humor_device_refused() -> None:
    assert fields(classified(genres=present("comedy"))) == ["dimensions.humor_devices"]


def test_comedy_with_humor_device_passes() -> None:
    check(classified(genres=present("comedy"), humor_devices=present("deadpan")))


def test_satirical_without_satire_refused() -> None:
    assert fields(classified(approaches=present("satirical"))) == ["satire"]


def test_satire_without_satirical_refused() -> None:
    data = Classification.model_validate(
        {"satire": {"target": "Banks", "criticized_behavior": "Hidden fees"}}
    )
    assert fields(data) == ["satire"]


def test_parodic_without_references_refused() -> None:
    assert fields(classified(approaches=present("parodic"))) == ["parody_references"]


def test_references_without_parodic_refused() -> None:
    assert fields(Classification(parody_references=["A famous ad"])) == ["parody_references"]


def test_check_raises_with_every_refusal() -> None:
    with pytest.raises(ClassificationRefused) as refused:
        check(classified(genres=present("comedy"), approaches=present("parodic")))
    assert [found.field for found in refused.value.refusals] == [
        "dimensions.humor_devices",
        "parody_references",
    ]


def test_observed_profile_counts_primary_and_share() -> None:
    episodes = [
        episode(1, classified(tones=present("ironic"))),
        episode(2, classified(tones=present("serious", "ironic"))),
        episode(3, Classification(), is_reviewed=False),
    ]
    observed = observed_profile(Classification(), episodes)
    ironic = next(found for found in observed.observations if found.value == "ironic")
    assert (observed.sample_size, observed.unreviewed) == (3, 1)
    assert (ironic.primary_count, ironic.episode_count) == (1, 2)
    assert ironic.prevalence == pytest.approx(2 / 3)


def test_observed_profile_declared_primary_below_half_drifts() -> None:
    declared = classified(tones=present("serious"))
    episodes = [
        episode(1, classified(tones=present("ironic"))),
        episode(2, classified(tones=present("ironic", "serious"))),
        episode(3, classified(tones=present("ironic"))),
    ]
    drift = observed_profile(declared, episodes).drift
    assert [(found.value, round(found.prevalence, 2)) for found in drift] == [("serious", 0.33)]


def test_observed_profile_declared_primary_held_by_half_no_drift() -> None:
    declared = classified(tones=present("serious"))
    episodes = [episode(1, classified(tones=present("serious"))), episode(2, Classification())]
    assert observed_profile(declared, episodes).drift == []


def test_observed_profile_no_episodes_no_drift() -> None:
    observed = observed_profile(classified(tones=present("serious")), [])
    assert (observed.sample_size, observed.drift) == (0, [])
