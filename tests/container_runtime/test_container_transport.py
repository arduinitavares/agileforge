"""Controller maintenance can run safely after ordinary consumers stop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import container


def test_maintenance_uses_one_shot_container_after_consumers_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maintenance cannot depend on docker exec into a stopped service."""
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(container, "_maintenance_consumers", lambda _resources: [])
    monkeypatch.setattr(
        container,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="sha256:reviewed-image\n"),
    )
    monkeypatch.setattr(
        container, "_forward", lambda argv: commands.append(tuple(argv))
    )
    container.main(
        [
            "--project-name",
            "synthetic",
            "exec",
            "--maintenance",
            "--",
            "./agileforge-dev",
            "verify-backup",
            "--bundle",
            "/backup",
        ]
    )
    command = commands[0]
    assert command[:2] == ("docker", "run")
    assert "--rm" in command
    assert "sha256:reviewed-image" in command
    assert "type=volume,source=synthetic-workspace,target=/workspace" in command
    assert "scripts/container_exec.py" not in command
    assert command[-4:] == ("./agileforge-dev", "verify-backup", "--bundle", "/backup")


def test_maintenance_refuses_unknown_writable_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external writer must be stopped before starting a maintenance worker."""
    monkeypatch.setattr(
        container, "_maintenance_consumers", lambda _resources: ["unknown-writer"]
    )
    with pytest.raises(RuntimeError, match="unknown-writer"):
        container.exec_command("synthetic", ("backup",), maintenance=True)


def test_normal_transport_keeps_stdin_available_for_reviewed_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent commands can receive a file edit through stdin under the shared lock."""
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        container, "_forward", lambda argv: commands.append(tuple(argv))
    )
    container.exec_command("synthetic", ("python", "-"))
    assert "--interactive" in commands[0]
    assert "scripts/container_exec.py" in commands[0]


def test_production_maintenance_transports_only_the_resolved_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutable image tag cannot be the image that receives durable-state access."""
    commands: list[tuple[str, ...]] = []
    inspections: list[tuple[str, ...]] = []
    resolved_image = f"sha256:{'a' * 64}"

    monkeypatch.setattr(container, "_maintenance_consumers", lambda _resources: [])

    def inspect_image(arguments: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        inspections.append(arguments)
        return SimpleNamespace(stdout=f"{resolved_image}\n")

    monkeypatch.setattr(container, "_run", inspect_image)
    monkeypatch.setattr(
        container, "_forward", lambda argv: commands.append(tuple(argv))
    )

    container.main(
        [
            "--project-name",
            "synthetic",
            "production-maintenance",
            "--image",
            "agileforge-production:reviewed",
            "--",
            "backup",
            "--profile",
            "demo",
            "--destination",
            "/workspace/backups/demo",
        ]
    )

    assert commands == [
        (
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--platform",
            "linux/amd64",
            "--mount",
            "type=volume,source=synthetic-workspace,target=/workspace",
            "--mount",
            "type=volume,source=synthetic-production-state,target=/var/lib/agileforge",
            resolved_image,
            "backup",
            "--profile",
            "demo",
            "--destination",
            "/workspace/backups/demo",
        )
    ]
    assert inspections == [
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "agileforge-production:reviewed",
        )
    ]


def test_production_maintenance_refuses_unknown_writable_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer of either durable volume blocks a production maintenance launch."""
    monkeypatch.setattr(
        container, "_maintenance_consumers", lambda _resources: ["unknown-writer"]
    )

    with pytest.raises(RuntimeError, match="unknown-writer"):
        container.production_maintenance(
            "synthetic", "sha256:reviewed-image", ("backup",)
        )


@pytest.mark.parametrize("action", ["configure-models", "recover-models"])
def test_model_maintenance_uses_exact_image_and_writable_consumer_check(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Both model commands pass through the production maintenance transport."""
    commands: list[tuple[str, ...]] = []
    resolved = f"sha256:{'a' * 64}"
    monkeypatch.setattr(container, "_maintenance_consumers", lambda _resources: [])
    monkeypatch.setattr(
        container, "_run", lambda *_args, **_kwargs: SimpleNamespace(stdout=resolved)
    )
    monkeypatch.setattr(
        container, "_forward", lambda argv: commands.append(tuple(argv))
    )
    container.production_maintenance(
        "synthetic", "agileforge-production:reviewed", (action, "--profile", "default")
    )
    assert commands[0][-3:] == (action, "--profile", "default")
    assert resolved in commands[0]
    assert "type=volume,source=synthetic-workspace,target=/workspace" in commands[0]
    monkeypatch.setattr(
        container, "_maintenance_consumers", lambda _resources: ["active-writer"]
    )
    with pytest.raises(RuntimeError, match="active-writer"):
        container.production_maintenance(
            "synthetic", "agileforge-production:reviewed", (action,)
        )


def test_production_maintenance_rejects_non_maintenance_runtime_command() -> None:
    """The controller cannot start the installed service through maintenance mode."""
    with pytest.raises(ValueError, match="init, backup, restore"):
        container.production_maintenance(
            "synthetic", "sha256:reviewed-image", ("serve", "--profile", "demo")
        )
