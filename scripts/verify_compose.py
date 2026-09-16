"""Rehearse the normal Docker Compose production lifecycle with synthetic state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_SUCCESS = 0
_STARTUP_TIMEOUT = 60.0
_PROFILE = "default"


def _run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker CLI is unavailable"
        raise FileNotFoundError(message)
    return subprocess.run(  # noqa: S603 # nosec B603
        (docker, *arguments),
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=None if environment is None else dict(environment),
    )


def _object(payload: str, *, label: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    start = payload.find("{")
    if start < 0:
        message = f"{label} did not return JSON"
        raise TypeError(message)
    value, _ = decoder.raw_decode(payload[start:])
    if not isinstance(value, dict):
        message = f"{label} did not return a JSON object"
        raise TypeError(message)
    return cast("dict[str, object]", value)


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        message = f"{label} is missing"
        raise TypeError(message)
    return cast("dict[str, object]", value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _compose_environment(image: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "AGILEFORGE_CONTAINER_PROJECT",
        "AGILEFORGE_PORT",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
    ):
        environment.pop(name, None)
    environment["AGILEFORGE_PRODUCTION_IMAGE"] = image
    environment["AGILEFORGE_PORT"] = "0"
    return environment


@dataclass
class ComposeRehearsal:
    """Own one disposable Compose project and prove its installed lifecycle."""

    image: str
    revision: str
    compose_file: Path
    project: str = field(
        default_factory=lambda: f"agileforge-compose-verify-{uuid4().hex[:12]}"
    )
    environment: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        """Resolve the reviewed configuration and isolate the test namespace."""
        self.compose_file = self.compose_file.resolve(strict=True)
        _require(
            self.project.startswith("agileforge-compose-verify-"),
            "refusing a non-verification Compose project",
        )
        self.environment = _compose_environment(self.image)

    @property
    def volumes(self) -> tuple[str, str, str]:
        """Name only disposable resources reserved by this rehearsal."""
        return (
            f"{self.project}-production-state",
            f"{self.project}-workspace",
            f"{self.project}-cache",
        )

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        timeout: float = 90.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run a bounded Compose operation against the explicit configuration."""
        return _run(
            (
                "compose",
                "-p",
                self.project,
                "-f",
                str(self.compose_file),
                *arguments,
            ),
            environment=self.environment,
            check=check,
            timeout=timeout,
        )

    def runtime(
        self, *arguments: str, check: bool = True, timeout: float = 90.0
    ) -> subprocess.CompletedProcess[str]:
        """Invoke the installed entrypoint in a disposable service container."""
        return self.compose(
            "run", "--rm", "production", *arguments, check=check, timeout=timeout
        )

    def _assert_fresh_namespace(self) -> None:
        for volume in self.volumes:
            existing = _run(("volume", "inspect", volume), check=False)
            _require(existing.returncode != _SUCCESS, "verification namespace exists")
        containers = self.compose("ps", "--all", "--quiet").stdout.strip()
        _require(not containers, "verification project already has containers")

    def _assert_default_service_selection(self) -> None:
        configured = _object(
            self.compose("config", "--format", "json").stdout,
            label="Compose config",
        )
        services = _mapping(configured.get("services"), label="Compose services")
        _require(set(services) == {"production"}, "default Compose services changed")
        production = _mapping(services.get("production"), label="production service")
        _require(production.get("image") == self.image, "unexpected Compose image")
        mounts = production.get("volumes")
        _require(isinstance(mounts, list), "production mounts are missing")
        actual: dict[str, object] = {}
        for raw in cast("list[object]", mounts):
            mount = _mapping(raw, label="production mount")
            _require(mount.get("type") == "volume", "unexpected host-state mount")
            target = str(mount.get("target"))
            _require(target not in actual, "duplicate production mount target")
            actual[target] = mount.get("source")
        _require(
            actual
            == {
                "/var/lib/agileforge": "production-state",
                "/workspace": "workspace",
            },
            "unexpected production mount layout",
        )
        volumes = _mapping(configured.get("volumes"), label="Compose volumes")
        _require(
            set(volumes) == {"production-state", "workspace"}, "unexpected volumes"
        )
        for name, raw in volumes.items():
            volume = _mapping(raw, label="Compose volume")
            _require(
                volume.get("name") == f"{self.project}-{name}"
                and not volume.get("external")
                and not volume.get("driver_opts")
                and volume.get("driver", "local") == "local",
                "Compose volume escapes the disposable project",
            )

    def _assert_created_volumes(self) -> None:
        for volume in self.volumes[:2]:
            created = _run(("volume", "inspect", volume), check=False)
            _require(created.returncode == _SUCCESS, "Compose did not create a volume")

    def _readiness(self) -> tuple[str, dict[str, object]]:
        publication = self.compose("port", "production", "8765").stdout.strip()
        _require(publication.startswith("127.0.0.1:"), "port is not loopback-only")
        url = f"http://{publication}/api/dashboard/config"
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=2) as response:  # noqa: S310 # nosec B310
                    config = _object(response.read().decode(), label="dashboard config")
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
                continue
            _require(config.get("commit") == self.revision, "wrong image revision")
            _require(
                config.get("checkout_root") == "/opt/agileforge",
                "dashboard is not using the installed runtime",
            )
            _require(bool(config.get("launch_nonce")), "readiness has no launch nonce")
            _require(bool(config.get("state_id")), "readiness has no state identity")
            root = f"/var/lib/agileforge/profiles/{_PROFILE}"
            _require(
                config.get("business_database") == f"{root}/business.sqlite3"
                and config.get("trace_database") == f"{root}/adk-trace.sqlite3",
                "readiness did not select the default profile",
            )
            return url, config
        message = "Compose dashboard did not become ready within the bound"
        raise RuntimeError(message)

    def _start(self) -> tuple[str, dict[str, object]]:
        self.compose("up", "-d")
        running = {
            name
            for name in self.compose(
                "ps", "--services", "--status", "running"
            ).stdout.splitlines()
            if name
        }
        _require(running == {"production"}, "Compose started an unexpected service")
        return self._readiness()

    def _stop(self, url: str) -> None:
        self.compose("stop", "--timeout", "10")
        stopped = self.compose("ps", "--all", "--quiet", "production")
        container_ids = stopped.stdout.split()
        _require(
            len(container_ids) == 1,
            "production container is not uniquely stopped",
        )
        state = _object(
            _run(("inspect", "--format", "{{json .State}}", container_ids[0])).stdout,
            label="production container state",
        )
        _require(state.get("Running") is False, "production container is still running")
        _require(state.get("ExitCode") == _SUCCESS, "production stopped uncleanly")
        try:
            with urlopen(url, timeout=2):  # noqa: S310 # nosec B310
                pass
        except (URLError, TimeoutError, ConnectionError):
            return
        message = "dashboard endpoint survived Compose stop"
        raise RuntimeError(message)

    def _seed_artifact(self) -> None:
        script = """
from pathlib import Path
from utils.runtime_fence import runtime_fence
root = Path('/var/lib/agileforge')
artifact = root / 'profiles/default/artifacts/compose-canary.bin'
with runtime_fence(root, exclusive=True):
    artifact.write_bytes(b'synthetic-compose-artifact\\n')
"""
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/build/.venv/bin/python",
            "production",
            "-c",
            script,
        )

    def _assert_artifact(self) -> None:
        script = """
from pathlib import Path
from utils.runtime_fence import runtime_fence
root = Path('/var/lib/agileforge')
with runtime_fence(root):
    actual = (root / 'profiles/default/artifacts/compose-canary.bin').read_bytes()
raise SystemExit(0 if actual == b'synthetic-compose-artifact\\n' else 1)
"""
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/build/.venv/bin/python",
            "production",
            "-c",
            script,
        )

    def _cleanup(self) -> None:
        with suppress(subprocess.SubprocessError):
            self.compose("down", "--volumes", "--remove-orphans", check=False)
        for volume in self.volumes:
            with suppress(subprocess.SubprocessError):
                _run(("volume", "rm", volume), check=False)

    def run(self) -> dict[str, object]:
        """Check first launch, refusal, shutdown, and persistent recreation."""
        self._assert_fresh_namespace()
        self._assert_default_service_selection()
        try:
            missing_info = self.runtime(
                "info", "--profile", _PROFILE, "--json", check=False
            )
            _require(missing_info.returncode != _SUCCESS, "info accepted missing state")
            missing_serve = self.runtime(
                "serve",
                "--profile",
                _PROFILE,
                "--ready-timeout",
                "5",
                check=False,
                timeout=20,
            )
            _require(
                missing_serve.returncode != _SUCCESS,
                "serve initialized missing state",
            )
            initialized = _object(
                self.runtime("init", "--profile", _PROFILE, "--json").stdout,
                label="initialization",
            )
            build = _mapping(initialized.get("build"), label="initialization build")
            state = _mapping(initialized.get("state"), label="initialization state")
            state_id = state.get("state_id")
            _require(
                build.get("revision") == self.revision,
                "init used wrong image revision",
            )
            _require(state.get("profile_name") == _PROFILE, "init used another profile")
            _require(
                isinstance(state_id, str) and bool(state_id),
                "init omitted state identity",
            )
            self._assert_created_volumes()

            first_url, first = self._start()
            busy = self.runtime("init", "--profile", _PROFILE, "--json", check=False)
            _require(
                busy.returncode != _SUCCESS
                and "busy" in (busy.stdout + busy.stderr).lower(),
                "init bypassed the running service fence",
            )
            self._stop(first_url)
            repeated = self.runtime(
                "init", "--profile", _PROFILE, "--json", check=False
            )
            _require(
                repeated.returncode != _SUCCESS,
                "init overwrote stopped durable state",
            )
            self._seed_artifact()
            self.compose("down")

            second_url, second = self._start()
            _require(
                second.get("state_id") == state_id,
                "Compose restart changed state identity",
            )
            _require(
                second.get("launch_nonce") != first.get("launch_nonce"),
                "Compose restart reused launch nonce",
            )
            self._assert_artifact()
            self._stop(second_url)
            return {
                "status": "passed",
                "project": self.project,
                "image": self.image,
                "revision": self.revision,
                "state_id": state_id,
                "data": "synthetic only",
                "provider_calls": 0,
            }
        finally:
            self._cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    """Verify a supplied image without mounting any existing host state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expect-sha", required=True)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "compose.yaml",
    )
    arguments = parser.parse_args(argv)
    result = ComposeRehearsal(
        image=arguments.image,
        revision=arguments.expect_sha,
        compose_file=arguments.compose_file,
    ).run()
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
    return _SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
