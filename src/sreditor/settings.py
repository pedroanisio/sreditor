"""Settings, read from flags, then environment variables, then pyproject.toml."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
# Only a proxy on this machine may set X-Forwarded-For / -Proto unless configured otherwise.
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"


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
    # Requests one API token may make per minute, counted across the API and /mcp.
    agent_requests_per_minute: int = Field(default=DEFAULT_AGENT_REQUESTS_PER_MINUTE, ge=1)
    # Proxies trusted to report the client's address, such as the Cloudflare tunnel; without
    # this every visitor would share the proxy's address and its per-client rate limits.
    forwarded_allow_ips: str = DEFAULT_FORWARDED_ALLOW_IPS
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
            agent_requests_per_minute=int(
                environ.get("SREDITOR_AGENT_REQUESTS_PER_MINUTE")
                or DEFAULT_AGENT_REQUESTS_PER_MINUTE
            ),
            forwarded_allow_ips=environ.get("SREDITOR_FORWARDED_ALLOW_IPS")
            or DEFAULT_FORWARDED_ALLOW_IPS,
            metrics_port=metrics_port(environ, project),
            max_media_bytes=max_media_bytes(environ, project),
        )


def max_media_bytes(environ: Mapping[str, str], project: Mapping[str, str]) -> int:
    """SREDITOR_MAX_MEDIA_BYTES, then max_media_bytes in pyproject.toml, then 95 MB."""
    limit = environ.get("SREDITOR_MAX_MEDIA_BYTES") or project.get("max_media_bytes")
    return int(limit) if limit else DEFAULT_MAX_MEDIA_BYTES


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
