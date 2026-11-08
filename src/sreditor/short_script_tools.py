"""MCP tools for an episode's short script and its generated voice-over (rule agentic.mcp).

Task-sized tools (agentic.mcp.tools.outcomes): read or write the script, list the voices it can
use, estimate and generate its voice-over, read each block's words and adopt the times a take
measured.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict

from sreditor import short_script_operations as scripts
from sreditor import voice_estimate
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.elevenlabs import ElevenLabsError, Voice
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import principal
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.short_script import ShortScript, ShortScriptInput
from sreditor.voice_estimate import VoiceEstimate
from sreditor.voice_generation import Take, TakeNotFound
from sreditor.voice_report import VoiceOverTake


class ShortScriptView(BaseModel):
    """A short script with the latest voice-over take made from it, if any."""

    model_config = ConfigDict(frozen=True)

    short_script: ShortScript
    latest_take: Take | None


class TakeResult(BaseModel):
    """A voice-over take."""

    model_config = ConfigDict(frozen=True)

    take: VoiceOverTake


class VoiceList(BaseModel):
    """The ElevenLabs voices a short script can use."""

    model_config = ConfigDict(frozen=True)

    voices: list[Voice]


def voice_error_message(error: Exception) -> str | None:
    """How to fix a refused short script or voice call (agentic.mcp.errors)."""
    if isinstance(error, scripts.ScriptNotReady):
        return f"{error} Call write_short_script with voice.voice_id from list_voices and speech."
    if isinstance(error, scripts.TimingsDoNotFit):
        return f"{error} Call generate_voice_over for a take of the current script."
    if isinstance(error, scripts.VoiceOverNotMade):
        return f"{error} Call generate_voice_over first; estimate_voice_over shows what it costs."
    if isinstance(error, TakeNotFound):
        return f"Take {error} not found; use the `key` that generate_voice_over returned."
    if isinstance(error, ElevenLabsError):
        return f"ElevenLabs refused the request ({error}); check the voice id with list_voices."
    if isinstance(error, scripts.VoiceUnavailable):
        return str(error)
    return None


def voice_reported[T](action: Callable[[], T]) -> T:
    """Run a tool's action; voice refusals name their fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except (
            scripts.ScriptNotReady,
            scripts.TimingsDoNotFit,
            scripts.VoiceUnavailable,
            scripts.VoiceOverNotMade,
            TakeNotFound,
            ElevenLabsError,
        ) as refusal:
            raise ToolError(voice_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def add_short_script_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write a short script and to make its voice-over."""
    add_script_tools(server, services)
    add_voice_tools(server, services)


def channel_access(services: Services, context: Context, slug: str) -> ChannelAccess:
    """The caller's access to the channel, from the token behind the MCP request."""
    who = principal(context)
    return access_for(services, slug, who.user, who.token)


def add_script_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write an episode's short script."""

    @server.tool(annotations=tool_annotations("Read a short script", Effect.READ))
    def get_short_script(context: Context, slug: str, number: int) -> ShortScriptView:
        """Read an episode's short script for vertical video: length, voice, timed blocks
        (hook, development, turn, punch, cta) and on-screen texts, plus `warnings` that weaken
        it without blocking it and the latest voice-over take. Revision 0 means nobody wrote it
        yet."""

        def read() -> ShortScriptView:
            channel = channel_access(services, context, slug)
            return ShortScriptView(
                short_script=scripts.get_short_script(services, channel, number),
                latest_take=scripts.latest_take(services, channel, number),
            )

        return voice_reported(read)

    @server.tool(annotations=tool_annotations("Write a short script", Effect.REPLACE))
    def write_short_script(
        context: Context,
        slug: str,
        number: int,
        short_script: ShortScriptInput,
        expected_revision: int | None = None,
    ) -> ShortScript:
        """Replace an episode's whole short script. Needs content:write.

        Speech takes voice tags: [pause:0.5] at a point, [irony]...[/irony] over a span; an
        unknown tag, a wrong value or an unclosed span is refused. On-screen texts hold at most
        90 characters. Pass `revision` from get_short_script as `expected_revision` (0 when
        empty) so you never overwrite a version you have not read."""
        return voice_reported(
            lambda: scripts.write_short_script(
                services,
                channel_access(services, context, slug),
                number,
                short_script,
                expected_revision,
            )
        )


def add_voice_tools(server: MCPServer, services: Services) -> None:
    """Tools to list voices, generate a voice-over and adopt its times."""

    @server.tool(annotations=tool_annotations("List voices", Effect.READ, is_open_world=True))
    def list_voices(context: Context, slug: str) -> VoiceList:
        """List the ElevenLabs voices whose voice_id a short script can name. Needs an owner or
        editor. Fails when the server has no ElevenLabs key."""
        return voice_reported(
            lambda: VoiceList(
                voices=scripts.list_voices(services, channel_access(services, context, slug))
            )
        )

    @server.tool(annotations=tool_annotations("Estimate a voice-over", Effect.READ))
    def estimate_voice_over(context: Context, slug: str, number: int) -> VoiceEstimate:
        """Show what generate_voice_over would spend now, without spending anything: per spoken
        block the speaker, the voice, the characters of its speech without voice tags and whether
        a stored take is reused for free, then the new characters and the estimated ElevenLabs
        credits for the model. Call it before generate_voice_over; credits are an estimate, as
        ElevenLabs may also count voice tags. Needs an owner or editor and a voice set in the
        script or as the channel's narrator voice."""
        return voice_reported(
            lambda: voice_estimate.estimate_voice_over(
                services, channel_access(services, context, slug), number
            )
        )

    @server.tool(annotations=tool_annotations("Read a voice-over", Effect.READ))
    def get_voice_over(context: Context, slug: str, number: int) -> TakeResult:
        """Read the episode's latest voice-over: its media file and, for each spoken block, the
        take's voice and license (provider, voice, model, account plan), start_s and end_s and
        every word with its start and end in seconds, both from the start of the block's own take
        (words) and from the start of the joined voice-over (joined_words). Use it to place
        sound, cuts and on-screen text on the spoken words. Fails when there is no voice-over
        yet: call generate_voice_over first. Does not spend credits."""
        return voice_reported(
            lambda: TakeResult(
                take=scripts.get_voice_over(
                    services, channel_access(services, context, slug), number
                )
            )
        )

    @server.tool(
        annotations=tool_annotations("Generate a voice-over", Effect.CREATE, is_open_world=True)
    )
    def generate_voice_over(
        context: Context, slug: str, number: int, idempotency_key: IdempotencyKey = None
    ) -> Annotated[CallToolResult, TakeResult]:
        """Speak the saved short script with its ElevenLabs voice and add the audio to the
        episode's media as a voice_over file. Spends ElevenLabs credits, so call
        estimate_voice_over first and call this only after the script is final; the same
        speech, voice, model and language returns the earlier take (is_reused true) for free, and
        only the blocks the estimate counted as new spend credits. The result holds each block's
        take with its words timed from the take and from the joined voice-over. Needs an owner or
        editor and a voice set in the script or as the channel's narrator voice. Pass an
        `idempotency_key` to retry after a timeout without generating twice: the retry returns
        the first result, marked replayed, even if the script changed since."""
        call = IdempotentCall(operation=f"generate_voice_over/{slug}/{number}", key=idempotency_key)
        return voice_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: TakeResult(
                    take=scripts.generate_voice_over(
                        services, channel_access(services, context, slug), number
                    )
                ),
                TakeResult,
            )
        )

    @server.tool(annotations=tool_annotations("Use a take's timings", Effect.REPLACE))
    def apply_take_timings(
        context: Context,
        slug: str,
        number: int,
        take_key: str,
        expected_revision: int | None = None,
    ) -> ShortScript:
        """Replace the short script's planned block times with those a take measured in its
        audio, and its length with the audio's. Use the take's `key` from generate_voice_over;
        the script must still have the same blocks. Needs content:write."""
        return voice_reported(
            lambda: scripts.apply_take_timings(
                services,
                channel_access(services, context, slug),
                number,
                take_key,
                expected_revision,
            )
        )
