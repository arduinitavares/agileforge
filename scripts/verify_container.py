"""Exercise an installed image using only disposable, synthetic Docker volumes."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Sequence

_SUCCESS = 0
_STARTUP_TIMEOUT = 60.0


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("docker")
    if executable is None:
        message = "Docker CLI is unavailable"
        raise FileNotFoundError(message)
    return subprocess.run(  # noqa: S603  # nosec B603
        (executable, *arguments),
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _object(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        message = "container returned a non-object result"
        raise TypeError(message)
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        message = "container returned missing identity metadata"
        raise TypeError(message)
    return cast("dict[str, object]", value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


@dataclass
class ImageRehearsal:
    """Own only the uniquely named disposable resources of this verification."""

    image: str
    revision: str
    prefix: str = field(default_factory=lambda: f"agileforge-verify-{uuid4().hex[:12]}")
    containers: list[str] = field(default_factory=list)

    @property
    def volume(self) -> str:
        """Return the one synthetic durable-state volume."""
        return f"{self.prefix}-state"

    def command(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run an offline installed entrypoint against synthetic state only."""
        return _docker(
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={self.volume},target=/var/lib/agileforge",
            self.image,
            *arguments,
            check=check,
        )

    def seed_artifact(self) -> None:
        """Add an exact-byte synthetic artifact while the service is stopped."""
        script = """
from pathlib import Path
from utils.runtime_fence import runtime_fence
root = Path('/var/lib/agileforge')
with runtime_fence(root, exclusive=True):
    (root / 'backups').mkdir()
    artifact = root / 'profiles/source/artifacts/canary.bin'
    artifact.write_bytes(b'synthetic\\0artifact\\n')
"""
        _docker(
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={self.volume},target=/var/lib/agileforge",
            "--entrypoint",
            "/build/.venv/bin/python",
            self.image,
            "-c",
            script,
        )

    def verify_runtime_secret(self) -> None:
        """Load a synthetic credential from a private ephemeral runtime mount."""
        script = """
import os
from pathlib import Path
from cli.container_runtime import main
secret = Path('/run/secrets/agileforge')
descriptor = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    stream.write('OPEN_ROUTER_API_KEY=synthetic-container-secret-canary\\n')
assert secret.stat().st_uid == 10001
raise SystemExit(main(['cli', '--profile', 'source', '--secrets-file',
                      str(secret), '--', 'project', 'list']))
"""
        result = _docker(
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={self.volume},target=/var/lib/agileforge",
            "--tmpfs",
            "/run/secrets:rw,noexec,nosuid,size=65536,uid=10001,gid=10001,mode=0700",
            "--entrypoint",
            "/build/.venv/bin/python",
            self.image,
            "-c",
            script,
        )
        _require(
            "synthetic-container-secret-canary" not in result.stdout + result.stderr,
            "runtime secret appeared in output",
        )

    def verify_restored_payload(self) -> None:
        """Read exact restored artifact bytes without provider or business writes."""
        script = """
from pathlib import Path
from utils.runtime_fence import runtime_fence
root = Path('/var/lib/agileforge')
with runtime_fence(root):
    profile = root / 'profiles/restored'
    data = (profile / 'artifacts/canary.bin').read_bytes()
    valid = data == b'synthetic\\0artifact\\n'
    valid = valid and not (profile / 'adk-trace.sqlite3').exists()
    raise SystemExit(0 if valid else 1)
"""
        _docker(
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--mount",
            f"type=volume,source={self.volume},target=/var/lib/agileforge",
            "--entrypoint",
            "/build/.venv/bin/python",
            self.image,
            "-c",
            script,
        )

    def start(
        self,
        suffix: str,
        *,
        profile: str = "source",
    ) -> tuple[str, str, dict[str, object], float]:
        """Start one service and bind its readiness to the image and profile."""
        name = f"{self.prefix}-{suffix}"
        self.containers.append(name)
        started = time.monotonic()
        identifier = _docker(
            "run",
            "--detach",
            "--name",
            name,
            "--platform",
            "linux/amd64",
            "--publish",
            "127.0.0.1::8765",
            "--mount",
            f"type=volume,source={self.volume},target=/var/lib/agileforge",
            self.image,
            "serve",
            "--profile",
            profile,
            "--ready-timeout",
            "45",
        ).stdout.strip()
        publication = _docker("port", name, "8765/tcp").stdout.strip()
        _require(
            publication.startswith("127.0.0.1:"), "port is not published on loopback"
        )
        url = f"http://{publication}/api/dashboard/config"
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=2) as response:  # noqa: S310  # nosec B310
                    config = _object(response.read().decode())
                _require(config.get("commit") == self.revision, "wrong image revision")
                _require(
                    config.get("checkout_root") == "/opt/agileforge",
                    "wrong installed root",
                )
                _require(
                    bool(config.get("launch_nonce")), "readiness has no launch nonce"
                )
                _require(
                    bool(config.get("state_id")), "readiness has no state identity"
                )
                expected_root = f"/var/lib/agileforge/profiles/{profile}"
                _require(
                    config.get("business_database")
                    == f"{expected_root}/business.sqlite3"
                    and config.get("trace_database")
                    == f"{expected_root}/adk-trace.sqlite3",
                    "readiness used another profile's databases",
                )
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
            else:
                return identifier, url, config, time.monotonic() - started
        logs = _docker("logs", "--tail", "40", name, check=False)
        message = f"container readiness timed out: {logs.stdout}{logs.stderr}"
        raise RuntimeError(message)

    def stop(self, suffix: str, url: str) -> float:
        """Require graceful container and endpoint termination within a bound."""
        name = f"{self.prefix}-{suffix}"
        started = time.monotonic()
        _docker("stop", "--time", "10", name)
        state = _object(_docker("inspect", "--format", "{{json .State}}", name).stdout)
        _require(state.get("ExitCode") == _SUCCESS, f"unclean container exit: {state}")
        _require(state.get("Running") is False, "container is still running")
        try:
            with urlopen(url, timeout=2):  # noqa: S310  # nosec B310
                pass
        except (URLError, TimeoutError, ConnectionError):
            return time.monotonic() - started
        message = "dashboard endpoint survived container shutdown"
        raise RuntimeError(message)

    def run(self) -> dict[str, object]:
        """Verify refusal, init, CLI, maintenance, restart, backup and restore."""
        _docker("volume", "create", self.volume)
        try:
            missing = self.command("info", "--profile", "source", "--json", check=False)
            _require(
                missing.returncode != _SUCCESS, "missing state was silently accepted"
            )
            missing_service = self.command(
                "serve", "--profile", "source", "--json", check=False
            )
            _require(
                missing_service.returncode != _SUCCESS,
                "service initialized missing state",
            )
            initialized = _object(
                self.command("init", "--profile", "source", "--json").stdout
            )
            state = _mapping(initialized.get("state"))
            build = _mapping(initialized.get("build"))
            _require(
                build.get("revision") == self.revision, "init used wrong image revision"
            )
            self.command("cli", "--profile", "source", "--", "project", "list")
            self.verify_runtime_secret()
            self.seed_artifact()
            first_id, first_url, first, first_start = self.start("first")
            busy = self.command(
                "backup",
                "--profile",
                "source",
                "--destination",
                "/var/lib/agileforge/backups/busy",
                "--json",
                check=False,
            )
            _require(
                busy.returncode != _SUCCESS and "busy" in busy.stdout,
                "backup bypassed live service fence",
            )
            first_stop = self.stop("first", first_url)
            self.command(
                "backup",
                "--profile",
                "source",
                "--destination",
                "/var/lib/agileforge/backups/snapshot",
                "--json",
            )
            restored = _object(
                self.command(
                    "restore",
                    "--profile",
                    "restored",
                    "--bundle",
                    "/var/lib/agileforge/backups/snapshot",
                    "--json",
                ).stdout
            )
            restored_state = _mapping(restored.get("state"))
            _require(
                restored_state.get("state_id") == state.get("state_id"),
                "restore lost state identity",
            )
            self.verify_restored_payload()
            second_id, second_url, second, second_start = self.start(
                "second", profile="restored"
            )
            _require(first_id != second_id, "container was not recreated")
            _require(
                first.get("state_id") == second.get("state_id"),
                "restart changed durable identity",
            )
            _require(
                first.get("launch_nonce") != second.get("launch_nonce"),
                "restart reused launch nonce",
            )
            second_stop = self.stop("second", second_url)
            return {
                "status": "passed",
                "image": self.image,
                "revision": self.revision,
                "state_id": state.get("state_id"),
                "containers": [first_id, second_id],
                "startup_seconds": [first_start, second_start],
                "shutdown_seconds": [first_stop, second_stop],
                "data": "synthetic only",
                "provider_calls": 0,
            }
        finally:
            for name in self.containers:
                with suppress(subprocess.SubprocessError):
                    _docker("rm", "--force", name, check=False)
            _docker("volume", "rm", self.volume, check=False)


def main(argv: Sequence[str] | None = None) -> int:
    """Run an explicit image verification without mounting any host directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expect-sha", required=True)
    arguments = parser.parse_args(argv)
    result = ImageRehearsal(arguments.image, arguments.expect_sha).run()
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
