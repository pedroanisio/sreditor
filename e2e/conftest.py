"""End-to-end fixtures: the app as built for Docker (rule ui.e2e.playwright), plus evidence.

They run only when Pedro asks, because a browser and a container per test overload a development
machine: otherwise every test is skipped. SREDITOR_E2E=1 turns them on, which the CI e2e job does
when started by hand (`SREDITOR_E2E=1 uv run pytest e2e --no-cov`).
Each test gets a fresh container, so its database and
rate limiter start empty. Screenshots go to e2e-evidence/ (git-ignored, rule ui.e2e.evidence);
a failing test also leaves its Playwright trace and the container log there.
"""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.sync_api import Page

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "e2e-evidence"
IMAGE = "sreditor:e2e"
CONTAINER_PORT = 8000
STARTUP_SECONDS = 30
MAIL_SECONDS = 10
POLL_SECONDS = 0.2

# Rule ui.e2e.evidence: each step at a phone and a desktop width, in both themes.
VIEWPORTS = {390: 844, 1280: 800}
COLOR_SCHEMES = ("light", "dark")
# Where each test runs between captures.
WORKING_VIEWPORT = {"width": 1280, "height": 800}
WORKING_SCHEME = "light"

# LogMailer writes "Email to <address>: <subject>" and then the body.
MAIL_HEADER = "Email to "
LINK = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Server:
    """One running container and how to reach it."""

    url: str
    container: str

    def log(self) -> str:
        """Everything the container has written so far."""
        result = subprocess.run(
            ["docker", "logs", self.container], capture_output=True, text=True, check=True
        )
        return result.stdout + result.stderr

    def link_for(self, email: str) -> str:
        """The link in the latest email sent to `email`, waiting for it to be logged."""
        deadline = time.monotonic() + MAIL_SECONDS
        while time.monotonic() < deadline:
            links = [
                found.group(0)
                for chunk in self.log().split(MAIL_HEADER)[1:]
                if chunk.startswith(f"{email}:") and (found := LINK.search(chunk))
            ]
            if links:
                return links[-1]
            time.sleep(POLL_SECONDS)
        raise AssertionError(f"no email to {email} in the container log")

    def emails_to(self, email: str) -> int:
        """How many emails were sent to `email`."""
        return sum(chunk.startswith(f"{email}:") for chunk in self.log().split(MAIL_HEADER))


def run(command: list[str]) -> str:
    """Run a command and fail with its output when it fails."""
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def copy_store(target: Path) -> None:
    """A private copy of the rule store, so the legal pages have their real text."""
    target.mkdir()
    with (
        closing(sqlite3.connect(ROOT / "store" / "rules.sqlite")) as source,
        closing(sqlite3.connect(target / "rules.sqlite")) as copy,
    ):
        source.backup(copy)


def wait_until_healthy(server: Server) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{server.url}/health"):
                return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(POLL_SECONDS)
    raise AssertionError(f"the app at {server.url} did not start:\n{server.log()}")


@pytest.fixture(scope="session")
def image() -> str:
    """Build the Docker image once per run, from the same Dockerfile as production."""
    run(["docker", "build", "--quiet", "--tag", IMAGE, "."])
    return IMAGE


@pytest.fixture
def server(image: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Server]:
    copy_store(tmp_path / "store")
    (tmp_path / "data").mkdir()
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = {
        "SREDITOR_PUBLIC_URL": url,
        "SREDITOR_COOKIE_SECURE": "false",  # plain http on localhost
        "SREDITOR_MAIL_LOG_TOKENS": "true",  # the tests follow the links in the log
        "SREDITOR_DEV_PAGES": "true",
    }
    container = run(
        ["docker", "run", "--detach", "--user", f"{os.getuid()}:{os.getgid()}"]
        + ["--publish", f"127.0.0.1:{port}:{CONTAINER_PORT}"]
        + ["--volume", f"{tmp_path / 'store'}:/store", "--volume", f"{tmp_path / 'data'}:/data"]
        + [arg for name, value in environment.items() for arg in ("--env", f"{name}={value}")]
        + [image]
    )
    started = Server(url, container)
    try:
        wait_until_healthy(started)
        yield started
    finally:
        if request.node.stash.get(FAILED, False):
            log = evidence_dir(request.node.name) / "container.log"
            log.write_text(started.log())
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)


FAILED = pytest.StashKey[bool]()
ENABLE_VARIABLE = "SREDITOR_E2E"
ENABLED_VALUE = "1"
DISABLED_REASON = f"e2e tests run only when Pedro asks; {ENABLE_VARIABLE}={ENABLED_VALUE} runs them"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every e2e test unless they were asked for, so no container or browser starts."""
    if os.environ.get(ENABLE_VARIABLE) == ENABLED_VALUE:
        return
    skip = pytest.mark.skip(reason=DISABLED_REASON)
    for item in items:
        item.add_marker(skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> pytest.TestReport:
    """Remember a failure so the server fixture can keep the container log."""
    report: pytest.TestReport = yield
    if report.failed:
        item.stash[FAILED] = True
    return report


def pytest_configure(config: pytest.Config) -> None:
    """Keep a Playwright trace of every failing test next to the screenshots."""
    config.option.tracing = "retain-on-failure"
    config.option.output = str(EVIDENCE / "traces")


def evidence_dir(name: str) -> Path:
    folder = EVIDENCE / re.sub(r"[^\w-]+", "-", name).strip("-")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@dataclass
class Evidence:
    """Screenshots of each step at every width and theme (rule ui.e2e.evidence)."""

    page: Page
    folder: Path
    step: int = 0

    def capture(self, name: str) -> None:
        self.step += 1
        for width, height in VIEWPORTS.items():
            self.page.set_viewport_size({"width": width, "height": height})
            for scheme in COLOR_SCHEMES:
                self.page.emulate_media(color_scheme=scheme)
                path = self.folder / f"{self.step:02d}-{name}-{width}-{scheme}.png"
                self.page.screenshot(path=path, full_page=True, animations="disabled")
        self.page.set_viewport_size(WORKING_VIEWPORT)
        self.page.emulate_media(color_scheme=WORKING_SCHEME)


@pytest.fixture
def evidence(page: Page, request: pytest.FixtureRequest) -> Evidence:
    return Evidence(page, evidence_dir(request.node.name))
