"""Reading what scene-render prints and writes: its CLI envelope, its render report and its
diagnostics (rules project.render.jobs and project.render.checked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sreditor.engine_output import (
    EngineFailed,
    EngineOutputInvalid,
    adapters_from,
    encoded_from,
    findings_from,
    validation_from,
)
from sreditor.renders import FindingSeverity
from tests.engine_samples import (
    ADAPTER,
    DIAGNOSTIC,
    FINDING,
    encoded,
    envelope,
    failed,
    gpus_envelope,
    report,
    text,
    validated,
)


def test_adapters_from_gpus_envelope_lists_each() -> None:
    adapters = adapters_from(text(gpus_envelope()))
    assert [(adapter.name, adapter.is_software) for adapter in adapters] == [("RTX 3050", False)]


@pytest.mark.parametrize(
    "printed",
    [
        "not json",
        "[]",
        text({"result": {"adapters": [ADAPTER]}}),
        text(gpus_envelope() | {"format": "scene-render-cli/2"}),
        text(gpus_envelope() | {"schema_version": "2.0.0"}),
        text(gpus_envelope() | {"surprise": True}),
        text(envelope("gpus", {"adapters": [ADAPTER | {"name": None}]})),
        text(envelope("gpus", {"adapters": [ADAPTER | {"surprise": 1}]})),
        text(envelope("encode", {"adapters": [ADAPTER]})),
    ],
    ids=[
        "not-json",
        "not-an-object",
        "no-envelope",
        "other-format",
        "other-major-schema",
        "unknown-field",
        "adapter-without-name",
        "adapter-unknown-field",
        "other-command",
    ],
)
def test_adapters_from_malformed_output_raises(printed: str) -> None:
    with pytest.raises(EngineOutputInvalid):
        adapters_from(printed)


def test_adapters_from_failed_command_raises_with_its_message() -> None:
    with pytest.raises(EngineFailed, match="no GPU adapter"):
        adapters_from(text(failed("gpus", "io", "no GPU adapter found")))


def test_findings_from_report_keeps_every_field(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(text(report([FINDING])))
    (finding,) = findings_from(path)
    assert finding.severity is FindingSeverity.WARNING
    assert (finding.path, finding.node, finding.node_name) == (FINDING["path"], "title", "Title")
    assert (finding.time, finding.measured, finding.limit, finding.unit) == (
        (0.0, 2.5),
        2.1,
        4.5,
        "ratio",
    )


def test_findings_from_report_without_optional_fields_leaves_them_empty(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    bare = {"code": "A07", "severity": "info", "path": "/scene", "message": "inert"}
    path.write_text(text(report([bare])))
    (finding,) = findings_from(path)
    assert finding.node is None and finding.time is None and finding.unit is None


def test_findings_from_missing_report_raises(tmp_path: Path) -> None:
    with pytest.raises(EngineOutputInvalid, match="report"):
        findings_from(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "document",
    [
        {"findings": [FINDING]},
        report([FINDING]) | {"format": "scene-render-report/2"},
        report([FINDING | {"severity": "fatal"}]),
        report([FINDING | {"time": [1.0]}]),
        report([FINDING | {"surprise": 1}]),
        report([{"severity": "warning", "path": "/scene", "message": "no code"}]),
        report([FINDING]) | {"surprise": 1},
    ],
    ids=[
        "no-format",
        "other-format",
        "unknown-severity",
        "time-not-a-range",
        "finding-unknown-field",
        "finding-without-code",
        "report-unknown-field",
    ],
)
def test_findings_from_malformed_report_raises(tmp_path: Path, document: dict[str, object]) -> None:
    path = tmp_path / "report.json"
    path.write_text(text(document))
    with pytest.raises(EngineOutputInvalid):
        findings_from(path)


def test_findings_from_truncated_report_raises(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(text(report([FINDING]))[:40])
    with pytest.raises(EngineOutputInvalid):
        findings_from(path)


def test_validation_from_valid_scene_has_no_errors() -> None:
    validation = validation_from(text(validated([])))
    assert validation.is_valid and validation.errors() == []


def test_validation_from_invalid_scene_names_each_error() -> None:
    warning = DIAGNOSTIC | {"code": "W02", "severity": "warning", "message": "unused"}
    validation = validation_from(text(validated([DIAGNOSTIC, warning])))
    assert not validation.is_valid
    assert validation.errors() == [f"S01 at /bad: {DIAGNOSTIC['message']}"]


def test_validation_from_scene_with_information_lines_stays_valid() -> None:
    document = validated([])
    document["result"]["files"][0]["info"] = ["info: safe area audit sampled 40 of 672 frames"]
    validation = validation_from(text(document))
    assert validation.is_valid and validation.errors() == []


def test_validation_from_diagnostic_with_measure_reads_it() -> None:
    measured = DIAGNOSTIC | {"measured": {"value": 3.0, "limit": 2.0, "unit": "s"}}
    assert not validation_from(text(validated([measured]))).is_valid


@pytest.mark.parametrize(
    "document",
    [
        validated([DIAGNOSTIC | {"severity": "fatal"}]),
        validated([DIAGNOSTIC | {"loc": {"line": 1}}]),
        validated([DIAGNOSTIC | {"surprise": 1}]),
        envelope("validate", None),
        envelope("validate", {"files": [], "valid": True}),
    ],
    ids=["unknown-severity", "partial-location", "unknown-field", "no-result", "no-file"],
)
def test_validation_from_malformed_output_raises(document: dict[str, object]) -> None:
    with pytest.raises(EngineOutputInvalid):
        validation_from(text(document))


def test_validation_from_usage_error_raises_with_its_message() -> None:
    with pytest.raises(EngineFailed, match="cannot read"):
        validation_from(text(failed("validate", "io", "cannot read episode.scene.xml")))


def test_validation_from_unreadable_scene_raises_rather_than_calling_it_invalid() -> None:
    unreadable = validated([]) | {
        "error": {"codes": [], "kind": "io", "message": "cannot read episode.scene.xml"},
        "exit_code": 2,
        "ok": False,
    }
    unreadable["result"]["files"][0] |= {"io_error": "No such file", "valid": False}
    unreadable["result"]["valid"] = False
    with pytest.raises(EngineFailed, match="cannot read episode.scene.xml"):
        validation_from(text(unreadable))


def test_encoded_from_output_lists_what_was_not_rendered() -> None:
    printed = text(encoded("/work/video.mp4", ["fx: GLSL compile/link failed; passed through."]))
    (output,) = encoded_from(printed).outputs
    assert output.unsupported == ["fx: GLSL compile/link failed; passed through."]


@pytest.mark.parametrize(
    "document",
    [
        envelope("encode", {"outputs": []}),
        envelope("encode", {"outputs": [{"path": "/v.mp4"}]}),
        envelope("encode", {"outputs": [{"path": "/v.mp4", "unsupported": "one"}]}),
        envelope("encode", None),
    ],
    ids=["no-output", "no-unsupported-list", "unsupported-not-a-list", "no-result"],
)
def test_encoded_from_malformed_output_raises(document: dict[str, object]) -> None:
    with pytest.raises(EngineOutputInvalid):
        encoded_from(text(document))


def test_adapters_from_failure_without_error_raises_saying_so() -> None:
    with pytest.raises(EngineFailed, match="reported no result"):
        adapters_from(text(gpus_envelope() | {"ok": False}))
