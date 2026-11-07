"""MCP tools for an episode's short script and its generated voice-over (rule agentic.mcp).

Task-sized tools (agentic.mcp.tools.outcomes): read or write the script, list the voices it can
use, generate its voice-over and adopt the times a take measured.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from sreditor import short_script_operations as scripts
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.elevenlabs import ElevenLabsError, Voice
from sreditor.mcp_server import Effect, principal, reported, tool_annotations
from sreditor.short_script import ShortScript, ShortScriptInput
from sreditor.voice_generation import Take, TakeNotFound


class ShortScriptView(BaseModel):
    """A short script with the latest voice-over take made from it, if any."""

    model_config = ConfigDict(frozen=True)

    short_script: ShortScript
    latest_take: Take | None


class TakeResult(BaseModel):
    """A voice-over take."""

    model_config = ConfigDict(frozen=True)

    take: Take


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

    @server.tool(annotations=tool_annotations("List voices", Effect.READ))
    def list_voices(context: Context, slug: str) -> VoiceList:
        """List the ElevenLabs voices whose voice_id a short script can name. Needs an owner or
        editor. Fails when the server has no ElevenLabs key."""
        return voice_reported(
            lambda: VoiceList(
                voices=scripts.list_voices(services, channel_access(services, context, slug))
            )
        )

    @server.tool(annotations=tool_annotations("Generate a voice-over", Effect.CREATE))
    def generate_voice_over(context: Context, slug: str, number: int) -> TakeResult:
        """Speak the saved short script with its ElevenLabs voice and add the audio to the
        episode's media as a voice_over file. Spends ElevenLabs credits, so call it only after
        the script is final; the same speech, voice, model and language returns the earlier take
        (is_reused true) for free. Needs an owner or editor and a voice set in the script."""
        return voice_reported(
            lambda: TakeResult(
                take=scripts.generate_voice_over(
                    services, channel_access(services, context, slug), number
                )
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
