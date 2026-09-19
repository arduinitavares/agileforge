# cli/repository_transfer.py
"""Constrained Git administrative repair for restored linked worktrees."""

# ruff: noqa: EM101, EM102, TRY003

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, cast

from cli.state_transfer import (
    RestoredRepository,
    TransferManifest,
    install_approved_git_config,
    restored_repositories,
    verify_restored_repositories,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_GIT_POINTER_PREFIX = "gitdir: "
_GIT_POINTER_MAX_BYTES = 4096
_REPAIR_TIMEOUT_SECONDS = 30
_GETUID = cast("Callable[[], int] | None", getattr(os, "getuid", None))
_RELOCATION_FORMAT = "agileforge.repository-relocations.v1"
_RELOCATION_NAME = "repository-relocations.json"


class RepositoryTransferError(RuntimeError):
    """Raised when restored Git metadata cannot be repaired safely."""


@dataclass(frozen=True, slots=True)
class RepositoryRelocation:
    """One Project whose active binding must move to a restored worktree."""

    project_id: int
    source_path: str
    restored_path: str

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""
        return {
            "project_id": self.project_id,
            "source_path": self.source_path,
            "restored_path": self.restored_path,
        }


def _current_uid() -> int:
    if _GETUID is None:
        raise RepositoryTransferError("Git relocation is unsupported on this platform")
    return _GETUID()


def _owned_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RepositoryTransferError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RepositoryTransferError(f"{label} must be a real directory")
    if resolved != candidate:
        raise RepositoryTransferError(f"{label} must not contain a symlink")
    if metadata.st_uid != _current_uid():
        raise RepositoryTransferError(f"{label} is not owned by this process user")
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise RepositoryTransferError(f"{label} has a privileged mode")
    return resolved


def _owned_regular(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RepositoryTransferError(f"{label} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RepositoryTransferError(f"{label} must be a regular file")
    if metadata.st_uid != _current_uid():
        raise RepositoryTransferError(f"{label} is not owned by this process user")
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise RepositoryTransferError(f"{label} has a privileged mode")
    return path


def _git_pointer(path: Path, *, label: str) -> Path:
    pointer = _owned_regular(path, label=label)
    try:
        content = pointer.read_bytes()
        if len(content) > _GIT_POINTER_MAX_BYTES:
            raise RepositoryTransferError(f"{label} is too large")
        line = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RepositoryTransferError(f"{label} is not UTF-8") from error
    if not line.startswith(_GIT_POINTER_PREFIX):
        raise RepositoryTransferError(f"{label} is malformed")
    raw = line.removeprefix(_GIT_POINTER_PREFIX)
    if not raw or any(character in raw for character in ("\x00", "\r", "\n")):
        raise RepositoryTransferError(f"{label} is malformed")
    target = Path(raw)
    return target if target.is_absolute() else pointer.parent / target


def _repair_environment() -> dict[str, str]:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
    }
    if path := os.environ.get("PATH"):
        environment["PATH"] = path
    return environment


def _verify_repaired_link(main_git: Path, linked: Path) -> None:
    pointer = _git_pointer(linked / ".git", label="linked worktree Git pointer")
    try:
        admin = pointer.resolve(strict=True)
    except OSError as error:
        raise RepositoryTransferError(
            "repaired Git directory is unavailable"
        ) from error
    worktree_admin = main_git / "worktrees"
    if not admin.is_relative_to(worktree_admin):
        raise RepositoryTransferError(
            "repaired Git directory escapes the main repository"
        )
    _owned_directory(admin, label="linked worktree Git directory")

    backlink = _owned_regular(admin / "gitdir", label="linked worktree backlink")
    try:
        backlink_target = Path(backlink.read_text(encoding="utf-8").strip())
    except UnicodeDecodeError as error:
        raise RepositoryTransferError(
            "linked worktree backlink is not UTF-8"
        ) from error
    if backlink_target.resolve(strict=True) != (linked / ".git").resolve(strict=True):
        raise RepositoryTransferError("linked worktree backlink does not match")

    common = _owned_regular(admin / "commondir", label="linked common-dir pointer")
    try:
        common_target = Path(common.read_text(encoding="utf-8").strip())
    except UnicodeDecodeError as error:
        raise RepositoryTransferError(
            "linked common-dir pointer is not UTF-8"
        ) from error
    if not common_target.is_absolute():
        common_target = admin / common_target
    if common_target.resolve(strict=True) != main_git:
        raise RepositoryTransferError("linked common-dir pointer does not match")


def repair_linked_worktrees(main: Path, linked: tuple[Path, ...]) -> None:
    """Repair moved linked worktrees using Git's fixed native repair operation.

    The caller supplies already restored worktree roots. This function accepts no
    Git arguments or configuration, disables system/global config and hooks, and
    verifies every rewritten pointer stays inside the restored main repository.
    """
    main_root = _owned_directory(main, label="main worktree")
    main_git = _owned_directory(main_root / ".git", label="main Git directory")
    linked_roots = tuple(
        _owned_directory(path, label="linked worktree") for path in linked
    )
    if not linked_roots:
        return
    if len(linked_roots) != len(set(linked_roots)) or main_root in linked_roots:
        raise RepositoryTransferError("worktree roots must be unique")
    for root in linked_roots:
        if root.is_relative_to(main_root) or main_root.is_relative_to(root):
            raise RepositoryTransferError("worktree roots must not overlap")
        _git_pointer(root / ".git", label="linked worktree Git pointer")

    command = [
        "git",
        "-C",
        os.fspath(main_root),
        "worktree",
        "repair",
        "--",
        *(os.fspath(root) for root in linked_roots),
    ]
    try:
        subprocess.run(  # noqa: S603 # nosec B603 - fixed Git operation and argv
            command,
            check=True,
            capture_output=True,
            env=_repair_environment(),
            text=False,
            timeout=_REPAIR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RepositoryTransferError("Git worktree repair failed") from error

    for root in linked_roots:
        _verify_repaired_link(main_git, root)


def finalize_restored_repositories(
    manifest: TransferManifest,
    payload_root: Path,
) -> tuple[RestoredRepository, ...]:
    """Repair, install approved config, and verify every restored Git group."""
    repositories = restored_repositories(manifest, payload_root)
    groups: dict[str, list[RestoredRepository]] = {}
    for repository in repositories:
        groups.setdefault(repository.common_repository, []).append(repository)
    for common_repository in sorted(groups):
        group = groups[common_repository]
        mains = [item for item in group if (item.worktree / ".git").is_dir()]
        linked = [item for item in group if (item.worktree / ".git").is_file()]
        if len(mains) != 1 or len(mains) + len(linked) != len(group):
            raise RepositoryTransferError(
                "restored repository group has invalid main/linked membership"
            )
        repair_linked_worktrees(
            mains[0].worktree,
            tuple(
                item.worktree for item in sorted(linked, key=lambda item: item.index)
            ),
        )
    install_approved_git_config(repositories)
    verify_restored_repositories(repositories)
    return repositories


def _active_bindings(database: Path) -> dict[int, str]:
    try:
        connection = sqlite3.connect(
            f"{database.resolve(strict=True).as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        rows = connection.execute(
            "SELECT p.project_id, b.worktree_path "
            "FROM projects AS p JOIN repository_bindings AS b "
            "ON b.repository_binding_id = p.active_repository_binding_id "
            "WHERE p.active_repository_binding_id IS NOT NULL "
            "ORDER BY p.project_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise RepositoryTransferError(
            "active repository bindings are unavailable"
        ) from error
    finally:
        if "connection" in locals():
            connection.close()
    bindings: dict[int, str] = {}
    for project_id, source_path in rows:
        if (
            not isinstance(project_id, int)
            or not isinstance(source_path, str)
            or not (
                Path(source_path).is_absolute()
                or PureWindowsPath(source_path).is_absolute()
            )
        ):
            raise RepositoryTransferError("active repository binding is invalid")
        bindings[project_id] = source_path
    return bindings


def _write_atomic(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise RepositoryTransferError("repository relocation record already exists")
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=".relocations-", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def write_relocation_record(
    manifest: TransferManifest,
    payload_root: Path,
    profile_root: Path,
) -> Path:
    """Persist a non-secret source-to-restored Project mapping after restore."""
    payload = _owned_directory(payload_root, label="restored payload root")
    profile = _owned_directory(profile_root, label="profile root")
    by_source = {
        item.source_path: str(item.worktree)
        for item in restored_repositories(manifest, payload)
    }
    active = _active_bindings(payload / "business.sqlite3")
    relocations: list[RepositoryRelocation] = []
    for project_id, source_path in active.items():
        restored_path = by_source.get(source_path)
        if restored_path is None:
            raise RepositoryTransferError(
                "active repository has no restored payload mapping"
            )
        relocations.append(RepositoryRelocation(project_id, source_path, restored_path))
    document = {
        "format": _RELOCATION_FORMAT,
        "repositories": [item.to_dict() for item in relocations],
    }
    target = profile / _RELOCATION_NAME
    content = (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    _write_atomic(target, content)
    return target


def _read_relocation_record(profile_root: Path) -> tuple[RepositoryRelocation, ...]:
    profile = _owned_directory(profile_root, label="profile root")
    record = profile / _RELOCATION_NAME
    if not record.exists():
        return ()
    _owned_regular(record, label="repository relocation record")
    try:
        document = json.loads(record.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RepositoryTransferError(
            "repository relocation record is invalid"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"format", "repositories"}
        or document.get("format") != _RELOCATION_FORMAT
        or not isinstance(document.get("repositories"), list)
    ):
        raise RepositoryTransferError("repository relocation record is invalid")
    relocations: list[RepositoryRelocation] = []
    for raw in document["repositories"]:
        if not isinstance(raw, dict) or set(raw) != {
            "project_id",
            "source_path",
            "restored_path",
        }:
            raise RepositoryTransferError("repository relocation entry is invalid")
        project_id = raw.get("project_id")
        source_path = raw.get("source_path")
        restored_path = raw.get("restored_path")
        if (
            not isinstance(project_id, int)
            or isinstance(project_id, bool)
            or project_id <= 0
            or not isinstance(source_path, str)
            or not (
                Path(source_path).is_absolute()
                or PureWindowsPath(source_path).is_absolute()
            )
            or not isinstance(restored_path, str)
            or not Path(restored_path).is_absolute()
        ):
            raise RepositoryTransferError("repository relocation entry is invalid")
        relocations.append(RepositoryRelocation(project_id, source_path, restored_path))
    if [item.project_id for item in relocations] != sorted(
        {item.project_id for item in relocations}
    ):
        raise RepositoryTransferError("repository relocation entries are not unique")
    return tuple(relocations)


def pending_relocations(
    database: Path,
    profile_root: Path,
) -> tuple[RepositoryRelocation, ...]:
    """Return bindings still pointing at pre-restore source paths; fail on drift."""
    relocations = _read_relocation_record(profile_root)
    if not relocations:
        return ()
    active = _active_bindings(database)
    pending: list[RepositoryRelocation] = []
    for relocation in relocations:
        current = active.get(relocation.project_id)
        if current == relocation.source_path:
            pending.append(relocation)
        elif current != relocation.restored_path:
            raise RepositoryTransferError("repository relocation binding diverged")
    return tuple(pending)


__all__ = [
    "RepositoryRelocation",
    "RepositoryTransferError",
    "finalize_restored_repositories",
    "pending_relocations",
    "repair_linked_worktrees",
    "write_relocation_record",
]
