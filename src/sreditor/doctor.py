"""`sreditor doctor`: find what a deployment is missing and, with --fix, fill it in."""

from __future__ import annotations

import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sreditor.cloudflare import DEFAULT_API_URL, Cloudflare, CloudflareError, TunnelPlan
from sreditor.cloudflare import ensure_tunnel as cloudflare_tunnel

ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"
PRIVATE_FILE_MODE = 0o600  # .env holds secrets
DATA_DIR = "var"
RULES_FILE = "store/rules.csv"
HEALTH_PATH = "/health"
PROBE_TIMEOUT_SECONDS = 5.0
# Cloudflare refuses the default Python-urllib agent (error 1010), so the probe names itself.
PROBE_USER_AGENT = "sreditor-doctor"
LOCAL_URL = "http://localhost:8000"
HTTPS = "https://"
TRUE = "true"
FALSE = "false"
TUNNEL_PROFILE = "tunnel"
TUNNEL_NAME_PREFIX = "sreditor-"
# The compose service cloudflared forwards to, on the compose network.
DEFAULT_TUNNEL_SERVICE = "http://sreditor:8000"


class Key(StrEnum):
    """Variables doctor reads and writes in .env, or reads from the shell."""

    ENVIRONMENT = "SREDITOR_ENVIRONMENT"
    HOSTNAME = "SREDITOR_TUNNEL_HOSTNAME"
    UID = "SREDITOR_UID"
    GID = "SREDITOR_GID"
    PUBLIC_URL = "SREDITOR_PUBLIC_URL"
    MAIL_LOG_TOKENS = "SREDITOR_MAIL_LOG_TOKENS"
    SMTP2GO_API_KEY = "SREDITOR_SMTP2GO_API_KEY"
    MAIL_SENDER = "SREDITOR_MAIL_SENDER"
    PROFILES = "COMPOSE_PROFILES"
    TUNNEL_TOKEN = "CLOUDFLARE_TUNNEL_TOKEN"
    TUNNEL_SERVICE = "SREDITOR_TUNNEL_SERVICE"
    CLOUDFLARE_API_TOKEN = "CLOUDFLARE_API_TOKEN"  # shell only, never written to .env
    CLOUDFLARE_API_URL = "CLOUDFLARE_API_URL"


class Environment(StrEnum):
    """Development may log email tokens and run without a tunnel; production may not."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class CheckStatus(StrEnum):
    """What a check found."""

    OK = "ok"
    FIXABLE = "fixable"  # --fix would change it
    FIXED = "fixed"
    PROBLEM = "problem"  # needs a person


# How each status is shown on the terminal.
MARKS: Mapping[CheckStatus, str] = {
    CheckStatus.OK: "ok     ",
    CheckStatus.FIXABLE: "fix    ",
    CheckStatus.FIXED: "fixed  ",
    CheckStatus.PROBLEM: "MISSING",
}


class Finding(BaseModel):
    """What one check found, and the .env values that would fix it."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    detail: str
    updates: dict[str, str] = Field(default_factory=dict)


class DoctorOptions(BaseModel):
    """The command line: where, for which environment, and whether to change anything."""

    model_config = ConfigDict(frozen=True)

    root: Path
    environment: Environment | None = None
    hostname: str | None = None
    is_fixing: bool = False


Probe = Callable[[str], bool]
CloudflareFactory = Callable[[SecretStr, str], Cloudflare]


@dataclass(frozen=True)
class Tools:
    """The side effects checks may use, replaceable in tests."""

    probe: Probe
    cloudflare: CloudflareFactory


@dataclass(frozen=True)
class Context:
    """What a check sees."""

    options: DoctorOptions
    values: Mapping[str, str]  # .env as it stands, with earlier fixes applied
    environ: Mapping[str, str]  # the shell running doctor
    tools: Tools

    @property
    def environment(self) -> Environment:
        """--environment, else the one .env remembers, else development."""
        chosen = self.options.environment or self.values.get(Key.ENVIRONMENT)
        return Environment(chosen or Environment.DEVELOPMENT)

    @property
    def hostname(self) -> str | None:
        """--hostname, else the one .env remembers; None when there is no tunnel."""
        return self.options.hostname or self.values.get(Key.HOSTNAME) or None

    @property
    def is_production(self) -> bool:
        """Whether this is the production deployment."""
        return self.environment is Environment.PRODUCTION


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines of a .env file, without comments or surrounding quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def env_line(key: str, value: str) -> str:
    """One KEY=value line, quoted when the value would otherwise be misread."""
    needs_quotes = any(character in value for character in " #'\"")
    return f'{key}="{value}"' if needs_quotes else f"{key}={value}"


def set_line(text: str, key: str, line: str) -> str:
    """Replace the key's line, else its commented-out line, else append the line."""
    for prefix in ("", "#[ \t]*"):
        pattern = re.compile(rf"^[ \t]*{prefix}{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
        found = pattern.search(text)
        if found:
            return text[: found.start()] + line + text[found.end() :]
    return text.rstrip("\n") + f"\n{line}\n"


def with_values(text: str, updates: Mapping[str, str]) -> str:
    """The .env text with each key set, keeping every other line as it was."""
    for key, value in updates.items():
        text = set_line(text, key, env_line(key, value))
    return text


def fixable(name: str, detail: str, updates: Mapping[str, str]) -> Finding:
    """A finding --fix can resolve, with the values it would write."""
    return Finding(name=name, status=CheckStatus.FIXABLE, detail=detail, updates=dict(updates))


def ok(name: str, detail: str) -> Finding:
    """A finding that needs nothing."""
    return Finding(name=name, status=CheckStatus.OK, detail=detail)


def problem(name: str, detail: str) -> Finding:
    """A finding only a person can resolve."""
    return Finding(name=name, status=CheckStatus.PROBLEM, detail=detail)


def check_environment(context: Context) -> Finding:
    """Remember the environment and hostname so later runs need no flags."""
    updates = {Key.ENVIRONMENT.value: context.environment.value}
    if context.hostname:
        updates[Key.HOSTNAME.value] = context.hostname
    return fixable(
        "environment", f"{context.environment} at {context.hostname or 'localhost'}", updates
    )


def check_owner(context: Context) -> Finding:
    """The container runs as the owner of ./var so it can write the stmt store and data."""
    data = context.options.root / DATA_DIR
    if not data.is_dir():
        return problem("owner", f"{data} is missing; create it as the user the app runs as")
    stat = data.stat()
    wanted = {Key.UID.value: str(stat.st_uid), Key.GID.value: str(stat.st_gid)}
    return fixable("owner", f"container runs as {stat.st_uid}:{stat.st_gid}", wanted)


def check_rules_store(context: Context) -> Finding:
    """The project's rules must be in place: the app loads them into its store at start."""
    rules = context.options.root / RULES_FILE
    if rules.is_file():
        return ok("rules", f"{rules} found")
    return problem("rules", f"{rules} is missing; restore it from git before starting")


def wanted_public_url(context: Context) -> str | None:
    """The tunnel's https address, else the one set, else localhost in development."""
    if context.hostname:
        return HTTPS + context.hostname
    current = context.values.get(Key.PUBLIC_URL)
    if current or context.is_production:
        return current or None
    return LOCAL_URL


def check_public_url(context: Context) -> Finding:
    """Forms are accepted only from this origin, so it must be the address people open."""
    wanted = wanted_public_url(context)
    if wanted is None or (context.is_production and not wanted.startswith(HTTPS)):
        return problem("public URL", "production needs an https address: pass --hostname")
    return fixable("public URL", wanted, {Key.PUBLIC_URL.value: wanted})


def check_mail(context: Context) -> Finding:
    """Production sends real email and never logs tokens; development may log them."""
    if not context.is_production:
        if context.values.get(Key.SMTP2GO_API_KEY):
            return ok("email", "sent through SMTP2GO")
        return ok("email", "written to the log (development)")
    if not (context.values.get(Key.SMTP2GO_API_KEY) and context.values.get(Key.MAIL_SENDER)):
        return problem(
            "email",
            f"production needs {Key.SMTP2GO_API_KEY} and {Key.MAIL_SENDER} in .env "
            "(the key is a secret: paste it there yourself)",
        )
    return fixable(
        "email", "sent through SMTP2GO, tokens never logged", {Key.MAIL_LOG_TOKENS.value: FALSE}
    )


def with_tunnel_profile(profiles: str) -> str:
    """COMPOSE_PROFILES with the tunnel profile added once."""
    names = [name for name in profiles.split(",") if name]
    return ",".join(names if TUNNEL_PROFILE in names else [*names, TUNNEL_PROFILE])


def check_tunnel(context: Context) -> Finding:
    """Create or update the Cloudflare tunnel for the hostname, when an API token allows it."""
    hostname = context.hostname
    if hostname is None:
        if context.is_production:
            return problem("tunnel", "production needs a public hostname: pass --hostname")
        return ok("tunnel", "none: reachable on this machine only")
    api_token = context.environ.get(Key.CLOUDFLARE_API_TOKEN)
    if not api_token:
        return tunnel_without_api(context, hostname)
    if not context.options.is_fixing:
        return fixable("tunnel", f"would create or update the tunnel for {hostname}", {})
    plan = TunnelPlan(
        name=TUNNEL_NAME_PREFIX + context.environment,
        hostname=hostname,
        service=context.values.get(Key.TUNNEL_SERVICE) or DEFAULT_TUNNEL_SERVICE,
    )
    api_url = context.environ.get(Key.CLOUDFLARE_API_URL) or DEFAULT_API_URL
    try:
        token = cloudflare_tunnel(context.tools.cloudflare(SecretStr(api_token), api_url), plan)
    except CloudflareError as refusal:
        return problem("tunnel", str(refusal))
    updates = {
        Key.TUNNEL_TOKEN.value: token,
        Key.PROFILES.value: with_tunnel_profile(context.values.get(Key.PROFILES, "")),
    }
    return fixable("tunnel", f"{plan.name} routes https://{hostname} to {plan.service}", updates)


def tunnel_without_api(context: Context, hostname: str) -> Finding:
    """Without an API token doctor can only see whether a tunnel token is set."""
    profiles = context.values.get(Key.PROFILES, "").split(",")
    if context.values.get(Key.TUNNEL_TOKEN) and TUNNEL_PROFILE in profiles:
        return ok(
            "tunnel", f"token set for {hostname} (export {Key.CLOUDFLARE_API_TOKEN} to verify)"
        )
    return problem(
        "tunnel",
        f"export {Key.CLOUDFLARE_API_TOKEN} (permissions: Account Cloudflare Tunnel Edit, "
        "Zone DNS Edit, Zone Read) and run with --fix to create it",
    )


def check_running(context: Context) -> Finding:
    """The app answers at its public address, through the tunnel when there is one."""
    url = (context.values.get(Key.PUBLIC_URL) or LOCAL_URL) + HEALTH_PATH
    if context.tools.probe(url):
        return ok("running", f"{url} answers")
    return problem(
        "running", f"{url} does not answer: start or restart with `docker compose up -d`"
    )


CHECKS: Sequence[Callable[[Context], Finding]] = (
    check_environment,
    check_owner,
    check_rules_store,
    check_tunnel,
    check_public_url,
    check_mail,
    check_running,
)


def settle(finding: Finding, values: Mapping[str, str], is_fixing: bool) -> Finding:
    """OK when the values already match, FIXED when --fix applies them."""
    if finding.status is not CheckStatus.FIXABLE:
        return finding
    changed = {key: value for key, value in finding.updates.items() if values.get(key) != value}
    # A fix with values that are all in place already is not a fix; one without values
    # (an action such as creating the tunnel) stays pending.
    if finding.updates and not changed:
        return finding.model_copy(update={"status": CheckStatus.OK, "updates": {}})
    status = CheckStatus.FIXED if is_fixing else CheckStatus.FIXABLE
    return finding.model_copy(update={"status": status, "updates": changed})


def ensure_env_file(root: Path, is_fixing: bool) -> Finding:
    """.env exists, copied from .env.example with owner-only permissions when it does not."""
    env = root / ENV_FILE
    if env.is_file():
        return ok(ENV_FILE, f"{env} found")
    if not is_fixing:
        return fixable(ENV_FILE, f"would copy {ENV_EXAMPLE} to {ENV_FILE}", {})
    shutil.copyfile(root / ENV_EXAMPLE, env)
    os.chmod(env, PRIVATE_FILE_MODE)
    return Finding(name=ENV_FILE, status=CheckStatus.FIXED, detail=f"copied {ENV_EXAMPLE}")


def run(options: DoctorOptions, environ: Mapping[str, str], tools: Tools) -> list[Finding]:
    """Run every check in order; with --fix, write each fix to .env before the next check."""
    first = ensure_env_file(options.root, options.is_fixing)
    env = options.root / ENV_FILE
    if not env.is_file():
        return [first]
    text = env.read_text()
    context = Context(options=options, values=parse_env(text), environ=environ, tools=tools)
    findings = [first]
    for check in CHECKS:
        finding = settle(check(context), context.values, options.is_fixing)
        findings.append(finding)
        if options.is_fixing and finding.updates:
            text = with_values(text, finding.updates)
            context = replace(context, values={**context.values, **finding.updates})
    if options.is_fixing:
        env.write_text(text)
    return findings


def probe(url: str) -> bool:
    """Whether the URL answers 2xx."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": PROBE_USER_AGENT})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return HTTPStatus(response.status).is_success
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


DEFAULT_TOOLS = Tools(probe=probe, cloudflare=Cloudflare)
