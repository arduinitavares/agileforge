"""Production rehearsals must exercise the selected image architecture."""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from scripts import verify_compose, verify_container

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
PROJECT = "agileforge-compose-verify-platform-test"
ARGUMENT_ERROR = 2
IMAGE_RUN_PATH_COUNT: int = 5


class _DockerBoundaryReachedError(RuntimeError):
    """Stop the real CLI before creating any external test resources."""


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (None, "linux/amd64"),
        ("linux/amd64", "linux/amd64"),
        ("linux/arm64", "linux/arm64"),
    ],
)
def test_all_image_run_paths_use_selected_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str | None, expected: str
) -> None:
    """Catch any helper or service path retaining an AMD64-only argument."""
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, **_kwargs: object) -> SimpleNamespace:
        calls.append(arguments)
        output = "127.0.0.1:18765" if arguments[0] == "port" else "container-id"
        return SimpleNamespace(args=arguments, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(verify_container, "_docker", docker)
    config = {
        "commit": REVISION,
        "checkout_root": "/opt/agileforge",
        "launch_nonce": "fresh",
        "state_id": "synthetic",
        "business_database": "/var/lib/agileforge/profiles/source/business.sqlite3",
        "trace_database": "/var/lib/agileforge/profiles/source/adk-trace.sqlite3",
    }
    monkeypatch.setattr(
        verify_container,
        "urlopen",
        lambda *_args, **_kwargs: BytesIO(json.dumps(config).encode()),
    )
    rehearsal = verify_container.ImageRehearsal("fixture:prod", REVISION)
    if platform is not None:
        rehearsal.platform = platform
    rehearsal.command("info", "--profile", "source", "--json")
    rehearsal.seed_artifact()
    rehearsal.verify_runtime_secret()
    rehearsal.verify_restored_payload()
    rehearsal.start("case")

    runs = [call for call in calls if call[0] == "run"]
    assert len(runs) == IMAGE_RUN_PATH_COUNT
    for call in runs:
        assert call[call.index("--platform") + 1] == expected
        assert (
            f"type=volume,source={rehearsal.volume},target=/var/lib/agileforge" in call
        )
        if "--detach" in call:
            assert call[call.index("--publish") + 1] == "127.0.0.1::8765"
        else:
            assert call[call.index("--network") + 1] == "none"


@pytest.mark.parametrize("kind", ["image", "compose"])
@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (None, "linux/amd64"),
        ("linux/amd64", "linux/amd64"),
        ("linux/arm64", "linux/arm64"),
    ],
)
def test_rehearsal_cli_propagates_platform_to_real_docker_boundary(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    platform: str | None,
    expected: str,
) -> None:
    """Exercise real parsing/construction; ambient Compose settings cannot win."""
    monkeypatch.setenv("AGILEFORGE_PRODUCTION_PLATFORM", "linux/arm64")
    selected: list[str] = []

    def docker(*arguments: str, **_kwargs: object) -> SimpleNamespace:
        if arguments[0] == "run":
            selected.append(arguments[arguments.index("--platform") + 1])
            raise _DockerBoundaryReachedError
        return SimpleNamespace(args=arguments, returncode=0, stdout="", stderr="")

    def compose(
        arguments: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> SimpleNamespace:
        if "config" in arguments:
            assert environment is not None
            selected.append(environment["AGILEFORGE_PRODUCTION_PLATFORM"])
            raise _DockerBoundaryReachedError
        code = 1 if arguments[:2] == ("volume", "inspect") else 0
        return SimpleNamespace(args=arguments, returncode=code, stdout="", stderr="")

    monkeypatch.setattr(verify_container, "_docker", docker)
    monkeypatch.setattr(verify_compose, "_run", compose)
    arguments = ["--image", "fixture:prod", "--expect-sha", REVISION]
    if platform is not None:
        arguments += ["--platform", platform]
    module = verify_container if kind == "image" else verify_compose
    with pytest.raises(_DockerBoundaryReachedError):
        module.main(arguments)
    assert selected == [expected]
    assert os.environ["AGILEFORGE_PRODUCTION_PLATFORM"] == "linux/arm64"


@pytest.mark.parametrize(
    ("selected", "effective", "accepted"),
    [
        ("linux/amd64", "linux/amd64", True),
        ("linux/arm64", "linux/arm64", True),
        ("linux/amd64", "linux/arm64", False),
        ("linux/arm64", "linux/amd64", False),
    ],
)
def test_compose_rehearsal_requires_effective_selected_platform(
    monkeypatch: pytest.MonkeyPatch, selected: str, effective: str, accepted: bool
) -> None:
    """Refuse an override selecting another architecture before starting a service."""
    configured = {
        "services": {
            "production": {
                "image": "fixture:prod",
                "platform": effective,
                "volumes": [
                    {
                        "type": "volume",
                        "source": "production-state",
                        "target": "/var/lib/agileforge",
                    },
                    {"type": "volume", "source": "workspace", "target": "/workspace"},
                ],
            }
        },
        "volumes": {
            "production-state": {"name": PROJECT + "-production-state"},
            "workspace": {"name": PROJECT + "-workspace"},
        },
    }
    monkeypatch.setattr(
        verify_compose,
        "_run",
        lambda argv, **_kwargs: SimpleNamespace(
            args=argv, returncode=0, stdout=json.dumps(configured), stderr=""
        ),
    )
    rehearsal = verify_compose.ComposeRehearsal(
        "fixture:prod",
        REVISION,
        ROOT / "compose.yaml",
        project=PROJECT,
        platform=selected,
    )
    if accepted:
        rehearsal._assert_default_service_selection()
    else:
        with pytest.raises(RuntimeError, match="platform"):
            rehearsal._assert_default_service_selection()


@pytest.mark.parametrize("kind", ["image", "compose"])
def test_rehearsal_cli_rejects_unsupported_platform_before_docker(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """An unsupported selection must fail without allocating external state."""
    calls: list[object] = []
    monkeypatch.setattr(
        verify_container, "_docker", lambda *args, **_kwargs: calls.append(args)
    )
    monkeypatch.setattr(
        verify_compose, "_run", lambda *args, **_kwargs: calls.append(args)
    )
    module = verify_container if kind == "image" else verify_compose
    with pytest.raises(SystemExit) as error:
        module.main(
            [
                "--image",
                "fixture:prod",
                "--expect-sha",
                REVISION,
                "--platform",
                "linux/riscv64",
            ]
        )
    assert error.value.code == ARGUMENT_ERROR
    assert calls == []


@pytest.mark.parametrize("kind", ["image", "compose"])
def test_direct_rehearsal_rejects_unsupported_platform(kind: str) -> None:
    """Programmatic callers receive the same validation before run can allocate."""
    if kind == "image":
        with pytest.raises(ValueError, match="platform"):
            verify_container.ImageRehearsal(
                "fixture:prod", REVISION, platform="linux/riscv64"
            )
    else:
        with pytest.raises(ValueError, match="platform"):
            verify_compose.ComposeRehearsal(
                "fixture:prod",
                REVISION,
                ROOT / "compose.yaml",
                platform="linux/riscv64",
            )
