"""Short script routes: an episode's script for vertical video and its generated voice-over."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sreditor import short_script_operations as scripts
from sreditor.deps import Access, ServicesDep
from sreditor.elevenlabs import ElevenLabsError, Voice
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import etag
from sreditor.idempotency import IdempotencyDep
from sreditor.problems import problem_response, problem_type
from sreditor.short_script import ShortScript, ShortScriptInput
from sreditor.voice_generation import KEY_LENGTH, Take, TakeNotFound

router = APIRouter(tags=["short script"])

EPISODE = "/channels/{slug}/episodes/{number}"
SHORT_SCRIPT = f"{EPISODE}/short-script"


class TimingsChoice(BaseModel):
    """Which take's measured times to use."""

    model_config = ConfigDict(frozen=True)

    take_key: str = Field(
        min_length=KEY_LENGTH,
        max_length=KEY_LENGTH,
        description="The `key` of a take from generate_voice_over.",
    )


def tagged(script: ShortScript, response: Response) -> ShortScript:
    """The script, with its revision as the ETag."""
    response.headers["ETag"] = etag(script.revision)
    return script


@router.get(SHORT_SCRIPT, summary="Read the short script")
def get_short_script(
    number: Number, access: Access, app: ServicesDep, response: Response
) -> ShortScript:
    """Read an episode's short script: length, voice, timed blocks and on-screen texts.

    `warnings` lists what weakens it without blocking it, such as a gap between blocks or too
    many words for the time. Revision 0 (ETag "0") means nobody wrote it yet.
    """
    return tagged(scripts.get_short_script(app, access, number), response)


@router.put(SHORT_SCRIPT, summary="Write the short script")
def write_short_script(
    target: WriteTarget,
    data: ShortScriptInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> ShortScript:
    """Replace an episode's whole short script. Needs content:write.

    Speech takes voice tags such as [pause:0.3] or [irony]…[/irony]; an unknown tag, a wrong
    value or an unclosed span is refused with 422. Send If-Match with the ETag you read ("0"
    when it is empty) so a change you have not seen is never overwritten.
    """
    written = scripts.write_short_script(app, access, target.number, data, target.expected_revision)
    return tagged(written, response)


@router.get("/channels/{slug}/voices", summary="List voices")
def list_voices(access: Access, app: ServicesDep) -> list[Voice]:
    """List the ElevenLabs voices a short script can name in voice.voice_id.

    Needs an owner or editor and content:write; answers 503 when generation is off.
    """
    return scripts.list_voices(app, access)


@router.post(
    f"{EPISODE}/voice-over-takes",
    status_code=status.HTTP_201_CREATED,
    response_model=Take,
    summary="Generate a voice-over",
)
def generate_voice_over(
    number: Number, access: Access, app: ServicesDep, idempotency: IdempotencyDep
) -> JSONResponse:
    """Speak the short script with its chosen ElevenLabs voice and add it as a voice_over file.

    Needs an owner or editor and content:write: it spends the account's credits. The same
    speech, voice, model and language returns the earlier take (is_reused true) for free.
    `suggested` holds each block's measured times, to use with apply_take_timings.
    """
    return idempotency.run(
        lambda: scripts.generate_voice_over(app, access, number), status.HTTP_201_CREATED
    )


@router.post(f"{SHORT_SCRIPT}/timings", summary="Use a take's timings")
def apply_take_timings(
    target: WriteTarget,
    choice: TimingsChoice,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> ShortScript:
    """Replace the planned block times with those a take measured in its audio.

    Needs content:write. The script must still have as many blocks as when the take was made.
    Send If-Match with the script's ETag.
    """
    timed = scripts.apply_take_timings(
        app, access, target.number, choice.take_key, target.expected_revision
    )
    return tagged(timed, response)


def install_short_script_error_handlers(app: FastAPI) -> None:
    """Errors of short scripts and voice generation, as RFC 9457 problems."""

    @app.exception_handler(scripts.VoiceUnavailable)
    async def voice_unavailable(request: Request, off: scripts.VoiceUnavailable) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(off),
            problem_type("voice-generation-off"),
        )

    @app.exception_handler(scripts.ScriptNotReady)
    async def not_ready(request: Request, missing: scripts.ScriptNotReady) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(missing),
            problem_type("short-script-not-ready"),
        )

    @app.exception_handler(scripts.TimingsDoNotFit)
    async def do_not_fit(request: Request, refused: scripts.TimingsDoNotFit) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(refused), problem_type("timings-do-not-fit")
        )

    @app.exception_handler(TakeNotFound)
    async def take_not_found(request: Request, missing: TakeNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Take {missing} not found")

    @app.exception_handler(ElevenLabsError)
    async def provider_failed(request: Request, failure: ElevenLabsError) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_502_BAD_GATEWAY,
            str(failure),
            problem_type("voice-generation-failed"),
        )
