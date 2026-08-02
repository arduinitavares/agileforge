"""Complete Git-aware repository inventory with bounded model selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from git import InvalidGitRepositoryError, Repo

from workflow.repository_inventory import (
    InventoryFingerprintEntry,
    canonical_inventory_payload,
    inventory_binding_fingerprint,
    repository_path_bytes,
)


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if isinstance(value, int):
        return value
    message = f"Secure repository inventory requires os.{name}."
    raise RuntimeError(message)


_HASH_CHUNK_BYTES = 1024 * 1024
_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = _required_open_flag("O_DIRECTORY")
_NO_FOLLOW = _required_open_flag("O_NOFOLLOW")
_NONBLOCK = _required_open_flag("O_NONBLOCK")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | _DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
)
_HASHABLE_FILE_OPEN_FLAGS = (
    os.O_RDONLY | _NO_FOLLOW | _NONBLOCK | _CLOSE_ON_EXEC
)
_FALLBACK_IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
_FALLBACK_IGNORED_FILES: frozenset[str] = frozenset({".coverage", ".DS_Store"})
_SECRET_EXACT_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
        "service_account.json",
        "secrets.json",
    }
)
_SECRET_TERMINAL_TOKENS: frozenset[str] = frozenset(
    {
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SECRET_TERMINAL_TOKEN_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("api", "key"),
        ("private", "key"),
        ("service", "account"),
    }
)
_SECRET_ROOT_DIRECTORIES: frozenset[str] = frozenset(
    {
        "credential",
        "credentials",
        "private-key",
        "private-keys",
        "private_key",
        "private_keys",
        "secret",
        "secrets",
    }
)
_NON_SECRET_SSH_BASENAMES: frozenset[str] = frozenset(
    {"authorized_keys", "config", "known_hosts", "known_hosts.old"}
)
_SECRET_TEMPLATE_SUFFIXES: tuple[str, ...] = (
    ".dist",
    ".example",
    ".sample",
    ".template",
)
_ESTABLISHED_SECRET_PATHS: frozenset[str] = frozenset(
    {
        ".aws/credentials",
        ".azure/accesstokens.json",
        ".azure/azureprofile.json",
        ".config/gh/hosts.yml",
        ".config/gcloud/application_default_credentials.json",
        ".docker/config.json",
        ".kube/config",
    }
)
_SECRET_SUFFIXES: tuple[str, ...] = (
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
)
_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_CONFIG_SUFFIXES: frozenset[str] = frozenset(
    {".ini", ".json", ".toml", ".xml", ".yaml", ".yml"}
)
_CONFIG_BASENAMES: frozenset[str] = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        "dockerfile",
        "makefile",
    }
)
_DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".adoc", ".md", ".rst", ".txt"})


@dataclass(frozen=True)
class InventoryLimits:
    """Hard inventory limits and independent model-context limits."""

    max_files: int = 50_000
    max_total_bytes: int = 2_000_000_000
    max_hash_bytes_per_file: int = 10_000_000
    max_model_files: int = 500
    max_model_bytes: int = 2_000_000


@dataclass(frozen=True)
class InventoryFile:
    """One complete inventory entry with explicit content suppression state."""

    path: str
    size_bytes: int
    sha256: str | None
    content_status: Literal["hashable", "secret", "oversized", "symlink"]


@dataclass(frozen=True)
class RepositoryInventoryResult:
    """Verified complete repository inventory and bounded model selection."""

    root: Path
    git_available: bool
    commit: str | None
    dirty: bool
    files: tuple[InventoryFile, ...]
    selected_for_model: tuple[str, ...]
    total_bytes: int
    inventory_fingerprint: str
    truncated: Literal[False] = False


class RepositoryInventoryLimitError(RuntimeError):
    """Raised when a complete inventory exceeds a configured hard bound."""

    def __init__(
        self,
        *,
        file_count: int,
        total_bytes: int,
        max_files: int,
        max_total_bytes: int,
    ) -> None:
        """Retain measured totals, configured limits, and remediation."""
        self.file_count = file_count
        self.total_bytes = total_bytes
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.remediation = (
            "Increase InventoryLimits after reviewing repository capacity, "
            "or remove generated evidence from the Git-visible file set."
        )
        exceeded = []
        if file_count > max_files:
            exceeded.append("max_files")
        if total_bytes > max_total_bytes:
            exceeded.append("max_total_bytes")
        message = (
            "Repository inventory exceeds "
            f"{', '.join(exceeded)}: count={file_count}, bytes={total_bytes}, "
            f"max_files={max_files}, max_total_bytes={max_total_bytes}. "
            f"{self.remediation}"
        )
        super().__init__(message)


class RepositoryChangedDuringInventoryError(RuntimeError):
    """Raised when repository or filesystem state changes during hashing."""


@dataclass(frozen=True)
class _RepositoryState:
    commit: str | None
    status_fingerprint: str
    dirty: bool


@dataclass(frozen=True)
class _FilesystemSnapshot:
    device: int
    inode: int
    mode: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _InventoryCandidate:
    path: str
    absolute_path: Path
    size_bytes: int
    symlink: bool
    snapshot: _FilesystemSnapshot


@dataclass(frozen=True)
class _GitInventorySnapshot:
    state: _RepositoryState
    paths: tuple[str, ...]
    candidates: tuple[_InventoryCandidate, ...]


class RepositoryInventoryService:
    """Build complete deterministic inventories without model/provider calls."""

    def __init__(self, *, limits: InventoryLimits | None = None) -> None:
        """Use explicit limits or the acceptance-sized defaults."""
        self._limits = limits or InventoryLimits()

    def inventory(self, root: Path | str) -> RepositoryInventoryResult:
        """Inventory one Git repository or fixed-policy fallback directory."""
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            message = f"Repository inventory root is not a directory: {resolved}"
            raise NotADirectoryError(message)
        with ExitStack() as descriptors:
            try:
                root_metadata = resolved.lstat()
                root_descriptor = os.open(resolved, _DIRECTORY_OPEN_FLAGS)
                descriptors.callback(os.close, root_descriptor)
                opened_root_metadata = os.fstat(root_descriptor)
            except OSError as error:
                raise _repository_changed_error() from error
            root_snapshot = _filesystem_snapshot(root_metadata)
            _require_unchanged_snapshot(
                stat.S_ISDIR(root_metadata.st_mode)
                and root_snapshot == _filesystem_snapshot(opened_root_metadata)
            )
            try:
                with Repo(resolved) as repo:
                    result = self._git_inventory(resolved, root_descriptor, repo)
            except InvalidGitRepositoryError:
                result = self._fallback_inventory(resolved, root_descriptor)
            _verify_root_snapshot(resolved, root_descriptor, root_snapshot)
            return result

    def _git_inventory(
        self,
        root: Path,
        root_descriptor: int,
        repo: Repo,
    ) -> RepositoryInventoryResult:
        with repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"):
            before = _git_snapshot(root, repo)
            self._enforce_hard_limits(before.candidates)
            files = self._hash_candidates(root_descriptor, before.candidates)
            self._verify_git_snapshot(
                root=root,
                root_descriptor=root_descriptor,
                repo=repo,
                before=before,
                files=files,
            )
        return self._result(
            root=root,
            git_available=True,
            commit=before.state.commit,
            dirty=before.state.dirty,
            files=files,
        )

    def _fallback_inventory(
        self,
        root: Path,
        root_descriptor: int,
    ) -> RepositoryInventoryResult:
        paths = _fallback_visible_paths(root)
        candidates = _candidates(root, paths)
        self._enforce_hard_limits(candidates)
        files = self._hash_candidates(root_descriptor, candidates)
        self._verify_fallback_snapshot(
            root=root,
            root_descriptor=root_descriptor,
            paths=paths,
            candidates=candidates,
            files=files,
        )
        return self._result(
            root=root,
            git_available=False,
            commit=None,
            dirty=False,
            files=files,
        )

    def _verify_git_snapshot(
        self,
        *,
        root: Path,
        root_descriptor: int,
        repo: Repo,
        before: _GitInventorySnapshot,
        files: tuple[InventoryFile, ...],
    ) -> None:
        _require_unchanged_snapshot(before == _git_snapshot(root, repo))
        self._revalidate_hashes(root_descriptor, before.candidates, files)
        _require_unchanged_snapshot(before == _git_snapshot(root, repo))

    def _verify_fallback_snapshot(
        self,
        *,
        root: Path,
        root_descriptor: int,
        paths: tuple[str, ...],
        candidates: tuple[_InventoryCandidate, ...],
        files: tuple[InventoryFile, ...],
    ) -> None:
        after_paths = _fallback_visible_paths(root)
        after_candidates = _candidates(root, after_paths)
        _require_unchanged_snapshot(
            paths == after_paths and candidates == after_candidates
        )
        self._revalidate_hashes(root_descriptor, candidates, files)
        final_paths = _fallback_visible_paths(root)
        final_candidates = _candidates(root, final_paths)
        _require_unchanged_snapshot(
            paths == final_paths and candidates == final_candidates
        )

    def _revalidate_hashes(
        self,
        root_descriptor: int,
        candidates: tuple[_InventoryCandidate, ...],
        files: tuple[InventoryFile, ...],
    ) -> None:
        candidates_by_path = {item.path: item for item in candidates}
        for item in files:
            if item.content_status != "hashable":
                continue
            candidate = candidates_by_path[item.path]
            _require_unchanged_snapshot(
                _hash_file(root_descriptor, candidate) == item.sha256
            )

    def _enforce_hard_limits(
        self,
        candidates: tuple[_InventoryCandidate, ...],
    ) -> None:
        total_bytes = sum(item.size_bytes for item in candidates)
        if (
            len(candidates) > self._limits.max_files
            or total_bytes > self._limits.max_total_bytes
        ):
            raise RepositoryInventoryLimitError(
                file_count=len(candidates),
                total_bytes=total_bytes,
                max_files=self._limits.max_files,
                max_total_bytes=self._limits.max_total_bytes,
            )

    def _hash_candidates(
        self,
        root_descriptor: int,
        candidates: tuple[_InventoryCandidate, ...],
    ) -> tuple[InventoryFile, ...]:
        files: list[InventoryFile] = []
        for candidate in candidates:
            if candidate.symlink:
                status: Literal["hashable", "secret", "oversized", "symlink"] = (
                    "symlink"
                )
            elif _is_secret_path(candidate.path):
                status = "secret"
            elif candidate.size_bytes > self._limits.max_hash_bytes_per_file:
                status = "oversized"
            else:
                status = "hashable"
            files.append(
                InventoryFile(
                    path=candidate.path,
                    size_bytes=candidate.size_bytes,
                    sha256=(
                        _hash_file(root_descriptor, candidate)
                        if status == "hashable"
                        else None
                    ),
                    content_status=status,
                )
            )
        return tuple(files)

    def _result(
        self,
        *,
        root: Path,
        git_available: bool,
        commit: str | None,
        dirty: bool,
        files: tuple[InventoryFile, ...],
    ) -> RepositoryInventoryResult:
        total_bytes = sum(item.size_bytes for item in files)
        selected = _select_for_model(files, self._limits)
        fingerprint_entries: tuple[InventoryFingerprintEntry, ...] = tuple(
            (
                item.path,
                item.size_bytes,
                item.sha256,
                item.content_status,
            )
            for item in files
        )
        payload = canonical_inventory_payload(
            git_available=git_available,
            commit=commit,
            dirty=dirty,
            files=fingerprint_entries,
            total_bytes=total_bytes,
        )
        fingerprint = inventory_binding_fingerprint(payload, selected)
        return RepositoryInventoryResult(
            root=root,
            git_available=git_available,
            commit=commit,
            dirty=dirty,
            files=files,
            selected_for_model=selected,
            total_bytes=total_bytes,
            inventory_fingerprint=fingerprint,
        )


def _repository_state(repo: Repo) -> _RepositoryState:
    commit = repo.head.commit.hexsha if repo.head.is_valid() else None
    porcelain = repo.git.status(
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    encoded = porcelain.encode("utf-8", errors="surrogateescape")
    status_fingerprint = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return _RepositoryState(
        commit=commit,
        status_fingerprint=status_fingerprint,
        dirty=bool(porcelain),
    )


def _git_snapshot(root: Path, repo: Repo) -> _GitInventorySnapshot:
    paths = _git_visible_paths(repo)
    return _GitInventorySnapshot(
        state=_repository_state(repo),
        paths=paths,
        candidates=_candidates(root, paths),
    )


def _git_visible_paths(repo: Repo) -> tuple[str, ...]:
    raw_output: object = repo.git.ls_files("-co", "--exclude-standard", "-z")
    if not isinstance(raw_output, str):
        message = "Git ls-files returned a non-text result."
        raise TypeError(message)
    return tuple(
        sorted(
            {path for path in raw_output.split("\0") if path},
            key=_path_bytes,
        )
    )


def _fallback_visible_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        entries = sorted(os.scandir(directory), key=lambda item: _path_bytes(item.name))
        for entry in entries:
            relative = prefix / entry.name
            relative_path = relative.as_posix()
            if entry.is_symlink():
                paths.append(relative_path)
            elif entry.is_dir(follow_symlinks=False):
                if entry.name not in _FALLBACK_IGNORED_DIRECTORIES:
                    visit(Path(entry.path), relative)
            elif (
                entry.is_file(follow_symlinks=False)
                and entry.name not in _FALLBACK_IGNORED_FILES
            ):
                paths.append(relative_path)

    visit(root, PurePosixPath())
    return tuple(sorted(paths, key=_path_bytes))


def _candidates(
    root: Path,
    paths: tuple[str, ...],
) -> tuple[_InventoryCandidate, ...]:
    candidates: list[_InventoryCandidate] = []
    for relative_path in paths:
        absolute_path = root / relative_path
        try:
            metadata = absolute_path.lstat()
        except FileNotFoundError:
            continue
        symlink = stat.S_ISLNK(metadata.st_mode)
        if not symlink and not stat.S_ISREG(metadata.st_mode):
            continue
        snapshot = _filesystem_snapshot(metadata)
        candidates.append(
            _InventoryCandidate(
                path=relative_path,
                absolute_path=absolute_path,
                size_bytes=metadata.st_size,
                symlink=symlink,
                snapshot=snapshot,
            )
        )
    return tuple(sorted(candidates, key=lambda item: _path_bytes(item.path)))


def _path_bytes(path: str) -> bytes:
    return repository_path_bytes(path)


def _is_secret_path(relative_path: str) -> bool:
    """Match secret artifacts by basename, known location, or terminal name."""
    path = PurePosixPath(relative_path)
    lower_path = path.as_posix().lower()
    name = path.name.lower()
    if name.endswith(_SECRET_TEMPLATE_SUFFIXES):
        return False
    separated_stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", path.stem)
    stem_tokens = tuple(
        token for token in re.split(r"[^a-z0-9]+", separated_stem.lower()) if token
    )
    root_directory = path.parts[0].lower() if len(path.parts) > 1 else None
    terminal_pair = tuple(stem_tokens[-2:])
    real_environment_file = (
        name == ".env" or name.startswith(".env.") or name.endswith(".env")
    )
    ssh_secret = (
        root_directory == ".ssh"
        and name not in _NON_SECRET_SSH_BASENAMES
        and not name.endswith(".pub")
    )
    return (
        name in _SECRET_EXACT_BASENAMES
        or real_environment_file
        or name.endswith(_SECRET_SUFFIXES)
        or (bool(stem_tokens) and stem_tokens[-1] in _SECRET_TERMINAL_TOKENS)
        or terminal_pair in _SECRET_TERMINAL_TOKEN_PAIRS
        or root_directory in _SECRET_ROOT_DIRECTORIES
        or ssh_secret
        or lower_path in _ESTABLISHED_SECRET_PATHS
    )


def _require_unchanged_snapshot(unchanged: bool) -> None:
    if unchanged:
        return
    raise _repository_changed_error()


def _repository_changed_error() -> RepositoryChangedDuringInventoryError:
    message = (
        "Repository HEAD, porcelain status, path set, metadata, or hashable "
        "content changed during inventory; retry after the worktree is stable."
    )
    return RepositoryChangedDuringInventoryError(message)


def _filesystem_snapshot(metadata: os.stat_result) -> _FilesystemSnapshot:
    return _FilesystemSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _verify_root_snapshot(
    root: Path,
    root_descriptor: int,
    expected: _FilesystemSnapshot,
) -> None:
    try:
        descriptor_metadata = os.fstat(root_descriptor)
        path_metadata = root.lstat()
    except OSError as error:
        raise _repository_changed_error() from error
    _require_unchanged_snapshot(
        expected == _filesystem_snapshot(descriptor_metadata)
        and expected == _filesystem_snapshot(path_metadata)
    )


def _hash_file(root_descriptor: int, candidate: _InventoryCandidate) -> str:
    """Hash through a no-follow descriptor walk rooted at the verified repository."""
    relative_path = PurePosixPath(candidate.path)
    parts = relative_path.parts
    _require_unchanged_snapshot(
        bool(parts)
        and not relative_path.is_absolute()
        and all(part not in {"", ".", ".."} for part in parts)
    )
    try:
        with ExitStack() as descriptors:
            parent_descriptor = root_descriptor
            for component in parts[:-1]:
                component_descriptor = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=parent_descriptor,
                )
                descriptors.callback(os.close, component_descriptor)
                _require_unchanged_snapshot(
                    stat.S_ISDIR(os.fstat(component_descriptor).st_mode)
                )
                parent_descriptor = component_descriptor

            pre_open_metadata = os.stat(
                parts[-1],
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_unchanged_snapshot(
                stat.S_ISREG(pre_open_metadata.st_mode)
                and _filesystem_snapshot(pre_open_metadata) == candidate.snapshot
            )
            file_descriptor = os.open(
                parts[-1],
                _HASHABLE_FILE_OPEN_FLAGS,
                dir_fd=parent_descriptor,
            )
            descriptors.callback(os.close, file_descriptor)
            opened_metadata = os.fstat(file_descriptor)
            _require_unchanged_snapshot(
                stat.S_ISREG(opened_metadata.st_mode)
                and _filesystem_snapshot(opened_metadata) == candidate.snapshot
            )
            digest = _hash_descriptor(file_descriptor)
            final_descriptor_metadata = os.fstat(file_descriptor)
            final_path_metadata = os.stat(
                parts[-1],
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_unchanged_snapshot(
                _filesystem_snapshot(final_descriptor_metadata) == candidate.snapshot
                and _filesystem_snapshot(final_path_metadata) == candidate.snapshot
            )
            return digest
    except RepositoryChangedDuringInventoryError:
        raise
    except OSError as error:
        raise _repository_changed_error() from error


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _selection_rank(item: InventoryFile) -> tuple[int, int, int, bytes]:
    path = PurePosixPath(item.path)
    suffix = path.suffix.lower()
    basename = path.name.lower()
    if suffix in _SOURCE_SUFFIXES:
        kind_rank = 0
    elif suffix in _CONFIG_SUFFIXES or basename in _CONFIG_BASENAMES:
        kind_rank = 1
    elif suffix in _DOCUMENT_SUFFIXES:
        kind_rank = 2
    else:
        kind_rank = 3
    return kind_rank, len(path.parts), item.size_bytes, _path_bytes(item.path)


def _select_for_model(
    files: tuple[InventoryFile, ...],
    limits: InventoryLimits,
) -> tuple[str, ...]:
    selected: list[str] = []
    selected_bytes = 0
    safe_files = sorted(
        (item for item in files if item.content_status == "hashable"),
        key=_selection_rank,
    )
    for item in safe_files:
        if len(selected) >= limits.max_model_files:
            break
        if selected_bytes + item.size_bytes > limits.max_model_bytes:
            continue
        selected.append(item.path)
        selected_bytes += item.size_bytes
    return tuple(selected)


def _summary(result: RepositoryInventoryResult) -> dict[str, object]:
    return {
        "commit": result.commit,
        "dirty": result.dirty,
        "file_count": len(result.files),
        "git_available": result.git_available,
        "inventory_fingerprint": result.inventory_fingerprint,
        "selected_for_model_count": len(result.selected_for_model),
        "total_bytes": result.total_bytes,
        "truncated": result.truncated,
    }


def main() -> None:
    """Print a deterministic repository inventory summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = RepositoryInventoryService().inventory(args.root)
    payload: object = _summary(result) if args.summary else asdict(result)
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    sys.stdout.write(f"{rendered}\n")


if __name__ == "__main__":
    main()


__all__ = [
    "InventoryFile",
    "InventoryLimits",
    "RepositoryChangedDuringInventoryError",
    "RepositoryInventoryLimitError",
    "RepositoryInventoryResult",
    "RepositoryInventoryService",
]
