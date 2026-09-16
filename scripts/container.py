"""Transport a reviewed Git source tree into the pinned Linux containers."""
# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess  # nosec B404
import tarfile
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Sequence


WORKSPACE = "/workspace/repos/agileforge"
DEFAULT_PROJECT_NAME = os.environ.get("AGILEFORGE_CONTAINER_PROJECT", "agileforge")
PIN_PATH = Path("containers/pins.json")
GIT_SHA_LENGTH = 40
SHA256_HEX_LENGTH = 64
REMOTE_TEMPORARY_DIRECTORY = Path(os.sep, "tmp")


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    stdout: BinaryIO | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one fixed-argument command and raise its useful error on failure."""
    if stdout is None:
        return subprocess.run(  # noqa: S603 # nosec B603
            list(arguments),
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
    completed = subprocess.run(  # noqa: S603 # nosec B603
        list(arguments),
        cwd=cwd,
        check=True,
        text=False,
        stdout=stdout,
        stderr=subprocess.PIPE,
        env=env,
    )
    return subprocess.CompletedProcess(
        completed.args, completed.returncode, "", completed.stderr.decode()
    )


def project_resources(project_name: str) -> dict[str, str]:
    """Derive task resources from an explicit, reusable project namespace."""
    permitted = "abcdefghijklmnopqrstuvwxyz0123456789-"
    contains_only_permitted = all(character in permitted for character in project_name)
    if not project_name or not contains_only_permitted:
        raise ValueError(
            "project name must contain lowercase letters, digits, and hyphens"
        )
    return {
        "container": f"{project_name}-dev",
        "workspace_volume": f"{project_name}-workspace",
        "cache_volume": f"{project_name}-cache",
        "production_state_volume": f"{project_name}-production-state",
    }


def _compose_env(project_name: str) -> dict[str, str]:
    """Pass the selected namespace to Compose without mutating host state."""
    environment = os.environ.copy()
    environment["AGILEFORGE_CONTAINER_PROJECT"] = project_name
    return environment


def _git_output(checkout: Path, *arguments: str) -> str:
    """Return one Git query from the selected checkout."""
    completed = _run(("git", *arguments), cwd=checkout)
    return completed.stdout.strip()


def _require_checkout(checkout: Path) -> Path:
    """Resolve and validate the Git checkout selected for export."""
    resolved = checkout.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"checkout is not a directory: {resolved}")
    if _git_output(resolved, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError(f"checkout is not a Git worktree: {resolved}")
    return resolved


def _require_clean(checkout: Path) -> None:
    """Refuse a production export unless its tree is exactly committed."""
    if _git_output(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("production builds require a clean committed checkout")


def _sha256(file_path: Path) -> str:
    """Hash a file without loading its complete contents into memory."""
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_source(checkout: Path, destination: Path) -> str:
    """Export committed files and return the archive content digest."""
    with tempfile.TemporaryFile() as archive_stream:
        _run(
            ("git", "archive", "--format=tar", "HEAD"),
            cwd=checkout,
            stdout=archive_stream,
        )
        archive_stream.seek(0)
        digest = hashlib.sha256()
        while chunk := archive_stream.read(1024 * 1024):
            digest.update(chunk)
        archive_stream.seek(0)
        with tarfile.open(fileobj=archive_stream, mode="r:") as archive:
            archive.extractall(destination, filter="data")
    return digest.hexdigest()


def _build_identity(checkout: Path, source_sha256: str) -> dict[str, str]:
    """Create the versioned identity carried by built images."""
    pins_path = checkout / PIN_PATH
    with pins_path.open("rb") as stream:
        pins = json.load(stream)
    with (checkout / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    project_metadata = project.get("project")
    if not isinstance(project_metadata, dict):
        raise TypeError("pyproject.toml project table is not a mapping")
    package_version = project_metadata.get("version")
    python_version = pins.get("python") if isinstance(pins, dict) else None
    uv_version = (
        pins.get("uv_version", pins.get("uv")) if isinstance(pins, dict) else None
    )
    if (
        not isinstance(package_version, str)
        or not package_version
        or not isinstance(python_version, str)
        or not python_version
        or not isinstance(uv_version, str)
        or not uv_version
    ):
        raise ValueError("build identity requires package, Python, and uv versions")
    revision = _git_output(checkout, "rev-parse", "HEAD")
    if len(revision) != GIT_SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("build identity requires a full Git revision")
    return {
        "schema_version": "agileforge.build.v1",
        "revision": revision,
        "source_sha256": source_sha256,
        "lock_sha256": _sha256(checkout / "uv.lock"),
        "python_version": python_version,
        "uv_version": uv_version,
        "package_version": package_version,
    }


def export_source(
    checkout: Path,
    destination: Path,
    *,
    production: bool = False,
) -> Path:
    """Write a tracked-only build context and immutable build identity file."""
    source = _require_checkout(checkout)
    if production:
        _require_clean(source)
    target = destination.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"build context already exists and is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    source_sha256 = _archive_source(source, target)
    build_json = target / "containers" / "build.json"
    build_json.parent.mkdir(parents=True, exist_ok=True)
    build_json.write_text(
        json.dumps(_build_identity(source, source_sha256), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return target


def build(checkout: Path, *, target: str, tag: str) -> None:
    """Build a target from an explicit Git export, never the working directory."""
    if target not in {"development", "test", "production"}:
        raise ValueError(f"unsupported build target: {target}")
    with tempfile.TemporaryDirectory(
        prefix="agileforge-container-"
    ) as temporary_directory:
        context = export_source(
            checkout,
            Path(temporary_directory) / "context",
            production=target == "production",
        )
        identity_path = context / "containers" / "build.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        _run(
            (
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--file",
                str(context / "Dockerfile"),
                "--target",
                target,
                "--tag",
                tag,
                "--build-arg",
                f"SOURCE_REVISION={identity['revision']}",
                "--build-arg",
                f"SOURCE_SHA256={identity['source_sha256']}",
                "--build-arg",
                f"LOCK_SHA256={identity['lock_sha256']}",
                str(context),
            )
        )


def up(project_name: str) -> None:
    """Start the development service after provisioning its fixed Linux volumes."""
    resources = project_resources(project_name)
    for volume in (resources["workspace_volume"], resources["cache_volume"]):
        _run(("docker", "volume", "create", volume))
    _run(
        (
            "docker",
            "compose",
            "--project-name",
            project_name,
            "up",
            "--detach",
            "development",
        ),
        env=_compose_env(project_name),
    )


def import_source(checkout: Path, project_name: str) -> None:
    """Clone one clean committed Git bundle into an empty Linux workspace."""
    source = _require_checkout(checkout)
    _require_clean(source)
    resources = project_resources(project_name)
    destination = WORKSPACE
    existing = subprocess.run(  # noqa: S603 # nosec B603
        ("docker", "exec", resources["container"], "test", "-e", destination),  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        raise ValueError("refusing to overwrite an existing Linux checkout")
    if existing.returncode != 1:
        raise RuntimeError("could not inspect the Linux workspace")
    revision = _git_output(source, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(
        prefix="agileforge-bundle-"
    ) as temporary_directory:
        bundle = Path(temporary_directory) / "source.bundle"
        remote_directory = _run(
            ("docker", "exec", resources["container"], "mktemp", "--directory")
        ).stdout.strip()
        if Path(remote_directory).parent != REMOTE_TEMPORARY_DIRECTORY:
            raise RuntimeError("could not create a private Linux bundle directory")
        remote_bundle = f"{remote_directory}/source.bundle"
        try:
            _run(("git", "bundle", "create", str(bundle), "HEAD"), cwd=source)
            _run(
                (
                    "docker",
                    "cp",
                    str(bundle),
                    f"{resources['container']}:{remote_bundle}",
                )
            )
            _run(
                (
                    "docker",
                    "exec",
                    resources["container"],
                    "mkdir",
                    "-p",
                    "/workspace/repos",
                )
            )
            _run(
                (
                    "docker",
                    "exec",
                    resources["container"],
                    "git",
                    "clone",
                    remote_bundle,
                    destination,
                )
            )
            _run(
                (
                    "docker",
                    "exec",
                    "--workdir",
                    destination,
                    resources["container"],
                    "git",
                    "remote",
                    "remove",
                    "origin",
                )
            )
        finally:
            _run(
                (
                    "docker",
                    "exec",
                    resources["container"],
                    "rm",
                    "-rf",
                    "--",
                    remote_directory,
                )
            )
    copied_revision = _run(
        (
            "docker",
            "exec",
            "--workdir",
            destination,
            resources["container"],
            "git",
            "rev-parse",
            "HEAD",
        )
    ).stdout.strip()
    if copied_revision != revision:
        raise RuntimeError("Linux checkout revision does not match the selected source")


def _maintenance_consumers(resources: dict[str, str]) -> list[str]:
    """List running containers with writable selected durable-volume mounts."""
    running = _run(("docker", "ps", "--quiet")).stdout.splitlines()
    if not running:
        return []
    inspected = _run(("docker", "inspect", *running)).stdout
    payload = json.loads(inspected)
    if not isinstance(payload, list):
        raise TypeError("Docker inspect did not return a list")
    protected = {
        resources["workspace_volume"],
        resources["production_state_volume"],
    }
    consumers: list[str] = []
    for container in payload:
        if not isinstance(container, dict):
            continue
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            continue
        writes_selected_volume = any(
            isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Name") in protected
            and mount.get("RW") is True
            for mount in mounts
        )
        if writes_selected_volume:
            name = container.get("Name")
            consumers.append(str(name).lstrip("/"))
    return sorted(consumers)


def _forward(arguments: Sequence[str]) -> None:
    """Run an explicit command while preserving its stdout and stderr."""
    subprocess.run(list(arguments), check=True)  # noqa: S603 # nosec B603


def _maintenance_runtime_command(command: Sequence[str]) -> tuple[str, ...]:
    """Accept only installed-runtime commands that perform maintenance."""
    runtime_command = tuple(command)
    while runtime_command[:1] == ("--",):
        runtime_command = runtime_command[1:]
    if runtime_command[:1] not in {("init",), ("backup",), ("restore",)}:
        raise ValueError("production maintenance accepts only init, backup, or restore")
    return runtime_command


def _resolved_image(image: str) -> str:
    """Resolve a reviewed image reference to its immutable local image ID."""
    resolved = _run(
        ("docker", "image", "inspect", "--format", "{{.Id}}", image)
    ).stdout.strip()
    digest = resolved.removeprefix("sha256:")
    if len(digest) != SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise RuntimeError("could not resolve the production image to an exact SHA-256")
    return resolved


def production_maintenance(
    project_name: str,
    image: str,
    command: Sequence[str],
) -> None:
    """Run one fenced installed-runtime maintenance command in a fresh container."""
    runtime_command = _maintenance_runtime_command(command)
    resources = project_resources(project_name)
    consumers = _maintenance_consumers(resources)
    if consumers:
        names = ", ".join(consumers)
        raise RuntimeError(f"maintenance refused; writable volume consumers: {names}")
    resolved_image = _resolved_image(image)
    _forward(
        (
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--platform",
            "linux/amd64",
            "--mount",
            f"type=volume,source={resources['workspace_volume']},target=/workspace",
            "--mount",
            "type=volume,source="
            f"{resources['production_state_volume']},target=/var/lib/agileforge",
            resolved_image,
            *runtime_command,
        )
    )


def exec_command(
    project_name: str,
    command: Sequence[str],
    *,
    maintenance: bool = False,
) -> None:
    """Run one non-interactive development command at the stable workspace path."""
    command = tuple(command)
    while command[:1] == ("--",):
        command = command[1:]
    if not command:
        raise ValueError("exec requires a command")
    resources = project_resources(project_name)
    if maintenance:
        consumers = _maintenance_consumers(resources)
        if consumers:
            names = ", ".join(consumers)
            message = f"maintenance refused; writable volume consumers: {names}"
            raise RuntimeError(message)
        image = _run(
            ("docker", "inspect", "--format", "{{.Image}}", resources["container"])
        ).stdout.strip()
        if not image.startswith("sha256:"):
            raise RuntimeError("could not resolve the stopped development image")
        _forward(
            (
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--platform",
                "linux/amd64",
                "--workdir",
                WORKSPACE,
                "--mount",
                f"type=volume,source={resources['workspace_volume']},target=/workspace",
                "--mount",
                f"type=volume,source={resources['cache_volume']},target=/cache",
                image,
                *command,
            )
        )
        return
    _forward(
        (
            "docker",
            "exec",
            "--interactive",
            "--workdir",
            WORKSPACE,
            resources["container"],
            "python",
            "scripts/container_exec.py",
            "--",
            *command,
        )
    )


def stop(project_name: str) -> None:
    """Stop the task-owned development container without deleting its volumes."""
    _run(
        ("docker", "compose", "--project-name", project_name, "stop", "development"),
        env=_compose_env(project_name),
    )


def metadata(checkout: Path, project_name: str) -> dict[str, object]:
    """Return non-secret platform and source identity for operator records."""
    source = _require_checkout(checkout)
    with (source / PIN_PATH).open(encoding="utf-8") as stream:
        pins = json.load(stream)
    resources = project_resources(project_name)
    return {
        "project_name": project_name,
        "container": resources["container"],
        "workspace": WORKSPACE,
        "workspace_volume": resources["workspace_volume"],
        "cache_volume": resources["cache_volume"],
        "revision": _git_output(source, "rev-parse", "HEAD"),
        "pins": pins,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    subparsers = parser.add_subparsers(dest="action", required=True)
    export_parser = subparsers.add_parser("export-source")
    export_parser.add_argument("--destination", type=Path, required=True)
    export_parser.add_argument("--production", action="store_true")
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument(
        "--target",
        required=True,
        choices=("development", "test", "production"),
    )
    build_parser.add_argument("--tag", required=True)
    subparsers.add_parser("up")
    subparsers.add_parser("import-source")
    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--maintenance", action="store_true")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    production_maintenance_parser = subparsers.add_parser("production-maintenance")
    production_maintenance_parser.add_argument("--image", required=True)
    production_maintenance_parser.add_argument("command", nargs=argparse.REMAINDER)
    subparsers.add_parser("stop")
    subparsers.add_parser("metadata")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch the explicit host-to-container transport commands."""
    parsed = _parser().parse_args(arguments)
    if parsed.action == "export-source":
        print(  # noqa: T201
            export_source(
                parsed.checkout,
                parsed.destination,
                production=parsed.production,
            )
        )
    elif parsed.action == "build":
        build(parsed.checkout, target=parsed.target, tag=parsed.tag)
    elif parsed.action == "up":
        up(parsed.project_name)
    elif parsed.action == "import-source":
        import_source(parsed.checkout, parsed.project_name)
    elif parsed.action == "exec":
        exec_command(
            parsed.project_name,
            parsed.command,
            maintenance=parsed.maintenance,
        )
    elif parsed.action == "production-maintenance":
        production_maintenance(parsed.project_name, parsed.image, parsed.command)
    elif parsed.action == "stop":
        stop(parsed.project_name)
    else:
        print(  # noqa: T201
            json.dumps(
                metadata(parsed.checkout, parsed.project_name),
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
