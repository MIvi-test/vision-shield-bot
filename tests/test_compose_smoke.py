"""Isolation and failure diagnostics of the disposable Compose runner."""

import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import smoke_compose
from smoke_compose import ComposeSmoke, SmokeError


@pytest.fixture
def smoke(tmp_path: Path) -> ComposeSmoke:
    work = tmp_path / "work"
    work.mkdir()
    return ComposeSmoke(work, tmp_path / "artifacts")


def test_shell_configuration_cannot_redirect_smoke_to_user_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    inherited = secrets.token_hex(24)
    for name in (
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "POSTGRES_PASSWORD",
        "MAX_EMULATOR_TOKEN",
        "BACKEND_CAMPAIGN_ID",
        "MAX_BOT_TOKEN",
    ):
        monkeypatch.setenv(name, inherited)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    one = ComposeSmoke(first, tmp_path / "artifacts")
    two = ComposeSmoke(second, tmp_path / "artifacts")
    assert one.project != two.project
    assert inherited not in one.environment.values()
    assert inherited not in one.env_file.read_text()
    assert one.env_file.stat().st_mode & 0o777 == 0o600
    assert one.command[-2:] == ["-p", one.project]
    assert not list(one.artifacts.iterdir())


def test_failed_command_logs_hide_credentials(
    smoke: ComposeSmoke, monkeypatch: pytest.MonkeyPatch
):
    value = "\n".join(smoke.credentials.values())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, value, value),
    )
    with pytest.raises(SmokeError, match="exit 1"):
        smoke.run("up")
    log = (smoke.artifacts / "commands.log").read_text()
    assert "[REDACTED]" in log
    assert all(secret not in log for secret in smoke.credentials.values())


def test_timeout_preserves_redacted_partial_build_log(
    smoke: ComposeSmoke, monkeypatch: pytest.MonkeyPatch
):
    secret = smoke.credentials["POSTGRES_PASSWORD"]

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args, 1, output=secret.encode(), stderr=b"build stalled"
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SmokeError, match="timed out"):
        smoke.run("build", timeout=1)
    log = (smoke.artifacts / "commands.log").read_text()
    assert "build stalled" in log and "TIMEOUT" in log and secret not in log


@pytest.mark.parametrize("diagnostics_timeout", [False, True])
def test_failure_collects_logs_cleans_only_own_project_and_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostics_timeout: bool
):
    calls = []
    credentials = []

    def fail(self):
        credentials.extend(self.credentials.values())
        self.run("up")
        raise SmokeError(credentials[0])

    def command(args, **kwargs):
        calls.append(args)
        if diagnostics_timeout and "logs" in args:
            raise subprocess.TimeoutExpired(args, 30)
        output = "\n".join(credentials) if "logs" in args else "[]"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(ComposeSmoke, "execute", fail)
    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr(smoke_compose.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        sys, "argv", ["smoke_compose.py", "--artifacts-dir", str(tmp_path)]
    )
    assert smoke_compose.main() == 1
    directory = next(tmp_path.iterdir())
    result = json.loads((directory / "result.json").read_text())
    assert result["error"] == "SmokeError: [REDACTED]"
    assert (directory / "services.log").exists()
    for call in calls:
        assert call[call.index("-p") + 1] == result["project"]
        assert not Path(call[call.index("--env-file") + 1]).exists()
    assert "down" in calls[-1] and "--volumes" in calls[-1]
    for path in directory.iterdir():
        assert all(secret not in path.read_text() for secret in credentials)
