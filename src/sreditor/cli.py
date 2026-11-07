"""Command line: `sreditor render [--check]`, `sreditor serve` and `sreditor doctor`."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from sreditor.render import DOCUMENTS
from sreditor.settings import ENVIRONMENT, resolve_settings
from sreditor.store import StmtClient, StoreError

EXIT_OK = 0
EXIT_STALE = 1
EXIT_ERROR = 2


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line; each subcommand takes its own options."""
    parser = argparse.ArgumentParser(prog="sreditor", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="write documents generated from the stmt store")
    render.add_argument("--check", action="store_true", help="fail if a document is out of date")
    render.add_argument("--only", choices=sorted(DOCUMENTS), action="append", help="one document")
    render.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    for name, variable in ENVIRONMENT.items():
        render.add_argument(f"--{name}", help=f"overrides {variable} and pyproject.toml")
    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    admin = commands.add_parser("create-admin", help="create an active admin user")
    admin.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True, help="display name")
    add_doctor_parser(commands)
    return parser.parse_args(argv)


def add_doctor_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the `doctor` subcommand, which checks a deployment for missing settings."""
    from sreditor.doctor import Environment

    doctor = commands.add_parser("doctor", help="find what the deployment is missing")
    doctor.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    doctor.add_argument(
        "--environment", choices=[member.value for member in Environment], help="default: .env"
    )
    doctor.add_argument("--hostname", help="public hostname served through the Cloudflare tunnel")
    doctor.add_argument("--fix", action="store_true", help="write .env and create the tunnel")


def create_admin(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Create an admin. The password comes from SREDITOR_ADMIN_PASSWORD or a prompt."""
    from getpass import getpass

    from sreditor.auth_routes import RegisterInput
    from sreditor.database import Database
    from sreditor.security import Passwords
    from sreditor.settings import AppSettings
    from sreditor.users import UserExists, UserStatus, UserStore

    password = environ.get("SREDITOR_ADMIN_PASSWORD") or getpass("Password (12+ characters): ")
    try:
        data = RegisterInput(email=args.email, display_name=args.name, password=password)
        settings = AppSettings.load(args.root.resolve(), environ)
        users = UserStore(Database(settings.channels_db))
        user = users.create(
            data.email, data.display_name, Passwords().hash(data.password), UserStatus.ACTIVE, True
        )
    except (ValidationError, UserExists) as invalid:
        message = (
            f"{invalid} already has an account" if isinstance(invalid, UserExists) else str(invalid)
        )
        print(f"sreditor: {message}", file=sys.stderr)
        return EXIT_ERROR
    print(f"created admin {user.email}")
    return EXIT_OK


def serve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Run the API with uvicorn, with tokens redacted from logs."""
    import uvicorn

    from sreditor.api import create_app
    from sreditor.mail import MAIL_LOGGER
    from sreditor.redact import logging_config
    from sreditor.settings import AppSettings

    settings = AppSettings.load(args.root.resolve(), environ)
    # SREDITOR_MAIL_LOG_TOKENS is for development: emails are logged with their links intact.
    reveal = [MAIL_LOGGER] if settings.has_tokens_in_mail_log else []
    if settings.metrics_port is not None:
        from sreditor import metrics

        metrics.serve(args.host, settings.metrics_port)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=logging_config(uvicorn.config.LOGGING_CONFIG, reveal),
    )
    return EXIT_OK


def doctor(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Print one line per check; fail while something still needs fixing."""
    from sreditor.doctor import DEFAULT_TOOLS, MARKS, DoctorOptions, Status, run

    options = DoctorOptions(
        root=args.root.resolve(),
        environment=args.environment,
        hostname=args.hostname,
        is_fixing=args.fix,
    )
    findings = run(options, environ, DEFAULT_TOOLS)
    for finding in findings:
        print(f"{MARKS[finding.status]} {finding.name}: {finding.detail}")
    pending = {Status.FIXABLE, Status.PROBLEM}
    return EXIT_ERROR if any(finding.status in pending for finding in findings) else EXIT_OK


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run the subcommand named on the command line and return its exit code."""
    args = parse_args(argv)
    environ = os.environ if environ is None else environ
    if args.command == "serve":
        return serve(args, environ)
    if args.command == "create-admin":
        return create_admin(args, environ)
    if args.command == "doctor":
        return doctor(args, environ)
    root: Path = args.root.resolve()
    flags = {name: getattr(args, name) for name in ENVIRONMENT}
    try:
        settings = resolve_settings(root, flags, environ)
        statements = StmtClient(settings).statements()
    except (StoreError, ValidationError, tomllib.TOMLDecodeError) as failure:
        print(f"sreditor: {failure}", file=sys.stderr)
        return EXIT_ERROR

    stale: list[str] = []
    for name in args.only or sorted(DOCUMENTS):
        document = DOCUMENTS[name]
        path = root / document.path
        text = document.render(statements, settings)
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == text:
            continue
        if args.check:
            stale.append(document.path)
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {document.path}")
    if stale:
        print(f"sreditor: out of date: {', '.join(stale)}; run `sreditor render`", file=sys.stderr)
        return EXIT_STALE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
