"""Git-aware complete repository inventory tests."""

from __future__ import annotations

import hashlib
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
_POST_HASH_CAPTURE_NUMBER = 2
_SECRET_PATHS = (
    ".env",
    ".npmrc",
    ".env.production",
    ".netrc",
    ".pypirc",
    ".aws/credentials",
    ".azure/accessTokens.json",
    ".config/gh/hosts.yml",
    ".config/gcloud/application_default_credentials.json",
    ".docker/config.json",
    ".git-credentials",
    ".kube/config",
    ".ssh/custom-deploy-key",
    ".ssh/id_rsa",
    "api-key.txt",
    "api_token.txt",
    "apiToken.txt",
    "config/service-account.json",
    "credentials/client.json",
    "db-password.txt",
    "dbPassword.txt",
    "oauth-credential.json",
    "password.txt",
    "private-keys/deploy.txt",
    "prod.env",
    "refresh_token.txt",
    "secrets/production.json",
    "token.txt",
)
_SAFE_SECRET_SHAPED_PATHS = (
    ".env.dist",
    ".env.example",
    ".env.production.sample",
    ".env.sample",
    ".env.template",
    "auth/token_service.py",
    "docs/private-key-rotation.md",
    "docs/secrets-management.md",
    "schemas/credential_model.ts",
    "security/password_policy.py",
    "src/api_key_parser.py",
    "src/passwordless_auth.py",
    "templates/service-account.json.example",
)


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


def _index_state(root: Path) -> tuple[str, int]:
    index_path = root / ".git" / "index"
    digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
    return digest, index_path.stat().st_mtime_ns


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


def test_secret_path_policy_suppresses_common_credentials_without_reading(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Represent common credential paths without hashing or selecting them."""
    secret_files: set[Path] = set()
    for relative_path in _SECRET_PATHS:
        path = git_repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("credential material\n", encoding="utf-8")
        secret_files.add(path)

    original = inventory_module._hash_file

    def reject_secret_read(
        root_descriptor: int,
        candidate: inventory_module._InventoryCandidate,
    ) -> str:
        assert candidate.absolute_path not in secret_files
        return original(root_descriptor, candidate)

    monkeypatch.setattr(inventory_module, "_hash_file", reject_secret_read)

    result = RepositoryInventoryService().inventory(git_repo)

    by_path = {item.path: item for item in result.files}
    for relative_path in _SECRET_PATHS:
        assert by_path[relative_path].content_status == "secret"
        assert by_path[relative_path].sha256 is None
        assert relative_path not in result.selected_for_model


def test_secret_path_policy_keeps_source_docs_and_templates_model_eligible(
    git_repo: Path,
) -> None:
    """Do not suppress non-secret source, documentation, or example templates."""
    for relative_path in _SAFE_SECRET_SHAPED_PATHS:
        path = git_repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe example material\n", encoding="utf-8")

    result = RepositoryInventoryService().inventory(git_repo)

    by_path = {item.path: item for item in result.files}
    for relative_path in _SAFE_SECRET_SHAPED_PATHS:
        assert by_path[relative_path].content_status == "hashable"
        assert by_path[relative_path].sha256 is not None
        assert relative_path in result.selected_for_model


def test_final_file_symlink_swap_is_rejected_before_external_target_read(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a final-component symlink swap before reading outside the root."""
    tracked = git_repo / "tracked.py"
    external = tmp_path / "external-secret.txt"
    external.write_text("must never be read\n", encoding="utf-8")
    real_open = os.open
    real_read = os.read
    swapped = False
    reads_after_swap: list[int] = []

    def adversarial_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "tracked.py" and dir_fd is not None and not swapped:
            tracked.unlink()
            tracked.symlink_to(external)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_read(descriptor: int, length: int) -> bytes:
        if swapped:
            reads_after_swap.append(descriptor)
        return real_read(descriptor, length)

    monkeypatch.setattr(inventory_module.os, "open", adversarial_open)
    monkeypatch.setattr(inventory_module.os, "read", guarded_read)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)

    assert swapped is True
    assert reads_after_swap == []


def test_intermediate_directory_symlink_swap_is_rejected_before_external_read(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a directory-component symlink swap before opening its target file."""
    nested = git_repo / "nested"
    nested.mkdir()
    (nested / "module.py").write_text("LOCAL = True\n", encoding="utf-8")
    external = tmp_path / "external-directory"
    external.mkdir()
    (external / "module.py").write_text("must never be read\n", encoding="utf-8")
    moved = git_repo / "nested-before-swap"
    real_open = os.open
    real_read = os.read
    swapped = False
    reads_after_swap: list[int] = []

    def adversarial_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "nested" and dir_fd is not None and not swapped:
            nested.rename(moved)
            nested.symlink_to(external, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_read(descriptor: int, length: int) -> bytes:
        if swapped:
            reads_after_swap.append(descriptor)
        return real_read(descriptor, length)

    monkeypatch.setattr(inventory_module.os, "open", adversarial_open)
    monkeypatch.setattr(inventory_module.os, "read", guarded_read)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)

    assert swapped is True
    assert reads_after_swap == []


@pytest.mark.parametrize("fail_during_read", [False, True])
def test_inventory_closes_descriptors_on_success_and_read_failure(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_during_read: bool,
) -> None:
    """Close root, component, and file descriptors on every hashing outcome."""
    real_open = os.open
    real_close = os.close
    real_read = os.read
    opened: set[int] = set()
    closed: set[int] = set()
    target_descriptor: int | None = None

    def tracked_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal target_descriptor
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == git_repo or dir_fd is not None:
            opened.add(descriptor)
        if path == "tracked.py" and dir_fd is not None:
            target_descriptor = descriptor
        return descriptor

    def tracked_close(descriptor: int) -> None:
        if descriptor in opened:
            closed.add(descriptor)
        real_close(descriptor)

    def optionally_failing_read(descriptor: int, length: int) -> bytes:
        if fail_during_read and descriptor == target_descriptor:
            message = "adversarial descriptor read failure"
            raise OSError(message)
        return real_read(descriptor, length)

    monkeypatch.setattr(inventory_module.os, "open", tracked_open)
    monkeypatch.setattr(inventory_module.os, "close", tracked_close)
    monkeypatch.setattr(inventory_module.os, "read", optionally_failing_read)

    if fail_during_read:
        with pytest.raises(RepositoryChangedDuringInventoryError):
            RepositoryInventoryService().inventory(git_repo)
    else:
        RepositoryInventoryService().inventory(git_repo)

    assert target_descriptor is not None
    assert opened
    assert opened <= closed


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

    def mutate_then_hash(
        root_descriptor: int,
        candidate: inventory_module._InventoryCandidate,
    ) -> str:
        nonlocal changed
        if not changed:
            changed = True
            (git_repo / "tracked.py").write_text("TRACKED = False\n", encoding="utf-8")
        return original(root_descriptor, candidate)

    monkeypatch.setattr(inventory_module, "_hash_file", mutate_then_hash)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)


def test_already_dirty_same_size_change_rejects_mixed_snapshot(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detect content changes even when porcelain status and size stay fixed."""
    tracked = git_repo / "tracked.py"
    tracked.write_bytes(b"A" * 32)
    original = inventory_module._hash_file
    changed = False

    def hash_then_mutate(
        root_descriptor: int,
        candidate: inventory_module._InventoryCandidate,
    ) -> str:
        nonlocal changed
        digest = original(root_descriptor, candidate)
        if candidate.absolute_path == tracked and not changed:
            changed = True
            tracked.write_bytes(b"B" * 32)
        return digest

    monkeypatch.setattr(inventory_module, "_hash_file", hash_then_mutate)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)


def test_already_dirty_metadata_change_rejects_mixed_snapshot(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind relevant lstat metadata even when Git's dirty text is unchanged."""
    tracked = git_repo / "tracked.py"
    tracked.write_bytes(b"A" * 32)
    original = inventory_module._hash_file
    changed = False

    def hash_then_chmod(
        root_descriptor: int,
        candidate: inventory_module._InventoryCandidate,
    ) -> str:
        nonlocal changed
        digest = original(root_descriptor, candidate)
        if candidate.absolute_path == tracked and not changed:
            changed = True
            candidate.absolute_path.chmod(0o600)
        return digest

    monkeypatch.setattr(inventory_module, "_hash_file", hash_then_chmod)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)


def test_hash_revalidation_detects_change_after_metadata_snapshot(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rehash safe content after the first post-hash metadata capture."""
    tracked = git_repo / "tracked.py"
    tracked.write_bytes(b"A" * 32)
    original = inventory_module._candidates
    captures = 0

    def mutate_after_second_capture(
        root: Path,
        paths: tuple[str, ...],
    ) -> tuple[inventory_module._InventoryCandidate, ...]:
        nonlocal captures
        candidates = original(root, paths)
        captures += 1
        if captures == _POST_HASH_CAPTURE_NUMBER:
            tracked.write_bytes(b"B" * 32)
        return candidates

    monkeypatch.setattr(inventory_module, "_candidates", mutate_after_second_capture)

    with pytest.raises(RepositoryChangedDuringInventoryError):
        RepositoryInventoryService().inventory(git_repo)


def test_inventory_fingerprint_binds_selection_and_repository_state(
    tmp_path: Path,
) -> None:
    """Fingerprint model inputs and Git provenance, not only file metadata."""
    root = tmp_path / "fingerprint"
    root.mkdir()
    (root / "app.py").write_text("print('app')\n", encoding="utf-8")
    (root / "README.md").write_text("# App\n", encoding="utf-8")

    one_selected = RepositoryInventoryService(
        limits=InventoryLimits(max_model_files=1)
    ).inventory(root)
    two_selected = RepositoryInventoryService(
        limits=InventoryLimits(max_model_files=2)
    ).inventory(root)
    assert one_selected.files == two_selected.files
    assert one_selected.selected_for_model != two_selected.selected_for_model
    assert one_selected.inventory_fingerprint != two_selected.inventory_fingerprint

    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Inventory Test")
            config.set_value("user", "email", "inventory@example.com")
        repo.index.add(["README.md", "app.py"])
        repo.index.commit("same files under Git")

    git_result = RepositoryInventoryService(
        limits=InventoryLimits(max_model_files=1)
    ).inventory(root)
    assert git_result.files == one_selected.files
    assert git_result.selected_for_model == one_selected.selected_for_model
    assert git_result.inventory_fingerprint != one_selected.inventory_fingerprint


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param(None, id="success"),
        pytest.param(InventoryLimits(max_files=1), id="limit-failure"),
    ],
)
def test_git_inventory_preserves_index_bytes_and_mtime(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: InventoryLimits | None,
) -> None:
    """Disable optional locks for every successful and failed Git inspection."""
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)
    tracked = git_repo / "tracked.py"
    metadata = tracked.stat()
    os.utime(
        tracked,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
    )
    before = _index_state(git_repo)
    service = RepositoryInventoryService(limits=limits)

    if limits is None:
        service.inventory(git_repo)
    else:
        with pytest.raises(RepositoryInventoryLimitError):
            service.inventory(git_repo)

    assert _index_state(git_repo) == before


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
