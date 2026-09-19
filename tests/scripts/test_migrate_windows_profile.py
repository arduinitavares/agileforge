# tests/scripts/test_migrate_windows_profile.py
# ruff: noqa: EM101, PLR0915, TRY003
"""Test Windows development profile migration utility."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import stat
from typing import TYPE_CHECKING

import pytest
from git import Repo

import scripts.migrate_windows_profile as mwp
from cli.state_transfer import TransferError, _win_extended, verify_backup
from scripts.migrate_windows_profile import (
    _LOCK_FILE_NAME,
    export_windows_profile,
    verify_database_quiescence,
    win32_fsync_directory,
    win32_runtime_fence,
)
from utils.runtime_fence import FenceError

if TYPE_CHECKING:
    from pathlib import Path

_WIN_MAX_PATH = 260

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows migration tests require Windows native platform.",
)


def _init_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test (value) VALUES ('hello')")
    conn.commit()
    conn.close()


def test_win32_runtime_fence_exclusive(tmp_path: Path) -> None:
    """Exclusive lock prevents concurrent acquisition and raises FenceError."""
    with (
        win32_runtime_fence(tmp_path),
        pytest.raises(FenceError, match="busy for maintenance"),
        win32_runtime_fence(tmp_path),
    ):
        assert (tmp_path / _LOCK_FILE_NAME).is_file()

    # After exit, acquiring fence again succeeds
    with win32_runtime_fence(tmp_path):
        pass


def test_win32_fsync_directory(tmp_path: Path) -> None:
    """Directory fsync flushes without raising an error."""
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "file.txt").write_text("content", encoding="utf-8")
    win32_fsync_directory(sub)
    win32_fsync_directory(tmp_path)


def test_verify_database_quiescence_wal_shm(tmp_path: Path) -> None:
    """Active WAL or SHM files trigger TransferError."""
    db_path = tmp_path / "test.sqlite3"
    _init_sqlite_db(db_path)

    wal_path = tmp_path / "test.sqlite3-wal"
    wal_path.write_bytes(b"wal content")
    with pytest.raises(TransferError, match="WAL present"):
        verify_database_quiescence(db_path)
    wal_path.unlink()

    shm_path = tmp_path / "test.sqlite3-shm"
    shm_path.write_bytes(b"shm content")
    with pytest.raises(TransferError, match="SHM present"):
        verify_database_quiescence(db_path)
    shm_path.unlink()

    # Clean DB succeeds
    verify_database_quiescence(db_path)


def test_verify_database_quiescence_locked(tmp_path: Path) -> None:
    """Exclusive lock held by another handle triggers TransferError."""
    db_path = tmp_path / "test.sqlite3"
    _init_sqlite_db(db_path)

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateFileW(
        str(db_path),
        0xC0000000,
        0,  # no sharing
        None,
        3,  # OPEN_EXISTING
        0x80,
        None,
    )
    assert handle != -1
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(TransferError, match="locked by an active process"):
            verify_database_quiescence(db_path)
    finally:
        kernel32.CloseHandle(handle)


def test_export_windows_profile_synthetic(tmp_path: Path) -> None:
    """Full export of a synthetic profile with dirty and untracked worktree files."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "output.txt").write_text("artifact payload", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)
    trace_db = profile_root / "adk-trace.sqlite3"
    _init_sqlite_db(trace_db)

    # Models config
    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\nmodels:\n  test: gpt-4\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    # Synthetic git repository
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    tracked_file = repo_dir / "tracked.txt"
    tracked_file.write_text("initial version", encoding="utf-8")
    repo.index.add(["tracked.txt"])
    commit = repo.index.commit("Initial commit")

    # Make tracked file dirty + add untracked file + canary .env
    tracked_file.write_text("modified uncommitted version", encoding="utf-8")
    untracked_file = repo_dir / "untracked.txt"
    untracked_file.write_text("untracked work", encoding="utf-8")
    canary_env = repo_dir / ".env"
    canary_env.write_text("SECRET_KEY=supersecret", encoding="utf-8")

    # Profile metadata
    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synthetic-win",
        "created_at": "2026-09-19T10:00:00Z",
        "checkout": {
            "root": str(repo_dir),
            "branch": "master",
            "commit": commit.hexsha,
        },
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # Execute export
    dest = tmp_path / "backup_bundle"
    result = export_windows_profile(
        profile_root=profile_root,
        destination=dest,
        repositories=(repo_dir,),
        model_config=model_config,
    )

    assert result == dest
    assert dest.is_dir()
    assert (dest / "manifest.json").is_file()
    assert (dest / "business.sqlite3").is_file()
    assert (dest / "trace.sqlite3").is_file()
    assert (dest / "artifacts" / "output.txt").is_file()
    assert (dest / "model-config").is_file()
    assert (dest / "provenance" / "profile.json").is_file()

    # Verify backup bundle
    manifest = verify_backup(dest)
    assert manifest.format == "agileforge.transfer.v1"
    assert len(manifest.repositories) == 1

    # Verify captured repo contents: dirty & untracked preserved, .env excluded!
    captured_repo = dest / "repositories" / "0000"
    captured_tracked = (captured_repo / "tracked.txt").read_text(encoding="utf-8")
    assert captured_tracked == "modified uncommitted version"
    captured_untracked = (captured_repo / "untracked.txt").read_text(encoding="utf-8")
    assert captured_untracked == "untracked work"
    assert not (captured_repo / ".env").exists()


def test_paired_backup_consistency_locks_out_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporter holds locks throughout capture; writer is blocked on all databases."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)
    trace_db = profile_root / "adk-trace.sqlite3"
    _init_sqlite_db(trace_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-lockout",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    real_backup_db = mwp._backup_database
    writer_business_attempted = False
    writer_business_blocked = False
    writer_trace_attempted = False
    writer_trace_blocked = False

    def hooked_backup_database(source: Path, destination: Path) -> None:
        nonlocal writer_business_attempted, writer_business_blocked
        nonlocal writer_trace_attempted, writer_trace_blocked
        real_backup_db(source, destination)
        if source == business_db:
            writer_business_attempted = True
            # Attempt concurrent write to business_db
            try:
                conn = sqlite3.connect(business_db, timeout=0.1)
                conn.execute("INSERT INTO test (value) VALUES ('race_writer')")
                conn.commit()
                conn.close()
            except (sqlite3.OperationalError, PermissionError, OSError):
                writer_business_blocked = True

            writer_trace_attempted = True
            # Attempt concurrent write to trace_db while guarded but uncopied
            try:
                conn_t = sqlite3.connect(trace_db, timeout=0.1)
                conn_t.execute("INSERT INTO test (value) VALUES ('race_writer')")
                conn_t.commit()
                conn_t.close()
            except (sqlite3.OperationalError, PermissionError, OSError):
                writer_trace_blocked = True

    monkeypatch.setattr(mwp, "_backup_database", hooked_backup_database)

    dest = tmp_path / "backup_bundle"
    result = export_windows_profile(
        profile_root=profile_root,
        destination=dest,
        repositories=(),
        model_config=model_config,
    )

    assert result == dest
    assert writer_business_attempted is True
    assert writer_business_blocked is True
    assert writer_trace_attempted is True
    assert writer_trace_blocked is True
    assert dest.is_dir()
    verify_backup(dest)

    # Verify both databases in dest have the original value, not race_writer
    with sqlite3.connect(dest / "business.sqlite3") as conn:
        rows = conn.execute("SELECT value FROM test").fetchall()
        assert [r[0] for r in rows] == ["hello"]

    with sqlite3.connect(dest / "trace.sqlite3") as conn:
        rows = conn.execute("SELECT value FROM test").fetchall()
        assert [r[0] for r in rows] == ["hello"]


def test_paired_backup_consistency_refuses_publication_on_mutated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporter detects database mutation during capture and refuses publication."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)
    trace_db = profile_root / "adk-trace.sqlite3"
    _init_sqlite_db(trace_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-race-repro",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    # Simulate what happens if exclusive lock was bypassed/degraded:
    # A mock fence records baseline snapshot without acquiring Win32 exclusion handles
    class MockDegradedFence(mwp.DatabaseExclusionFence):
        def __enter__(self) -> dict[Path, mwp._DatabaseSnapshot]:
            for db_path in self.database_paths:
                monitor = sqlite3.connect(
                    f"{db_path.resolve().as_uri()}?mode=ro", uri=True
                )
                self._monitors[db_path] = monitor
                self.initial_snapshots[db_path] = mwp._take_database_snapshot(
                    db_path, monitor_conn=monitor
                )
            return self.initial_snapshots

    monkeypatch.setattr(mwp, "win32_database_exclusion_fence", MockDegradedFence)

    real_backup_db = mwp._backup_database

    def hooked_backup_database(source: Path, destination: Path) -> None:
        real_backup_db(source, destination)
        if source == business_db:
            # Reproduce Codex race: after business_db copied, update both databases
            conn_b = sqlite3.connect(business_db)
            conn_b.execute("INSERT INTO test (value) VALUES ('version_1')")
            conn_b.commit()
            conn_b.close()
            conn_t = sqlite3.connect(trace_db)
            conn_t.execute("INSERT INTO test (value) VALUES ('version_1')")
            conn_t.commit()
            conn_t.close()

    monkeypatch.setattr(mwp, "_backup_database", hooked_backup_database)

    dest = tmp_path / "backup_bundle"
    with pytest.raises(
        TransferError, match=r"paired snapshot is inconsistent|data_version changed"
    ):
        export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=(),
            model_config=model_config,
        )

    # Exporter must NOT publish the inconsistent bundle
    assert not dest.exists()


def test_paired_backup_consistency_detects_data_version_change_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporter detects in-place data_version change even without file size change."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)
    trace_db = profile_root / "adk-trace.sqlite3"
    _init_sqlite_db(trace_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-data-version-inplace",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    class MockDegradedFence(mwp.DatabaseExclusionFence):
        def __enter__(self) -> dict[Path, mwp._DatabaseSnapshot]:
            for db_path in self.database_paths:
                monitor = sqlite3.connect(
                    f"{db_path.resolve().as_uri()}?mode=ro", uri=True
                )
                self._monitors[db_path] = monitor
                self.initial_snapshots[db_path] = mwp._take_database_snapshot(
                    db_path, monitor_conn=monitor
                )
            return self.initial_snapshots

    monkeypatch.setattr(mwp, "win32_database_exclusion_fence", MockDegradedFence)

    real_backup_db = mwp._backup_database

    def hooked_backup_database(source: Path, destination: Path) -> None:
        real_backup_db(source, destination)
        if source == business_db:
            # Perform in-place update (same byte length: 'hello' -> 'world')
            conn = sqlite3.connect(business_db)
            conn.execute("UPDATE test SET value = 'world' WHERE value = 'hello'")
            conn.commit()
            conn.close()

    monkeypatch.setattr(mwp, "_backup_database", hooked_backup_database)

    dest = tmp_path / "backup_bundle"
    with pytest.raises(
        TransferError, match=r"data_version changed|paired snapshot is inconsistent"
    ):
        export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=(),
            model_config=model_config,
        )

    assert not dest.exists()


def test_paired_backup_consistency_refuses_when_trace_db_created_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporter detects trace DB created during capture and refuses publication."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)
    # trace_db initially does NOT exist!

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-trace-created",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    real_backup_db = mwp._backup_database

    def hooked_backup_database(source: Path, destination: Path) -> None:
        real_backup_db(source, destination)
        if source == business_db:
            # Create trace_db during capture
            _init_sqlite_db(profile_root / "adk-trace.sqlite3")

    monkeypatch.setattr(mwp, "_backup_database", hooked_backup_database)

    dest = tmp_path / "backup_bundle"
    with pytest.raises(
        TransferError, match=r"adk-trace\.sqlite3 was created during capture"
    ):
        export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=(),
            model_config=model_config,
        )

    assert not dest.exists()


def test_export_windows_profile_cleans_up_destination_on_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exporter removes destination directory if post-publication verification fails."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-cleanup-failure",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    def mock_verify_backup(_bundle: Path) -> None:
        raise TransferError("simulated verification error")

    monkeypatch.setattr(mwp, "verify_backup", mock_verify_backup)

    dest = tmp_path / "backup_bundle"
    with pytest.raises(TransferError, match=r"simulated verification error"):
        export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=(),
            model_config=model_config,
        )

    assert not dest.exists()


def test_export_windows_profile_handles_long_paths_and_readonly_files(
    tmp_path: Path,
) -> None:
    """Exporter handles paths > 260 characters and read-only Git repository files."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = Repo.init(repo_dir)
    (repo_dir / "README.md").write_text("initial", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial")

    deep = repo_dir
    for i in range(12):
        deep = deep / f"long_path_segment_{i}"
    deep = _win_extended(deep)
    deep.mkdir(parents=True)
    deep_file = deep / "deep_file.txt"
    deep_file.write_text("deep content", encoding="utf-8")
    deep_file.chmod(stat.S_IREAD)
    assert len(str(deep_file)) > _WIN_MAX_PATH

    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-longpath",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    dest = tmp_path / "backup_bundle"
    try:
        exported = export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=[repo_dir],
            model_config=model_config,
        )
        assert exported.exists()
        manifest = verify_backup(exported)
        assert len(manifest.files) > 0
    finally:
        repo.close()


def test_export_windows_profile_state_only(tmp_path: Path) -> None:
    """State-only export produces a bundle with empty repositories list."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-state-only",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    dest = tmp_path / "backup_bundle"
    exported = export_windows_profile(
        profile_root=profile_root,
        destination=dest,
        model_config=model_config,
        state_only=True,
    )
    assert exported.exists()
    manifest = verify_backup(exported)
    assert manifest.repositories == ()


def test_export_windows_profile_state_only_rejects_repositories(tmp_path: Path) -> None:
    """State-only export rejects explicit repositories."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-state-only",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    dest = tmp_path / "backup_bundle"
    with pytest.raises(
        TransferError, match="state-only backup must not include repositories"
    ):
        export_windows_profile(
            profile_root=profile_root,
            destination=dest,
            repositories=[tmp_path / "some_repo"],
            model_config=model_config,
            state_only=True,
        )


def test_migrate_windows_profile_cli_state_only(tmp_path: Path) -> None:
    """CLI --state-only flag exports bundle with empty repositories."""
    profile_root = tmp_path / "dev_profile"
    profile_root.mkdir()
    artifacts = profile_root / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("ok", encoding="utf-8")

    business_db = profile_root / "business.sqlite3"
    _init_sqlite_db(business_db)

    model_config = tmp_path / "models.yaml"
    model_config.write_text("version: 1\n", encoding="utf-8")
    model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

    profile_json = profile_root / "profile.json"
    metadata = {
        "name": "synth-state-only",
        "model_config_path": str(model_config),
        "model_config_sha256": model_hash,
    }
    profile_json.write_text(json.dumps(metadata), encoding="utf-8")

    dest = tmp_path / "cli_bundle"
    code = mwp.main(
        [
            "--profile-root",
            str(profile_root),
            "--destination",
            str(dest),
            "--model-config",
            str(model_config),
            "--state-only",
            "--json",
        ]
    )
    assert code == 0
    assert dest.exists()
    manifest = verify_backup(dest)
    assert manifest.repositories == ()
