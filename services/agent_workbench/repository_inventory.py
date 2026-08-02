"""Complete Git-aware repository inventory with bounded model selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from git import InvalidGitRepositoryError, Repo

from workflow.fingerprints import canonical_hash

_HASH_CHUNK_BYTES = 1024 * 1024
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
_SECRET_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".netrc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
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
    """Raised when HEAD or porcelain status changes during file hashing."""


@dataclass(frozen=True)
class _RepositoryState:
    commit: str | None
    status_fingerprint: str
    dirty: bool


@dataclass(frozen=True)
class _InventoryCandidate:
    path: str
    absolute_path: Path
    size_bytes: int
    symlink: bool


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
        try:
            with Repo(resolved) as repo:
                return self._git_inventory(resolved, repo)
        except InvalidGitRepositoryError:
            return self._fallback_inventory(resolved)

    def _git_inventory(self, root: Path, repo: Repo) -> RepositoryInventoryResult:
        before = _repository_state(repo)
        paths = _git_visible_paths(repo)
        candidates = _candidates(root, paths)
        self._enforce_hard_limits(candidates)
        files = self._hash_candidates(candidates)
        after = _repository_state(repo)
        if before != after:
            message = (
                "Repository HEAD or porcelain status changed during inventory; "
                "retry after the worktree is stable."
            )
            raise RepositoryChangedDuringInventoryError(message)
        return self._result(
            root=root,
            git_available=True,
            commit=before.commit,
            dirty=before.dirty,
            files=files,
        )

    def _fallback_inventory(self, root: Path) -> RepositoryInventoryResult:
        candidates = _candidates(root, _fallback_visible_paths(root))
        self._enforce_hard_limits(candidates)
        return self._result(
            root=root,
            git_available=False,
            commit=None,
            dirty=False,
            files=self._hash_candidates(candidates),
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
                        _hash_file(candidate.absolute_path)
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
        fingerprint = canonical_hash(
            {
                "files": [asdict(item) for item in files],
                "total_bytes": total_bytes,
            }
        )
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
        symlink = absolute_path.is_symlink()
        if not symlink and not absolute_path.is_file():
            continue
        candidates.append(
            _InventoryCandidate(
                path=relative_path,
                absolute_path=absolute_path,
                size_bytes=metadata.st_size,
                symlink=symlink,
            )
        )
    return tuple(sorted(candidates, key=lambda item: _path_bytes(item.path)))


def _path_bytes(path: str) -> bytes:
    return path.encode("utf-8", errors="surrogateescape")


def _is_secret_path(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name.lower()
    return (
        name in _SECRET_BASENAMES
        or name.startswith(".env.")
        or name.endswith(_SECRET_SUFFIXES)
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
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
