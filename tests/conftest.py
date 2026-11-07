from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.api import create_app
from sreditor.security import Passwords
from sreditor.settings import AppSettings
from sreditor.store import Owner, StoreSettings


def statement(
    statement_id: str, category: str, text: str, priority: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "id": statement_id,
        "revision": 1,
        "revised_at": 1,
        "expires_at": None,
        "scope": "/",
        "statement": text,
        "owner": {"type": "user", "id": "tester"},
        "category": category,
        "priority": priority,
        "ttl_seconds": None,
        "evidence": [],
        "conditionals": [],
        "rule": None,
        "requires": [],
        "enabled": True,
        "override_key": None,
        **extra,
    }


STATEMENTS = [
    statement("readme.20.usage", "readme", "Run `sreditor render`."),
    statement("readme.10.intro", "readme", "A generator for documents."),
    statement("code.style", "engineering", "Write English code.", priority=1),
    statement("code.api", "engineering", "Use FastAPI.", priority=0),
    statement(
        "code.review",
        "engineering",
        "Block unreviewed writes to main.",
        priority=4,
        conditionals=["ctx.branch == 'main'", "!ctx.reviewed"],
        rule={
            "effect": "BLOCK",
            "trigger": {"kind": "EVENT", "phase": "BEFORE", "operation": "code.write"},
        },
    ),
    statement(
        "code.check",
        "engineering",
        "Check code after writes.",
        priority=4,
        rule={
            "effect": "ACT",
            "trigger": {"kind": "EVENT", "phase": "AFTER", "operation": "code.write"},
            "action": {"operation": "code.check"},
        },
    ),
    statement("agents.model", "agents", "Run subagents on Sonnet."),
]


class FakeStmt:
    """Stands in for the stmt executable and records each call."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self, args: Sequence[str], env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), env))
        return subprocess.CompletedProcess(list(args), self.returncode, self.stdout, self.stderr)


def ok_output(result: Any) -> str:
    return json.dumps({"protocol": 1, "ok": True, "result": result})


@pytest.fixture
def settings() -> StoreSettings:
    return StoreSettings(stmt="stmt", db="/tmp/rules.sqlite", domain="demo", scope="/")


ROOT = Path(__file__).resolve().parent.parent
TARBALL = ROOT / "stmt-1.0.0.tar.gz"


@pytest.fixture(scope="session")
def stmt_bin(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The real stmt launcher: STMT_BIN if set, else the archive in the repository, unpacked."""
    if os.environ.get("STMT_BIN"):
        return os.environ["STMT_BIN"]
    if shutil.which("node") is None or not TARBALL.is_file():
        pytest.skip("stmt needs Node.js and stmt-1.0.0.tar.gz")
    target = tmp_path_factory.mktemp("stmt")
    with tarfile.open(TARBALL) as archive:
        archive.extractall(target, filter="data")
    return str(target / "stmt-1.0.0" / "bin" / "stmt")


@pytest.fixture
def store(tmp_path: Path, stmt_bin: str) -> StoreSettings:
    """An empty stmt store with the domain 'test'."""
    settings = StoreSettings(stmt=stmt_bin, db=str(tmp_path / "rules.sqlite"), domain="test")
    env = {**os.environ, "STMT_DB": settings.db, "STMT_DOMAIN": "test", "STMT_SCOPE": "/"}
    subprocess.run([stmt_bin, "init"], env=env, check=True, capture_output=True)
    return settings


class RecordingMailer:
    """Keeps sent emails so tests can read the tokens in them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def token_for(self, to: str) -> str:
        body = next(body for address, _, body in reversed(self.sent) if address == to)
        match = re.search(r"(?:[?#]token=|/invitations/)([\w-]+)", body)
        assert match, body
        return match.group(1)


def fast_passwords() -> Passwords:
    """Cheap Argon2 parameters: tests check behavior, not hashing cost."""
    return Passwords(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


PASSWORD = "correct horse battery"


@pytest.fixture
def mailer() -> RecordingMailer:
    return RecordingMailer()


@pytest.fixture
def app_settings(tmp_path: Path, store: StoreSettings) -> AppSettings:
    return AppSettings(
        store=store,
        channels_db=tmp_path / "app.sqlite",
        owner=Owner(type="service", id="test"),
        public_url="https://sreditor.test",
        assets_dir=tmp_path / "assets",
    )


@pytest.fixture
def app(app_settings: AppSettings, mailer: RecordingMailer) -> FastAPI:
    return create_app(app_settings, mailer=mailer, passwords=fast_passwords())


@pytest.fixture
def sign_up(app: FastAPI, mailer: RecordingMailer) -> Callable[..., TestClient]:
    """Register, verify and sign in a user; returns a client authenticated as them."""

    def make(email: str, name: str = "Tester") -> TestClient:
        client = TestClient(app, base_url="https://testserver")
        response = client.post(
            "/auth/register", json={"email": email, "display_name": name, "password": PASSWORD}
        )
        assert response.status_code == 202, response.text
        verified = client.post("/auth/verify", json={"token": mailer.token_for(email)})
        assert verified.status_code == 200, verified.text
        signed_in = client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert signed_in.status_code == 200, signed_in.text
        client.headers["Authorization"] = f"Bearer {signed_in.json()['token']}"
        client.user_id = signed_in.json()["user"]["id"]  # type: ignore[attr-defined]
        return client

    return make
