"""The worker's encode against a real scene-render, read back in pixels (rule project.render).

The other worker tests use a fake engine, which proves the job's flow but never the picture. These
run only where SREDITOR_TEST_SCENE_RENDER names an engine (or scene-render is on PATH) and ffmpeg
is installed, and skip elsewhere, such as in CI, which has no engine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from sreditor.renders import RenderPreset
from sreditor.settings import DEFAULT_RENDER_BUILDER_VERSION
from sreditor.worker_engine import EncodeJob, Engine, NotRendered, WorkerSettings

ENGINE = os.environ.get("SREDITOR_TEST_SCENE_RENDER") or shutil.which("scene-render")
FFMPEG = shutil.which("ffmpeg")
# Hardware encoders refuse frames much smaller than this.
WIDTH, HEIGHT = 256, 144
RED = (255, 0, 0)
TOLERANCE = 12  # what h264 may move a flat colour by
SCENE = """<scene version="1.1">
<project width="{width}" height="{height}" fps="2" duration="1" background="#202020"/>
<output id="main" path="unused.mp4" codec="h264" audio="false"/>
<composition><shape id="s" shape="rect" width="{width}" height="{height}" fill="#FF0000"{effects}/>
</composition>{definitions}</scene>
"""
BROKEN_SHADER = '<effects><effect id="fx" type="shader" src="broken.glsl"/></effects>'

pytestmark = pytest.mark.skipif(
    not (ENGINE and FFMPEG), reason="needs scene-render (SREDITOR_TEST_SCENE_RENDER) and ffmpeg"
)


def engine(tmp_path: Path) -> Engine:
    """An engine whose scene-render is the real one."""
    return Engine(
        WorkerSettings(
            server_url="https://unused",
            token=SecretStr("srw_unused"),
            build_command="unused",
            scene_render=str(ENGINE),
            gpu_lock=tmp_path / "gpu.lock",
            builder_version=DEFAULT_RENDER_BUILDER_VERSION,
        )
    )


def encode_job(tmp_path: Path, is_shader_broken: bool = False) -> EncodeJob:
    """A one-second red scene, its shader broken on request, to encode in `tmp_path`."""
    scene = tmp_path / "episode.scene.xml"
    scene.write_text(
        SCENE.format(
            width=WIDTH,
            height=HEIGHT,
            effects=' effects="fx"' if is_shader_broken else "",
            definitions=BROKEN_SHADER if is_shader_broken else "",
        )
    )
    (tmp_path / "broken.glsl").write_text("void main() { this is not glsl }\n")
    return EncodeJob(
        scene=scene,
        video=tmp_path / "video.mp4",
        report=tmp_path / "report.json",
        preset=RenderPreset.PREVIEW,
    )


def middle_pixel(video: Path) -> tuple[int, ...]:
    """The colour at the centre of the video's first frame, decoded by ffmpeg."""
    frame = subprocess.run(
        [str(FFMPEG), "-v", "error", "-i", str(video), "-frames:v", "1"]
        + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
    ).stdout
    start = ((HEIGHT // 2) * WIDTH + WIDTH // 2) * 3
    return tuple(frame[start : start + 3])


def test_engine_encode_real_scene_draws_its_colour(tmp_path: Path) -> None:
    job = encode_job(tmp_path)
    engine(tmp_path).encode(job, threading.Event())
    pixel = middle_pixel(job.video)
    assert all(abs(got - wanted) <= TOLERANCE for got, wanted in zip(pixel, RED, strict=True))


def test_engine_encode_shader_that_fails_raises_not_rendered(tmp_path: Path) -> None:
    job = encode_job(tmp_path, is_shader_broken=True)
    with pytest.raises(NotRendered, match="GLSL compile/link failed"):
        engine(tmp_path).encode(job, threading.Event())
