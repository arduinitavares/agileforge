"""Git-aware complete repository inventory tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from git import Repo

import services.agent_workbench.repository_inventory as inventory_module
from services.agent_workbench.repository_inventory import (
    InventoryLimits,
    RepositoryChangedDuringInventoryError,
    RepositoryInventoryLimitError,
    RepositoryInventoryService,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURE_FILE_COUNT = 2
_MANY_FILE_COUNT = 1_200
_COMPLETE_FILE_COUNT = _MANY_FILE_COUNT + _FIXTURE_FILE_COUNT
_MODEL_FILE_COUNT = 500


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a repository with isolated global Git configuration."""
    root = tmp_path / "repository"
    root.mkdir()
    global_ignore = tmp_path / "global-ignore"
    global_ignore.write_text("*.global\n", encoding="utf-8")
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text(
        f"[core]\n\texcludesFile = {global_ignore}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Inventory Test")
            config.set_value("user", "email", "inventory@example.com")
        (root / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        (root / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
        repo.index.add([".gitignore", "tracked.py"])
        repo.index.commit("inventory fixture")

    return root


def _paths(result: inventory_module.RepositoryInventoryResult) -> tuple[str, ...]:
    return tuple(item.path for item in result.files)


def test_git_inventory_honors_ignores_and_preserves_suppressed_entries(
    git_repo: Path,
) -> None:
    """Use Git's complete tracked and non-ignored untracked file view."""
    (git_repo / "space name.py").write_text("VISIBLE = True\n", encoding="utf-8")
    (git_repo / "line\nbreak.md").write_text("# Visible\n", encoding="utf-8")
    (git_repo / "repo.ignored").write_text("ignored\n", encoding="utf-8")
    (git_repo / "local.exclude").write_text("ignored\n", encoding="utf-8")
    (git_repo / "global.global").write_text("ignored\n", encoding="utf-8")
    (git_repo / ".git" / "info" / "exclude").write_text(
        "*.exclude\n",
        encoding="utf-8",
    )
    (git_repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (git_repo / "oversized.txt").write_bytes(b"x" * 65)
    (git_repo / "linked.py").symlink_to("tracked.py")

    result = RepositoryInventoryService(
        limits=InventoryLimits(max_hash_bytes_per_file=64)
    ).inventory(git_repo)

    expected = tuple(
        sorted(
            (
                ".env",
                ".gitignore",
                "line\nbreak.md",
                "linked.py",
                "oversized.txt",
                "space name.py",
                "tracked.py",
            ),
            key=os.fsencode,
        )
    )
    assert _paths(result) == expected
    assert result.git_available is True
    assert result.commit is not None
    assert result.dirty is True
    assert result.truncated is False

    by_path = {item.path: item for item in result.files}
    assert by_path[".env"].content_status == "secret"
    assert by_path[".env"].sha256 is None
    assert by_path["oversized.txt"].content_status == "oversized"
    assert by_path["oversized.txt"].sha256 is None
    assert by_path["linked.py"].content_status == "symlink"
    assert by_path["linked.py"].sha256 is None
    assert all(
        path not in result.selected_for_model
        for path in (".env", "oversized.txt", "linked.py")
    )
    assert "tracked.py" in result.selected_for_model


def test_model_budget_does_not_truncate_complete_inventory(git_repo: Path) -> None:
    """Keep all files while bounding only deterministic model selection."""
    for index in range(_MANY_FILE_COUNT):
        (git_repo / f"file-{index:04d}.txt").write_text("x", encoding="utf-8")

    result = RepositoryInventoryService(
        limits=InventoryLimits(
            max_files=50_000,
            max_total_bytes=2_000_000_000,
            max_hash_bytes_per_file=10_000_000,
            max_model_files=_MODEL_FILE_COUNT,
            max_model_bytes=2_000_000,
        )
    ).inventory(git_repo)

    assert len(result.files) == _COMPLETE_FILE_COUNT
    assert len(result.selected_for_model) == _MODEL_FILE_COUNT
    assert result.truncated is False


@pytest.mark.parametrize(
    ("limits", "limit_name"),
    [
        (InventoryLimits(max_files=1), "max_files"),
        (InventoryLimits(max_total_bytes=1), "max_total_bytes"),
    ],
)
def test_hard_inventory_bounds_raise_typed_complete_error(
    git_repo: Path,
    limits: InventoryLimits,
    limit_name: str,
) -> None:
    """Fail with measured totals and remediation instead of truncating."""
    with pytest.raises(RepositoryInventoryLimitError) as raised:
        RepositoryInventoryService(limits=limits).inventory(git_repo)

    error = raised.value
    assert error.file_count == _FIXTURE_FILE_COUNT
    assert error.total_bytes > 1
    assert error.max_files == limits.max_files
    assert error.max_total_bytes == limits.max_total_bytes
    assert limit_name in str(error)
    assert "Increase InventoryLimits" in error.remediation


def test_status_change_during_hashing_rejects_mixed_snapshot(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject inventory if the porcelain-status fingerprint changes."""
    original = inventory_module._hash_file
    changed = False

    def mutate_then_hash(path: Path) -> str:
        nonlocal changed
        if not changed:
            changed = True
            (git_repo / "tracked.py").write_text("TRACKED = False\n", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(inventory_module, "_hash_file", mutate_then_hash)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)


def test_git_resources_close_after_success_and_limit_failure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close GitPython command resources after every scan outcome."""
    original = Repo.close
    close_count = 0

    def counted_close(repo: Repo) -> None:
        nonlocal close_count
        close_count += 1
        original(repo)

    monkeypatch.setattr(Repo, "close", counted_close)

    RepositoryInventoryService().inventory(git_repo)
    with pytest.raises(RepositoryInventoryLimitError):
        RepositoryInventoryService(limits=InventoryLimits(max_files=1)).inventory(
            git_repo
        )

    assert close_count >= _FIXTURE_FILE_COUNT


def test_non_git_fallback_uses_fixed_ignore_policy(tmp_path: Path) -> None:
    """Report absent Git provenance and never descend into fixed ignored paths."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    first = RepositoryInventoryService().inventory(root)
    second = RepositoryInventoryService().inventory(root)

    assert _paths(first) == (".env", "app.py")
    assert first.git_available is False
    assert first.commit is None
    assert first.dirty is False
    assert first.inventory_fingerprint == second.inventory_fingerprint
    assert first.selected_for_model == ("app.py",)
