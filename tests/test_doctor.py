"""`sreditor doctor`: what it reports, what --fix writes, and the tunnel it creates."""

from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

import pytest
from pydantic import SecretStr

from sreditor import cli
from sreditor.cloudflare import Cloudflare, Transport
from sreditor.doctor import (
    CheckStatus,
    DoctorOptions,
    Environment,
    Finding,
    Tools,
    parse_env,
    probe,
    run,
    with_values,
)

EXAMPLE = (
    "SREDITOR_UID=1000\nSREDITOR_PUBLIC_URL=http://localhost:8000\n# COMPOSE_PROFILES=tunnel\n"
)
API_TOKEN = {"CLOUDFLARE_API_TOKEN": "cf-api-token"}
HOSTNAME = "app.example.com"
DELIVERING = "SREDITOR_SMTP2GO_API_KEY=key\nSREDITOR_MAIL_SENDER=noreply@example.com\n"

# What the Cloudflare API answers, by "METHOD /path" with the query left out.
CLOUDFLARE_RESULTS: dict[str, object] = {
    "GET /zones": [{"id": "z", "account": {"id": "a"}}],
    "GET /accounts/a/cfd_tunnel": [{"id": "tun-1"}],
    "GET /zones/z/dns_records": [],
    "GET /accounts/a/cfd_tunnel/tun-1/token": "tunnel-token",
}


def cloudflare_answer(request: urllib.request.Request, timeout: float) -> bytes:
    path = request.full_url.split("/v4", 1)[1].split("?")[0]
    result = CLOUDFLARE_RESULTS.get(f"{request.get_method()} {path}")
    return json.dumps({"success": True, "result": result}).encode()


def cloudflare_refusal(request: urllib.request.Request, timeout: float) -> bytes:
    return json.dumps({"success": False, "errors": [{"message": "Invalid API token"}]}).encode()


def tools(is_up: bool = True, transport: Transport = cloudflare_answer) -> Tools:
    return Tools(
        probe=lambda url: is_up,
        cloudflare=lambda token, url: Cloudflare(token, url, transport),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "var").mkdir()
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "rules.csv").write_text("")
    (tmp_path / ".env.example").write_text(EXAMPLE)
    return tmp_path


def with_env(root: Path, text: str = EXAMPLE) -> Path:
    (root / ".env").write_text(text)
    return root


def finding(findings: list[Finding], name: str) -> Finding:
    return next(item for item in findings if item.name == name)


def doctor(root: Path, **options: object) -> list[Finding]:
    environ = options.pop("environ", {})
    used_tools = options.pop("tools", tools())
    return run(DoctorOptions(root=root, **options), environ, used_tools)  # type: ignore[arg-type]


def test_doctor_without_env_file_stops_after_reporting_it(root: Path) -> None:
    [only] = doctor(root)
    assert (only.name, only.status) == (".env", CheckStatus.FIXABLE)


def test_doctor_fix_without_env_file_copies_example_privately(root: Path) -> None:
    doctor(root, is_fixing=True)
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600


def test_doctor_without_fix_leaves_env_file_unchanged(root: Path) -> None:
    with_env(root)
    doctor(root, environment=Environment.PRODUCTION, hostname=HOSTNAME)
    assert (root / ".env").read_text() == EXAMPLE


def test_doctor_owner_differing_from_data_folder_is_fixable(root: Path) -> None:
    with_env(root, "SREDITOR_UID=0\n")
    assert finding(doctor(root), "owner").status is CheckStatus.FIXABLE


def test_doctor_fix_sets_owner_to_data_folder_owner(root: Path) -> None:
    with_env(root, "SREDITOR_UID=0\n")
    doctor(root, is_fixing=True)
    values = parse_env((root / ".env").read_text())
    assert values["SREDITOR_UID"] == str(os.getuid())
    assert values["SREDITOR_GID"] == str(os.getgid())


def test_doctor_without_data_folder_reports_owner_problem(root: Path) -> None:
    with_env(root)
    (root / "var").rmdir()
    assert finding(doctor(root), "owner").status is CheckStatus.PROBLEM


def test_doctor_without_rules_file_reports_problem(root: Path) -> None:
    with_env(root)
    (root / "store" / "rules.csv").unlink()
    assert finding(doctor(root), "rules").status is CheckStatus.PROBLEM


def test_doctor_development_without_public_url_uses_localhost(root: Path) -> None:
    with_env(root, "")
    doctor(root, is_fixing=True)
    assert parse_env((root / ".env").read_text())["SREDITOR_PUBLIC_URL"] == "http://localhost:8000"


def test_doctor_production_without_https_reports_problem(root: Path) -> None:
    with_env(root)
    result = doctor(root, environment=Environment.PRODUCTION)
    assert finding(result, "public URL").status is CheckStatus.PROBLEM


def test_doctor_production_without_hostname_reports_tunnel_problem(root: Path) -> None:
    with_env(root)
    result = doctor(root, environment=Environment.PRODUCTION)
    assert finding(result, "tunnel").status is CheckStatus.PROBLEM


def test_doctor_development_without_hostname_needs_no_tunnel(root: Path) -> None:
    with_env(root)
    assert finding(doctor(root), "tunnel").status is CheckStatus.OK


def test_doctor_production_without_mail_key_reports_problem(root: Path) -> None:
    with_env(root)
    result = doctor(root, environment=Environment.PRODUCTION, hostname=HOSTNAME)
    assert finding(result, "email").status is CheckStatus.PROBLEM


def test_doctor_fix_in_production_turns_off_token_logging(root: Path) -> None:
    with_env(root, DELIVERING + "SREDITOR_MAIL_LOG_TOKENS=true\n")
    doctor(root, environment=Environment.PRODUCTION, hostname=HOSTNAME, is_fixing=True)
    assert parse_env((root / ".env").read_text())["SREDITOR_MAIL_LOG_TOKENS"] == "false"


def test_doctor_development_with_mail_key_reports_smtp2go(root: Path) -> None:
    with_env(root, DELIVERING)
    assert "SMTP2GO" in finding(doctor(root), "email").detail


def test_doctor_hostname_without_api_token_reports_tunnel_problem(root: Path) -> None:
    with_env(root)
    result = doctor(root, hostname=HOSTNAME)
    assert "CLOUDFLARE_API_TOKEN" in finding(result, "tunnel").detail


def test_doctor_with_tunnel_token_but_no_api_token_is_ok(root: Path) -> None:
    with_env(root, "CLOUDFLARE_TUNNEL_TOKEN=t\nCOMPOSE_PROFILES=tunnel\n")
    assert finding(doctor(root, hostname=HOSTNAME), "tunnel").status is CheckStatus.OK


def test_doctor_with_api_token_without_fix_would_create_tunnel(root: Path) -> None:
    with_env(root)
    result = doctor(root, hostname=HOSTNAME, environ=API_TOKEN)
    assert finding(result, "tunnel").status is CheckStatus.FIXABLE


def test_doctor_fix_with_api_token_writes_tunnel_settings(root: Path) -> None:
    with_env(root)
    doctor(root, hostname=HOSTNAME, environ=API_TOKEN, is_fixing=True)
    values = parse_env((root / ".env").read_text())
    assert values["CLOUDFLARE_TUNNEL_TOKEN"] == "tunnel-token"
    assert values["COMPOSE_PROFILES"] == "tunnel"
    assert values["SREDITOR_PUBLIC_URL"] == "https://app.example.com"
    assert values["SREDITOR_TUNNEL_HOSTNAME"] == HOSTNAME


def test_doctor_fix_never_writes_cloudflare_api_token(root: Path) -> None:
    with_env(root)
    doctor(root, hostname=HOSTNAME, environ=API_TOKEN, is_fixing=True)
    assert "cf-api-token" not in (root / ".env").read_text()


def test_doctor_second_fix_reports_everything_ok(root: Path) -> None:
    with_env(root)
    doctor(root, hostname=HOSTNAME, environ=API_TOKEN, is_fixing=True)
    again = doctor(root, environ=API_TOKEN, is_fixing=True)
    assert {item.status for item in again} == {CheckStatus.OK}


def test_doctor_cloudflare_refusal_reports_problem(root: Path) -> None:
    with_env(root)
    result = doctor(
        root,
        hostname=HOSTNAME,
        environ=API_TOKEN,
        is_fixing=True,
        tools=tools(transport=cloudflare_refusal),
    )
    assert "Invalid API token" in finding(result, "tunnel").detail


def test_doctor_app_not_answering_reports_problem(root: Path) -> None:
    with_env(root)
    assert finding(doctor(root, tools=tools(is_up=False)), "running").status is CheckStatus.PROBLEM


def test_parse_env_skips_comments_and_strips_quotes() -> None:
    assert parse_env('# A=1\nB="two words"\n\nC=3\n') == {"B": "two words", "C": "3"}


def test_with_values_uncomments_a_commented_key() -> None:
    assert with_values("# COMPOSE_PROFILES=tunnel\n", {"COMPOSE_PROFILES": "tunnel"}) == (
        "COMPOSE_PROFILES=tunnel\n"
    )


def test_with_values_appends_a_missing_key() -> None:
    assert with_values("A=1\n", {"B": "2"}) == "A=1\nB=2\n"


def test_with_values_quotes_values_with_spaces() -> None:
    assert with_values("A=1\n", {"A": "x y"}) == 'A="x y"\n'


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_probe_answering_url_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: FakeResponse())
    assert probe("http://localhost:8000/health")


def test_probe_names_itself_so_cloudflare_lets_it_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    def answer(request: urllib.request.Request, timeout: float) -> FakeResponse:
        seen.append(request)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    probe("https://app.example.com/health")
    assert seen[0].get_header("User-agent") == "sreditor-doctor"


def test_probe_unreachable_url_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(url: str, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    assert not probe("http://localhost:8000/health")


def test_doctor_command_with_something_missing_exits_with_error(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor", "--root", str(root)], environ={}) == cli.EXIT_ERROR
    assert "fix     .env" in capsys.readouterr().out


def test_doctor_command_with_nothing_missing_exits_ok(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sreditor.doctor.DEFAULT_TOOLS", tools())
    cli.main(["doctor", "--root", str(root), "--fix"], environ={})
    assert cli.main(["doctor", "--root", str(root)], environ={}) == cli.EXIT_OK


def test_cloudflare_factory_builds_client_with_token() -> None:
    client = tools().cloudflare(SecretStr("t"), "https://api.test/v4")
    assert client.base_url == "https://api.test/v4"
