"""Deterministic Git repository probe tests."""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import signal
import stat
import struct
import sys
from contextlib import suppress
from errno import EACCES
from pathlib import Path
from subprocess import Popen, TimeoutExpired  # nosec B404  # owned test child only
from time import monotonic, sleep
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

import pytest
from git import Git, Repo
from git.exc import GitCommandError

import adapters.git.repository_probe as adapter_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
    RepositoryStatusEntry,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from git.diff import Diff


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


_BULK_ARGV_LIMIT: int = 256
_GIT_INDEX_V4: int = 4
_BULK_COMMAND_GROWTH_ALLOWANCE: int = 2
_PORCELAIN_RECORD_PREFIX_LENGTH: int = 3
_SDK_TIMEOUT_STATUS: int = -9
_LIVE_TIMEOUT_ELAPSED_LIMIT: float = 3.0
_PIPE_TIMEOUT_ELAPSED_LIMIT: float = 1.0
_OVERALL_TIMEOUT_ELAPSED_LIMIT: float = 0.9
_DARWIN_ZOMBIE_STATUS: int = 5
_OWNED_PIPE_ROLES: frozenset[str] = frozenset({"parent", "child", "grandchild"})


def _owned_pipe_git(tmp_path: Path, *, parent_exits: bool) -> tuple[Path, Path]:
    """Own one fake Git group whose child and grandchild inherit both pipes."""
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper = tmp_path / "pipe-holding-git"
    records = tmp_path / "pipe-holding-git-pids.jsonl"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if 'status' not in sys.argv[1:]:\n"
        f"    os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        "import json, time\n"
        f"records = {str(records)!r}\n"
        "def record(role):\n"
        "    with open(records, 'a') as stream:\n"
        "        stream.write(json.dumps([role, os.getpid(), os.getpgrp()]) + '\\n')\n"
        "if 'status' in sys.argv[1:]:\n"
        "    if os.getpgrp() != os.getpid(): os.setsid()\n"
        "    record('parent')\n"
        "    if os.fork() == 0:\n"
        "        record('child')\n"
        "        if os.fork() == 0:\n"
        "            record('grandchild')\n"
        "            time.sleep(6)\n"
        "            os._exit(0)\n"
        "        time.sleep(6)\n"
        "        os._exit(0)\n"
        "    ready = time.monotonic() + 1\n"
        "    while len(open(records).readlines()) < 3 and time.monotonic() < ready:\n"
        "        time.sleep(0.005)\n"
        f"    if {parent_exits!r}: os._exit(0)\n"
        "    time.sleep(6)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper, records


def _owned_process_is_executing(pid: int) -> bool:
    """Distinguish a dead adopted zombie from an executing pipe holder, without ps."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform == "linux":
        process_stat = Path(f"/proc/{pid}/stat")
        try:
            return process_stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = ctypes.create_string_buffer(256)
        # XNU includes adopted zombies only with a nonzero BSDINFO arg.
        size = libproc.proc_pidinfo(pid, 3, 1, info, len(info))
        if not size:
            # PID info can disappear between kill(0) and the state read. Prove that
            # race; unavailable information for a still-live PID is not death proof.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            message = "Cannot verify the state of the owned process."
            raise AssertionError(message)
        return struct.unpack_from("I", info.raw, 4)[0] != _DARWIN_ZOMBIE_STATUS
    return True


def _observe_owned_status_processes(
    monkeypatch: pytest.MonkeyPatch, wrapper: Path
) -> list[Popen[bytes]]:
    """Capture SDK-created status processes independently of fixture PID markers."""
    original_execute = cast("Callable[..., object]", Git.execute)
    processes: list[Popen[bytes]] = []

    def observe(
        git: Git, command: Sequence[object], *args: object, **kwargs: object
    ) -> object:
        result = original_execute(git, command, *args, **kwargs)
        if str(command[0]) == str(wrapper) and "status" in command:
            assert kwargs.get("as_process") is True
            assert kwargs.get("start_new_session") is True
            assert isinstance(result, Git.AutoInterrupt)
            assert result.proc is not None
            processes.append(result.proc)
        return result

    monkeypatch.setattr(Git, "execute", observe)
    return processes


def _assert_owned_pipe_group_stopped(
    records: Path, *, owned_processes: Sequence[Popen[bytes]]
) -> None:
    """Prove all recorded descendants stopped and clean only the owned group."""
    # Ownership comes from deliberate new-session Popen creation, never from
    # incomplete or malformed child-written PID markers.
    groups = {process.pid for process in owned_processes}
    assert groups
    assert len(groups) == 1
    assert all(group > 0 and group != os.getpgrp() for group in groups)
    for process in owned_processes:
        try:
            actual_group = os.getpgid(process.pid)
        except ProcessLookupError:
            continue  # An exited leader can still have pipe-holding descendants.
        assert actual_group == process.pid
    stopped = False
    try:
        assert _owned_process_is_executing(os.getpid())
        rows = [json.loads(line) for line in records.read_text().splitlines()]
        assert {row[0] for row in rows} == _OWNED_PIPE_ROLES
        assert {row[2] for row in rows} == groups
        assert {row[1] for row in rows if row[0] == "parent"} == groups
        for _ in range(20):
            if not any(_owned_process_is_executing(row[1]) for row in rows):
                break
            sleep(0.01)
        assert not any(_owned_process_is_executing(row[1]) for row in rows)
        stopped = True
    finally:
        if not stopped:
            for process in owned_processes:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=0.2)


@pytest.mark.skipif(
    os.name != "posix", reason="Owned process group cleanup uses POSIX."
)
@pytest.mark.parametrize("complete_markers", [False, True])
def test_owned_pipe_cleanup_cannot_turn_a_failed_oracle_into_a_pass(
    tmp_path: Path, *, complete_markers: bool
) -> None:
    """Readiness and live-state failures must still stop an explicitly owned group."""
    records = tmp_path / "recorded-roles.jsonl"
    ready = tmp_path / "actual-roles.jsonl"
    program = (
        "import json, os, time\n"
        f"records = {str(records)!r}\n"
        f"ready = {str(ready)!r}\n"
        "def record(role):\n"
        "    row = json.dumps([role, os.getpid(), os.getpgrp()]) + '\\n'\n"
        "    with open(ready, 'a') as stream: stream.write(row)\n"
        f"    if role == 'parent' or {complete_markers!r}:\n"
        "        with open(records, 'a') as stream: stream.write(row)\n"
        "record('parent')\n"
        "if os.fork() == 0:\n"
        "    record('child')\n"
        "    if os.fork() == 0: record('grandchild')\n"
        "time.sleep(10)\n"
    )
    leader = Popen(  # noqa: S603  # nosec B603  # owned synthetic process group only
        [sys.executable, "-c", program], start_new_session=True
    )
    pids = [leader.pid]
    try:
        deadline = monotonic() + 2
        while (
            not ready.exists()
            or len(ready.read_text().splitlines()) != len(_OWNED_PIPE_ROLES)
        ) and monotonic() < deadline:
            sleep(0.01)
        rows = [json.loads(line) for line in ready.read_text().splitlines()]
        pids.extend(row[1] for row in rows)
        assert {row[0] for row in rows} == _OWNED_PIPE_ROLES
        assert os.getpgid(leader.pid) == leader.pid
        assert all(_owned_process_is_executing(row[1]) for row in rows)
        with pytest.raises(AssertionError):
            _assert_owned_pipe_group_stopped(records, owned_processes=[leader])
        for _ in range(20):
            if not any(_owned_process_is_executing(row[1]) for row in rows):
                break
            sleep(0.01)
        assert not any(_owned_process_is_executing(row[1]) for row in rows)
    finally:
        if any(_owned_process_is_executing(pid) for pid in pids):
            with suppress(ProcessLookupError):
                os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=1)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin zombie-state oracle.")
def test_owned_process_oracle_recognizes_an_unreaped_darwin_child() -> None:
    """A known zombie is stopped even while kill(0) still recognizes its PID."""
    assert _owned_process_is_executing(os.getpid())
    child = Popen([sys.executable, "-c", "pass"], start_new_session=True)  # nosec B603
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = ctypes.create_string_buffer(256)
        size: int = 0
        for _ in range(40):
            # XNU's nonzero BSDINFO arg explicitly enables zombie lookup.
            size = libproc.proc_pidinfo(child.pid, 3, 1, info, len(info))
            if (
                size
                and struct.unpack_from("I", info.raw, 4)[0] == _DARWIN_ZOMBIE_STATUS
            ):
                break
            sleep(0.025)
        assert size
        assert struct.unpack_from("I", info.raw, 4)[0] == _DARWIN_ZOMBIE_STATUS
        os.kill(child.pid, 0)
        assert _owned_process_is_executing(child.pid) is False
    finally:
        if child.returncode is None:
            try:
                child.wait(timeout=1)
            except TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=1)


def _bulk_probe_fixture(
    root: Path, count: int, operation: str
) -> set[RepositoryStatusEntry]:
    """Create fixture-known staged, worktree, and untracked bulk changes."""
    root.mkdir()
    names = tuple(f"bulk-old-{number:04d}.txt" for number in range(count))
    expected = {
        RepositoryStatusEntry(area="index", change="modified", path="both.txt"),
        RepositoryStatusEntry(area="worktree", change="modified", path="both.txt"),
        RepositoryStatusEntry(
            area="untracked", change="added", path="space and\nnewline.txt"
        ),
    }
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Bulk Probe Test")
            config.set_value("user", "email", "bulk-probe@example.com")
        for number, name in enumerate(names):
            (root / name).write_text(f"original {number}\n", encoding="utf-8")
        (root / "both.txt").write_text("original\n", encoding="utf-8")
        repo.index.add([*names, "both.txt"])
        repo.index.commit("bulk probe fixture")
        if operation == "renamed":
            renamed = tuple(name.replace("old", "new") for name in names)
            for old, new in zip(names, renamed, strict=True):
                (root / old).rename(root / new)
                expected.add(
                    RepositoryStatusEntry(
                        area="index", change="renamed", path=new, previous_path=old
                    )
                )
            repo.index.remove(list(names))
            repo.index.add(list(renamed))
        else:
            for name in names:
                (root / name).write_text("actual change\n", encoding="utf-8")
                expected.add(
                    RepositoryStatusEntry(area="worktree", change="modified", path=name)
                )
        (root / "both.txt").write_text("staged\n", encoding="utf-8")
        repo.index.add(["both.txt"])
    (root / "both.txt").write_text("unstaged\n", encoding="utf-8")
    (root / "space and\nnewline.txt").write_text("untracked\n", encoding="utf-8")
    return expected


def _record_git_commands(
    commands: list[tuple[str, ...]], *, bound_argv: bool
) -> Callable[..., object]:
    """Record actual Git subprocess arguments without changing execution semantics."""
    original_execute = cast("Callable[..., object]", Git.execute)

    def observe_execute(
        git: Git, command: str | Sequence[str], *args: object, **kwargs: object
    ) -> object:
        assert not isinstance(command, str)
        argv = tuple(map(str, command))
        commands.append(argv)
        if "--name-only" in argv:
            assert git.environment()["GIT_LITERAL_PATHSPECS"] == "1"
            assert git.environment()["GIT_OPTIONAL_LOCKS"] == "0"
        if bound_argv and len(argv) > _BULK_ARGV_LIMIT:
            message = "Changed-path argv exceeds the bounded command budget."
            raise OSError(message)
        return original_execute(git, command, *args, **kwargs)

    return observe_execute


@pytest.mark.parametrize("operation", ["modified", "renamed"])
def test_bulk_probe_keeps_git_command_and_argv_budgets_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Thousands of actual changes must not spawn or expand per-path commands."""
    observations: list[list[tuple[str, ...]]] = []
    for count in (3, 3000):
        root = tmp_path / f"{operation}-{count}"
        expected = _bulk_probe_fixture(root, count, operation)
        index = root / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        commands: list[tuple[str, ...]] = []
        with monkeypatch.context() as context:
            context.setattr(
                Git,
                "execute",
                _record_git_commands(commands, bound_argv=operation == "renamed"),
            )
            observed = GitPythonRepositoryProbe().inspect(root)
        assert set(observed.status_entries) == expected
        assert observed.dirty is True
        assert (index.read_bytes(), index.stat().st_mtime_ns) == before
        observations.append(commands)

    small, large = observations
    assert len(large) <= len(small) + _BULK_COMMAND_GROWTH_ALLOWANCE
    assert max(map(len, large)) <= max(map(len, small)) + _BULK_COMMAND_GROWTH_ALLOWANCE
    for commands in observations:
        diffs = [command for command in commands if "diff" in command]
        assert all("--shortstat" not in command for command in diffs)
        batched = [command for command in diffs if "--name-only" in command]
        assert len(batched) == 1
        assert {"-z", "--no-ext-diff", "--no-textconv"} <= set(batched[0])
        assert not any(
            argument.startswith("bulk-") or argument == "both.txt"
            for command in commands
            for argument in command
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


def test_completion_identity_is_observable_without_head_or_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unborn repository still has a usable membership identity."""
    with Repo.init(tmp_path):
        pass

    def forbid_head(_repo: Repo) -> str:
        message = "Membership preflight must not require HEAD."
        pytest.fail(message)  # ty: ignore[invalid-argument-type]

    def forbid_status(_repo: Repo) -> tuple[RepositoryStatusEntry, ...]:
        message = "Membership preflight must not inspect status."
        pytest.fail(message)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(adapter_module, "_status_entries", forbid_status)
    probe = GitPythonRepositoryProbe(_read_head_sha=forbid_head)
    assert probe.inspect_common_git_dir(tmp_path) == str((tmp_path / ".git").resolve())


def test_completion_identity_resolves_linked_worktree_and_symlink(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    """Comparing worktree-specific Git directories would reject a linked member."""
    linked = tmp_path / "identity-linked"
    with Repo(git_repository) as repo:
        repo.git.worktree("add", "--detach", str(linked), "HEAD")
    alias = tmp_path / "identity-alias"
    alias.symlink_to(linked, target_is_directory=True)
    assert GitPythonRepositoryProbe().inspect_common_git_dir(alias) == str(
        (git_repository / ".git").resolve()
    )


@pytest.mark.parametrize("target", ["missing", "nongit"])
def test_completion_identity_has_typed_unreachable_errors(
    tmp_path: Path,
    target: str,
) -> None:
    """Missing/non-Git paths must not escape as arbitrary adapter exceptions."""
    selected = tmp_path / target
    if target == "nongit":
        selected.mkdir()
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect_common_git_dir(selected)
    assert caught.value.code is (
        RepositoryProbeErrorCode.PATH_MISSING
        if target == "missing"
        else RepositoryProbeErrorCode.NOT_GIT_WORKTREE
    )


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


def test_cheap_revision_includes_untracked_without_full_status(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cheap verification must detect untracked dirt without constructing entries."""

    def forbid_status(_repo: Repo) -> tuple[RepositoryStatusEntry, ...]:
        message = "Cheap inspection built full status entries."
        raise AssertionError(message)

    monkeypatch.setattr(adapter_module, "_status_entries", forbid_status)
    probe = GitPythonRepositoryProbe()
    with Repo(git_repository) as repo:
        head_sha = repo.head.commit.hexsha
    clean = probe.inspect_revision(git_repository)
    assert clean.head_sha == head_sha
    assert clean.dirty is False
    assert set(clean.model_dump()) == {"head_sha", "dirty"}
    (git_repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    assert probe.inspect_revision(git_repository).dirty is True


def test_topology_and_cheap_revision_support_linked_detached_worktrees(
    git_repository: Path, tmp_path: Path
) -> None:
    """Related linked worktrees share topology but preserve their own dirty state."""
    probe = GitPythonRepositoryProbe()
    assert probe.has_other_worktrees(git_repository) is False
    linked = tmp_path / "linked"
    with Repo(git_repository) as repo:
        repo.git.worktree("add", "--detach", str(linked), "HEAD")
        head_sha = repo.head.commit.hexsha
    (linked / "untracked.txt").write_text("linked\n", encoding="utf-8")
    assert probe.has_other_worktrees(git_repository) is True
    assert probe.has_other_worktrees(linked) is True
    assert probe.inspect_revision(git_repository).dirty is False
    linked_revision = probe.inspect_revision(linked)
    assert linked_revision.head_sha == head_sha
    assert linked_revision.dirty is True


def test_cheap_revision_changed_head_is_typed(git_repository: Path) -> None:
    """A HEAD race must remain a mismatch rather than an unavailable result."""
    heads = iter(("a" * 40, "b" * 40))
    probe = GitPythonRepositoryProbe(_read_head_sha=lambda _repo: next(heads))
    with pytest.raises(RepositoryProbeError) as caught:
        probe.inspect_revision(git_repository)
    assert caught.value.code is RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE


@pytest.mark.parametrize("cheap", [False, True], ids=["full", "cheap"])
def test_probe_does_not_refresh_target_index_for_clean_stale_stat(
    git_repository: Path, *, cheap: bool
) -> None:
    """Git status must not rewrite the target index merely to refresh stat data."""
    tracked = git_repository / "tracked.txt"
    observed_stat = tracked.stat()
    os.utime(
        tracked,
        ns=(observed_stat.st_atime_ns, observed_stat.st_mtime_ns + 2_000_000_000),
    )
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    probe = GitPythonRepositoryProbe()
    result = (
        probe.inspect_revision(git_repository)
        if cheap
        else probe.inspect(git_repository)
    )
    assert result.dirty is False
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("colon_dirty", "other_dirty"),
    [(True, False), (True, True), (False, True)],
    ids=["colon-only", "both-changed", "other-only"],
)
def test_actual_content_predicate_treats_colon_path_literally(
    git_repository: Path, *, colon_dirty: bool, other_dirty: bool
) -> None:
    """Git pathspec magic must neither hide :foo nor borrow changes from foo."""
    with Repo(git_repository) as repo:
        for name in (":foo", "foo"):
            (git_repository / name).write_text("unchanged\n", encoding="utf-8")
        repo.index.add([":foo", "foo"])
        repo.index.commit("literal path fixture")
        if colon_dirty:
            (git_repository / ":foo").write_text("colon changed\n", encoding="utf-8")
        if other_dirty:
            (git_repository / "foo").write_text("other changed\n", encoding="utf-8")
        index = git_repository / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        repo.git.set_persistent_git_options(c="diff.autoRefreshIndex=false")
        with repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"):
            porcelain = repo.git.status("--porcelain=v1", "-z")
            worktree_paths = {
                record[3:]
                for record in porcelain.split("\0")
                if len(record) > _PORCELAIN_RECORD_PREFIX_LENGTH and record[1] != " "
            }
            observed = adapter_module._has_diff(
                repo,
                path=":foo",
                changed_paths=(
                    adapter_module._worktree_diff_paths(repo) & worktree_paths
                ),
            )
        assert observed is colon_dirty
        assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_does_not_match_stat_only_bracket_path_to_other_dirty_file(
    git_repository: Path,
) -> None:
    """A glob-shaped clean path must not inflate actual dirty paths or their count."""
    with Repo(git_repository) as repo:
        for name in ("file[1].txt", "file1.txt"):
            (git_repository / name).write_text("unchanged\n", encoding="utf-8")
        repo.index.add(["file[1].txt", "file1.txt"])
        repo.index.commit("bracket path fixture")
    stat_only = git_repository / "file[1].txt"
    observed_stat = stat_only.stat()
    os.utime(
        stat_only,
        ns=(observed_stat.st_atime_ns, observed_stat.st_mtime_ns + 2_000_000_000),
    )
    (git_repository / "file1.txt").write_text("actual change\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert observed.dirty is True
    paths = sorted({entry.path for entry in observed.status_entries})
    assert paths == ["file1.txt"]
    assert len(paths) == 1
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_retains_fifo_change_without_reading_content_or_writing_index(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked FIFO replacement needs raw metadata, never a content comparison."""
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        message = "FIFO creation is unavailable on this platform"
        raise pytest.skip.Exception(message)
    tracked = git_repository / "tracked.txt"
    tracked.unlink()
    mkfifo(tracked)
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    original_execute = cast("Callable[..., object]", Git.execute)

    def metadata_diff(
        git: Git, command: str | Sequence[str], *args: object, **kwargs: object
    ) -> object:
        if "--shortstat" in command:
            message = "A FIFO must not be opened for content comparison."
            raise AssertionError(message)
        return original_execute(git, command, *args, **kwargs)

    monkeypatch.setattr(Git, "execute", metadata_diff)
    result = GitPythonRepositoryProbe().inspect(git_repository)

    assert result.dirty is True
    assert result.status_entries == (
        RepositoryStatusEntry(area="worktree", change="modified", path="tracked.txt"),
    )
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("coexisting", ["none", "tracked", "untracked"])
def test_full_probe_rejects_incomplete_colon_path_observation(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch, coexisting: str
) -> None:
    """Unsupported tracked colon names must fail before raw diff can omit them."""
    with Repo(git_repository) as repo:
        (git_repository / ":foo").write_text("unchanged\n", encoding="utf-8")
        repo.index.add([":foo"])
        repo.index.commit("colon completeness fixture")
    (git_repository / ":foo").write_text("colon changed\n", encoding="utf-8")
    if coexisting == "tracked":
        (git_repository / "tracked.txt").write_text(
            "tracked changed\n", encoding="utf-8"
        )
    elif coexisting == "untracked":
        (git_repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    diff_calls: list[object] = []
    original = adapter_module._diff_entries

    def observe_diff(
        diffs: Iterable[Diff],
        *,
        area: Literal["index", "worktree"],
        reverse: bool = False,
    ) -> tuple[RepositoryStatusEntry, ...]:
        diff_calls.append(None)
        return original(diffs, area=area, reverse=reverse)

    monkeypatch.setattr(adapter_module, "_diff_entries", observe_diff)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert str(caught.value) == "Git metadata could not be read."
    assert diff_calls == []
    assert GitPythonRepositoryProbe().inspect_revision(git_repository).dirty is True
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_rejects_other_silent_raw_diff_omissions(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching dirty booleans must not conceal a missing normalized path."""
    (git_repository / "tracked.txt").write_text("tracked changed\n", encoding="utf-8")
    (git_repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "_diff_entries", lambda *_args, **_kwargs: ())
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert str(caught.value) == "Git metadata could not be read."
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    "stage_output",
    [
        b"160000 " + b"0" * 40 + b" 0\ttracked.txt",
        b"160000 " + b"0" * 40 + b" 0 tracked.txt\0",
        b"960000 " + b"0" * 40 + b" 0\ttracked.txt\0",
        b"160000 " + b"0" * 40 + b" 9\ttracked.txt\0",
        b"160000 " + b"x" * 40 + b" 0\ttracked.txt\0",
        b"160000 " + b"0" * 40 + b" 1\ttracked.txt\0",
    ],
    ids=[
        "unterminated-path",
        "missing-path-separator",
        "invalid-mode",
        "invalid-stage",
        "invalid-object-id",
        "nonzero-gitlink-stage",
    ],
)
def test_full_probe_rejects_unconfirmed_gitlink_index_metadata(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_output: bytes,
) -> None:
    """Incomplete or non-stage-zero metadata cannot fill a raw diff coverage gap."""
    (git_repository / "tracked.txt").write_text("tracked changed\n", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "_diff_entries", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        Git, "ls_files", lambda *_args, **_kwargs: stage_output, raising=False
    )
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_ctime_ns)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert (
        index.read_bytes(),
        index.stat().st_mtime_ns,
        index.stat().st_ctime_ns,
    ) == before


@pytest.mark.parametrize(
    "raw_output",
    [
        b":100644 100644 " + b"0" * 40 + b" " + b"0" * 40 + b" M\0tracked.txt",
        b"invalid header\0tracked.txt\0",
        b":100644 100644 " + b"0" * 40 + b" " + b"0" * 40 + b" R100\0tracked.txt\0",
    ],
    ids=["unterminated-path", "malformed-header", "missing-rename-target"],
)
def test_full_probe_rejects_incomplete_raw_record_framing(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch, raw_output: bytes
) -> None:
    """Partial NUL records must fail closed rather than drop dirty paths."""
    (git_repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

    def raw_diff(_git: Git, *arguments: object, **kwargs: object) -> object:
        if "--name-only" in arguments:
            return "tracked.txt\0"
        if kwargs.get("stdout_as_string") is False:
            assert "--raw" in arguments
            return raw_output
        return _git._call_process("diff", *arguments, **kwargs)

    monkeypatch.setattr(Git, "diff", raw_diff, raising=False)
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("operation", ["staged", "rename-source", "rename-target"])
def test_full_probe_rejects_colon_index_paths_before_raw_diff(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A staged current or rename-source leading colon has the same parser limit."""
    with Repo(git_repository) as repo:
        (git_repository / ":foo").write_text("unchanged\n", encoding="utf-8")
        repo.index.add([":foo"])
        repo.index.commit("colon staged fixture")
        if operation == "staged":
            (git_repository / ":foo").write_text("changed\n", encoding="utf-8")
            repo.index.add([":foo"])
        elif operation == "rename-source":
            repo.git.mv("--", ":foo", "ordinary.txt")
        else:
            repo.git.mv("--", "tracked.txt", ":target")

    def forbid_diff(*_args: object, **_kwargs: object) -> str:
        message = "Known unsupported colon path reached raw diff."
        raise AssertionError(message)

    monkeypatch.setattr(Git, "diff", forbid_diff, raising=False)
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(git_repository)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert str(caught.value) == "Git metadata could not be read."
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("dirty", [False, True])
def test_full_probe_preserves_supported_colon_path_cases(
    git_repository: Path, *, dirty: bool
) -> None:
    """Unchanged leading-colon and ordinary embedded-colon paths stay supported."""
    with Repo(git_repository) as repo:
        for name in (":unchanged", "inner:colon.txt"):
            (git_repository / name).write_text("unchanged\n", encoding="utf-8")
        repo.index.add([":unchanged", "inner:colon.txt"])
        repo.index.commit("supported colon fixture")
    stat_only = git_repository / ":unchanged"
    observed_stat = stat_only.stat()
    os.utime(
        stat_only,
        ns=(observed_stat.st_atime_ns, observed_stat.st_mtime_ns + 2_000_000_000),
    )
    if dirty:
        (git_repository / "inner:colon.txt").write_text("changed\n", encoding="utf-8")
        (git_repository / ":untracked").write_text("untracked\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert observed.dirty is dirty
    assert {entry.path for entry in observed.status_entries} == (
        {"inner:colon.txt", ":untracked"} if dirty else set()
    )
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_preserves_staged_and_unstaged_cancelled_net_diff(
    git_repository: Path,
) -> None:
    """A return to HEAD still has both status areas relative to the index."""
    tracked = git_repository / "tracked.txt"
    original = tracked.read_text(encoding="utf-8")
    with Repo(git_repository) as repo:
        tracked.write_text("staged change\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
    tracked.write_text(original, encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert observed.dirty is True
    assert {entry.path for entry in observed.status_entries} == {"tracked.txt"}
    assert {entry.area for entry in observed.status_entries} == {"index", "worktree"}
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_frames_unusual_dirty_names_next_to_unchanged_colon_path(
    git_repository: Path,
) -> None:
    """An ignored colon-leading record must not split supported dirty filenames."""
    modified = "space and\ncafé [1].txt"
    previous = "rename old\nname.txt"
    renamed = "rename new\nname.txt"
    with Repo(git_repository) as repo:
        for name in (":unchanged", modified, previous):
            (git_repository / name).write_text(f"original {name}\n", encoding="utf-8")
        repo.index.add([":unchanged", modified, previous])
        repo.index.commit("NUL framing fixture")
        repo.git.mv("--", previous, renamed)
    for name in (modified, renamed):
        (git_repository / name).write_text("worktree change\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert set(observed.status_entries) == {
        RepositoryStatusEntry(
            area="index", change="renamed", path=renamed, previous_path=previous
        ),
        RepositoryStatusEntry(area="worktree", change="modified", path=modified),
        RepositoryStatusEntry(area="worktree", change="modified", path=renamed),
    }
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_full_probe_retains_untracked_reuse_of_staged_rename_source(
    git_repository: Path,
) -> None:
    """An untracked current path can also be a tracked rename's previous path."""
    with Repo(git_repository) as repo:
        repo.git.mv("renamed-old.txt", "renamed-new.txt")
    (git_repository / "renamed-old.txt").write_text("new untracked\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert set(observed.status_entries) == {
        RepositoryStatusEntry(
            area="index",
            change="renamed",
            path="renamed-new.txt",
            previous_path="renamed-old.txt",
        ),
        RepositoryStatusEntry(area="untracked", change="added", path="renamed-old.txt"),
    }
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("renames_disabled", [False, True])
def test_full_probe_preserves_rename_current_path_and_unusual_untracked_paths(
    git_repository: Path, *, renames_disabled: bool
) -> None:
    """NUL metadata must skip rename sources and preserve path whitespace."""
    target = "renamed new.txt"
    untracked = "nested/space and\nnewline.txt"
    with Repo(git_repository) as repo:
        repo.git.mv("renamed-old.txt", target)
        if renames_disabled:
            with repo.config_writer() as config:
                config.set_value("status", "renames", "false")
    (git_repository / "nested").mkdir()
    (git_repository / untracked).write_text("untracked\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    assert {entry.path for entry in observed.status_entries} == {target, untracked}
    renamed = next(
        entry for entry in observed.status_entries if entry.change == "renamed"
    )
    assert renamed.previous_path == "renamed-old.txt"
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("operation", ["full", "cheap", "topology"])
def test_completion_probe_git_failures_are_typed_and_do_not_leak_stderr(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A non-diff Git failure must produce a closed safe probe error."""

    def fail_git(*_args: object, **_kwargs: object) -> bool:
        command = "test-git"
        raise GitCommandError(command, 128, stderr="private raw error output")

    if operation == "topology":
        monkeypatch.setattr(Git, "worktree", fail_git, raising=False)
    elif operation == "cheap":
        if os.name != "posix":
            monkeypatch.setattr(Git, "execute", fail_git)
        else:
            real_git = shutil.which("git")
            assert real_git is not None
            executable = git_repository.parent / "failing-status-git"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "if 'status' in sys.argv[1:]:\n"
                "    sys.stderr.write('private raw error output')\n"
                "    sys.exit(128)\n"
                f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", str(executable))
    else:
        monkeypatch.setattr(adapter_module, "_has_diff", fail_git)
        (git_repository / "tracked.txt").write_text("actual dirty\n", encoding="utf-8")
    probe = GitPythonRepositoryProbe()
    inspect = {
        "full": probe.inspect,
        "cheap": probe.inspect_revision,
        "topology": probe.has_other_worktrees,
    }[operation]
    with pytest.raises(RepositoryProbeError) as caught:
        inspect(git_repository)
    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert str(caught.value) == "Git metadata could not be read."
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("cheap", [False, True], ids=["full", "cheap"])
def test_probe_ignores_configured_external_diff_and_textconv_helpers(
    git_repository: Path, tmp_path: Path, *, cheap: bool
) -> None:
    """Custom diff helpers must neither execute nor hide real worktree changes."""
    marker = tmp_path / "driver-ran"
    driver = tmp_path / "diff-driver.sh"
    driver.write_text(
        "#!/bin/sh\n" + "touch " + shlex.quote(str(marker)) + "\nexit 0\n",
        encoding="utf-8",
    )
    command = shlex.join(("sh", str(driver)))
    with Repo(git_repository) as repo:
        (git_repository / ".gitattributes").write_text(
            "tracked.txt diff=inspection-test\n", encoding="utf-8"
        )
        repo.index.add([".gitattributes"])
        repo.index.commit("configured diff fixture")
        with repo.config_writer() as config:
            config.set_value('diff "inspection-test"', "command", command)
            config.set_value('diff "inspection-test"', "textconv", command)
    (git_repository / "tracked.txt").write_text("real change\n", encoding="utf-8")
    probe = GitPythonRepositoryProbe()
    result = (
        probe.inspect_revision(git_repository)
        if cheap
        else probe.inspect(git_repository)
    )
    assert result.dirty is True
    assert marker.exists() is False


@pytest.mark.parametrize("fail_head_read", [False, True], ids=["success", "error"])
def test_probe_reaps_git_process_before_returning_or_raising(
    git_repository: Path,
    fail_head_read: bool,
) -> None:
    """Release the owned Git subprocess without relying on garbage collection."""
    opened_repositories: list[Repo] = []
    processes: list[Popen[bytes]] = []

    def read_head(repo: Repo) -> str:
        # This injected reader deliberately opens a cached SDK process; production
        # now uses bounded synchronous rev-parse instead of the object database.
        head_sha = repo.head.commit.hexsha
        if not opened_repositories:
            opened_repositories.append(repo)
            cached_command = repo.git.cat_file_header
            assert cached_command is not None
            process = cached_command.proc
            assert process is not None
            processes.append(process)
        if fail_head_read:
            message = "HEAD metadata became unreadable"
            raise OSError(message)
        return head_sha

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


@pytest.mark.parametrize("area", ["worktree", "index", "both"])
def test_full_probe_treats_tracked_head_filename_as_path(
    git_repository: Path, area: str
) -> None:
    """A filename equal to the revision must preserve staged/worktree evidence."""
    tracked = git_repository / "HEAD"
    with Repo(git_repository) as repo:
        tracked.write_text("original\n", encoding="utf-8")
        repo.index.add(["HEAD"])
        repo.index.commit("HEAD filename fixture")
        tracked.write_text("changed\n", encoding="utf-8")
        if area != "worktree":
            repo.index.add(["HEAD"])
        if area == "both":
            tracked.write_text("unstaged after staged\n", encoding="utf-8")
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    observed = GitPythonRepositoryProbe().inspect(git_repository)

    expected_areas = {"index", "worktree"} if area == "both" else {area}
    assert {
        (entry.area, entry.change, entry.path, entry.previous_path)
        for entry in observed.status_entries
    } == {(item, "modified", "HEAD", None) for item in expected_areas}
    assert observed.dirty is True
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


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


@pytest.mark.parametrize(
    "raw_name",
    [
        "日本語.txt".encode(),
        pytest.param(
            b"surrogate-\xff.txt",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="POSIX surrogateescape filenames are not supported on Windows",
            ),
        ),
    ],
)
def test_non_ascii_and_surrogateescaped_paths_have_stable_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_name: bytes,
) -> None:
    """Expose surrogateescaped Git status through the public probe result."""
    decoded_path = os.fsdecode(raw_name)
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
        untracked_files=(unicode_path, decoded_path),
        remotes=(),
        working_tree_dir=worktree_path,
        common_dir=common_git_dir,
        active_branch=SimpleNamespace(name="main"),
        git=Git(tmp_path),
    )

    def repository_factory(*_args: object, **_kwargs: object) -> Repo:
        return cast("Repo", fake_repo)

    monkeypatch.setattr(adapter_module, "Repo", repository_factory)
    monkeypatch.setattr(
        Git,
        "status",
        lambda *_args, **_kwargs: f"?? {unicode_path}\0?? {decoded_path}\0",
        raising=False,
    )

    result = GitPythonRepositoryProbe(_read_head_sha=lambda _repo: "a" * 40).inspect(
        tmp_path
    )
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
                    path=decoded_path,
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


@pytest.mark.parametrize(
    "method",
    ["inspect", "inspect_common_git_dir", "inspect_revision", "has_other_worktrees"],
)
@pytest.mark.parametrize("path_check", ["exists", "is_dir"])
def test_filesystem_preflight_permission_failure_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path_check: str,
) -> None:
    """Filesystem failures before opening Git must honor the closed error contract."""
    target = tmp_path.resolve()
    original_check = getattr(Path, path_check)

    def denied_check(path: Path) -> bool:
        if path == target:
            raise PermissionError(EACCES, "filesystem preflight denied", str(target))
        return original_check(path)

    monkeypatch.setattr(Path, path_check, denied_check)
    with pytest.raises(RepositoryProbeError) as caught:
        getattr(GitPythonRepositoryProbe(), method)(target)

    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert caught.value.path == str(target)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert caught.value.__cause__.errno == EACCES


@pytest.mark.parametrize(
    "method",
    ["inspect", "inspect_common_git_dir", "inspect_revision", "has_other_worktrees"],
)
def test_inaccessible_parent_has_typed_preflight_error(
    tmp_path: Path, method: str
) -> None:
    """An actual unsearchable parent must not leak its filesystem exception."""
    parent = tmp_path / "unreadable"
    target = parent / "selected-target"
    target.mkdir(parents=True)
    original_mode = stat.S_IMODE(parent.stat().st_mode)
    parent.chmod(0)
    try:
        try:
            target.stat()
        except PermissionError:
            pass
        else:
            message = "Host privileges do not enforce directory search permissions."
            pytest.skip(message)  # ty: ignore[too-many-positional-arguments]
        with pytest.raises(RepositoryProbeError) as caught:
            getattr(GitPythonRepositoryProbe(), method)(target)
        assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
        assert caught.value.path == str(target)
        assert isinstance(caught.value.__cause__, PermissionError)
    finally:
        parent.chmod(original_mode)


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


@pytest.mark.parametrize(
    "setting", ["outside_timeout_seconds", "verification_timeout_seconds"]
)
@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True, False])
def test_probe_timeout_configuration_requires_finite_positive_numbers(
    setting: str,
    invalid: float,
) -> None:
    """Invalid deadlines must never disable the completion command bound."""
    with pytest.raises(ValueError, match="finite positive"):
        GitPythonRepositoryProbe(
            outside_timeout_seconds=(
                invalid if setting == "outside_timeout_seconds" else 10.0
            ),
            verification_timeout_seconds=(
                invalid if setting == "verification_timeout_seconds" else 2.0
            ),
        )


@pytest.mark.parametrize("configured", [False, True])
def test_completion_git_commands_use_phase_appropriate_deadline_ownership(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool,
) -> None:
    """Include hidden HEAD, raw diff, remote and topology subprocesses in the audit."""
    with Repo(git_repository) as repo:
        repo.create_remote("origin", "https://example.invalid/project.git")
        (git_repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        (git_repository / "tracked.txt").write_text("changed again\n", encoding="utf-8")
    (git_repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    outside, inside = (0.8, 0.3) if configured else (10.0, 2.0)
    probe = (
        GitPythonRepositoryProbe(
            outside_timeout_seconds=outside,
            verification_timeout_seconds=inside,
        )
        if configured
        else GitPythonRepositoryProbe()
    )
    original_execute = cast("Callable[..., object]", Git.execute)
    commands: list[tuple[str, ...]] = []
    verification = False

    def bounded_execute(
        git: Git,
        command: Sequence[object],
        *args: object,
        **kwargs: object,
    ) -> object:
        if verification and sys.platform != "win32":
            assert kwargs.get("as_process") is True, tuple(command)
            assert kwargs.get("start_new_session") is True, tuple(command)
            assert kwargs.get("shell") is False, tuple(command)
            assert "kill_after_timeout" not in kwargs, tuple(command)
            assert "core.fsmonitor=false" in command
            assert "submodule.recurse=false" in command
        elif sys.platform == "win32":
            assert "kill_after_timeout" not in kwargs, tuple(command)
        else:
            assert kwargs.get("kill_after_timeout") == outside, tuple(command)
            assert kwargs.get("as_process", False) is False, tuple(command)
        commands.append(tuple(str(part) for part in command))
        return original_execute(git, command, *args, **kwargs)

    monkeypatch.setattr(Git, "execute", bounded_execute)
    probe.inspect_common_git_dir(git_repository)
    observed = probe.inspect(git_repository)
    assert observed.dirty is True
    assert observed.remotes == ("https://example.invalid/project.git",)
    assert probe.has_other_worktrees(git_repository) is False
    assert any("rev-parse" in command for command in commands)
    assert any("--raw" in command for command in commands)
    assert any("--name-only" in command for command in commands)
    assert any("remote" in command for command in commands)
    assert any("worktree" in command for command in commands)
    assert all("cat-file" not in command for command in commands)
    verification = True
    assert probe.inspect_revision(git_repository).dirty is True


@pytest.mark.skipif(os.name != "posix", reason="Verification groups require POSIX.")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_verification_deadline_stops_owned_pipe_holding_descendants(
    git_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent_exits: bool,
) -> None:
    """Inherited pipes must not extend the deadline, even after the leader exits."""
    wrapper, records = _owned_pipe_git(tmp_path, parent_exits=parent_exits)
    owned_processes = _observe_owned_status_processes(monkeypatch, wrapper)
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", str(wrapper))
    # The old watchdog invokes a sandbox-denied ps command. Disable only that
    # legacy path so RED measures the six-second inherited-pipe stall safely.
    monkeypatch.setattr(adapter_module, "_deadline_options", lambda _seconds: {})
    # Actual child/grandchild forks avoid interpreter-startup confounders. The
    # separate 0.4s overall-budget test independently exercises a tighter bound.
    probe = GitPythonRepositoryProbe(verification_timeout_seconds=0.75)
    caught: RepositoryProbeError | None = None
    started = monotonic()
    try:
        try:
            probe.inspect_revision(git_repository)
        except RepositoryProbeError as error:
            caught = error
        elapsed = monotonic() - started
    finally:
        _assert_owned_pipe_group_stopped(records, owned_processes=owned_processes)
    assert elapsed < _PIPE_TIMEOUT_ELAPSED_LIMIT
    assert caught is not None
    assert caught.code is RepositoryProbeErrorCode.PROBE_TIMED_OUT
    assert str(caught) == "Repository probe timed out."


@pytest.mark.skipif(
    os.name != "posix", reason="Executable deadline fixture uses POSIX."
)
def test_verification_uses_one_deadline_for_all_git_commands(
    git_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three individually fast commands must not renew the overall budget."""
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper = tmp_path / "slow-git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "if any(part in sys.argv[1:] for part in ('status', 'rev-parse')):\n"
        "    time.sleep(0.18)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", str(wrapper))
    monkeypatch.setattr(adapter_module, "_deadline_options", lambda _seconds: {})
    probe = GitPythonRepositoryProbe(verification_timeout_seconds=0.4)
    started = monotonic()
    with pytest.raises(RepositoryProbeError) as caught:
        probe.inspect_revision(git_repository)
    assert caught.value.code is RepositoryProbeErrorCode.PROBE_TIMED_OUT
    assert monotonic() - started < _OVERALL_TIMEOUT_ELAPSED_LIMIT


def test_verification_expired_reader_hook_does_not_start_status(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted required HEAD read must refuse before the next Git command."""
    clock = [0.0]
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock[0], raising=False)

    def slow_head(_repo: Repo) -> str:
        clock[0] += 3.0
        return "1" * 40

    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        Git, "execute", _record_git_commands(commands, bound_argv=False)
    )
    probe = GitPythonRepositoryProbe(_read_head_sha=slow_head)
    with pytest.raises(RepositoryProbeError) as caught:
        probe.inspect_revision(git_repository)
    assert caught.value.code is RepositoryProbeErrorCode.PROBE_TIMED_OUT
    assert not any("status" in command for command in commands)


def test_verification_missing_git_executable_has_a_safe_typed_error(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable SDK executable must not leak a raw Popen type error."""
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", None)
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect_revision(git_repository)
    assert caught.value.code is RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE
    assert str(caught.value) == "Git metadata could not be read."


@pytest.mark.skipif(os.name != "posix", reason="Communicate deadline applies to POSIX.")
def test_verification_passes_only_decreasing_remaining_budgets(
    git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next required command receives elapsed time deducted from its budget."""
    clock = [0.0]
    budgets: list[float] = []
    monkeypatch.setattr(adapter_module, "monotonic", lambda: clock[0], raising=False)

    class ControlledProcess:
        """Model elapsed process time while keeping SDK command dispatch observable."""

        stdin = stdout = stderr = None
        returncode = 0

        def __init__(self, output: bytes) -> None:
            self.output = output

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            budgets.append(timeout)
            clock[0] += 0.25
            return self.output, b""

    def controlled_execute(
        _git: Git, command: Sequence[object], **kwargs: object
    ) -> object:
        output = b"1" * 40 if "rev-parse" in command else b""
        if kwargs.get("as_process"):
            return SimpleNamespace(proc=ControlledProcess(output), status=None)
        budgets.append(cast("float", kwargs.get("kill_after_timeout", 2.0)))
        clock[0] += 0.25
        return output.decode()

    monkeypatch.setattr(Git, "execute", controlled_execute)
    observed = GitPythonRepositoryProbe().inspect_revision(git_repository)
    assert observed.head_sha == "1" * 40
    assert observed.dirty is False
    assert budgets == pytest.approx([2.0, 1.75, 1.5])


@pytest.mark.skipif(
    os.name != "posix", reason="Executable fsmonitor fixture uses POSIX."
)
@pytest.mark.parametrize("dirty", [False, True])
def test_verification_disables_fsmonitor_without_changing_dirty_answer(
    git_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dirty: bool,
) -> None:
    """Local status must not execute the configured fsmonitor hook."""
    # The harness injects core.fsmonitor=false as command-scope configuration.
    # Remove that injection only for this disposable fixture to exercise the hook.
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    marker = tmp_path / "fsmonitor-invoked"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(marker)!r}, 'w').write('invoked')\n"
        "sys.stdout.buffer.write(b'token\\0/\\0')\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    if dirty:
        (git_repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    with Repo(git_repository) as repo:
        with repo.config_writer() as config:
            config.set_value("core", "fsmonitor", str(hook))
        with repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"):
            assert bool(repo.git.status("--porcelain", "-z")) is dirty
    assert marker.exists()
    marker.unlink()
    index = git_repository / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    assert GitPythonRepositoryProbe().inspect_revision(git_repository).dirty is dirty
    assert not marker.exists()
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("change", ["clean", "tracked", "untracked", "gitlink"])
@pytest.mark.parametrize(
    ("ignore", "index_version"),
    [
        ("default", 2),
        ("none", 2),
        ("untracked", 2),
        ("dirty", 2),
        ("all", 2),
        ("default", _GIT_INDEX_V4),
    ],
)
def test_probes_preserve_nested_submodule_status_semantics(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    ignore: str,
    index_version: int,
) -> None:
    """Containment must preserve nested changes and configured ignore semantics."""
    module_name = (
        "module\twith\nnewline"
        if index_version == _GIT_INDEX_V4 and os.name == "posix"
        else "module"
    )
    config_path = (
        json.dumps(module_name) if index_version == _GIT_INDEX_V4 else module_name
    )
    module_path = git_repository / module_name
    with Repo.init(module_path) as module:
        with module.config_writer() as config:
            config.set_value("user", "name", "Synthetic Module Test")
            config.set_value("user", "email", "module@example.invalid")
        (module_path / "file.txt").write_text("first\n", encoding="utf-8")
        module.index.add(["file.txt"])
        first_sha = module.index.commit("first module commit").hexsha
        with Repo(git_repository) as repo:
            (git_repository / ".gitmodules").write_text(
                f'[submodule "module"]\n\tpath = {config_path}\n'
                "\turl = https://example.invalid/module.git\n",
                encoding="utf-8",
            )
            repo.index.add([".gitmodules"])
            repo.git.update_index(
                "--add", "--cacheinfo", f"160000,{first_sha},{module_name}"
            )
            repo.index.commit("record gitlink")
            with repo.config_writer() as config:
                if ignore != "default":
                    config.set_value('submodule "module"', "ignore", ignore)
                config.set_value("submodule", "recurse", True)
        if change in {"tracked", "gitlink"}:
            (module_path / "file.txt").write_text("second\n", encoding="utf-8")
        if change == "gitlink":
            module.index.add(["file.txt"])
            module.index.commit("second module commit")
        if change == "untracked":
            (module_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    if index_version == _GIT_INDEX_V4:
        with Repo(git_repository) as repo:
            repo.git.update_index("--index-version=4")
    expected = (
        change != "clean"
        and ignore != "all"
        and not (change == "untracked" and ignore in {"untracked", "dirty"})
        and not (change == "tracked" and ignore == "dirty")
    )
    with (
        Repo(git_repository) as repo,
        repo.git.custom_environment(GIT_OPTIONAL_LOCKS="0"),
    ):
        baseline = bool(
            repo.git.status("--porcelain", "-z", "--untracked-files=normal")
        )
    assert baseline is expected
    index = git_repository / ".git" / "index"
    module_index = module_path / ".git" / "index"
    before = [
        (item.read_bytes(), item.stat().st_mtime_ns, item.stat().st_ctime_ns)
        for item in (index, module_index)
    ]

    def forbid_submodules(_repo: Repo) -> None:
        message = "Status parsing opened SDK submodule objects."
        raise AssertionError(message)

    monkeypatch.setattr(Repo, "submodules", property(forbid_submodules))
    entries = (
        (RepositoryStatusEntry(area="worktree", change="modified", path=module_name),)
        if expected
        else ()
    )
    probe = GitPythonRepositoryProbe()
    observed = probe.inspect(git_repository)
    assert observed.dirty is baseline
    assert observed.status_entries == entries
    assert observed.status_fingerprint == canonical_hash(
        {
            "probe_version": "agileforge.repository-probe.v1",
            "head_sha": observed.head_sha,
            "branch_name": observed.branch_name,
            "detached_head": False,
            "dirty": expected,
            "status_entries": [entry.model_dump(mode="json") for entry in entries],
            "remotes": (),
            "remote_omitted": False,
        }
    )
    assert probe.inspect_revision(git_repository).dirty is baseline
    assert [
        (item.read_bytes(), item.stat().st_mtime_ns, item.stat().st_ctime_ns)
        for item in (index, module_index)
    ] == before


@pytest.mark.parametrize(
    "phase", ["head", "status", "raw", "names", "remote", "topology"]
)
def test_gitpython_timeout_is_a_closed_probe_failure(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Only SDK timeout evidence becomes PROBE_TIMED_OUT; stderr stays private."""
    with Repo(git_repository) as repo:
        repo.create_remote("origin", "https://example.invalid/project.git")
    (git_repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    original_execute = cast("Callable[..., object]", Git.execute)

    def timeout_execute(
        git: Git,
        command: Sequence[object],
        *args: object,
        **kwargs: object,
    ) -> object:
        parts = tuple(str(part) for part in command)
        matching = {
            "head": "rev-parse" in parts or "cat-file" in parts,
            "status": "status" in parts,
            "raw": "--raw" in parts,
            "names": "--name-only" in parts,
            "remote": "remote" in parts,
            "topology": "worktree" in parts,
        }
        if matching[phase]:
            raise GitCommandError(
                list(parts),
                _SDK_TIMEOUT_STATUS,
                stderr=(
                    'Timeout: the command "private synthetic target" '
                    "did not complete in 0.1 secs."
                ),
            )
        return original_execute(git, command, *args, **kwargs)

    monkeypatch.setattr(Git, "execute", timeout_execute)
    probe = GitPythonRepositoryProbe()
    inspect = probe.has_other_worktrees if phase == "topology" else probe.inspect
    with pytest.raises(RepositoryProbeError) as caught:
        inspect(git_repository)
    assert caught.value.code.value == "PROBE_TIMED_OUT"
    assert str(caught.value) == "Repository probe timed out."
    assert "private synthetic target" not in str(caught.value)


@pytest.mark.skipif(
    os.name != "posix",
    reason="Live verification group ownership requires POSIX.",
)
def test_posix_owned_stalled_git_is_actually_terminated(
    git_repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real sleeping Git replacement must die before it can return status."""
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = tmp_path / "stalled-git"
    pid_file = tmp_path / "stalled-git.pid"
    fake_git.write_text(
        "#!/bin/sh\n"
        "for part do\n"
        '    if [ "$part" = status ]; then\n'
        f"        printf '%s' \"$$\" > {shlex.quote(str(pid_file))}\n"
        "        exec /bin/sleep 5\n"
        "    fi\n"
        "done\n"
        f'exec {shlex.quote(real_git)} "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    with Repo(git_repository) as repo:
        expected_head = repo.head.commit.hexsha
    monkeypatch.setattr(Git, "GIT_PYTHON_GIT_EXECUTABLE", str(fake_git))
    # This test isolates termination of status; the three-command overall budget
    # is exercised separately and must not spend this fixture's startup allowance.
    probe = GitPythonRepositoryProbe(
        verification_timeout_seconds=0.5, _read_head_sha=lambda _repo: expected_head
    )
    started = monotonic()
    with pytest.raises(RepositoryProbeError) as caught:
        probe.inspect_revision(git_repository)
    assert caught.value.code.value == "PROBE_TIMED_OUT"
    assert monotonic() - started < _LIVE_TIMEOUT_ELAPSED_LIMIT
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_windows_sdk_compatibility_preserves_healthy_inspection(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows SDK rejection must not break native healthy-probe test support."""
    monkeypatch.setattr(
        adapter_module, "_WINDOWS_TIMEOUT_UNSUPPORTED", True, raising=False
    )
    original_execute = cast("Callable[..., object]", Git.execute)

    def windows_execute(
        git: Git,
        command: Sequence[object],
        *args: object,
        **kwargs: object,
    ) -> object:
        if "kill_after_timeout" in kwargs:
            raise GitCommandError(
                [str(part) for part in command], "Windows SDK timeout unsupported"
            )
        return original_execute(git, command, *args, **kwargs)

    monkeypatch.setattr(Git, "execute", windows_execute)
    probe = GitPythonRepositoryProbe()
    assert probe.inspect(git_repository).dirty is False
    assert probe.inspect_revision(git_repository).dirty is False
    assert probe.has_other_worktrees(git_repository) is False


@pytest.mark.parametrize("staged", [False, True])
def test_gitlink_status_parsing_does_not_open_submodule_repositories(
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    staged: bool,
) -> None:
    """Raw gitlink metadata must preserve status without SDK object traversal."""
    module_path = git_repository / "module"
    with Repo.init(module_path) as module:
        with module.config_writer() as config:
            config.set_value("user", "name", "Synthetic Module Test")
            config.set_value("user", "email", "module@example.invalid")
        (module_path / "file.txt").write_text("first\n", encoding="utf-8")
        module.index.add(["file.txt"])
        first_module_sha = module.index.commit("first module commit").hexsha
        with Repo(git_repository) as repo:
            (git_repository / ".gitmodules").write_text(
                '[submodule "module"]\n\tpath = module\n'
                "\turl = https://example.invalid/module.git\n",
                encoding="utf-8",
            )
            repo.index.add([".gitmodules"])
            repo.git.update_index(
                "--add", "--cacheinfo", f"160000,{first_module_sha},module"
            )
            repo.index.commit("record gitlink")
        (module_path / "file.txt").write_text("second\n", encoding="utf-8")
        module.index.add(["file.txt"])
        second_module_sha = module.index.commit("second module commit").hexsha
        if staged:
            with Repo(git_repository) as repo:
                repo.git.update_index(
                    "--cacheinfo", f"160000,{second_module_sha},module"
                )
            (module_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    with Repo(git_repository) as repo:
        legacy_entries = adapter_module._diff_entries(
            repo.index.diff("HEAD" if staged else None),
            area="index" if staged else "worktree",
            reverse=staged,
        )
        expected_fingerprint = canonical_hash(
            {
                "probe_version": "agileforge.repository-probe.v1",
                "head_sha": repo.head.commit.hexsha,
                "branch_name": repo.active_branch.name,
                "detached_head": False,
                "dirty": True,
                "status_entries": [
                    entry.model_dump(mode="json") for entry in legacy_entries
                ],
                "remotes": (),
                "remote_omitted": False,
            }
        )
    assert legacy_entries == (
        RepositoryStatusEntry(
            area="index" if staged else "worktree", change="modified", path="module"
        ),
    )

    def forbid_submodules(_repo: Repo) -> None:
        message = "Status parsing opened SDK submodule objects."
        raise AssertionError(message)

    monkeypatch.setattr(Repo, "submodules", property(forbid_submodules))
    observed = GitPythonRepositoryProbe().inspect(git_repository)
    assert observed.status_entries == legacy_entries
    assert observed.status_fingerprint == expected_fingerprint
