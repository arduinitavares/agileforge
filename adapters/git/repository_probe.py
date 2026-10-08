"""GitPython implementation of the deterministic repository probe."""

from __future__ import annotations

import os
import signal
import stat
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from subprocess import TimeoutExpired  # nosec B404  # own SDK-created process
from time import monotonic
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from git import Repo
from git.compat import safe_decode
from git.diff import Diff, DiffIndex
from git.exc import BadName, GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryProbeResult,
    RepositoryProbeWarning,
    RepositoryRevisionProbeResult,
    RepositoryStatusEntry,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from typing import Protocol

    from git import Git

    class _VerificationGitExecutor(Protocol):
        """Pinned SDK runtime keywords omitted by its public execute overloads."""

        def __call__(
            self,
            command: list[str],
            *,
            shell: Literal[False],
            as_process: bool = False,
            start_new_session: bool = False,
        ) -> str | Git.AutoInterrupt: ...


_PROBE_VERSION = "agileforge.repository-probe.v1"
_DIRTY_WORKTREE_MESSAGE = "Repository worktree contains changes."
_REMOTE_OMITTED_MESSAGE = "Local or invalid repository remotes were omitted."
_PORCELAIN_PATH_OFFSET: int = 3
_RAW_DIFF_HEADER_FIELD_COUNT: int = 5
_WINDOWS_TIMEOUT_UNSUPPORTED: bool = sys.platform == "win32"
_TIMEOUT_EXIT_STATUS: int = -9  # GitPython's Unix watchdog uses SIGKILL.
_VERIFICATION_REAP_SECONDS: float = 0.1
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


@dataclass(frozen=True)
class _StatusDiffContext:
    """Prevent the pinned raw parser from opening submodule/object repositories."""

    submodules: tuple[()] = ()


class GitPythonRepositoryProbe:
    """Read stable Git worktree metadata without source reads or writes."""

    def __init__(
        self,
        *,
        outside_timeout_seconds: float = 10.0,
        verification_timeout_seconds: float = 2.0,
        _read_head_sha: Callable[[Repo], str] | None = None,
    ) -> None:
        """Use the production HEAD reader or a deterministic test reader."""
        self._outside_timeout_seconds = _positive_timeout(outside_timeout_seconds)
        self._verification_timeout_seconds = _positive_timeout(
            verification_timeout_seconds
        )
        self._read_head_sha = _read_head_sha

    def _current_head_sha(
        self, repo: Repo, *, timeout_seconds: float, deadline: float | None = None
    ) -> str:
        """Keep deterministic test readers separate from bounded production reads."""
        if self._read_head_sha is not None:
            return self._read_head_sha(repo)
        return _head_sha(repo, timeout_seconds=timeout_seconds, deadline=deadline)

    def inspect_common_git_dir(self, path: Path | str) -> str:
        """Read resolved Git identity before completion HEAD/status inspection."""
        with _completion_repository(
            path, require_head=False, timeout_seconds=self._outside_timeout_seconds
        ) as repo:
            return _normalize_text(str(Path(repo.common_dir).resolve()))

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        """Return one read-only, deterministic repository observation."""
        normalized_path = _normalize_path(path)
        try:
            if not normalized_path.exists():
                raise _error(RepositoryProbeErrorCode.PATH_MISSING, normalized_path)
            if not normalized_path.is_dir():
                raise _error(
                    RepositoryProbeErrorCode.PATH_NOT_DIRECTORY, normalized_path
                )
            return self._inspect_worktree(normalized_path)
        except RepositoryProbeError:
            raise
        except (BadName, InvalidGitRepositoryError, NoSuchPathError) as error:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.NOT_GIT_WORKTREE,
                str(normalized_path),
            ) from error
        except GitCommandError as error:
            raise _command_error(error, normalized_path) from error
        except (OSError, UnicodeError, ValueError) as error:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
                str(normalized_path),
            ) from error

    def inspect_revision(self, path: Path | str) -> RepositoryRevisionProbeResult:
        """Check stable HEAD and dirty state without building full evidence."""
        timeout = self._verification_timeout_seconds
        deadline = monotonic() + timeout
        with _completion_repository(
            path, require_head=False, timeout_seconds=timeout
        ) as repo:
            normalized = Path(repo.working_tree_dir or repo.working_dir)
            _remaining_verification_seconds(deadline, normalized)
            first_head_sha = self._current_head_sha(
                repo, timeout_seconds=timeout, deadline=deadline
            )
            _remaining_verification_seconds(deadline, normalized)
            dirty = bool(
                _verification_git(
                    repo,
                    "status",
                    "--porcelain",
                    "-z",
                    "--untracked-files=normal",
                    deadline=deadline,
                )
            )
            second_head_sha = self._current_head_sha(
                repo, timeout_seconds=timeout, deadline=deadline
            )
            _remaining_verification_seconds(deadline, normalized)
            if first_head_sha != second_head_sha:
                raise _error(
                    RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE,
                    Path(repo.working_tree_dir or repo.working_dir),
                )
            return RepositoryRevisionProbeResult(head_sha=first_head_sha, dirty=dirty)

    def has_other_worktrees(self, path: Path | str) -> bool:
        """Inspect registered topology without opening other worktree targets."""
        with _completion_repository(
            path, timeout_seconds=self._outside_timeout_seconds
        ) as repo:
            records = repo.git.worktree(
                "list",
                "--porcelain",
                "-z",
                **_deadline_options(self._outside_timeout_seconds),
            )
            return sum(item.startswith("worktree ") for item in records.split("\0")) > 1

    def _inspect_worktree(self, normalized_path: Path) -> RepositoryProbeResult:
        """Collect a stable observation from one opened Git worktree."""
        repo = Repo(normalized_path, search_parent_directories=False)
        repo.git.set_persistent_git_options(c="diff.autoRefreshIndex=false")
        try:
            with repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"):
                return self._collect_worktree(repo, normalized_path)
        finally:
            # Clear subprocesses now; Repo.close() forces global GC on Windows.
            repo.git.clear_cache()

    def _collect_worktree(
        self, repo: Repo, normalized_path: Path
    ) -> RepositoryProbeResult:
        """Collect metadata while optional Git writes are disabled locally."""
        if repo.bare:
            raise _error(RepositoryProbeErrorCode.NOT_GIT_WORKTREE, normalized_path)
        timeout = self._outside_timeout_seconds
        first_head_sha = self._current_head_sha(repo, timeout_seconds=timeout)
        entries = _status_entries(repo, timeout_seconds=timeout)
        detached_head = repo.head.is_detached
        branch_name = (
            None if detached_head else _normalize_text(repo.active_branch.name)
        )
        remote_urls: list[str] = []
        remote_omitted = False
        for remote in repo.remotes:
            urls = repo.git.remote(
                "get-url", "--all", remote.name, **_deadline_options(timeout)
            )
            for url in urls.split("\n"):
                identity = _remote_identity(url)
                if identity is None:
                    remote_omitted = True
                else:
                    remote_urls.append(identity)
        remotes: tuple[str, ...] = tuple(sorted(remote_urls))
        second_head_sha = self._current_head_sha(repo, timeout_seconds=timeout)
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


def _error(code: RepositoryProbeErrorCode, path: Path) -> RepositoryProbeError:
    """Create a typed error after path normalization."""
    return RepositoryProbeError(code, str(path))


@contextmanager
def _completion_repository(
    path: Path | str, *, require_head: bool = True, timeout_seconds: float = 10.0
) -> Iterator[Repo]:
    """Open one completion target and normalize all Git errors safely."""
    normalized = _normalize_path(path)
    repo: Repo | None = None
    try:
        if not normalized.exists():
            raise _error(RepositoryProbeErrorCode.PATH_MISSING, normalized)
        if not normalized.is_dir():
            raise _error(RepositoryProbeErrorCode.PATH_NOT_DIRECTORY, normalized)
        repo = Repo(normalized, search_parent_directories=False)
        repo.git.set_persistent_git_options(c="diff.autoRefreshIndex=false")
        if repo.bare:
            raise _error(RepositoryProbeErrorCode.NOT_GIT_WORKTREE, normalized)
        with repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"):
            if require_head:
                _head_sha(repo, timeout_seconds=timeout_seconds)
            yield repo
    except RepositoryProbeError:
        raise
    except (BadName, InvalidGitRepositoryError, NoSuchPathError) as error:
        raise _error(RepositoryProbeErrorCode.NOT_GIT_WORKTREE, normalized) from error
    except GitCommandError as error:
        raise _command_error(error, normalized) from error
    except (OSError, UnicodeError, ValueError) as error:
        raise _error(
            RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE, normalized
        ) from error
    finally:
        if repo is not None:
            repo.git.clear_cache()


def _validate_no_embedded_nul(raw_path: bytes) -> None:
    """Ensure encoded path bytes do not contain an embedded NUL byte."""
    if b"\x00" in raw_path:
        message = "Path contains embedded NUL byte."
        raise ValueError(message)


def _normalize_path(path: Path | str) -> Path:
    """Resolve one filesystem path through the platform error handler."""
    try:
        raw_path = os.fsencode(os.fspath(path))
        _validate_no_embedded_nul(raw_path)
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


def _positive_timeout(value: float) -> float:
    """Reject settings that could disable or defeat the command deadline."""
    if isinstance(value, bool) or not isfinite(value) or value <= 0:
        message = "Repository probe timeout must be a finite positive number."
        raise ValueError(message)
    return float(value)


def _deadline_options(timeout_seconds: float) -> dict[str, float]:
    """Bound synchronous GitPython commands on supported runtime platforms."""
    # Product runtime is Linux-only; retain healthy native Windows inspection tests.
    # The pinned SDK rejects this keyword on Windows before starting any command.
    return (
        {} if _WINDOWS_TIMEOUT_UNSUPPORTED else {"kill_after_timeout": timeout_seconds}
    )


def _command_error(error: GitCommandError, path: Path) -> RepositoryProbeError:
    """Classify only the pinned SDK watchdog's typed exit and timeout marker."""
    code = (
        RepositoryProbeErrorCode.PROBE_TIMED_OUT
        if error.status == _TIMEOUT_EXIT_STATUS
        and "Timeout: the command " in error.stderr
        else RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    )
    return _error(code, path)


def _remaining_verification_seconds(deadline: float, path: Path) -> float:
    """Reject exhausted overall verification budgets before or after required I/O."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _error(RepositoryProbeErrorCode.PROBE_TIMED_OUT, path)
    return remaining


def _verification_git(repo: Repo, *arguments: str, deadline: float) -> str:
    """Use SDK environment/launch integration with an owned POSIX process group."""
    normalized = Path(repo.working_tree_dir or repo.working_dir)
    _remaining_verification_seconds(deadline, normalized)
    executable = repo.git.GIT_PYTHON_GIT_EXECUTABLE
    if executable is None:
        raise _error(RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE, normalized)
    command: list[str] = [
        executable,
        "-c",
        "diff.autoRefreshIndex=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "submodule.recurse=false",
        *arguments,
    ]
    # 3.1.57's implementation accepts shell/**subprocess_kwargs, but its public
    # overloads omit them. This boundary describes only the two modes used here.
    execute = cast("_VerificationGitExecutor", repo.git.execute)
    if _WINDOWS_TIMEOUT_UNSUPPORTED:
        # Preserve healthy Windows tests; product runtime and group bounds are POSIX.
        output = cast("str", execute(command, shell=False))
    else:
        process = cast(
            "Git.AutoInterrupt",
            execute(command, as_process=True, start_new_session=True, shell=False),
        )
        proc = process.proc
        if proc is None:
            raise _error(RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE, normalized)
        try:
            stdout, stderr = proc.communicate(
                timeout=_remaining_verification_seconds(deadline, normalized)
            )
            output = cast("str", safe_decode(stdout)).removesuffix("\n")
            _remaining_verification_seconds(deadline, normalized)
        except BaseException as error:
            # The leader may have exited already while descendants still hold pipes.
            # Its new-session PGID remains ours; never enumerate unrelated processes.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            # No unbounded pipe drain or destructor wait may retain the DB writer.
            with suppress(TimeoutExpired):
                proc.wait(timeout=_VERIFICATION_REAP_SECONDS)
            if isinstance(error, (TimeoutExpired, RepositoryProbeError)):
                raise _error(
                    RepositoryProbeErrorCode.PROBE_TIMED_OUT, normalized
                ) from error
            raise
        finally:
            # We own cleanup. Disarm before closing so an I/O error cannot trigger
            # GitPython's potentially unbounded AutoInterrupt destructor wait.
            process.status = proc.returncode
            process.proc = None
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()
        if proc.returncode:
            raise GitCommandError(command, proc.returncode, stderr, stdout)
    _remaining_verification_seconds(deadline, normalized)
    return output


def _head_sha(
    repo: Repo, *, timeout_seconds: float = 10.0, deadline: float | None = None
) -> str:
    """Resolve HEAD as a commit without opening GitPython's persistent cat-file."""
    try:
        if deadline is not None:
            return _verification_git(
                repo, "rev-parse", "--verify", "HEAD^{commit}", deadline=deadline
            )
        return repo.git.rev_parse(
            "--verify", "HEAD^{commit}", **_deadline_options(timeout_seconds)
        )
    except GitCommandError as error:
        normalized = Path(repo.working_tree_dir or repo.working_dir)
        failure = _command_error(error, normalized)
        if failure.code is RepositoryProbeErrorCode.PROBE_TIMED_OUT:
            raise failure from error
        if "Needed a single revision" in error.stderr:
            raise _error(RepositoryProbeErrorCode.UNBORN_HEAD, normalized) from error
        raise


def _status_entries(
    repo: Repo, *, timeout_seconds: float = 10.0
) -> tuple[RepositoryStatusEntry, ...]:
    """Collect the three required Git status areas in stable order."""
    current_paths, tracked_paths, worktree_paths, untracked_paths = (
        _porcelain_status_paths(repo, timeout_seconds=timeout_seconds)
    )
    if not current_paths:
        return ()
    index_entries: tuple[RepositoryStatusEntry, ...] = ()
    worktree_entries: tuple[RepositoryStatusEntry, ...] = ()
    if tracked_paths:
        with repo.git.custom_environment(GIT_LITERAL_PATHSPECS="1"):
            index_entries = tuple(
                entry
                for entry in _raw_diff_entries(
                    repo,
                    area="index",
                    tracked_paths=tracked_paths,
                    reverse=True,
                    timeout_seconds=timeout_seconds,
                )
                if entry.path in tracked_paths
            )
            changed_paths = _worktree_diff_paths(repo, timeout_seconds=timeout_seconds)
            worktree_entries = tuple(
                entry
                for entry in _raw_diff_entries(
                    repo,
                    area="worktree",
                    tracked_paths=tracked_paths,
                    timeout_seconds=timeout_seconds,
                )
                if entry.path in worktree_paths
                and _has_diff(repo, path=entry.path, changed_paths=changed_paths)
            )
    entries = [
        *index_entries,
        *worktree_entries,
        *(
            RepositoryStatusEntry(
                area="untracked",
                change="added",
                path=_normalize_text(path),
            )
            for path in untracked_paths
        ),
    ]
    if {entry.path for entry in entries} != current_paths:
        raise _error(
            RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
            Path(repo.working_tree_dir or repo.working_dir),
        )
    return tuple(sorted(entries, key=_entry_sort_key))


def _porcelain_status_paths(
    repo: Repo,
    *,
    timeout_seconds: float = 10.0,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Validate full-status completeness without replacing normalized entries."""
    output = repo.git.status(
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--renames",
        **_deadline_options(timeout_seconds),
    )
    error = _error(
        RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
        Path(repo.working_tree_dir or repo.working_dir),
    )
    if output and not output.endswith("\0"):
        raise error
    records = iter(output[:-1].split("\0") if output else ())
    paths: set[str] = set()
    tracked_paths: set[str] = set()
    worktree_paths: set[str] = set()
    untracked_paths: set[str] = set()
    for record in records:
        if len(record) <= _PORCELAIN_PATH_OFFSET or record[2] != " ":
            raise error
        status = record[:2]
        current = _normalize_text(record[_PORCELAIN_PATH_OFFSET:])
        previous = ""
        if "R" in status or "C" in status:
            previous = next(records, "")
            if not previous:
                raise error
        # GitPython's raw-diff parser mistakes NUL-colon paths for metadata.
        if status != "??" and (current.startswith(":") or previous.startswith(":")):
            raise error
        paths.add(current)
        if status == "??":
            untracked_paths.add(current)
        else:
            tracked_paths.add(current)
            if previous:
                tracked_paths.add(_normalize_text(previous))
            if status[1] != " ":
                worktree_paths.add(current)
    return paths, tracked_paths, worktree_paths, untracked_paths


def _worktree_diff_paths(repo: Repo, *, timeout_seconds: float = 10.0) -> set[str]:
    """Collect worktree diff names once without expanding paths into argv."""
    with repo.git.custom_environment(GIT_LITERAL_PATHSPECS="1"):
        output = repo.git.diff(
            "--name-only",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
            **_deadline_options(timeout_seconds),
        )
    return {_normalize_text(path) for path in output.split("\0") if path}


def _raw_diff_entries(
    repo: Repo,
    *,
    area: Literal["index", "worktree"],
    tracked_paths: set[str],
    reverse: bool = False,
    timeout_seconds: float = 10.0,
) -> tuple[RepositoryStatusEntry, ...]:
    """Filter complete raw records before GitPython's status normalization."""
    arguments = [
        "--raw",
        "-z",
        "--abbrev=40",
        "--full-index",
        "-M",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if area == "index":
        arguments.extend(("-R", "HEAD", "--cached"))
    arguments.append("--")
    output = cast(
        "bytes",
        repo.git.diff(
            *arguments, stdout_as_string=False, **_deadline_options(timeout_seconds)
        ),
    )
    diffs: DiffIndex[Diff] = DiffIndex()
    for record, previous_path, current_path in _raw_diff_records(output):
        if current_path in tracked_paths or previous_path in tracked_paths:
            # Diff.__init__ otherwise traverses repo.submodules and starts cat-file.
            # Only change/path/rename metadata is consumed; Blob data is never read.
            Diff._handle_diff_line(record, cast("Repo", _StatusDiffContext()), diffs)
    return _diff_entries(diffs, area=area, reverse=reverse)


def _raw_diff_records(output: bytes) -> Iterator[tuple[bytes, str, str]]:
    """Frame NUL-terminated raw headers and their one or two path fields."""
    if not output:
        return
    if not output.endswith(b"\0"):
        message = "Incomplete raw diff observation."
        raise ValueError(message)
    fields = iter(output[:-1].split(b"\0"))
    for header in fields:
        metadata = header.split()
        if not header.startswith(b":") or len(metadata) != _RAW_DIFF_HEADER_FIELD_COUNT:
            message = "Malformed raw diff header."
            raise ValueError(message)
        paths = [next(fields, b"")]
        if metadata[-1][:1] in {b"R", b"C"}:
            paths.append(next(fields, b""))
        if not all(paths):
            message = "Incomplete raw diff pathname."
            raise ValueError(message)
        yield (
            b"\0".join((header, *paths, b"")),
            _normalize_text(paths[0]),
            _normalize_text(paths[-1]),
        )


def _has_diff(repo: Repo, *, path: str, changed_paths: set[str]) -> bool:
    """Keep actual or special-file changes without refreshing index stat data."""
    try:
        metadata = (Path(repo.working_tree_dir or repo.working_dir) / path).lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(metadata.st_mode):
        # Raw diff already records this transition; special files cannot be hashed.
        return True
    return path in changed_paths


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
