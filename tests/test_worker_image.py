"""The worker image pins the engine and srstudio it renders with (rules project.render and
project.render.workers): an image tag can be pushed again and a branch moves, so the image names
the engine's digest and srstudio's commit, and both agree with what the instance pins."""

from __future__ import annotations

import re
from pathlib import Path

from sreditor.settings import DEFAULT_RENDER_BUILDER_VERSION, DEFAULT_RENDER_ENGINE_VERSION

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile.worker"
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def argument(name: str) -> str:
    """The default of one ARG in the worker's Dockerfile."""
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    assert match, f"ARG {name} has no default"
    return match.group(1)


def test_worker_image_engine_is_pinned_by_digest() -> None:
    assert DIGEST.fullmatch(argument("SCENE_RENDER_DIGEST"))
    assert re.search(
        r"^FROM pedroanisio/scene-render:\$\{SCENE_RENDER_VERSION\}@\$\{SCENE_RENDER_DIGEST\}$",
        DOCKERFILE.read_text(),
        re.MULTILINE,
    )


def test_worker_image_engine_version_matches_instance_default() -> None:
    assert argument("SCENE_RENDER_VERSION") == DEFAULT_RENDER_ENGINE_VERSION


def test_worker_image_srstudio_is_pinned_to_instance_builder_commit() -> None:
    reference = argument("SRSTUDIO_REF")
    assert COMMIT.fullmatch(reference) and reference == DEFAULT_RENDER_BUILDER_VERSION


def test_worker_image_tells_worker_its_builder_version() -> None:
    assert "SREDITOR_WORKER_BUILDER_VERSION=${SRSTUDIO_REF}" in DOCKERFILE.read_text()
