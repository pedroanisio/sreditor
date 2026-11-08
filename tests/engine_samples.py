"""What scene-render 0.9.0 prints, captured from the real engine, for tests of the worker."""

from __future__ import annotations

import json
from typing import Any

ADAPTER: dict[str, Any] = {
    "backend": "Vulkan",
    "chosen": True,
    "device_type": "DiscreteGpu",
    "driver": "NVIDIA",
    "name": "RTX 3050",
    "ray_query": True,
    "software": False,
}

FINDING: dict[str, Any] = {
    "code": "LEG-CONTRAST",
    "severity": "warning",
    "path": "/scene/composition/text[1]",
    "node": "title",
    "nodeName": "Title",
    "time": [0.0, 2.5],
    "measured": 2.1,
    "limit": 4.5,
    "unit": "ratio",
    "message": "text contrast 2.1:1 is below 4.5:1",
}

DIAGNOSTIC: dict[str, Any] = {
    "code": "S01",
    "help": 'wrap the document in <scene version="1.1"> … </scene>',
    "loc": {"column": 1, "line": 1, "offset": 0},
    "message": "root element is <bad>; a scene-render document starts with <scene>",
    "path": "/bad",
    "severity": "error",
}


def envelope(command: str, result: Any, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """A scene-render-cli/1 envelope as the engine prints it."""
    return {
        "command": command,
        "diagnostics": [],
        "error": error,
        "exit_code": 0 if error is None else 1,
        "format": "scene-render-cli/1",
        "ok": error is None,
        "result": result,
        "schema_version": "1.4.0",
    }


def gpus_envelope() -> dict[str, Any]:
    """`scene-render gpus --json` on a machine with one GPU."""
    return envelope("gpus", {"adapters": [ADAPTER]})


def validated(diagnostics: list[dict[str, Any]], file: str = "episode.scene.xml") -> dict[str, Any]:
    """`scene-render validate --format json` for one file with these diagnostics."""
    errors = sum(1 for diagnostic in diagnostics if diagnostic["severity"] == "error")
    warnings = sum(1 for diagnostic in diagnostics if diagnostic["severity"] == "warning")
    result = {
        "files": [
            {
                "diagnostics": diagnostics,
                "errors": errors,
                "file": file,
                "valid": errors == 0,
                "warnings": warnings,
            }
        ],
        "valid": errors == 0,
    }
    error = (
        {
            "codes": [d["code"] for d in diagnostics],
            "kind": "invalid",
            "message": "1 of 1 file(s) invalid",
        }
        if errors
        else None
    )
    return envelope("validate", result, error)


def encoded(path: str, unsupported: list[str] | None = None) -> dict[str, Any]:
    """`scene-render encode --json` for one output, with the fields the worker reads and others."""
    output = {
        "encoder": "h264_nvenc",
        "files": [path],
        "findings": [],
        "frames": 60,
        "path": path,
        "seconds": 0.95,
        "stage_seconds": [0.01, 0.12, 0.02, 0.002],
        "unsupported": unsupported or [],
        "warnings": [],
    }
    return envelope("encode", {"outputs": [output]})


def failed(command: str, kind: str, message: str) -> dict[str, Any]:
    """An envelope for a command that failed with no result."""
    return envelope(command, None, {"codes": [], "kind": kind, "message": message}) | {
        "exit_code": 2 if kind in ("usage", "io", "internal") else 1
    }


def report(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """A scene-render-report/1 file."""
    return {
        "format": "scene-render-report/1",
        "engine": {"name": "rs-scene-render", "version": "0.9.0"},
        "scene": {"sha256": "28" * 32, "version": "1.1"},
        "output": {"id": "main", "width": 1920, "height": 1080, "start": 0.0, "end": 2.0},
        "findings": findings,
    }


def text(document: dict[str, Any]) -> str:
    """The document as the engine writes it."""
    return json.dumps(document)
