"""Deterministic Git repository probe tests."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from git import Repo

import adapters.git.repository_probe as adapter_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryStatusEntry,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import Popen  # nosec B404  # type-only process annotation


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    """Create one committed repository with local-only Git identity."""
    root = tmp_path / "repository"
    root.mkdir()
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Repository Probe Test")
            config.set_value("user", "email", "repository-probe@example.com")
        for name in ("deleted.txt", "renamed-old.txt", "tracked.txt"):
            (root / name).write_text(f"{name}\n", encoding="utf-8")
        repo.index.add(["deleted.txt", "renamed-old.txt", "tracked.txt"])
        repo.index.commit("repository probe fixture")
    return root


def _status_sort_key(entry: RepositoryStatusEntry) -> tuple[str, str, bytes, bytes]:
    """Mirror the required stable ordering without inspecting implementation."""
    return (
        entry.area,
        entry.change,
        os.fsencode(entry.path),
        os.fsencode(entry.previous_path or ""),
    )


def test_missing_path_has_typed_error(tmp_path: Path) -> None:
    """Reject missing roots with a stable typed error."""
    path = tmp_path / "missing"
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(path)
    assert caught.value.code is RepositoryProbeErrorCode.PATH_MISSING
    assert caught.value.path == str(path)


def test_non_repository_directory_has_typed_error(tmp_path: Path) -> None:
    """Reject ordinary directories without leaking GitPython errors."""
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(tmp_path)
    assert caught.value.code is RepositoryProbeErrorCode.NOT_GIT_WORKTREE


def test_unborn_head_has_typed_error(tmp_path: Path) -> None:
    """Reject repositories that do not yet have an inspectable commit."""
    Repo.init(tmp_path)
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(tmp_path)
    assert caught.value.code is RepositoryProbeErrorCode.UNBORN_HEAD


def test_clean_branch_returns_identity_and_empty_status(git_repository: Path) -> None:
    """Return committed worktree identity without a synthetic warning."""
    repo = Repo(git_repository)

    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.worktree_path == str(git_repository)
    assert result.common_git_dir == str(git_repository / ".git")
    assert result.head_sha == repo.head.commit.hexsha
    assert result.branch_name == repo.active_branch.name
    assert result.detached_head is False
    assert result.dirty is False
    assert result.status_entries == ()
    assert result.warnings == ()
    assert result.probe_version == "agileforge.repository-probe.v1"


@pytest.mark.parametrize("fail_head_read", [False, True], ids=["success", "error"])
def test_probe_reaps_git_process_before_returning_or_raising(
    git_repository: Path,
    fail_head_read: bool,
) -> None:
    """Release the owned Git subprocess without relying on garbage collection."""
    opened_repositories: list[Repo] = []
    processes: list[Popen[bytes]] = []

    def read_head(repo: Repo) -> str:
        if not opened_repositories:
            opened_repositories.append(repo)
            # HEAD validation has already started the real cached cat-file process.
            cached_command = repo.git.cat_file_header
            assert cached_command is not None
            process = cached_command.proc
            assert process is not None
            processes.append(process)
        if fail_head_read:
            message = "HEAD metadata became unreadable"
            raise OSError(message)
        return repo.head.commit.hexsha

    probe = GitPythonRepositoryProbe(_read_head_sha=read_head)
    try:
        if fail_head_read:
            with pytest.raises(RepositoryProbeError) as caught:
                probe.inspect(git_repository)
            assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
        else:
            assert probe.inspect(git_repository).dirty is False

        assert len(processes) == 1
        # Do not poll here: the probe must have waited for its process already.
        assert processes[0].returncode is not None
        assert opened_repositories[0].git.cat_file_header is None
    finally:
        # Also release real resources when exercising the unfixed implementation.
        for repo in opened_repositories:
            repo.close()


def test_staged_unstaged_deleted_renamed_and_untracked_entries_are_sorted(
    git_repository: Path,
) -> None:
    """Represent all Git status areas in a deterministic order."""
    repo = Repo(git_repository)
    (git_repository / "staged.txt").write_text("staged\n", encoding="utf-8")
    repo.index.add(["staged.txt"])
    (git_repository / "deleted.txt").unlink()
    repo.index.remove(["deleted.txt"])
    repo.git.mv("renamed-old.txt", "renamed-new.txt")
    (git_repository / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (git_repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    entries = GitPythonRepositoryProbe().inspect(git_repository).status_entries

    actual_entries = {
        (entry.area, entry.change, entry.path, entry.previous_path) for entry in entries
    }
    assert actual_entries >= {
        ("index", "added", "staged.txt", None),
        ("index", "deleted", "deleted.txt", None),
        ("index", "renamed", "renamed-new.txt", "renamed-old.txt"),
        ("worktree", "modified", "tracked.txt", None),
        ("untracked", "added", "untracked.txt", None),
    }
    assert entries == tuple(sorted(entries, key=_status_sort_key))


def test_dirty_probe_returns_dirty_warning(git_repository: Path) -> None:
    """Return the one closed warning when observable status is non-empty."""
    (git_repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.dirty is True
    assert result.warnings[0].code == "DIRTY_WORKTREE"
    assert result.warnings[0].message == "Repository worktree contains changes."


def test_detached_head_succeeds_without_branch_name(git_repository: Path) -> None:
    """Preserve a detached commit without attempting active branch access."""
    repo = Repo(git_repository)
    repo.git.checkout(repo.head.commit.hexsha)

    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.detached_head is True
    assert result.branch_name is None


def test_linked_worktree_uses_worktree_root_and_shared_common_dir(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    """Distinguish a linked checkout root from its shared Git directory."""
    linked = tmp_path / "linked"
    repo = Repo(git_repository)
    repo.git.worktree("add", "-b", "linked-probe", str(linked))

    result = GitPythonRepositoryProbe().inspect(linked)

    assert result.worktree_path == str(linked)
    assert result.common_git_dir == str(git_repository / ".git")


def test_zero_one_and_multiple_remote_urls_are_sorted(git_repository: Path) -> None:
    """Probe all configured remote URLs without network access."""
    repo = Repo(git_repository)
    probe = GitPythonRepositoryProbe()

    assert probe.inspect(git_repository).remotes == ()
    repo.create_remote("zulu", "ssh://example.test/zulu.git")
    assert probe.inspect(git_repository).remotes == ("ssh://example.test/zulu.git",)
    repo.create_remote("alpha", "ssh://example.test/alpha.git")

    assert probe.inspect(git_repository).remotes == (
        "ssh://example.test/alpha.git",
        "ssh://example.test/zulu.git",
    )

    repo.create_remote("local", str(git_repository / "private-source"))

    result = probe.inspect(git_repository)
    assert result.remotes == (
        "ssh://example.test/alpha.git",
        "ssh://example.test/zulu.git",
    )
    assert [warning.code for warning in result.warnings] == ["REMOTE_OMITTED"]


@pytest.mark.parametrize(
    ("remote_url", "expected_identity"),
    [
        (
            "https://operator:CREDENTIAL_SENTINEL@example.test/team/repo.git"
            "?access_token=QUERY_SENTINEL#configured",
            "https://example.test/team/repo.git",
        ),
        (
            "ssh://CREDENTIAL_SENTINEL@example.test/team/repo.git",
            "ssh://example.test/team/repo.git",
        ),
        (
            "CREDENTIAL_SENTINEL@example.test:team/repo.git",
            "example.test:team/repo.git",
        ),
        (
            "CREDENTIAL_SENTINEL@example.test:team/repo.git"
            "?access_token=QUERY_SENTINEL#configured",
            "example.test:team/repo.git",
        ),
    ],
)
def test_remote_identity_excludes_credentials_before_leaving_probe(
    git_repository: Path,
    remote_url: str,
    expected_identity: str,
) -> None:
    """Expose scheme, host, and path without URL or SCP-style userinfo."""
    Repo(git_repository).create_remote("origin", remote_url)

    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.remotes == (expected_identity,)
    assert "CREDENTIAL_SENTINEL" not in repr(result)
    assert "QUERY_SENTINEL" not in repr(result)


@pytest.mark.parametrize(
    "remote_url",
    [
        "/private/host/repository.git",
        "../relative/repository.git",
        "file:///private/host/repository.git",
        "file:/private/host/repository.git",
        "C:/private/host/repository.git",
        "https:///missing-host.git",
        "not a remote URL",
    ],
)
def test_probe_omits_local_and_malformed_remote_locations(
    git_repository: Path,
    remote_url: str,
) -> None:
    """Host-local or malformed locations cannot enter portable evidence."""
    Repo(git_repository).create_remote("origin", remote_url)

    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.remotes == ()
    assert remote_url not in repr(result)


def test_non_ascii_and_surrogateescaped_paths_have_stable_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose surrogateescaped Git status through the public probe result."""
    raw_name = b"surrogate-\xff.txt"
    surrogate_path = os.fsdecode(raw_name)
    unicode_path = "cafe\u00e9.txt"
    worktree_path = str(tmp_path)
    common_git_dir = str(tmp_path / ".git")
    fake_repo = SimpleNamespace(
        bare=False,
        head=SimpleNamespace(
            commit=SimpleNamespace(hexsha="a" * 40),
            is_detached=False,
            is_valid=lambda: True,
        ),
        index=SimpleNamespace(diff=lambda _other: ()),
        untracked_files=(unicode_path, surrogate_path),
        remotes=(),
        working_tree_dir=worktree_path,
        common_dir=common_git_dir,
        active_branch=SimpleNamespace(name="main"),
        git=SimpleNamespace(clear_cache=lambda: None),
    )

    def repository_factory(*_args: object, **_kwargs: object) -> Repo:
        return cast("Repo", fake_repo)

    monkeypatch.setattr(adapter_module, "Repo", repository_factory)

    result = GitPythonRepositoryProbe().inspect(tmp_path)
    expected_entries = tuple(
        sorted(
            (
                RepositoryStatusEntry(
                    area="untracked",
                    change="added",
                    path=unicode_path,
                ),
                RepositoryStatusEntry(
                    area="untracked",
                    change="added",
                    path=surrogate_path,
                ),
            ),
            key=lambda entry: os.fsencode(entry.path),
        )
    )
    expected_fingerprint = canonical_hash(
        {
            "probe_version": "agileforge.repository-probe.v1",
            "head_sha": "a" * 40,
            "branch_name": "main",
            "detached_head": False,
            "dirty": True,
            "status_entries": [
                entry.model_dump(mode="json") for entry in expected_entries
            ],
            "remotes": (),
            "remote_omitted": False,
        }
    )

    assert result.status_entries == expected_entries
    assert result.status_fingerprint == expected_fingerprint


def test_regular_file_path_returns_path_not_directory(git_repository: Path) -> None:
    """Reject file roots before asking GitPython to open them."""
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository / "tracked.txt")
    assert caught.value.code is RepositoryProbeErrorCode.PATH_NOT_DIRECTORY


def test_unreadable_git_metadata_has_typed_error(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collapse Git metadata read failures into the closed error contract."""

    def inaccessible_repo(*_args: object, **_kwargs: object) -> Repo:
        message = "metadata denied"
        raise OSError(message)

    monkeypatch.setattr(adapter_module, "Repo", inaccessible_repo)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE


def test_malformed_path_has_typed_error() -> None:
    """Reject paths that cannot be represented by the local filesystem."""
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect("bad\x00path")
    assert caught.value.code is RepositoryProbeErrorCode.MALFORMED_PATH


def test_head_change_during_probe_writes_no_result(git_repository: Path) -> None:
    """Fail closed when the checked commit differs during one observation."""
    shas = iter(("a" * 40, "b" * 40))

    def read_head(_repo: Repo) -> str:
        return next(shas)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe(_read_head_sha=read_head).inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE


def test_equivalent_probe_replays_the_same_status_fingerprint(
    git_repository: Path,
) -> None:
    """Hash equivalent observations identically across repeated probes."""
    (git_repository / "same.txt").write_text("same\n", encoding="utf-8")
    probe = GitPythonRepositoryProbe()

    first = probe.inspect(git_repository)
    second = probe.inspect(git_repository)

    assert first.status_fingerprint == second.status_fingerprint


def test_status_fingerprint_is_portable_across_checkout_roots(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    """Ignore checkout-local absolute paths while retaining Git semantics."""
    relocated = tmp_path / "relocated-repository"
    shutil.copytree(git_repository, relocated)
    probe = GitPythonRepositoryProbe()

    original = probe.inspect(git_repository)
    copy = probe.inspect(relocated)

    assert original.worktree_path != copy.worktree_path
    assert original.common_git_dir != copy.common_git_dir
    assert original.head_sha == copy.head_sha
    assert original.status_entries == copy.status_entries
    assert original.status_fingerprint == copy.status_fingerprint


def test_status_fingerprint_changes_when_untracked_path_changes(
    git_repository: Path,
) -> None:
    """Bind a dirty result to its exact normalized status paths."""
    (git_repository / "first.txt").write_text("same\n", encoding="utf-8")
    probe = GitPythonRepositoryProbe()
    first = probe.inspect(git_repository)
    (git_repository / "first.txt").rename(git_repository / "second.txt")

    second = probe.inspect(git_repository)

    assert first.status_fingerprint != second.status_fingerprint
