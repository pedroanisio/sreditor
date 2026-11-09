"""What scene-render prints and writes, parsed into typed models (rules project.render,
project.render.jobs and project.render.checked).

Every --json command prints one scene-render-cli/1 envelope; encode --report writes one
scene-render-report/1 file. Output that is not in those formats, or a schema major other than the
one read here, raises EngineOutputInvalid instead of passing for "nothing to report": a worker
whose engine changed shape stops delivering rather than delivering unchecked videos.

The models reject unknown fields, so a new engine whose output grew is caught by
tests/test_render_engine.py before it is pinned. The exception is encode's output, which carries
dozens of measurements the worker never reads; only the ones it reads are declared there.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from sreditor.renders import FindingSeverity, ReportFinding

CLI_FORMAT = "scene-render-cli/1"
REPORT_FORMAT = "scene-render-report/1"
SCHEMA_MAJOR = "1"


INVALID_KIND = "invalid"  # the error kind of a document that does not validate


class EngineOutputInvalid(ValueError):
    """scene-render printed or wrote something that is not in the format the worker reads."""


class EngineFailed(RuntimeError):
    """scene-render ran and reported, in its own format, that the command failed."""


class Exact(BaseModel):
    """A part of scene-render's output whose every field is known."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CliError(Exact):
    """Why a command failed: its kind (usage, io, invalid, failed, internal) and codes."""

    kind: str
    codes: list[str]
    message: str


class Location(Exact):
    """Where in the document a diagnostic points."""

    line: int
    column: int
    offset: int


class Measure(Exact):
    """The quantity a check measured, against its limit."""

    value: float
    limit: float | None = None
    unit: str


class Diagnostic(Exact):
    """One diagnostic of validation: what is wrong and where."""

    code: str
    severity: FindingSeverity
    message: str
    loc: Location
    path: str
    file: str | None = None
    help: str | None = None
    label: str | None = None
    measured: Measure | None = None

    def described(self) -> str:
        """The diagnostic in one line for a job's failure."""
        return f"{self.code} at {self.path}: {self.message}"


class Envelope(Exact):
    """The one JSON document every --json command prints (scene-render-cli/1)."""

    command: str
    diagnostics: list[Diagnostic]
    error: CliError | None
    exit_code: int
    format: str
    ok: bool
    result: JsonValue
    schema_version: str

    @field_validator("format")
    @classmethod
    def is_cli_format(cls, value: str) -> str:
        """Only scene-render-cli/1 is read."""
        if value != CLI_FORMAT:
            raise ValueError(f"format {value!r} is not {CLI_FORMAT}")
        return value

    @field_validator("schema_version")
    @classmethod
    def is_known_major(cls, value: str) -> str:
        """Within a major version the engine only adds; another major may change what fields mean."""
        if value.split(".")[0] != SCHEMA_MAJOR:
            raise ValueError(f"schema version {value} is not {SCHEMA_MAJOR}.x")
        return value

    def failure(self) -> str:
        """The command's own account of why it failed."""
        if self.error is None:
            return f"scene-render {self.command} reported no result"
        return f"scene-render {self.command} failed ({self.error.kind}): {self.error.message}"


class Adapter(Exact):
    """A GPU or software adapter as `scene-render gpus --json` lists it."""

    name: str
    backend: str
    device_type: str
    driver: str
    chosen: bool
    ray_query: bool
    is_software: bool = Field(alias="software")


class Adapters(Exact):
    """The result of `scene-render gpus --json`."""

    adapters: list[Adapter]


class ValidatedFile(Exact):
    """One document's validation."""

    file: str
    valid: bool
    errors: int
    warnings: int
    diagnostics: list[Diagnostic]
    io_error: str | None = None
    # Information lines (how the safe area was sampled, nodes forced outside it); they never stop a
    # render, and scene-render leaves the field out when there are none.
    info: list[str] = []


class Validation(Exact):
    """The result of `scene-render validate --format json` for one scene."""

    files: list[ValidatedFile] = Field(min_length=1, max_length=1)
    is_valid: bool = Field(alias="valid")

    def errors(self) -> list[str]:
        """Each error diagnostic in one line; warnings and information do not stop a render."""
        return [
            diagnostic.described()
            for diagnostic in self.files[0].diagnostics
            if diagnostic.severity is FindingSeverity.ERROR
        ]


class EncodedOutput(BaseModel):
    """One output of `scene-render encode --json`; only the fields the worker reads."""

    model_config = ConfigDict(frozen=True)

    path: str
    # What the engine passed through instead of drawing, such as a shader that failed to compile.
    unsupported: list[str]


class Encoded(Exact):
    """The result of `scene-render encode --json`."""

    outputs: list[EncodedOutput] = Field(min_length=1)


class ReportEngine(Exact):
    """The engine that wrote a report."""

    name: str
    version: str


class ReportScene(Exact):
    """The document a report is about."""

    sha256: str
    version: str | None = None


class ReportOutput(Exact):
    """The output as rendered."""

    id: str | None = None
    width: int
    height: int
    start: float
    end: float


class AssetCredit(Exact):
    """An asset's declared licence or credit."""

    id: str
    kind: str
    license: str | None = None
    credit: str | None = None


class SourceTimecodes(Exact):
    """The source range of a video layer whose asset declares a timecode origin."""

    layer: str
    asset: str
    fps: str
    source_in: str = Field(alias="in")
    source_out: str = Field(alias="out")


class EngineFinding(Exact):
    """One finding as the report writes it."""

    code: str
    severity: FindingSeverity
    path: str
    node: str | None = None
    node_name: str | None = Field(default=None, alias="nodeName")
    time: tuple[float, float] | None = None
    measured: float | None = None
    limit: float | None = None
    unit: str | None = None
    message: str


class RenderReport(Exact):
    """A scene-render-report/1 file."""

    format: str
    engine: ReportEngine
    scene: ReportScene
    output: ReportOutput
    findings: list[EngineFinding]
    assets: list[AssetCredit] = Field(default_factory=list)
    sources: list[SourceTimecodes] = Field(default_factory=list)

    @field_validator("format")
    @classmethod
    def is_report_format(cls, value: str) -> str:
        """Only scene-render-report/1 is read."""
        if value != REPORT_FORMAT:
            raise ValueError(f"format {value!r} is not {REPORT_FORMAT}")
        return value


def envelope_of(printed: str, command: str) -> Envelope:
    """The envelope `command` printed; EngineOutputInvalid when it printed none or another's."""
    try:
        envelope = Envelope.model_validate_json(printed)
    except ValidationError as invalid:
        raise EngineOutputInvalid(
            f"scene-render {command} printed no {CLI_FORMAT} envelope: {invalid}"
        ) from invalid
    if envelope.command != command:
        raise EngineOutputInvalid(f"scene-render {command} printed {envelope.command}'s envelope")
    return envelope


def result_of(envelope: Envelope) -> JsonValue:
    """The envelope's result; EngineOutputInvalid when a command that gave a verdict gave none."""
    if envelope.result is None:
        raise EngineOutputInvalid(f"scene-render {envelope.command} printed no result")
    return envelope.result


def succeeded(printed: str, command: str) -> JsonValue:
    """The result of a command that must succeed; EngineFailed with its error otherwise."""
    envelope = envelope_of(printed, command)
    if not envelope.ok:
        raise EngineFailed(envelope.failure())
    return result_of(envelope)


def adapters_from(printed: str) -> list[Adapter]:
    """The adapters in what `scene-render gpus --json` printed."""
    result = succeeded(printed, "gpus")
    try:
        return Adapters.model_validate(result).adapters
    except ValidationError as invalid:
        raise EngineOutputInvalid(f"scene-render gpus listed no adapters: {invalid}") from invalid


def validation_from(printed: str) -> Validation:
    """The validation in what `scene-render validate --format json` printed for one scene.

    An invalid scene is a result, not a failure: the envelope is not ok but carries the files.
    Any other error, such as a scene it could not read, is EngineFailed and says nothing of the
    scene."""
    envelope = envelope_of(printed, "validate")
    is_verdict = envelope.ok or (envelope.error is not None and envelope.error.kind == INVALID_KIND)
    if not is_verdict:
        raise EngineFailed(envelope.failure())
    try:
        return Validation.model_validate(result_of(envelope))
    except ValidationError as invalid:
        raise EngineOutputInvalid(
            f"scene-render validate gave no validation: {invalid}"
        ) from invalid


def encoded_from(printed: str) -> Encoded:
    """The outputs in what `scene-render encode --json` printed."""
    result = succeeded(printed, "encode")
    try:
        return Encoded.model_validate(result)
    except ValidationError as invalid:
        raise EngineOutputInvalid(f"scene-render encode gave no outputs: {invalid}") from invalid


def report_finding(finding: EngineFinding) -> ReportFinding:
    """A finding of the report as a job keeps it."""
    return ReportFinding.model_validate(finding.model_dump())


def findings_from(report: Path) -> list[ReportFinding]:
    """The findings of the scene-render-report/1 file at `report`."""
    try:
        written = report.read_text(encoding="utf-8")
        return [
            report_finding(finding)
            for finding in RenderReport.model_validate_json(written).findings
        ]
    except (OSError, ValidationError) as unreadable:
        raise EngineOutputInvalid(
            f"scene-render wrote no readable {REPORT_FORMAT} report at {report.name}: {unreadable}"
        ) from unreadable
