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
    DistributionVerificationError,
    IsolationLayout,
    build_command,
    isolated_environment,
    tool_install_command,
    verify_archive_resources,
)
from services.agent_workbench.version import agileforge_version

if TYPE_CHECKING:
    from collections.abc import Callable


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
    assert '["project", "abandon"' in _RESOURCE_PROBE
    assert "include_optional is False" in _RESOURCE_PROBE
    assert 'request_kind == "abandon_project_shell"' in _RESOURCE_PROBE


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
