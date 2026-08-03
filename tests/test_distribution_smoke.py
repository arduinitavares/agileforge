"""Tests for isolated wheel and source-distribution verification."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_distribution import (
    REQUIRED_ARCHIVE_RESOURCES,
    DistributionVerificationError,
    IsolationLayout,
    build_command,
    isolated_environment,
    tool_install_command,
    verify_archive_resources,
)


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


def test_isolated_environment_excludes_provider_credentials(tmp_path: Path) -> None:
    """Pass only runtime necessities into installed-artifact children."""
    layout = IsolationLayout.create(tmp_path / "wheel")
    environment = isolated_environment(
        layout,
        parent_environment={
            "PATH": "/usr/bin",
            "HOME": "/source-home",
            "OPEN_ROUTER_API_KEY": "secret",
            "OPENROUTER_API_KEY": "secret",
            "MODEL_CONFIG_PATH": "/source/config/models.yaml",
            "AGILEFORGE_DB_URL": "sqlite:///source.sqlite3",
        },
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == str(layout.home)
    assert environment["UV_TOOL_DIR"] == str(layout.tool_dir)
    assert environment["UV_TOOL_BIN_DIR"] == str(layout.bin_dir)
    assert environment["AGILEFORGE_DB_URL"].endswith("business.sqlite3")
    assert environment["AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"].endswith(
        "trace.sqlite3"
    )
    assert "OPEN_ROUTER_API_KEY" not in environment
    assert "OPENROUTER_API_KEY" not in environment
    assert "MODEL_CONFIG_PATH" not in environment
    assert layout.business_database != layout.trace_database
    assert layout.cwd != layout.tool_dir


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
