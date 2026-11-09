"""Command line: `sreditor render [--check]`, `rules load`, `serve`, `doctor` and `worker`."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from sreditor.render import DOCUMENTS
from sreditor.rules_file import RULES_CSV, LoadReport, RulesFileError, load_rules, parse_rules_csv
from sreditor.settings import ENVIRONMENT, resolve_settings
from sreditor.store import StmtClient, StoreError, StoreSettings

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
    rules = commands.add_parser("rules", help="load store/rules.csv into the stmt store")
    rules.add_argument("action", choices=["load"])
    rules.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    for name, variable in ENVIRONMENT.items():
        rules.add_argument(f"--{name}", help=f"overrides {variable} and pyproject.toml")
    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    serve.add_argument(
        "--load-rules", action="store_true", help=f"load {RULES_CSV} into the store first"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    admin = commands.add_parser("create-admin", help="create an active admin user")
    admin.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True, help="display name")
    add_doctor_parser(commands)
    commands.add_parser(
        "worker", help="run a render worker: claim, build, encode and upload render jobs"
    )
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


def load_rules_file(root: Path, settings: StoreSettings) -> LoadReport:
    """Bring the store's project rules in step with the CSV git keeps."""
    text = (root / RULES_CSV).read_text(encoding="utf-8")
    return load_rules(StmtClient(settings), parse_rules_csv(text))


def rules(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """`sreditor rules load`: after a checkout, so the local store has the rules git holds."""
    root: Path = args.root.resolve()
    flags = {name: getattr(args, name) for name in ENVIRONMENT}
    try:
        report = load_rules_file(root, resolve_settings(root, flags, environ))
    except (
        OSError,
        RulesFileError,
        StoreError,
        ValidationError,
        tomllib.TOMLDecodeError,
    ) as failure:
        print(f"sreditor: {failure}", file=sys.stderr)
        return EXIT_ERROR
    print(report.summary())
    return EXIT_OK


def serve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Run the API with uvicorn, with tokens redacted from logs."""
    import uvicorn

    from sreditor.api import create_app
    from sreditor.mail import MAIL_LOGGER
    from sreditor.redact import logging_config
    from sreditor.settings import AppSettings

    settings = AppSettings.load(args.root.resolve(), environ)
    if args.load_rules:
        # The container's store lives in the data volume; a new image brings new project rules.
        print(load_rules_file(args.root.resolve(), settings.store).summary())
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
    from sreditor.doctor import DEFAULT_TOOLS, MARKS, CheckStatus, DoctorOptions, run

    options = DoctorOptions(
        root=args.root.resolve(),
        environment=args.environment,
        hostname=args.hostname,
        is_fixing=args.fix,
    )
    findings = run(options, environ, DEFAULT_TOOLS)
    for finding in findings:
        print(f"{MARKS[finding.status]} {finding.name}: {finding.detail}")
    pending = {CheckStatus.FIXABLE, CheckStatus.PROBLEM}
    return EXIT_ERROR if any(finding.status in pending for finding in findings) else EXIT_OK


def worker(environ: Mapping[str, str]) -> int:
    """Run a render worker until it is stopped; settings come from SREDITOR_WORKER_*."""
    import logging
    import signal
    import subprocess
    import threading

    from sreditor.render_worker import EngineRefused, WorkerClient, connect, run_forever
    from sreditor.worker_engine import Engine, WorkerMisconfigured, worker_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    # A stopped container finishes the job in hand; its lease would requeue it otherwise.
    signal.signal(signal.SIGTERM, lambda number, frame: stop.set())
    try:
        settings = worker_settings(environ)
        with connect(settings) as http:
            run_forever(WorkerClient(http), Engine(settings), settings, stop)
    except (WorkerMisconfigured, EngineRefused, OSError, subprocess.CalledProcessError) as failure:
        print(f"sreditor worker: {failure}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def render(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """`sreditor render [--check]`: write or check the documents generated from the store."""
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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {document.path}")
    if stale:
        print(f"sreditor: out of date: {', '.join(stale)}; run `sreditor render`", file=sys.stderr)
        return EXIT_STALE
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace, Mapping[str, str]], int]] = {
    "render": render,
    "rules": rules,
    "serve": serve,
    "create-admin": create_admin,
    "doctor": doctor,
    "worker": lambda args, environ: worker(environ),
}


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run the subcommand named on the command line and return its exit code."""
    args = parse_args(argv)
    return COMMANDS[args.command](args, os.environ if environ is None else environ)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
