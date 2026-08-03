"""Build and smoke-test isolated AgileForge wheel and source distributions."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.error import URLError
from urllib.request import urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REQUIRED_ARCHIVE_RESOURCES: frozenset[str] = frozenset(
    {
        "config/models.yaml",
        "frontend/index.html",
        "frontend/project.html",
        "frontend/app.js",
        "frontend/project.js",
    }
)
EXPECTED_BUSINESS_TABLES: frozenset[str] = frozenset(
    {"projects", "spec_registry", "workflow_events"}
)
FORBIDDEN_BUSINESS_TABLES: frozenset[str] = frozenset(
    {"products", "sessions", "cli_" + "mutation" + "_ledger"}
)
_PASSTHROUGH_ENVIRONMENT = (
    "PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_API_READY_TIMEOUT_SECONDS = 30.0
_API_STOP_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
_HTTP_TIMEOUT_SECONDS = 1.0
_PACKAGE_NAME = "agileforge"
_RESOURCE_PROBE = """
from importlib.resources import files
from cli.main import build_parser
from utils.model_config import get_story_pipeline_mode

assert files("config").joinpath("models.yaml").is_file()
frontend = files("frontend")
for name in ("index.html", "project.html", "app.js", "project.js"):
    assert frontend.joinpath(name).is_file(), name
assert get_story_pipeline_mode() in {"batch", "single"}
parser = build_parser()
next_args = parser.parse_args(
    ["workflow", "next", "--project-id", "1"]
)
assert next_args.group == "workflow"
assert next_args.workflow_action == "next"
assert next_args.project_id == 1
assert next_args.command_handler.__name__ == "_workflow_next"
position_args = parser.parse_args(
    ["workflow", "position", "--project-id", "1"]
)
assert position_args.group == "workflow"
assert position_args.workflow_action == "position"
assert position_args.project_id == 1
assert position_args.include_optional is False
assert position_args.command_handler.__name__ == "_workflow_position"
transition_args = parser.parse_args(
    ["project", "abandon", "--project-id", "1", "--graph-version", "graph-v1",
     "--expected-fact-fingerprint", "f" * 64,
     "--expected-decision-fingerprint", "d" * 64,
     "--idempotency-key", "distribution-probe", "--changed-by", "quality-gate",
     "--request-file", "request.json"]
)
assert transition_args.group == "project"
assert transition_args.project_action == "abandon"
assert transition_args.request_kind == "abandon_project_shell"
assert transition_args.instance_key is None
assert transition_args.correlation_id is None
assert transition_args.command_handler.__name__ == "_run_transition"
"""


class DistributionVerificationError(RuntimeError):
    """A built artifact failed an isolated verification boundary."""


@dataclass(frozen=True, slots=True)
class IsolationLayout:
    """Fresh filesystem ownership for one installed artifact."""

    root: Path
    tool_dir: Path
    bin_dir: Path
    home: Path
    state_root: Path
    cwd: Path
    business_database: Path
    trace_database: Path

    @classmethod
    def create(cls, root: Path) -> IsolationLayout:
        """Create all directories required by one artifact smoke."""
        layout = cls(
            root=root,
            tool_dir=root / "tools",
            bin_dir=root / "bin",
            home=root / "home",
            state_root=root / "state",
            cwd=root / "cwd",
            business_database=root / "state" / "business.sqlite3",
            trace_database=root / "state" / "trace.sqlite3",
        )
        for directory in (
            layout.root,
            layout.tool_dir,
            layout.bin_dir,
            layout.home,
            layout.state_root,
            layout.cwd,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return layout


@dataclass(frozen=True, slots=True)
class BuiltArtifact:
    """One exact wheel or source distribution."""

    kind: Literal["wheel", "sdist"]
    path: Path


def build_command(output_directory: Path) -> tuple[str, ...]:
    """Return the fixed uv build command."""
    return (
        "uv",
        "build",
        "--no-sources",
        "--out-dir",
        str(output_directory),
    )


def tool_install_command(artifact: Path) -> tuple[str, ...]:
    """Return the fixed uv tool installation command."""
    return ("uv", "tool", "install", "--force", str(artifact))


def isolated_environment(
    layout: IsolationLayout,
    *,
    parent_environment: Mapping[str, str],
) -> dict[str, str]:
    """Build a credential-free environment for one installed artifact."""
    environment = {
        name: parent_environment[name]
        for name in _PASSTHROUGH_ENVIRONMENT
        if parent_environment.get(name)
    }
    parent_home = parent_environment.get("HOME")
    cache_root = (
        Path(parent_home).expanduser() / ".cache" / "uv"
        if parent_home
        else layout.root / "uv-cache"
    )
    environment.update(
        {
            "HOME": str(layout.home),
            "UV_CACHE_DIR": parent_environment.get("UV_CACHE_DIR", str(cache_root)),
            "UV_LINK_MODE": "copy",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON": sys.executable,
            "UV_TOOL_DIR": str(layout.tool_dir),
            "UV_TOOL_BIN_DIR": str(layout.bin_dir),
            "AGILEFORGE_CONFIG_ROOT": str(layout.state_root),
            "AGILEFORGE_DB_URL": f"sqlite:///{layout.business_database.as_posix()}",
            "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
                f"sqlite:///{layout.trace_database.as_posix()}"
            ),
        }
    )
    return environment


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        tuple(command),
        cwd=cwd,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        rendered = " ".join(command)
        message = (
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{completed.stdout[-4000:]}\n"
            f"stderr:\n{completed.stderr[-4000:]}"
        )
        raise DistributionVerificationError(message)
    return completed


def _archive_members(archive: Path) -> set[str]:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as package:
            return set(package.namelist())
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, mode="r:gz") as package:
            members: set[str] = set()
            for member in package.getnames():
                parts = Path(member).parts
                if len(parts) > 1:
                    members.add(Path(*parts[1:]).as_posix())
            return members
    message = f"unsupported distribution archive: {archive}"
    raise DistributionVerificationError(message)


def verify_archive_resources(archive: Path) -> None:
    """Require packaged model configuration and dashboard resources."""
    missing = sorted(REQUIRED_ARCHIVE_RESOURCES.difference(_archive_members(archive)))
    if missing:
        message = f"{archive.name} is missing resources: {', '.join(missing)}"
        raise DistributionVerificationError(message)


def _find_artifacts(output_directory: Path) -> tuple[BuiltArtifact, BuiltArtifact]:
    wheels = sorted(output_directory.glob("*.whl"))
    source_distributions = sorted(output_directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_distributions) != 1:
        message = (
            "uv build must produce exactly one wheel and one source distribution; "
            f"found {len(wheels)} wheel(s) and {len(source_distributions)} sdist(s)"
        )
        raise DistributionVerificationError(message)
    return (
        BuiltArtifact(kind="wheel", path=wheels[0]),
        BuiltArtifact(kind="sdist", path=source_distributions[0]),
    )


def _installed_python(layout: IsolationLayout) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    scripts_directory = "Scripts" if os.name == "nt" else "bin"
    interpreter = layout.tool_dir / _PACKAGE_NAME / scripts_directory / executable
    if not interpreter.is_file():
        message = f"installed tool interpreter was not found: {interpreter}"
        raise DistributionVerificationError(message)
    return interpreter


def _installed_cli(layout: IsolationLayout) -> Path:
    executable = "agileforge.exe" if os.name == "nt" else "agileforge"
    cli = layout.bin_dir / executable
    if not cli.is_file():
        message = f"installed AgileForge executable was not found: {cli}"
        raise DistributionVerificationError(message)
    return cli


def _select_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast("tuple[str, int]", listener.getsockname())[1]


def _read_json(url: str) -> dict[str, object]:
    with urlopen(  # noqa: S310  # nosec B310
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
    ) as response:
        if response.status != HTTPStatus.OK:
            message = f"readiness returned HTTP {response.status}: {url}"
            raise DistributionVerificationError(message)
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        message = f"readiness returned a non-object payload: {url}"
        raise DistributionVerificationError(message)
    return cast("dict[str, object]", payload)


def _read_bytes(url: str) -> bytes:
    with urlopen(  # noqa: S310  # nosec B310
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
    ) as response:
        if response.status != HTTPStatus.OK:
            message = f"dashboard resource returned HTTP {response.status}: {url}"
            raise DistributionVerificationError(message)
        return response.read()


def _require_nonempty_resource(content: bytes, *, name: str) -> None:
    if not content:
        message = f"installed dashboard resource is empty: {name}"
        raise DistributionVerificationError(message)


def _wait_for_dashboard_config(
    process: subprocess.Popen[str],
    *,
    port: int,
) -> dict[str, object]:
    url = f"http://127.0.0.1:{port}/api/dashboard/config"
    deadline = time.monotonic() + _API_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            message = f"installed API exited before readiness: {return_code}"
            raise DistributionVerificationError(message)
        try:
            return _read_json(url)
        except (OSError, URLError, json.JSONDecodeError):
            time.sleep(_POLL_INTERVAL_SECONDS)
    message = f"installed API dashboard readiness timed out: {url}"
    raise DistributionVerificationError(message)


def _stop_api(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_API_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_API_STOP_TIMEOUT_SECONDS)


def _verify_openapi(openapi: dict[str, object]) -> None:
    raw_paths = openapi.get("paths")
    if not isinstance(raw_paths, dict):
        message = "installed API OpenAPI payload has no paths object"
        raise DistributionVerificationError(message)
    paths = cast("dict[str, object]", raw_paths)
    position_path = "/api/projects/{project_id}/position"
    state_path = "/api/projects/{project_id}/state"
    if position_path not in paths:
        message = f"installed API is missing route: {position_path}"
        raise DistributionVerificationError(message)
    if state_path in paths:
        message = f"installed API retains removed route: {state_path}"
        raise DistributionVerificationError(message)


def _verify_dashboard_config(
    config: dict[str, object],
    *,
    expected_process_id: int,
    layout: IsolationLayout,
) -> None:
    expected: dict[str, object] = {
        "status": "ready",
        "process_id": expected_process_id,
        "business_database": str(layout.business_database),
        "trace_database": str(layout.trace_database),
    }
    for field, expected_value in expected.items():
        actual = config.get(field)
        if actual != expected_value:
            message = (
                f"installed API dashboard config {field} mismatch: "
                f"expected {expected_value!r}, got {actual!r}"
            )
            raise DistributionVerificationError(message)


def _verify_schema(database: Path) -> None:
    if not database.is_file():
        message = f"installed API did not bootstrap the business database: {database}"
        raise DistributionVerificationError(message)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    tables = {str(row[0]) for row in rows}
    missing = EXPECTED_BUSINESS_TABLES.difference(tables)
    forbidden = FORBIDDEN_BUSINESS_TABLES.intersection(tables)
    if missing or forbidden:
        message = (
            f"installed schema mismatch; missing={sorted(missing)}, "
            f"forbidden={sorted(forbidden)}"
        )
        raise DistributionVerificationError(message)


def _verify_installed_api(
    layout: IsolationLayout,
    *,
    environment: Mapping[str, str],
) -> None:
    port = _select_loopback_port()
    command = (
        str(_installed_python(layout)),
        "-I",
        "-m",
        "uvicorn",
        "api:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as child_log:
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            command,
            cwd=layout.cwd,
            env=dict(environment),
            stdout=child_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            try:
                dashboard_config = _wait_for_dashboard_config(process, port=port)
                _verify_dashboard_config(
                    dashboard_config,
                    expected_process_id=process.pid,
                    layout=layout,
                )
                root = f"http://127.0.0.1:{port}"
                openapi = _read_json(f"{root}/openapi.json")
                _verify_openapi(openapi)
                _require_nonempty_resource(
                    _read_bytes(f"{root}/dashboard/"),
                    name="frontend/index.html",
                )
                _require_nonempty_resource(
                    _read_bytes(f"{root}/dashboard/project.js"),
                    name="frontend/project.js",
                )
                _verify_schema(layout.business_database)
            except Exception as error:
                child_log.seek(0)
                logs = child_log.read()[-4000:]
                message = f"installed API smoke failed: {error}\nchild output:\n{logs}"
                raise DistributionVerificationError(message) from error
        finally:
            _stop_api(process)


def _verify_artifact(
    artifact: BuiltArtifact,
    *,
    isolation_root: Path,
    parent_environment: Mapping[str, str],
) -> None:
    verify_archive_resources(artifact.path)
    layout = IsolationLayout.create(isolation_root / artifact.kind)
    environment = isolated_environment(
        layout,
        parent_environment=parent_environment,
    )
    _run_checked(
        tool_install_command(artifact.path),
        cwd=layout.cwd,
        environment=environment,
    )
    cli = _installed_cli(layout)
    _run_checked((str(cli), "--help"), cwd=layout.cwd, environment=environment)
    _run_checked((str(cli), "--version"), cwd=layout.cwd, environment=environment)
    _run_checked(
        (str(_installed_python(layout)), "-I", "-c", _RESOURCE_PROBE),
        cwd=layout.cwd,
        environment=environment,
    )
    _verify_installed_api(layout, environment=environment)
    sys.stdout.write(f"verified {artifact.kind}: {artifact.path.name}\n")
    sys.stdout.flush()


def main() -> int:
    """Build and verify one wheel plus one source distribution."""
    checkout_root = Path(__file__).resolve().parents[1]
    parent_environment = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(
            prefix="agileforge-distributions-"
        ) as temporary:
            temporary_root = Path(temporary)
            output_directory = temporary_root / "dist"
            output_directory.mkdir()
            build_layout = IsolationLayout.create(temporary_root / "build")
            build_environment = isolated_environment(
                build_layout,
                parent_environment=parent_environment,
            )
            _run_checked(
                build_command(output_directory),
                cwd=checkout_root,
                environment=build_environment,
            )
            for artifact in _find_artifacts(output_directory):
                _verify_artifact(
                    artifact,
                    isolation_root=temporary_root / "installations",
                    parent_environment=parent_environment,
                )
    except (OSError, DistributionVerificationError) as error:
        sys.stderr.write(f"distribution verification failed: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
