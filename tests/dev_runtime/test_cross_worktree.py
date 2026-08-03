"""Real-process proof that linked worktrees keep runtime profiles isolated."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2026-08-03T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-08-03T10:00:00+00:00",
}
_WORKTREE_COUNT = 2


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603 - fixed local test commands
        arguments,
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"command failed: {arguments!r}\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


def _git(
    checkout: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> str:
    result = _run(
        ("git", "-C", str(checkout), *arguments),
        cwd=checkout,
        env=env,
    )
    return result.stdout.strip()


def _profile_snapshot(profile_root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(profile_root)): path.read_bytes()
        for path in sorted(profile_root.rglob("*"))
        if path.is_file()
    }


def _launcher_environment(fake_bin: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "MODEL_CONFIG_PATH",
        "OPEN_ROUTER_API_KEY",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UV_OFFLINE": "1",
            "UV_NO_PROGRESS": "1",
        }
    )
    return environment


def _launcher(
    worktree: Path,
    arguments: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return _run((str(worktree / "agileforge-dev"), *arguments), cwd=worktree, env=env)


def _commit_launcher_fixtures(source_root: Path, clone: Path) -> tuple[str, str]:
    _run(
        ("git", "clone", "--no-hardlinks", str(source_root), str(clone)),
        cwd=clone.parent,
    )
    _git(clone, "config", "user.name", "Cross Worktree Tests")
    _git(clone, "config", "user.email", "cross-worktree@example.invalid")
    for relative in ("cli/dev_main.py", "cli/main.py"):
        shutil.copy2(source_root / relative, clone / relative)

    fixture = clone / "tests" / "dev_runtime" / "cross_worktree_fixture.txt"
    fixture.write_text("launcher fixture one\n", encoding="utf-8")
    _git(
        clone,
        "add",
        "cli/dev_main.py",
        "cli/main.py",
        str(fixture.relative_to(clone)),
    )
    _git(
        clone,
        "commit",
        "-m",
        "test: add first launcher fixture",
        env={**os.environ, **_COMMIT_ENVIRONMENT},
    )
    commit_one = _git(clone, "rev-parse", "HEAD")

    fixture.write_text("launcher fixture two\n", encoding="utf-8")
    _git(clone, "add", str(fixture.relative_to(clone)))
    _git(
        clone,
        "commit",
        "-m",
        "test: advance launcher fixture",
        env={
            **os.environ,
            **_COMMIT_ENVIRONMENT,
            "GIT_AUTHOR_DATE": "2026-08-03T10:01:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-03T10:01:00+00:00",
        },
    )
    return commit_one, _git(clone, "rev-parse", "HEAD")


def _fake_path_shim(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_agileforge = fake_bin / "agileforge"
    fake_agileforge.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'PATH shim must not run' >&2\nexit 97\n",
        encoding="utf-8",
    )
    fake_agileforge.chmod(0o700)
    return fake_bin


def test_same_profile_name_is_fully_isolated_across_linked_worktrees(
    tmp_path: Path,
) -> None:
    """Run each committed launcher against only its own profile and databases."""
    source_root = Path(__file__).parents[2]
    clone = tmp_path / "clone"
    worktree_one = tmp_path / "worktree-one"
    worktree_two = tmp_path / "worktree-two"
    fake_bin = _fake_path_shim(tmp_path)
    commit_one, commit_two = _commit_launcher_fixtures(source_root, clone)
    assert commit_one != commit_two

    added_worktrees: list[Path] = []
    try:
        for path, commit in (
            (worktree_one, commit_one),
            (worktree_two, commit_two),
        ):
            _git(clone, "worktree", "add", "--detach", str(path), commit)
            added_worktrees.append(path)

        environment = _launcher_environment(fake_bin)
        for path in added_worktrees:
            _launcher(
                path,
                ("init", "--profile", "local", "--json"),
                env=environment,
            )

        info_one = json.loads(
            _launcher(
                worktree_one,
                ("info", "--profile", "local", "--json"),
                env=environment,
            ).stdout
        )
        info_two = json.loads(
            _launcher(
                worktree_two,
                ("info", "--profile", "local", "--json"),
                env=environment,
            ).stdout
        )

        profile_one = cast("dict[str, object]", info_one["profile"])
        profile_two = cast("dict[str, object]", info_two["profile"])
        checkout_one = cast("dict[str, object]", profile_one["checkout"])
        checkout_two = cast("dict[str, object]", profile_two["checkout"])
        roots = {str(checkout_one["root"]), str(checkout_two["root"])}
        commits = {str(info_one["current_commit"]), str(info_two["current_commit"])}
        profile_roots = {
            str(Path(str(profile_one["business_database"])).parent),
            str(Path(str(profile_two["business_database"])).parent),
        }
        assert roots == {str(worktree_one.resolve()), str(worktree_two.resolve())}
        assert commits == {commit_one, commit_two}
        assert len(profile_roots) == _WORKTREE_COUNT
        assert profile_one["business_database"] != profile_two["business_database"]
        assert profile_one["trace_database"] != profile_two["trace_database"]

        root_one = Path(str(profile_one["business_database"])).parent
        root_two = Path(str(profile_two["business_database"])).parent
        snapshot_two = _profile_snapshot(root_two)
        cli_one = _launcher(
            worktree_one,
            ("cli", "--profile", "local", "--", "project", "list"),
            env=environment,
        )
        assert json.loads(cli_one.stdout)["ok"] is True
        assert f"Checkout: {worktree_one.resolve()}" in cli_one.stderr
        assert f"Commit: {commit_one}" in cli_one.stderr
        assert "PATH shim must not run" not in cli_one.stderr
        assert _profile_snapshot(root_two) == snapshot_two

        snapshot_one = _profile_snapshot(root_one)
        cli_two = _launcher(
            worktree_two,
            ("cli", "--profile", "local", "--", "project", "list"),
            env=environment,
        )
        assert json.loads(cli_two.stdout)["ok"] is True
        assert f"Checkout: {worktree_two.resolve()}" in cli_two.stderr
        assert f"Commit: {commit_two}" in cli_two.stderr
        assert "PATH shim must not run" not in cli_two.stderr
        assert _profile_snapshot(root_one) == snapshot_one
    finally:
        for path in reversed(added_worktrees):
            _run(
                ("git", "-C", str(clone), "worktree", "remove", "--force", str(path)),
                cwd=clone,
            )
        _run(("git", "-C", str(clone), "worktree", "prune"), cwd=clone)
