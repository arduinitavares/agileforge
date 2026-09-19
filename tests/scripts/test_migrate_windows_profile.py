# tests/scripts/test_migrate_windows_profile.py
"""Test Windows development profile migration utility."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
from typing import TYPE_CHECKING

import pytest
from git import Repo

from cli.state_transfer import TransferError, verify_backup
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
