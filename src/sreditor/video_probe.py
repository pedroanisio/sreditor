"""Measure a video's duration, resolution and loudness with ffprobe and ffmpeg (rule project.media).

A master made outside sreditor arrives as bytes only, so the app measures what the delivery rules
need when the file comes in. Without ffprobe, or on a file it cannot read, the result is None and
the file is kept unmeasured; a measurement never refuses an upload.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from sreditor.media import MediaMeasures

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
# A master is at most 95 MB, so a minute is far more than ffmpeg needs; beyond it something is stuck.
TIMEOUT_SECONDS = 60
# ffmpeg's ebur128 filter prints its summary on stderr, "I:  -16.2 LUFS"; silence prints -inf.
LOUDNESS_PATTERN = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?) LUFS")


def probe_command(path: Path) -> list[str]:
    """ffprobe reading the container's duration and its first video stream's size as JSON."""
    return [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        str(path),
    ]


def loudness_command(path: Path) -> list[str]:
    """ffmpeg measuring the integrated loudness of the file's audio and writing nothing."""
    return [
        FFMPEG,
        "-nostats",
        "-v",
        "info",
        "-i",
        str(path),
        "-vn",
        "-filter:a",
        "ebur128",
        "-f",
        "null",
        "-",
    ]


def run_tool(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a tool and return its finished process; None when it is missing, slow or fails."""
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done if done.returncode == 0 else None


def read_loudness(path: Path) -> float | None:
    """The integrated loudness in LUFS, or None for a file without audio or when ffmpeg fails."""
    done = run_tool(loudness_command(path))
    if done is None:
        return None
    found = LOUDNESS_PATTERN.findall(done.stderr)
    return float(found[-1]) if found else None


def probe_video(path: Path) -> MediaMeasures | None:
    """The video's measures, or None when ffprobe cannot read it."""
    done = run_tool(probe_command(path))
    if done is None:
        return None
    try:
        facts = json.loads(done.stdout)
        duration = float(facts["format"]["duration"])
        stream = (facts.get("streams") or [{}])[0]
        return MediaMeasures(
            duration_seconds=round(duration, 3),
            width=stream.get("width"),
            height=stream.get("height"),
            loudness_lufs=read_loudness(path),
        )
    except (KeyError, ValueError, TypeError):
        return None
