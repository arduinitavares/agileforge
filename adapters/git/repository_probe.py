"""GitPython implementation of the deterministic repository probe."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from git import Repo
from git.exc import BadName, InvalidGitRepositoryError, NoSuchPathError

from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryProbeResult,
    RepositoryProbeWarning,
    RepositoryStatusEntry,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from git.diff import Diff

_PROBE_VERSION = "agileforge.repository-probe.v1"
_DIRTY_WORKTREE_MESSAGE = "Repository worktree contains changes."
_REMOTE_OMITTED_MESSAGE = "Local or invalid repository remotes were omitted."
type StatusChange = Literal[
    "added",
    "modified",
    "deleted",
    "renamed",
    "type_changed",
]
_CHANGE_TYPES: dict[str, StatusChange] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "T": "type_changed",
}


@dataclass(frozen=True)
class _ProbeState:
    """Repository metadata collected before the second HEAD read."""

    normalized_path: Path
    head_sha: str
    branch_name: str | None
    detached_head: bool
    entries: tuple[RepositoryStatusEntry, ...]
    remotes: tuple[str, ...]
    remote_omitted: bool


class GitPythonRepositoryProbe:
    """Read stable Git worktree metadata without source reads or writes."""

    def __init__(
        self,
        *,
        _read_head_sha: Callable[[Repo], str] | None = None,
    ) -> None:
        """Use the production HEAD reader or a deterministic test reader."""
        self._read_head_sha = _read_head_sha or _head_sha

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        """Return one read-only, deterministic repository observation."""
        normalized_path = _normalize_path(path)
        if not normalized_path.exists():
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.PATH_MISSING,
                str(normalized_path),
            )
        if not normalized_path.is_dir():
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.PATH_NOT_DIRECTORY,
                str(normalized_path),
            )
        try:
            return self._inspect_worktree(normalized_path)
        except RepositoryProbeError:
            raise
        except (BadName, InvalidGitRepositoryError, NoSuchPathError) as error:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.NOT_GIT_WORKTREE,
                str(normalized_path),
            ) from error
        except (OSError, UnicodeError, ValueError) as error:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
                str(normalized_path),
            ) from error

    def _inspect_worktree(self, normalized_path: Path) -> RepositoryProbeResult:
        """Collect a stable observation from one opened Git worktree."""
        repo = Repo(normalized_path, search_parent_directories=False)
        try:
            if repo.bare:
                raise _error(RepositoryProbeErrorCode.NOT_GIT_WORKTREE, normalized_path)
            if not repo.head.is_valid():
                raise _error(RepositoryProbeErrorCode.UNBORN_HEAD, normalized_path)
            first_head_sha = self._read_head_sha(repo)
            entries = _status_entries(repo)
            detached_head = repo.head.is_detached
            branch_name = (
                None if detached_head else _normalize_text(repo.active_branch.name)
            )
            remote_urls: list[str] = []
            remote_omitted = False
            for remote in repo.remotes:
                for url in remote.urls:
                    identity = _remote_identity(url)
                    if identity is None:
                        remote_omitted = True
                    else:
                        remote_urls.append(identity)
            remotes: tuple[str, ...] = tuple(sorted(remote_urls))
            second_head_sha = self._read_head_sha(repo)
            if first_head_sha != second_head_sha:
                raise _error(
                    RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE,
                    normalized_path,
                )
            return _result(
                repo,
                _ProbeState(
                    normalized_path=normalized_path,
                    head_sha=first_head_sha,
                    branch_name=branch_name,
                    detached_head=detached_head,
                    entries=entries,
                    remotes=remotes,
                    remote_omitted=remote_omitted,
                ),
            )
        finally:
            # Clear subprocesses now; Repo.close() forces global GC on Windows.
            repo.git.clear_cache()


def _error(code: RepositoryProbeErrorCode, path: Path) -> RepositoryProbeError:
    """Create a typed error after path normalization."""
    return RepositoryProbeError(code, str(path))


def _normalize_path(path: Path | str) -> Path:
    """Resolve one filesystem path through the platform error handler."""
    try:
        raw_path = os.fsencode(os.fspath(path))
        normalized = Path(os.fsdecode(raw_path)).expanduser().resolve()
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise RepositoryProbeError(
            RepositoryProbeErrorCode.MALFORMED_PATH,
            _safe_path_text(path),
        ) from error
    return normalized


def _safe_path_text(path: object) -> str:
    """Return a stable error path without querying the filesystem."""
    try:
        return _normalize_text(path)
    except (UnicodeError, ValueError):
        return "<malformed-path>"


def _head_sha(repo: Repo) -> str:
    """Read the current commit SHA through GitPython's HEAD API."""
    return repo.head.commit.hexsha


def _status_entries(repo: Repo) -> tuple[RepositoryStatusEntry, ...]:
    """Collect the three required Git status areas in stable order."""
    entries = [
        *_diff_entries(repo.index.diff("HEAD"), area="index", reverse=True),
        *_diff_entries(repo.index.diff(None), area="worktree"),
        *(
            RepositoryStatusEntry(
                area="untracked",
                change="added",
                path=_normalize_text(path),
            )
            for path in repo.untracked_files
        ),
    ]
    return tuple(sorted(entries, key=_entry_sort_key))


def _diff_entries(
    diffs: Iterable[Diff],
    *,
    area: Literal["index", "worktree"],
    reverse: bool = False,
) -> tuple[RepositoryStatusEntry, ...]:
    """Normalize GitPython diff records into the closed status vocabulary."""
    entries: list[RepositoryStatusEntry] = []
    for diff in diffs:
        change_type = cast("str", diff.change_type)
        if reverse:
            change_type = {"A": "D", "D": "A"}.get(change_type, change_type)
        change = _CHANGE_TYPES.get(change_type)
        if change is None:
            continue
        old_path = _normalize_text(cast("str", diff.a_path))
        new_path = _normalize_text(cast("str", diff.b_path))
        if change == "renamed" and reverse:
            path = old_path
            previous_path = new_path
        else:
            path = old_path if change == "deleted" else new_path
            previous_path = old_path if change == "renamed" else None
        entries.append(
            RepositoryStatusEntry(
                area=area,
                change=change,
                path=path,
                previous_path=previous_path,
            )
        )
    return tuple(entries)


def _normalize_text(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes] | object,
) -> str:
    """Round-trip Git text with the platform filesystem error handler."""
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return os.fsdecode(os.fsencode(str(value)))


def _remote_identity(
    value: str | bytes | os.PathLike[str] | os.PathLike[bytes] | object,
) -> str | None:
    """Retain remote location identity without credentials or URL metadata."""
    remote = _normalize_text(value)
    if "://" in remote:
        return _url_remote_identity(remote)
    separator = remote.find(":")
    if separator <= 0:
        return None
    prefix = remote[:separator]
    path = remote[separator + 1 :]
    path = path.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    if prefix.casefold() == "file" or (
        len(prefix) == 1
        and prefix.isascii()
        and prefix.isalpha()
        and path.startswith("/")
    ):
        return None
    host = prefix.rsplit("@", maxsplit=1)[-1]
    if (
        not host
        or not path
        or "@" in path
        or any(character.isspace() for character in remote)
        or any(character in "/\\?#" for character in host)
        or "\\" in path
    ):
        return None
    return f"{host}:{path}"


def _url_remote_identity(remote: str) -> str | None:
    """Return one sanitized network URL identity, excluding local URL forms."""
    try:
        parsed = urlsplit(remote)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() == "file" or host is None:
        return None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit((parsed.scheme.casefold(), authority, parsed.path, "", ""))


def _entry_sort_key(entry: RepositoryStatusEntry) -> tuple[str, str, bytes, bytes]:
    """Sort status entries independently of locale or Unicode collation."""
    return (
        entry.area,
        entry.change,
        os.fsencode(entry.path),
        os.fsencode(entry.previous_path or ""),
    )


def _result(repo: Repo, state: _ProbeState) -> RepositoryProbeResult:
    """Create the complete immutable result after the two-head verification."""
    worktree_path = _normalize_text(repo.working_tree_dir or str(state.normalized_path))
    common_git_dir = _normalize_text(str(Path(repo.common_dir).resolve()))
    dirty = bool(state.entries)
    warning_values: list[RepositoryProbeWarning] = []
    if dirty:
        warning_values.append(
            RepositoryProbeWarning(
                code="DIRTY_WORKTREE",
                message=_DIRTY_WORKTREE_MESSAGE,
            )
        )
    if state.remote_omitted:
        warning_values.append(
            RepositoryProbeWarning(
                code="REMOTE_OMITTED",
                message=_REMOTE_OMITTED_MESSAGE,
            )
        )
    warnings = tuple(warning_values)
    fingerprint_payload = {
        "probe_version": _PROBE_VERSION,
        "head_sha": state.head_sha,
        "branch_name": state.branch_name,
        "detached_head": state.detached_head,
        "dirty": dirty,
        "status_entries": [entry.model_dump(mode="json") for entry in state.entries],
        "remotes": state.remotes,
        "remote_omitted": state.remote_omitted,
    }
    return RepositoryProbeResult(
        worktree_path=worktree_path,
        common_git_dir=common_git_dir,
        head_sha=state.head_sha,
        branch_name=state.branch_name,
        detached_head=state.detached_head,
        dirty=dirty,
        status_entries=state.entries,
        status_fingerprint=canonical_hash(fingerprint_payload),
        remotes=state.remotes,
        probe_version=_PROBE_VERSION,
        inspected_at=datetime.now(UTC),
        warnings=warnings,
    )
