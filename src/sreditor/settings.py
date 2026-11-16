"""Settings, read from flags, then environment variables, then pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from sreditor.elevenlabs import DEFAULT_ELEVENLABS_URL, VoiceSettings
from sreditor.mail import DEFAULT_SMTP2GO_URL, MailSettings
from sreditor.store import Owner, StoreSettings

# Store setting name -> environment variable that overrides pyproject.toml.
ENVIRONMENT = {"stmt": "STMT_BIN", "db": "STMT_DB", "domain": "STMT_DOMAIN", "scope": "STMT_SCOPE"}


def project_settings(root: Path) -> dict[str, str]:
    """Read [tool.sreditor] from the project's pyproject.toml, if present."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return dict(tomllib.load(handle).get("tool", {}).get("sreditor", {}))


def resolve_settings(
    root: Path, flags: Mapping[str, str | None], environ: Mapping[str, str]
) -> StoreSettings:
    """Flags win over environment variables, which win over pyproject.toml."""
    values = {k: v for k, v in project_settings(root).items() if k in ENVIRONMENT}
    for name, variable in ENVIRONMENT.items():
        if environ.get(variable):
            values[name] = environ[variable]
        if flags.get(name):
            values[name] = str(flags[name])
    if "db" in values:
        values["db"] = str((root / values["db"]).resolve())
    return StoreSettings.model_validate(values)


# Brand files can be short videos; larger ones belong in a media library, not the brand kit.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# An episode's voice-over, sound and visual effects (rule project.media). Below 100 MB because
# Cloudflare's free plan refuses larger requests, and the form adds a little to the file.
DEFAULT_MAX_MEDIA_BYTES = 95 * 1024 * 1024
DEFAULT_ASSETS_DIR = "var/assets"
DEFAULT_AGENT_REQUESTS_PER_MINUTE = 120
DEFAULT_EVENTS_RETENTION_DAYS = 30
# Only a proxy on this machine may set X-Forwarded-For / -Proto unless configured otherwise.
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"
# A rendered video arrives in parts (rule project.render.upload), so only the disk bounds it.
DEFAULT_MAX_RENDER_BYTES = 8 * 1024 * 1024 * 1024
# A worker renews its lease well within this; a lapsed lease sends the job back to the queue.
DEFAULT_RENDER_LEASE_SECONDS = 120
# The scene-render version workers must run, so one export renders to the same file everywhere.
DEFAULT_RENDER_ENGINE_VERSION = "0.9.0"
# The srstudio commit that builds scenes on workers: a branch moves, so the same export would build
# a different scene with nothing to say so. Dockerfile.worker's SRSTUDIO_REF must be this commit.
DEFAULT_RENDER_BUILDER_VERSION = "2dee0214b0f52fc62dbb4fa06aae0519bfa241ef"


# Our channels' short vertical videos: at most 24 s, at most 5 hashtags and a caption line that
# says AI was used ("IA" or "AI", or the words in Portuguese or English).
DEFAULT_DELIVERY_MAX_SECONDS = 24.0
DEFAULT_DELIVERY_MAX_HASHTAGS = 5
DEFAULT_AI_NOTICE_PATTERN = (
    r"\b(?:IA|AI)\b|[Ii]nteligência [Aa]rtificial|[Aa]rtificial [Ii]ntelligence"
)


class RenderSettings(BaseModel):
    """Rendering on GPU workers (rule project.render.workers)."""

    model_config = ConfigDict(frozen=True)

    max_render_bytes: int = Field(default=DEFAULT_MAX_RENDER_BYTES, ge=1)
    lease_seconds: int = Field(default=DEFAULT_RENDER_LEASE_SECONDS, ge=10)
    engine_version: str = DEFAULT_RENDER_ENGINE_VERSION
    builder_version: str = DEFAULT_RENDER_BUILDER_VERSION


class DeliveryRules(BaseModel):
    """The limits a delivery cut is checked against; a breach only warns (rule project.delivery)."""

    model_config = ConfigDict(frozen=True)

    max_seconds: float = Field(default=DEFAULT_DELIVERY_MAX_SECONDS, gt=0)
    max_hashtags: int = Field(default=DEFAULT_DELIVERY_MAX_HASHTAGS, ge=0)
    # A caption must match this regular expression; empty turns the check off.
    ai_notice_pattern: str = DEFAULT_AI_NOTICE_PATTERN

    @field_validator("ai_notice_pattern")
    @classmethod
    def is_pattern(cls, value: str) -> str:
        """Refuse a pattern that is not a regular expression, at startup and not per request."""
        try:
            re.compile(value)
        except re.error as broken:
            raise ValueError(f"not a regular expression: {broken}") from broken
        return value


class AppSettings(BaseModel):
    """Everything the app reads from pyproject.toml and the environment, loaded once at startup."""

    model_config = ConfigDict(frozen=True)

    store: StoreSettings
    channels_db: Path
    owner: Owner  # records stmt writes made outside a user's request
    public_url: str = "http://127.0.0.1:8000"
    has_secure_cookies: bool = True
    session_days: int = Field(default=30, ge=1, le=365)
    has_tokens_in_mail_log: bool = False  # development only: LogMailer keeps tokens in the log
    has_dev_pages: bool = False  # development only: serves /design (rule ui.design-system)
    assets_dir: Path = Path(DEFAULT_ASSETS_DIR)  # brand files uploaded on the pages
    max_upload_bytes: int = Field(default=DEFAULT_MAX_UPLOAD_BYTES, ge=1)
    max_media_bytes: int = Field(default=DEFAULT_MAX_MEDIA_BYTES, ge=1)
    mail: MailSettings = MailSettings()
    voice: VoiceSettings = VoiceSettings()
    render: RenderSettings = RenderSettings()
    delivery: DeliveryRules = DeliveryRules()
    # Requests one API token may make per minute, counted across the API and /mcp.
    agent_requests_per_minute: int = Field(default=DEFAULT_AGENT_REQUESTS_PER_MINUTE, ge=1)
    # How long the change feed keeps events (rule agentic.events).
    events_retention_days: int = Field(default=DEFAULT_EVENTS_RETENTION_DAYS, ge=1, le=365)
    # Proxies trusted to report the client's address, such as the Cloudflare tunnel; without
    # this every visitor would share the proxy's address and its per-client rate limits.
    forwarded_allow_ips: str = DEFAULT_FORWARDED_ALLOW_IPS
    # A header the edge proxy sets itself to the visitor's address, such as Cloudflare's
    # CF-Connecting-IP. Per-client rate limits key on it, since the leftmost X-Forwarded-For
    # entry is whatever the client sent; none (the default) keys on the connection's address.
    client_ip_header: str | None = None
    # Prometheus reads /metrics on this port; none (the default) serves no metrics.
    metrics_port: int | None = Field(default=None, ge=1, le=65535)

    @classmethod
    def load(cls, root: Path, environ: Mapping[str, str]) -> AppSettings:
        """SREDITOR_* environment variables override [tool.sreditor] in pyproject.toml."""
        project = project_settings(root)
        channels_db = environ.get("SREDITOR_CHANNELS_DB") or project.get(
            "channels_db", "var/channels.sqlite"
        )
        assets_dir = environ.get("SREDITOR_ASSETS_DIR") or project.get(
            "assets_dir", DEFAULT_ASSETS_DIR
        )
        max_upload = environ.get("SREDITOR_MAX_UPLOAD_BYTES") or project.get("max_upload_bytes")
        return cls(
            store=resolve_settings(root, {}, environ),
            channels_db=(root / channels_db).resolve(),
            assets_dir=(root / assets_dir).resolve(),
            max_upload_bytes=int(max_upload) if max_upload else DEFAULT_MAX_UPLOAD_BYTES,
            owner=Owner(
                type=environ.get("SREDITOR_OWNER_TYPE") or "service",
                id=environ.get("SREDITOR_OWNER_ID") or "sreditor",
            ),
            public_url=environ.get("SREDITOR_PUBLIC_URL") or "http://127.0.0.1:8000",
            has_secure_cookies=environ.get("SREDITOR_COOKIE_SECURE", "true").lower() != "false",
            has_tokens_in_mail_log=environ.get("SREDITOR_MAIL_LOG_TOKENS", "false").lower()
            == "true",
            has_dev_pages=environ.get("SREDITOR_DEV_PAGES", "false").lower() == "true",
            mail=mail_settings(environ),
            voice=voice_settings(environ),
            render=render_settings(environ, project),
            delivery=delivery_rules(environ, project),
            agent_requests_per_minute=int(
                environ.get("SREDITOR_AGENT_REQUESTS_PER_MINUTE")
                or DEFAULT_AGENT_REQUESTS_PER_MINUTE
            ),
            events_retention_days=int(
                environ.get("SREDITOR_EVENTS_RETENTION_DAYS")
                or project.get("events_retention_days")
                or DEFAULT_EVENTS_RETENTION_DAYS
            ),
            forwarded_allow_ips=environ.get("SREDITOR_FORWARDED_ALLOW_IPS")
            or DEFAULT_FORWARDED_ALLOW_IPS,
            client_ip_header=environ.get("SREDITOR_CLIENT_IP_HEADER") or None,
            metrics_port=metrics_port(environ, project),
            max_media_bytes=max_media_bytes(environ, project),
        )


def max_media_bytes(environ: Mapping[str, str], project: Mapping[str, str]) -> int:
    """SREDITOR_MAX_MEDIA_BYTES, then max_media_bytes in pyproject.toml, then 95 MB."""
    limit = environ.get("SREDITOR_MAX_MEDIA_BYTES") or project.get("max_media_bytes")
    return int(limit) if limit else DEFAULT_MAX_MEDIA_BYTES


def render_settings(environ: Mapping[str, str], project: Mapping[str, str]) -> RenderSettings:
    """SREDITOR_MAX_RENDER_BYTES, SREDITOR_RENDER_LEASE_SECONDS, SREDITOR_RENDER_ENGINE_VERSION and
    SREDITOR_RENDER_BUILDER_VERSION, then [tool.sreditor] in pyproject.toml, then the defaults."""
    max_bytes = environ.get("SREDITOR_MAX_RENDER_BYTES") or project.get("max_render_bytes")
    lease = environ.get("SREDITOR_RENDER_LEASE_SECONDS") or project.get("render_lease_seconds")
    engine = environ.get("SREDITOR_RENDER_ENGINE_VERSION") or project.get("render_engine_version")
    builder = environ.get("SREDITOR_RENDER_BUILDER_VERSION") or project.get(
        "render_builder_version"
    )
    return RenderSettings(
        max_render_bytes=int(max_bytes) if max_bytes else DEFAULT_MAX_RENDER_BYTES,
        lease_seconds=int(lease) if lease else DEFAULT_RENDER_LEASE_SECONDS,
        engine_version=str(engine) if engine else DEFAULT_RENDER_ENGINE_VERSION,
        builder_version=str(builder) if builder else DEFAULT_RENDER_BUILDER_VERSION,
    )


def delivery_rules(environ: Mapping[str, str], project: Mapping[str, str]) -> DeliveryRules:
    """SREDITOR_DELIVERY_MAX_SECONDS, SREDITOR_DELIVERY_MAX_HASHTAGS and
    SREDITOR_DELIVERY_AI_NOTICE_PATTERN, then [tool.sreditor], then the defaults."""
    seconds = environ.get("SREDITOR_DELIVERY_MAX_SECONDS") or project.get("delivery_max_seconds")
    hashtags = environ.get("SREDITOR_DELIVERY_MAX_HASHTAGS") or project.get("delivery_max_hashtags")
    pattern = environ.get("SREDITOR_DELIVERY_AI_NOTICE_PATTERN")
    if pattern is None:
        pattern = project.get("delivery_ai_notice_pattern")
    return DeliveryRules(
        max_seconds=float(seconds) if seconds else DEFAULT_DELIVERY_MAX_SECONDS,
        max_hashtags=int(hashtags) if hashtags else DEFAULT_DELIVERY_MAX_HASHTAGS,
        ai_notice_pattern=DEFAULT_AI_NOTICE_PATTERN if pattern is None else str(pattern),
    )


def metrics_port(environ: Mapping[str, str], project: Mapping[str, str]) -> int | None:
    """SREDITOR_METRICS_PORT, then metrics_port in pyproject.toml; unset serves no metrics."""
    port = environ.get("SREDITOR_METRICS_PORT") or project.get("metrics_port")
    return int(port) if port else None


def mail_settings(environ: Mapping[str, str]) -> MailSettings:
    """SMTP2GO delivery from SREDITOR_SMTP2GO_API_KEY and SREDITOR_MAIL_SENDER, if set."""
    return MailSettings(
        smtp2go_api_key=environ.get("SREDITOR_SMTP2GO_API_KEY") or None,
        smtp2go_url=environ.get("SREDITOR_SMTP2GO_URL") or DEFAULT_SMTP2GO_URL,
        sender=environ.get("SREDITOR_MAIL_SENDER") or None,
    )


def voice_settings(environ: Mapping[str, str]) -> VoiceSettings:
    """ElevenLabs from SREDITOR_ELEVENLABS_API_KEY and SREDITOR_ELEVENLABS_URL, if set."""
    key = environ.get("SREDITOR_ELEVENLABS_API_KEY")
    return VoiceSettings(
        api_key=SecretStr(key) if key else None,
        url=environ.get("SREDITOR_ELEVENLABS_URL") or DEFAULT_ELEVENLABS_URL,
    )
