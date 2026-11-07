"""Matching titles for the quick search, ignoring case and accents."""

from __future__ import annotations

from sreditor.search import HitKind, SearchHit, folded, matches

EPISODE = SearchHit(kind=HitKind.EPISODE, channel="memes", key="12", title="Produção do Piloto")
PITCH = SearchHit(kind=HitKind.PITCH, channel="memes", key="12", title="Ideias")


def test_folded_text_drops_accents_and_case() -> None:
    assert folded("Produção ÁGIL") == "producao agil"


def test_matches_title_without_accents_is_true() -> None:
    assert matches("producao", EPISODE)


def test_matches_episode_number_with_hash_is_true() -> None:
    assert matches("#12", EPISODE)


def test_matches_number_of_pitch_is_false() -> None:
    assert not matches("12", PITCH)
