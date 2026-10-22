"""Architecture selection must remain explicit at build and durable-state entry."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import container

ARGUMENT_ERROR = 2


@pytest.mark.parametrize("platform", [None, "linux/amd64", "linux/arm64"])
def test_production_build_selects_matching_pinned_bases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str | None
) -> None:
    """An ARM image cannot copy AMD64-only base binaries from the old pins."""
    context = tmp_path / "context"
    (context / "containers").mkdir(parents=True)
    (context / "containers/build.json").write_text(
        json.dumps(
            {"revision": "a" * 40, "source_sha256": "b" * 64, "lock_sha256": "c" * 64}
        )
    )
    expected_platform = platform or "linux/amd64"
    pins = {
        "production_images": {
            expected_platform: {
                "ubuntu": "ubuntu:24.04@sha256:ubuntu-pin",
                "uv": "uv:0.12.8@sha256:uv-pin",
            }
        }
    }
    (context / "containers/pins.json").write_text(json.dumps(pins))
    commands = []
    monkeypatch.setattr(container, "export_source", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        container, "_run", lambda argv, **_kwargs: commands.append(tuple(argv))
    )

    arguments = [
        "--checkout",
        str(tmp_path),
        "build",
        "--target",
        "production",
        "--tag",
        "fixture:prod",
    ]
    if platform is not None:
        arguments += ["--platform", platform]
    container.main(arguments)

    command = commands[0]
    assert command[command.index("--platform") + 1] == expected_platform
    assert "UBUNTU_IMAGE=ubuntu:24.04@sha256:ubuntu-pin" in command
    assert "UV_IMAGE=uv:0.12.8@sha256:uv-pin" in command


@pytest.mark.parametrize("target", ["development", "test"])
def test_arm64_build_refuses_unqualified_nonproduction_targets(
    tmp_path: Path, target: str
) -> None:
    """The x64 Node toolchain cannot be advertised as a native ARM image."""
    with pytest.raises(ValueError, match=r"ARM64.*production"):
        container.build(
            tmp_path, target=target, tag="fixture:bad", platform="linux/arm64"
        )


def test_maintenance_rejects_emulated_image_before_durable_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requesting ARM64 cannot silently emulate an AMD64 maintenance binary."""
    commands = []
    monkeypatch.setattr(container, "_maintenance_consumers", lambda _resources: [])
    monkeypatch.setattr(container, "_forward", commands.append)
    monkeypatch.setattr(
        container,
        "_run",
        lambda argv, **_kwargs: SimpleNamespace(
            stdout="linux/amd64"
            if "{{.Os}}/{{.Architecture}}" in argv
            else "sha256:" + "a" * 64
        ),
    )
    with pytest.raises(ValueError, match=r"platform.*mismatch"):
        container.production_maintenance(
            "fixture", "fixture:old", ("backup",), platform="linux/arm64"
        )
    assert commands == []


def test_arm64_maintenance_uses_immutable_native_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native model/state maintenance retains the exact image and consumer fence."""
    commands = []
    monkeypatch.setattr(container, "_maintenance_consumers", lambda _resources: [])
    monkeypatch.setattr(
        container, "_forward", lambda argv: commands.append(tuple(argv))
    )
    monkeypatch.setattr(
        container,
        "_run",
        lambda argv, **_kwargs: SimpleNamespace(
            stdout="linux/arm64"
            if "{{.Os}}/{{.Architecture}}" in argv
            else "sha256:" + "a" * 64
        ),
    )
    container.main(
        [
            "--project-name",
            "fixture",
            "production-maintenance",
            "--platform",
            "linux/arm64",
            "--image",
            "fixture:native",
            "--",
            "backup",
            "--profile",
            "default",
            "--destination",
            "/workspace/backup",
        ]
    )
    command = commands[0]
    assert command[command.index("--platform") + 1] == "linux/arm64"
    assert "sha256:" + "a" * 64 in command
    assert (
        "type=volume,source=fixture-production-state,target=/var/lib/agileforge"
        in command
    )
    monkeypatch.setattr(
        container, "_maintenance_consumers", lambda _resources: ["live"]
    )
    with pytest.raises(RuntimeError, match="live"):
        container.production_maintenance(
            "fixture", "fixture:native", ("backup",), platform="linux/arm64"
        )


@pytest.mark.parametrize("action", ["build", "production-maintenance"])
def test_invalid_platform_rejected_before_docker(action: str) -> None:
    """Only the two reviewed Linux architectures are admitted."""
    arguments = [action, "--platform", "linux/riscv64"]
    if action == "build":
        arguments += ["--target", "production", "--tag", "fixture:bad"]
    else:
        arguments += ["--image", "fixture:bad", "--", "backup"]
    with pytest.raises(SystemExit) as result:
        container.main(arguments)
    assert result.value.code == ARGUMENT_ERROR
