"""Tests for isolated wheel and source-distribution verification."""

from __future__ import annotations

import gc
import io
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import pytest

import api as api_module
import scripts.verify_distribution as distribution_verifier
from scripts.verify_distribution import (
    _RESOURCE_PROBE,
    REQUIRED_ARCHIVE_RESOURCES,
    BuiltArtifact,
    DistributionVerificationError,
    IsolationLayout,
    build_command,
    isolated_environment,
    tool_install_command,
    verify_archive_resources,
    verify_archive_retired_labels_absent,
)
from services.agent_workbench.version import agileforge_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


# Two isolated Windows dependency installations need headroom.
_DISTRIBUTION_SMOKE_TIMEOUT_SECONDS: int = 600 if sys.platform == "win32" else 180


class _TrackingConnection(sqlite3.Connection):
    """Record whether the verifier closes each SQLite connection it opens."""

    instances: ClassVar[list[_TrackingConnection]] = []
    closed: bool

    def close(self) -> None:
        self.closed = True
        super().close()


def _tracked_sqlite_connections(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deny_table_query: bool = False,
) -> list[_TrackingConnection]:
    """Make verifier connections observable while retaining real SQLite behavior."""
    original_connect = sqlite3.connect
    _TrackingConnection.instances = []

    def tracked_connect(database: str | Path) -> _TrackingConnection:
        connection = original_connect(database, factory=_TrackingConnection)
        connection.closed = False
        if deny_table_query:
            connection.set_authorizer(_deny_sqlite_master_reads)
        _TrackingConnection.instances.append(connection)
        return connection

    monkeypatch.setattr(distribution_verifier.sqlite3, "connect", tracked_connect)
    return _TrackingConnection.instances


def _deny_sqlite_master_reads(
    action: int,
    argument_one: str | None,
    argument_two: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    """Reject the metadata read that schema verification requires."""
    del argument_two, database, trigger
    if action == sqlite3.SQLITE_READ and argument_one == "sqlite_master":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _create_business_schema(database: Path, tables: frozenset[str]) -> None:
    """Create the supplied business-table names in a disposable SQLite database."""
    connection = sqlite3.connect(database)
    try:
        with connection:
            for table in tables:
                connection.execute(f"CREATE TABLE {table} (id INTEGER)")
    finally:
        connection.close()


def _assert_closed_connection(
    connection: _TrackingConnection,
    database: Path,
) -> None:
    """Prove the tracked connection is closed and releases its Windows handle."""
    assert connection.closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    if os.name == "nt":
        database.unlink()


@pytest.mark.parametrize(
    ("tables", "expected_error"),
    [
        (distribution_verifier.EXPECTED_BUSINESS_TABLES, None),
        (
            distribution_verifier.EXPECTED_BUSINESS_TABLES - {"projects"},
            r"missing=\['projects'\]",
        ),
        (
            distribution_verifier.EXPECTED_BUSINESS_TABLES | {"products"},
            r"forbidden=\['products'\]",
        ),
    ],
)
def test_schema_verification_closes_connections_after_valid_and_invalid_schemas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tables: frozenset[str],
    expected_error: str | None,
) -> None:
    """Close the verifier connection after every schema-validation outcome."""
    database = tmp_path / "business.sqlite3"
    _create_business_schema(database, tables)
    connections = _tracked_sqlite_connections(monkeypatch)

    garbage_collection_enabled = gc.isenabled()
    if os.name == "nt":
        gc.disable()
    try:
        if expected_error is None:
            distribution_verifier._verify_schema(database)
        else:
            with pytest.raises(DistributionVerificationError, match=expected_error):
                distribution_verifier._verify_schema(database)

        assert len(connections) == 1
        _assert_closed_connection(connections[0], database)
    finally:
        if os.name == "nt":
            gc.collect()
            if garbage_collection_enabled:
                gc.enable()


def test_schema_verification_closes_connection_when_table_query_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Close the verifier connection when its SQLite metadata query raises."""
    database = tmp_path / "business.sqlite3"
    _create_business_schema(database, distribution_verifier.EXPECTED_BUSINESS_TABLES)
    connections = _tracked_sqlite_connections(monkeypatch, deny_table_query=True)

    garbage_collection_enabled = gc.isenabled()
    if os.name == "nt":
        gc.disable()
    try:
        with pytest.raises(sqlite3.DatabaseError, match=r"sqlite_master.*prohibited"):
            distribution_verifier._verify_schema(database)

        assert len(connections) == 1
        _assert_closed_connection(connections[0], database)
    finally:
        if os.name == "nt":
            gc.collect()
            if garbage_collection_enabled:
                gc.enable()


def _checkout_file_state(checkout: Path) -> dict[str, tuple[int, bytes]]:
    """Capture non-Git file modes and bytes for mutation checks."""
    state: dict[str, tuple[int, bytes]] = {}
    for path in sorted(checkout.rglob("*")):
        relative = path.relative_to(checkout)
        if ".git" in relative.parts or not path.is_file():
            continue
        state[relative.as_posix()] = (path.stat().st_mode, path.read_bytes())
    return state


def _clone_checkout(source: Path, destination: Path) -> None:
    """Create one independent local checkout for a distribution probe."""
    git_executable = shutil.which("git")
    assert git_executable is not None
    subprocess.run(  # noqa: S603  # nosec B603
        (
            git_executable,
            "clone",
            "--quiet",
            "--no-hardlinks",
            str(source),
            str(destination),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_status_with_ignored(checkout: Path) -> str:
    """Return exact tracked, untracked, and ignored state for a fixture checkout."""
    git_executable = shutil.which("git")
    assert git_executable is not None
    return subprocess.run(  # noqa: S603  # nosec B603
        (
            git_executable,
            "status",
            "--short",
            "--untracked-files=all",
            "--ignored",
        ),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_contaminating_uv(bin_directory: Path) -> Path:
    """Write a local builder that packages stale files found in its source root."""
    bin_directory.mkdir()
    fake_uv = bin_directory / "uv.py"
    fake_uv.write_text(
        f"""#!{sys.executable}
import io
import sys
import tarfile
import zipfile
from pathlib import Path

arguments = sys.argv[1:]
output = Path(arguments[arguments.index("--out-dir") + 1])
output.mkdir(parents=True, exist_ok=True)
source = Path.cwd()
payloads = {{"cli/__init__.py": (source / "cli/__init__.py").read_bytes()}}
for relative in (
    "build/lib/cli/stale_shadow.py",
    "agileforge.egg-info/SOURCES.txt",
):
    candidate = source / relative
    if candidate.is_file():
        payloads[relative] = candidate.read_bytes()
with zipfile.ZipFile(output / "agileforge-0.1.0-py3-none-any.whl", "w") as wheel:
    for relative, content in payloads.items():
        wheel.writestr(relative, content)
with tarfile.open(output / "agileforge-0.1.0.tar.gz", "w:gz") as sdist:
    for relative, content in payloads.items():
        member = tarfile.TarInfo(f"agileforge-0.1.0/{{relative}}")
        member.size = len(content)
        sdist.addfile(member, io.BytesIO(content))
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    return fake_uv


def _run_uv_build(
    source: Path,
    output: Path,
    environment: Mapping[str, str],
    fake_uv: Path,
) -> None:
    """Run one bounded uv build for a distribution test fixture."""
    output.mkdir()
    completed = subprocess.run(  # noqa: S603  # nosec B603
        (sys.executable, str(fake_uv), *build_command(output)[1:]),
        cwd=source,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_distribution_commands_use_uv_and_exact_artifact_paths(tmp_path: Path) -> None:
    """Build and install only through fixed uv argv."""
    output = tmp_path / "dist"
    artifact = output / "agileforge-0.1.0-py3-none-any.whl"

    assert build_command(output) == (
        "uv",
        "build",
        "--no-sources",
        "--out-dir",
        str(output),
    )
    assert tool_install_command(artifact) == (
        "uv",
        "tool",
        "install",
        "--force",
        str(artifact),
    )


def test_isolated_environment_excludes_credentials_from_child_output(
    tmp_path: Path,
) -> None:
    """Pass no parent proxy, index, auth, or provider secret to children."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    credentials = {
        "HTTPS_PROXY": "https://upper-user:upper-pass@proxy.invalid:8443",
        "HTTP_PROXY": "http://upper-user:upper-pass@proxy.invalid:8080",
        "NO_PROXY": "upper-user:upper-pass@internal.invalid",
        "https_proxy": "https://lower-user:lower-pass@proxy.invalid:8443",
        "http_proxy": "http://lower-user:lower-pass@proxy.invalid:8080",
        "no_proxy": "lower-user:lower-pass@internal.invalid",
        "UV_INDEX_URL": "https://uv-user:uv-pass@index.invalid/simple",
        "PIP_INDEX_URL": "https://pip-user:pip-pass@index.invalid/simple",
        "OPEN_ROUTER_API_KEY": "provider-secret",
        "OPENROUTER_API_KEY": "provider-secret-alias",
    }
    environment = isolated_environment(
        layout,
        parent_environment={
            "PATH": "/usr/bin",
            "HOME": "/source-home",
            "MODEL_CONFIG_PATH": "/source/config/models.yaml",
            "AGILEFORGE_DB_URL": "sqlite:///source.sqlite3",
            **credentials,
        },
    )
    completed = subprocess.run(  # nosec B603
        (
            sys.executable,
            "-I",
            "-c",
            "import os; print('\\n'.join(f'{k}={v}' for k, v in os.environ.items()))",
        ),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == str(layout.home)
    assert environment["UV_TOOL_DIR"] == str(layout.tool_dir)
    assert environment["UV_TOOL_BIN_DIR"] == str(layout.bin_dir)
    assert environment["AGILEFORGE_DB_URL"].endswith("business.sqlite3")
    assert environment["AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"].endswith(
        "trace.sqlite3"
    )
    for name, value in credentials.items():
        assert name not in environment
        assert value not in completed.stdout
        assert value not in completed.stderr
    assert "MODEL_CONFIG_PATH" not in environment
    assert layout.business_database != layout.trace_database
    assert layout.cwd != layout.tool_dir


@pytest.mark.parametrize(
    ("parent_system_root_name", "parent_system_root_value", "expected_system_root"),
    [
        ("SystemRoot", "C:/Windows", "C:/Windows"),
        ("SYSTEMROOT", "D:/Windows", "D:/Windows"),
        ("SystemRoot", "", None),
        ("SYSTEMROOT", "", None),
    ],
)
def test_isolated_environment_preserves_windows_system_root_only_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    parent_system_root_name: str,
    parent_system_root_value: str,
    expected_system_root: str | None,
) -> None:
    """Keep the Windows runtime root required by isolated child processes."""
    monkeypatch.setattr(distribution_verifier.sys, "platform", "win32")
    parent_environment = {
        "PATH": "C:/Windows/System32",
        parent_system_root_name: parent_system_root_value,
        "TEMP": "C:/parent-temp",
        "TMP": "C:/parent-tmp",
    }
    layout = IsolationLayout.create(tmp_path / "wheel")

    environment = isolated_environment(
        layout,
        parent_environment=parent_environment,
    )

    if expected_system_root is None:
        assert "SystemRoot" not in environment
    else:
        assert environment["SystemRoot"] == expected_system_root
    assert environment["TEMP"] == str(layout.temp_dir)
    assert environment["TMP"] == str(layout.temp_dir)
    assert layout.temp_dir.is_dir()


def test_isolated_environment_keeps_non_windows_system_root_excluded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Leave the non-Windows child environment policy unchanged."""
    monkeypatch.setattr(distribution_verifier.sys, "platform", "linux")

    environment = isolated_environment(
        IsolationLayout.create(tmp_path / "wheel"),
        parent_environment={
            "PATH": "/usr/bin",
            "SystemRoot": "C:/Windows",
        },
    )

    assert "SystemRoot" not in environment
    assert "TEMP" not in environment
    assert "TMP" not in environment


def _set_dashboard_databases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "AGILEFORGE_DB_URL",
        f"sqlite:///{(tmp_path / 'business.sqlite3').as_posix()}",
    )
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        f"sqlite:///{(tmp_path / 'trace.sqlite3').as_posix()}",
    )


def test_dashboard_config_preserves_source_checkout_git_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep Task 4 source readiness bound to the exact checkout commit."""
    checkout = Path(api_module.__file__).resolve().parent
    _set_dashboard_databases(monkeypatch, tmp_path)
    git_executable = shutil.which("git")
    assert git_executable is not None
    expected = subprocess.run(  # noqa: S603  # nosec B603
        (git_executable, "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config = api_module.get_dashboard_config()

    assert config.checkout_root == checkout
    assert config.commit == expected
    assert config.launch_nonce is None


def test_dashboard_config_uses_stable_installed_provenance_without_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Return package identity when api.py is installed outside a checkout."""
    package_root = tmp_path / "site-packages"
    package_root.mkdir()
    _set_dashboard_databases(monkeypatch, tmp_path)
    monkeypatch.setattr(api_module, "__file__", str(package_root / "api.py"))

    config = api_module.get_dashboard_config()

    assert config.checkout_root == package_root
    assert config.commit == f"installed:agileforge@{agileforge_version()}"
    assert config.launch_nonce is None


def test_dashboard_config_exposes_launcher_nonce_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Return the non-secret per-launch identity only when a supervisor sets it."""
    _set_dashboard_databases(monkeypatch, tmp_path)
    monkeypatch.setenv("AGILEFORGE_UI_LAUNCH_NONCE", "supervisor-launch-nonce")

    config = api_module.get_dashboard_config()

    assert config.launch_nonce == "supervisor-launch-nonce"


def test_installed_dashboard_config_validation_binds_child_and_databases(
    tmp_path: Path,
) -> None:
    """Reject readiness config that does not identify the installed child layout."""
    verify = getattr(distribution_verifier, "_verify_dashboard_config", None)
    assert verify is not None, "distribution verifier must validate dashboard config"
    typed_verify = cast("Callable[..., None]", verify)
    layout = IsolationLayout.create(tmp_path / "wheel")
    valid: dict[str, object] = {
        "status": "ready",
        "process_id": 1234,
        "business_database": str(layout.business_database),
        "trace_database": str(layout.trace_database),
    }
    typed_verify(valid, expected_process_id=1234, layout=layout)

    invalid_values: tuple[tuple[str, object], ...] = (
        ("status", "starting"),
        ("process_id", 4321),
        ("business_database", str(tmp_path / "wrong-business.sqlite3")),
        ("trace_database", str(tmp_path / "wrong-trace.sqlite3")),
    )
    for field, value in invalid_values:
        invalid = {**valid, field: value}
        with pytest.raises(DistributionVerificationError, match=field):
            typed_verify(invalid, expected_process_id=1234, layout=layout)


def _installed_python_fixture(layout: IsolationLayout) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    scripts_directory = "Scripts" if os.name == "nt" else "bin"
    interpreter = layout.tool_dir / "agileforge" / scripts_directory / executable
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    return interpreter


def test_installed_api_python_keeps_non_windows_launch_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep the portable installed-API command on its existing interpreter."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    interpreter = _installed_python_fixture(layout)
    monkeypatch.setattr(distribution_verifier.sys, "platform", "linux")

    def unexpected_inspection(*_args: object, **_kwargs: object) -> object:
        message = "non-Windows launch must not inspect a base interpreter"
        raise AssertionError(message)

    monkeypatch.setattr(
        distribution_verifier,
        "_inspect_installed_python",
        unexpected_inspection,
        raising=False,
    )
    environment = {"AGILEFORGE_DB_URL": "fixture"}

    selected, copied = distribution_verifier._installed_api_python(
        layout,
        environment=environment,
    )

    assert selected == interpreter
    assert copied == environment
    assert copied is not environment


def test_installed_api_python_uses_verified_installed_venv_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Select the base reported by the installed venv and retain its identity."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    interpreter = _installed_python_fixture(layout)
    base_executable = tmp_path / "base-python.exe"
    base_executable.touch()
    base_prefix = tmp_path / "base-prefix"
    base_prefix.mkdir()
    runtime: dict[str, object] = {
        "executable": str(interpreter),
        "base_executable": str(base_executable),
        "prefix": str(interpreter.parents[1]),
        "base_prefix": str(base_prefix),
    }
    monkeypatch.setattr(distribution_verifier.sys, "platform", "win32")
    monkeypatch.setattr(
        distribution_verifier,
        "_inspect_installed_python",
        lambda *_args, **_kwargs: runtime,
        raising=False,
    )
    environment = {
        "AGILEFORGE_DB_URL": "fixture",
        "__PYVENV_LAUNCHER__": "ambient-must-not-leak",
    }
    before = dict(environment)

    selected, copied = distribution_verifier._installed_api_python(
        layout,
        environment=environment,
    )

    assert selected == base_executable
    assert copied == {
        "AGILEFORGE_DB_URL": "fixture",
        "__PYVENV_LAUNCHER__": str(interpreter),
    }
    assert environment == before


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_message"),
    [
        ("executable", "other-python.exe", "installed Python executable"),
        ("base_executable", None, "base Python executable"),
        ("base_executable", "relative.exe", "base Python executable"),
        ("base_executable", "missing", "base Python executable"),
        ("base_executable", "directory", "base Python executable"),
        ("prefix", "wrong-prefix", "installed Python prefix"),
        ("base_prefix", "relative-base-prefix", "base Python prefix"),
    ],
)
def test_installed_api_python_rejects_unverified_windows_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    invalid_value: object,
    expected_message: str,
) -> None:
    """Reject an unavailable or foreign base interpreter before API startup."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    interpreter = _installed_python_fixture(layout)
    base_executable = tmp_path / "base-python.exe"
    base_executable.touch()
    other_executable = tmp_path / "other-python.exe"
    other_executable.touch()
    base_prefix = tmp_path / "base-prefix"
    base_prefix.mkdir()
    wrong_prefix = tmp_path / "wrong-prefix"
    wrong_prefix.mkdir()
    replacements: dict[str, object] = {
        "other-python.exe": str(tmp_path / "other-python.exe"),
        "missing": str(tmp_path / "missing.exe"),
        "directory": str(tmp_path),
        "wrong-prefix": str(tmp_path / "wrong-prefix"),
        "relative-base-prefix": "relative-base-prefix",
    }
    runtime: dict[str, object] = {
        "executable": str(interpreter),
        "base_executable": str(base_executable),
        "prefix": str(interpreter.parents[1]),
        "base_prefix": str(base_prefix),
    }
    runtime[field] = replacements.get(cast("str", invalid_value), invalid_value)
    monkeypatch.setattr(distribution_verifier.sys, "platform", "win32")
    monkeypatch.setattr(
        distribution_verifier,
        "_inspect_installed_python",
        lambda *_args, **_kwargs: runtime,
        raising=False,
    )

    with pytest.raises(DistributionVerificationError, match=expected_message):
        distribution_verifier._installed_api_python(
            layout,
            environment={},
        )


@pytest.mark.parametrize(
    "failure",
    [
        OSError("interpreter could not start"),
        subprocess.TimeoutExpired(("python",), timeout=5),
    ],
)
def test_installed_api_python_reports_inspection_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: OSError | subprocess.TimeoutExpired,
) -> None:
    """Fail before API startup when the installed interpreter cannot report itself."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    _installed_python_fixture(layout)
    monkeypatch.setattr(distribution_verifier.sys, "platform", "win32")

    def fail_inspection(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(distribution_verifier.subprocess, "run", fail_inspection)

    with pytest.raises(
        DistributionVerificationError,
        match="installed Python runtime inspection failed",
    ):
        distribution_verifier._installed_api_python(
            layout,
            environment={},
        )


def test_installed_parser_probe_covers_navigation_and_public_transition() -> None:
    """Keep installed smoke coverage on current graph parser entry points."""
    assert '["workflow", "next", "--project-id", "1"]' in _RESOURCE_PROBE
    assert '["workflow", "position", "--project-id", "1"]' in _RESOURCE_PROBE
    assert '["project", "create"' in _RESOURCE_PROBE
    assert "include_optional is False" in _RESOURCE_PROBE
    assert 'create_args.command_handler.__name__ == "_create_project"' in (
        _RESOURCE_PROBE
    )


@pytest.mark.parametrize("archive_kind", ["wheel", "sdist"])
def test_archive_resource_verification_requires_models_and_frontend(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Reject either archive format when a runtime resource is absent."""
    members = sorted(REQUIRED_ARCHIVE_RESOURCES - {"frontend/project.js"})
    if archive_kind == "wheel":
        archive = tmp_path / "agileforge.whl"
        with zipfile.ZipFile(archive, "w") as package:
            for member in members:
                package.writestr(member, "fixture\n")
    else:
        archive = tmp_path / "agileforge.tar.gz"
        source = tmp_path / "source"
        for member in members:
            path = source / "agileforge-0.1.0" / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as package:
            package.add(source / "agileforge-0.1.0", arcname="agileforge-0.1.0")

    with pytest.raises(
        DistributionVerificationError,
        match=r"frontend/project\.js",
    ):
        verify_archive_resources(archive)


def _write_archive_member(
    archive: Path,
    *,
    archive_kind: str,
    member_name: str,
    content: bytes | None,
) -> None:
    """Write one directory or regular-file member to a distribution fixture."""
    if archive_kind == "wheel":
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member_name, b"" if content is None else content)
        return

    rooted_name = f"agileforge-0.1.0/{member_name}"
    with tarfile.open(archive, "w:gz") as package:
        member = tarfile.TarInfo(rooted_name)
        if content is None:
            member.type = tarfile.DIRTYPE
            package.addfile(member)
            return
        member.size = len(content)
        package.addfile(member, io.BytesIO(content))


@pytest.mark.parametrize("archive_kind", ["wheel", "sdist"])
def test_archive_retired_label_scan_rejects_directory_member_paths(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Reject case-insensitive retired labels in directory-only members."""
    retired_label = "BrOwN" + "FiElD"
    archive = tmp_path / (
        "agileforge.whl" if archive_kind == "wheel" else "agileforge.tar.gz"
    )
    _write_archive_member(
        archive,
        archive_kind=archive_kind,
        member_name=f"package/{retired_label}/",
        content=None,
    )

    with pytest.raises(
        DistributionVerificationError,
        match=re.escape(retired_label),
    ):
        verify_archive_retired_labels_absent(archive)


@pytest.mark.parametrize("archive_kind", ["wheel", "sdist"])
def test_archive_retired_label_scan_rejects_utf8_file_content(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    """Reject case-insensitive retired labels in UTF-8 regular-file content."""
    retired_label = "GrEeN" + "FiElD"
    archive = tmp_path / (
        "agileforge.whl" if archive_kind == "wheel" else "agileforge.tar.gz"
    )
    _write_archive_member(
        archive,
        archive_kind=archive_kind,
        member_name="package/marker.txt",
        content=f"retired label: {retired_label}\n".encode(),
    )

    with pytest.raises(
        DistributionVerificationError,
        match=r"package/marker\.txt",
    ):
        verify_archive_retired_labels_absent(archive)


def test_clean_snapshot_build_excludes_ignored_stale_state_and_preserves_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build working-tree source without stale ignored build metadata or modules."""
    source_checkout = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    _clone_checkout(source_checkout, checkout)
    tracked_source = checkout / "cli" / "__init__.py"
    tracked_marker = "# clean-snapshot-working-tree-marker"
    tracked_source.write_text(
        tracked_source.read_text(encoding="utf-8") + tracked_marker + "\n",
        encoding="utf-8",
    )
    stale_module = checkout / "build" / "lib" / "cli" / "stale_shadow.py"
    stale_module.parent.mkdir(parents=True)
    stale_module.write_text("STALE_BUILD_SHADOW = True\n", encoding="utf-8")
    stale_egg_info = checkout / "agileforge.egg-info" / "SOURCES.txt"
    stale_egg_info.parent.mkdir()
    stale_egg_info.write_text(
        "build/lib/cli/stale_shadow.py\nSTALE_EGG_INFO_SENTINEL\n",
        encoding="utf-8",
    )
    fake_uv = _write_contaminating_uv(tmp_path / "fake-bin")
    build_environment = dict(os.environ)
    live_output = tmp_path / "live-checkout-dist"
    _run_uv_build(checkout, live_output, build_environment, fake_uv)
    with zipfile.ZipFile(next(live_output.glob("*.whl"))) as package:
        assert "build/lib/cli/stale_shadow.py" in package.namelist()
        assert b"STALE_EGG_INFO_SENTINEL" in package.read(
            "agileforge.egg-info/SOURCES.txt"
        )
    before_status = _git_status_with_ignored(checkout)
    assert "!! agileforge.egg-info/" in before_status
    assert "!! build/" in before_status
    before_files = _checkout_file_state(checkout)
    build_distributions = getattr(
        distribution_verifier,
        "_build_distributions_from_clean_snapshot",
        None,
    )
    assert build_distributions is not None
    typed_build = cast(
        "Callable[..., tuple[BuiltArtifact, BuiltArtifact]]",
        build_distributions,
    )
    monkeypatch.setattr(
        distribution_verifier,
        "build_command",
        lambda output: (sys.executable, str(fake_uv), *build_command(output)[1:]),
    )

    with tempfile.TemporaryDirectory(
        prefix="agileforge-distributions-",
    ) as directory:
        artifacts = typed_build(
            checkout_root=checkout,
            temporary_root=Path(directory),
            parent_environment=build_environment,
        )

        assert _git_status_with_ignored(checkout) == before_status
        assert _checkout_file_state(checkout) == before_files
        wheel_path = next(
            artifact.path for artifact in artifacts if artifact.kind == "wheel"
        )
        with zipfile.ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())
            assert "cli/stale_shadow.py" not in names
            assert "build/lib/cli/stale_shadow.py" not in names
            assert tracked_marker.encode() in wheel.read("cli/__init__.py")
            assert all(
                b"STALE_EGG_INFO_SENTINEL" not in wheel.read(name)
                for name in names
                if not name.endswith("/")
            )
        sdist_path = next(
            artifact.path for artifact in artifacts if artifact.kind == "sdist"
        )
        with tarfile.open(sdist_path, mode="r:gz") as source_distribution:
            files = [
                member for member in source_distribution.getmembers() if member.isfile()
            ]
            assert not any(
                member.name.endswith("/cli/stale_shadow.py") for member in files
            )
            init_member = next(
                member for member in files if member.name.endswith("/cli/__init__.py")
            )
            init_stream = source_distribution.extractfile(init_member)
            assert init_stream is not None
            assert tracked_marker.encode() in init_stream.read()
            for member in files:
                stream = source_distribution.extractfile(member)
                assert stream is not None
                assert b"STALE_EGG_INFO_SENTINEL" not in stream.read()


def test_built_distributions_pass_isolated_smoke_and_preserve_checkout() -> None:
    """Verify both installed artifacts while leaving the checkout unchanged."""
    checkout = Path(__file__).resolve().parents[1]
    status_command = ("git", "status", "--short", "--untracked-files=all")
    before = subprocess.run(  # noqa: S603  # nosec B603
        status_command,
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    environment = dict(os.environ)
    environment["OPEN_ROUTER_API_KEY"] = "must-not-reach-installed-artifact"
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    completed = subprocess.run(  # noqa: S603  # nosec B603
        (
            uv_executable,
            "run",
            "--locked",
            "python",
            "scripts/verify_distribution.py",
        ),
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=_DISTRIBUTION_SMOKE_TIMEOUT_SECONDS,
    )
    after = subprocess.run(  # noqa: S603  # nosec B603
        status_command,
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert completed.returncode == 0, completed.stderr
    assert "verified wheel" in completed.stdout
    assert "verified sdist" in completed.stdout
    assert after == before
