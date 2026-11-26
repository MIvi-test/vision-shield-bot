"""Build and exercise a disposable mock Compose project using only the stdlib."""

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]


class SmokeError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


class ComposeSmoke:
    def __init__(self, work: Path, artifacts: Path):
        self.project = f"vision-shield-smoke-{secrets.token_hex(6)}"
        self.artifacts = artifacts / self.project
        self.artifacts.mkdir(parents=True)
        self.env_file = work / "smoke.env"
        self.credentials = {
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "RABBITMQ_PASSWORD": secrets.token_hex(24),
            "MAX_EMULATOR_TOKEN": secrets.token_hex(24),
        }
        self.write_environment()
        override = work / "override.yaml"
        backend_image = f"{self.project}-backend:smoke"
        override.write_text(
            "services:\n"
            + "".join(
                f"  {service}:\n    image: {backend_image}\n"
                for service in ("bot", "migrate", "dispatcher", "notifier")
            )
            + "  max-emulator:\n"
            + f"    image: {self.project}-emulator:smoke\n"
            + '    ports: !override ["127.0.0.1::8080"]\n'
            + "  backend:\n"
            + f"    image: {backend_image}\n"
            + '    ports: !override ["127.0.0.1::8000"]\n'
        )
        # Explicit files and a scrubbed environment isolate this run from .env
        # and shell exports (including COMPOSE_FILE/COMPOSE_PROJECT_NAME).
        interpolation = set(
            re.findall(r"\$\{(\w+)", (ROOT / "compose.yaml").read_text())
        )
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key not in interpolation
            and key != "MAX_BOT_TOKEN"
            and not key.startswith("COMPOSE_")
        }
        self.command = [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(self.env_file),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(override),
            "-p",
            self.project,
        ]
        self.emulator_url = ""
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.checks: list[str] = []

    def redact(self, value: str) -> str:
        for secret in self.credentials.values():
            value = value.replace(secret, "[REDACTED]")
        return value

    def write_environment(self, campaign: str = "") -> None:
        values = self.credentials | {
            "POSTGRES_USER": "vision_shield",
            "POSTGRES_DB": "vision_shield",
            "MAX_SOURCE_ID": self.project,
            "BACKEND_CAMPAIGN_ID": campaign,
        }
        self.env_file.touch(mode=0o600)
        self.env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
        )

    def run(self, *args: str, timeout: int = 180, check: bool = True) -> str:
        command = self.command + list(args)
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = "".join(
                value.decode(errors="replace")
                if isinstance(value, bytes)
                else value or ""
                for value in (exc.stdout, exc.stderr)
            )
            with (self.artifacts / "commands.log").open("a") as stream:
                stream.write(
                    self.redact(f"$ {shlex.join(command)}\n{output}\nTIMEOUT\n")
                )
            raise SmokeError(f"Compose {args[0]} timed out after {timeout}s") from None
        with (self.artifacts / "commands.log").open("a") as stream:
            stream.write(
                self.redact(
                    f"$ {shlex.join(command)}\n{result.stdout}{result.stderr}"
                    f"\nexit={result.returncode}\n"
                )
            )
        if check and result.returncode:
            raise SmokeError(f"Compose {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def sql(self, query: str) -> Any:
        output = self.run(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$1"',
            "sh",
            query,
            timeout=15,
        )
        return json.loads(output)

    def state(self) -> dict:
        return self.sql(
            "SELECT json_build_object("
            "'submissions', (SELECT coalesce(json_agg(json_build_object("
            "'status', status, 'version', version) ORDER BY created_at, id), '[]') "
            "FROM submissions), "
            "'consents', (SELECT count(*) FROM consents), "
            "'audit', (SELECT count(*) FROM audit_events), "
            "'inbox', (SELECT count(*) FROM max_inbox_events), "
            "'sent', (SELECT count(*) FROM max_inbox_events WHERE state = 'sent'), "
            "'unresolved', (SELECT count(*) FROM max_inbox_events "
            "WHERE state IN ('pending', 'sending', 'unknown')), "
            "'marker', (SELECT max(marker) FROM max_polling_cursors))"
        )

    def api(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.emulator_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": self.credentials["MAX_EMULATOR_TOKEN"],
                "Content-Type": "application/json",
            },
        )
        with self.http.open(request, timeout=5) as response:
            return json.load(response)

    def wait_state(self, versions: list[int], events: int) -> dict:
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            last = self.state()
            expected = [{"status": "validating", "version": v} for v in versions]
            if versions == [1] and events == 1:
                expected = [{"status": "awaiting_consent", "version": 1}]
            if (
                last["submissions"] == expected
                and last["sent"] == last["inbox"] == last["marker"] == events
                and last["unresolved"] == 0
            ):
                require(
                    len(self.api("/__test__/outgoing")["messages"]) == events,
                    "Unexpected number of bot replies",
                )
                return last
            time.sleep(0.5)
        raise SmokeError(f"Timed out waiting for submissions/replies: {last}")

    def passed(self, message: str) -> None:
        self.checks.append(message)
        print(f"OK: {message}", flush=True)

    def execute(self) -> None:
        print(f"Project: {self.project}\nArtifacts: {self.artifacts}", flush=True)
        self.run("config", "--quiet")
        print("Building application images", flush=True)
        self.run("build", timeout=600)
        print("Starting on empty volumes", flush=True)
        self.run(
            "up", "--no-build", "-d", "--wait", "--wait-timeout", "180", timeout=240
        )
        self.emulator_url = self.url("max-emulator", "8080")
        with self.http.open(
            self.url("backend", "8000") + "/health/live", timeout=5
        ) as response:
            require(json.load(response) == {"status": "ok"}, "Backend liveness failed")
        heads = self.run(
            "exec",
            "-T",
            "backend",
            "alembic",
            "-c",
            "apps/backend/alembic.ini",
            "heads",
        )
        expected_heads = sorted(line.split()[0] for line in heads.splitlines())
        actual_heads = self.sql(
            "SELECT json_agg(version_num ORDER BY version_num) FROM alembic_version"
        )
        require(actual_heads == expected_heads, "Database migrations are not at head")
        self.passed("Images built, services started, migrations at head, HTTP live")

        campaign = self.run(
            "run",
            "--rm",
            "--no-deps",
            "bot",
            "python",
            "apps/backend/manage.py",
            "--slug",
            "compose-smoke",
            "--rules-version",
            "demo-v1",
            "--rules-file",
            "apps/backend/demo-rules.txt",
        )
        campaign_id = str(UUID(campaign.removeprefix("BACKEND_CAMPAIGN_ID=")))
        self.write_environment(campaign_id)
        self.run("up", "--no-build", "-d", "--no-deps", "bot")
        self.api("/__test__/messages", {"user_id": 2001, "photo": True})
        before = self.wait_state([1], 1)
        require(
            before["consents"] == 0 and before["audit"] == 1,
            "Consent granted before confirmation",
        )
        self.api("/__test__/messages", {"user_id": 2001, "text": "/agree demo-v1"})
        agreed = self.wait_state([2], 2)
        require(
            agreed["consents"] == 1 and agreed["audit"] == 2, "Consent/audit mismatch"
        )
        self.passed(
            "Photo waits for consent; delivered rules and agreement advance submission"
        )

        self.run("stop", "bot")
        self.run("start", "bot")
        self.api("/__test__/messages", {"user_id": 2001, "photo": True})
        saved = self.wait_state([2, 1], 3)
        require(
            saved["consents"] == 1 and saved["audit"] == 3,
            "Restart duplicated consent/audit",
        )
        self.passed("Bot restart preserves consent and processes the next photo")

        self.run("stop", "bot")
        # Deliberate replay in our disposable DB, keeping the emulator alive.
        self.run(
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            '-c "UPDATE max_polling_cursors SET marker = 0"',
        )
        self.run("start", "bot")
        require(self.wait_state([2, 1], 3) == saved, "Replay changed database state")
        self.passed(
            "Replaying the same events creates no duplicate submissions or replies"
        )

        self.run("stop", "bot")
        self.run("stop", "postgres", "rabbitmq")
        self.run("rm", "-f", "postgres", "rabbitmq")
        self.run(
            "up",
            "--no-build",
            "-d",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
            "rabbitmq",
            timeout=240,
        )
        require(
            self.state() == saved, "Replacing database containers lost persisted state"
        )
        self.run("start", "bot")
        self.api("/__test__/messages", {"user_id": 2001, "photo": True})
        final = self.wait_state([2, 1, 1], 4)
        require(
            final["consents"] == 1 and final["audit"] == 4,
            "Recovery changed consent/audit",
        )
        self.passed(
            "Database volume survives replacement; broker starts; bot resumes processing"
        )

    def url(self, service: str, port: str) -> str:
        address = self.run("port", service, port)
        require(
            bool(re.fullmatch(r"127\.0\.0\.1:\d+", address)),
            "Expected a loopback-only random port",
        )
        return "http://" + address

    def diagnostics(self) -> None:
        for name, args in (
            ("services.json", ("ps", "-a", "--format", "json")),
            ("services.log", ("logs", "--no-color", "--timestamps", "--tail", "500")),
        ):
            try:
                output = self.run(*args, timeout=30, check=False)
                (self.artifacts / name).write_text(self.redact(output) + "\n")
            except (OSError, subprocess.SubprocessError, SmokeError) as exc:
                (self.artifacts / name).write_text(type(exc).__name__ + "\n")


def interrupted(signum: int, _frame: Any) -> None:
    raise InterruptedError(f"Interrupted by signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-dir", type=Path, default=ROOT / "tmp" / "compose-smoke"
    )
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="vision-shield-smoke-") as directory:
        smoke = ComposeSmoke(Path(directory), args.artifacts_dir.resolve())
        error = ""
        try:
            smoke.execute()
        except (Exception, KeyboardInterrupt) as exc:
            error = smoke.redact(f"{type(exc).__name__}: {exc}")
            print(f"FAILED: {error}", flush=True)
        finally:
            try:
                smoke.diagnostics()
            except (OSError, SmokeError) as exc:
                error = error or smoke.redact(f"Diagnostics failed: {exc}")
            try:
                smoke.run(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "30",
                    timeout=120,
                )
            except (OSError, subprocess.SubprocessError, SmokeError) as exc:
                error = error or smoke.redact(
                    f"Cleanup failed: {type(exc).__name__}: {exc}"
                )
            (smoke.artifacts / "result.json").write_text(
                json.dumps(
                    {"project": smoke.project, "checks": smoke.checks, "error": error},
                    indent=2,
                )
                + "\n"
            )
        print(
            f"{'FAILED' if error else 'PASSED'}; artifacts: {smoke.artifacts}",
            flush=True,
        )
        return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
