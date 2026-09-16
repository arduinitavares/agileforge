"""Contract tests for the explicit container build-context transport."""

import hashlib
import importlib
import json
import subprocess  # nosec B404
from pathlib import Path

import pytest

from scripts.container import export_source

SHA256_HEX_LENGTH = 64


def _run_git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 # nosec B603, B607
        ["git", *args],  # noqa: S607
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def make_committed_repository(tmp_path: Path) -> Path:
    """Create a minimal committed checkout with the required build inputs."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _run_git(checkout, "init", "--quiet")
    _run_git(checkout, "config", "user.email", "container-tests@example.invalid")
    _run_git(checkout, "config", "user.name", "Container Tests")
    (checkout / "containers").mkdir()
    (checkout / "module.py").write_text("VALUE = 'tracked'\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '1.2.3'\n",
        encoding="utf-8",
    )
    (checkout / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (checkout / "containers" / "pins.json").write_text(
        json.dumps({"python": "3.13.15", "uv": "0.12.8"}),
        encoding="utf-8",
    )
    _run_git(checkout, "add", ".")
    _run_git(checkout, "commit", "--quiet", "-m", "fixture")
    return checkout


def test_export_omits_untracked_runtime_state(tmp_path: Path) -> None:
    """The image context contains only the committed source tree."""
    checkout = make_committed_repository(tmp_path)
    (checkout / ".env").write_text("CANARY=container-test-only", encoding="utf-8")
    (checkout / ".agileforge").mkdir()
    (checkout / ".agileforge" / "state.db").write_text("runtime", encoding="utf-8")
    (checkout / "runtime.sqlite").write_text("runtime", encoding="utf-8")
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "CANARY").write_text("cache", encoding="utf-8")
    (checkout / ".git" / "config").write_text("CANARY=git-config", encoding="utf-8")

    archive = export_source(checkout, tmp_path / "context")

    assert (archive / "module.py").read_text(encoding="utf-8") == "VALUE = 'tracked'\n"
    assert not (archive / ".env").exists()
    assert not (archive / ".agileforge").exists()
    assert not (archive / "runtime.sqlite").exists()
    assert not (archive / ".pytest_cache").exists()
    assert not (archive / ".git").exists()


def test_production_export_rejects_a_dirty_checkout(tmp_path: Path) -> None:
    """Production images bind to one committed source revision."""
    checkout = make_committed_repository(tmp_path)
    (checkout / "module.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean"):
        export_source(checkout, tmp_path / "context", production=True)


def test_export_writes_the_pinned_build_identity(tmp_path: Path) -> None:
    """The build context records the source and locked dependency identities."""
    checkout = make_committed_repository(tmp_path)

    archive = export_source(checkout, tmp_path / "context", production=True)
    identity = json.loads((archive / "containers" / "build.json").read_text())

    assert identity == {
        "lock_sha256": hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest(),
        "package_version": "1.2.3",
        "python_version": "3.13.15",
        "revision": _run_git(checkout, "rev-parse", "HEAD"),
        "schema_version": "agileforge.build.v1",
        "source_sha256": identity["source_sha256"],
        "uv_version": "0.12.8",
    }
    assert len(identity["source_sha256"]) == SHA256_HEX_LENGTH


def test_project_resources_are_selectable_without_issue_specific_defaults() -> None:
    """One controller can target an explicit task namespace without hard-coding it."""
    module = importlib.import_module("scripts.container")

    assert module.project_resources("review-17") == {
        "cache_volume": "review-17-cache",
        "container": "review-17-dev",
        "production_state_volume": "review-17-production-state",
        "workspace_volume": "review-17-workspace",
    }
