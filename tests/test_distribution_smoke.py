"""Tests for isolated wheel and source-distribution verification."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

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
)
from services.agent_workbench.version import agileforge_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


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
    fake_uv = bin_directory / "uv"
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
) -> None:
    """Run one bounded uv build for a distribution test fixture."""
    output.mkdir()
    completed = subprocess.run(  # noqa: S603  # nosec B603
        build_command(output),
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


def test_clean_snapshot_build_excludes_ignored_stale_state_and_preserves_checkout(
    tmp_path: Path,
) -> None:
    """Build working-tree source without stale ignored build metadata or modules."""
    source_checkout = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    _clone_checkout(source_checkout, checkout)
    tracked_source = checkout / "cli" / "__init__.py"
    tracked_marker = "# clean-snapshot-working-tree-marker\n"
    tracked_source.write_text(
        tracked_source.read_text(encoding="utf-8") + tracked_marker,
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
    fake_bin = tmp_path / "fake-bin"
    _write_contaminating_uv(fake_bin)
    build_environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    live_output = tmp_path / "live-checkout-dist"
    _run_uv_build(checkout, live_output, build_environment)
    live_wheel = next(live_output.glob("*.whl"))
    with zipfile.ZipFile(live_wheel) as package:
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

    artifacts = typed_build(
        checkout_root=checkout,
        temporary_root=tmp_path / "distribution-workspace",
        parent_environment=build_environment,
    )

    after_status = _git_status_with_ignored(checkout)
    assert after_status == before_status
    assert _checkout_file_state(checkout) == before_files
    wheel_path = next(
        artifact.path for artifact in artifacts if artifact.kind == "wheel"
    )
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        assert "cli/stale_shadow.py" not in names
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
        assert not any(member.name.endswith("/cli/stale_shadow.py") for member in files)
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
        timeout=180,
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
