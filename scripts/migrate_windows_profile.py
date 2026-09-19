# scripts/migrate_windows_profile.py
"""One-time migration utility for Windows AgileForge development profile."""

# ruff: noqa: C901, EM101, EM102, PLR0912, PLR0915, TRY003, TRY004, TRY300, TRY301

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cli.state_transfer import (
    _FORMAT,
    _UNPUBLISHED_NAME,
    TransferError,
    TransferManifest,
    _backup_database,
    _capture_repositories,
    _copy_regular,
    _copy_tree,
    _database_manifest,
    _expand_repository_groups,
    _file_inventory,
    _safe_rmtree,
    _sha256_file,
    _verify_payload,
    _win_extended_str,
    _write_manifest,
    _write_unpublished_marker,
    discover_registered_repositories,
    verify_backup,
)
from utils.runtime_fence import FenceError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger: logging.Logger = logging.getLogger(name=__name__)

_LOCK_FILE_NAME = ".agileforge-runtime.lock"
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_ALWAYS = 4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_ERROR_LOCK_VIOLATION = 33
_ERROR_SHARING_VIOLATION = 32
_SQLITE_HEADER_MIN_SIZE: int = 28


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


@contextmanager
def win32_runtime_fence(profile_root: Path) -> Iterator[None]:
    """Hold an exclusive Win32 OS-level lock on .agileforge-runtime.lock."""
    if os.name != "nt":
        raise FenceError("win32_runtime_fence requires Windows")
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    lock_path = profile_root / _LOCK_FILE_NAME
    handle = kernel32.CreateFileW(
        str(lock_path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_ALWAYS,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == -1 or handle == ctypes.c_void_p(-1).value:
        error_code = ctypes.GetLastError()
        if error_code in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
            raise FenceError(f"runtime fence is busy for maintenance: {profile_root}")
        raise FenceError(
            f"runtime fence lock file could not be opened: WinError {error_code}"
        )
    overlapped = _OVERLAPPED()
    flags = _LOCKFILE_FAIL_IMMEDIATELY | _LOCKFILE_EXCLUSIVE_LOCK
    locked = kernel32.LockFileEx(
        handle,
        flags,
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        ctypes.byref(overlapped),
    )
    if not locked:
        error_code = ctypes.GetLastError()
        kernel32.CloseHandle(handle)
        if error_code in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
            raise FenceError(f"runtime fence is busy for maintenance: {profile_root}")
        raise FenceError(
            f"runtime fence acquisition failed with error {error_code}: {profile_root}"
        )
    try:
        yield
    finally:
        unlock_overlapped = _OVERLAPPED()
        kernel32.UnlockFileEx(
            handle,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(unlock_overlapped),
        )
        kernel32.CloseHandle(handle)


def win32_fsync_directory(path: Path) -> None:
    """Flush directory entries to disk using Win32 CreateFileW and FlushFileBuffers."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateFileW(
        _win_extended_str(path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == -1 or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError()
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError()
    finally:
        kernel32.CloseHandle(handle)


def verify_database_quiescence(database_path: Path) -> None:
    """Verify that no writers or active WAL/SHM sessions exist on the database."""
    if not database_path.is_file():
        return
    wal_path = database_path.with_name(database_path.name + "-wal")
    shm_path = database_path.with_name(database_path.name + "-shm")
    if wal_path.exists():
        raise TransferError(
            f"database {database_path.name} is active (WAL present: {wal_path.name})"
        )
    if shm_path.exists():
        raise TransferError(
            f"database {database_path.name} is active (SHM present: {shm_path.name})"
        )
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateFileW(
            str(database_path),
            _GENERIC_READ | _GENERIC_WRITE,
            0,  # exclusive access: fails with ERROR_SHARING_VIOLATION if open
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == -1 or handle == ctypes.c_void_p(-1).value:
            error_code = ctypes.GetLastError()
            if error_code == _ERROR_SHARING_VIOLATION:
                raise TransferError(
                    f"database {database_path.name} is locked by an active process"
                )
            raise TransferError(
                f"database {database_path.name} quiescence check error {error_code}"
            )
        kernel32.CloseHandle(handle)

    try:
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        cursor = connection.cursor()
        result = cursor.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise TransferError(
                f"database {database_path.name} failed quick_check: {result}"
            )
    except sqlite3.Error as error:
        raise TransferError(
            f"database {database_path.name} failed SQLite verification: {error}"
        ) from error
    finally:
        if "connection" in locals():
            connection.close()


@dataclass(frozen=True)
class _DatabaseSnapshot:
    sha256: str
    size: int
    header: bytes
    change_counter: int
    data_version: int


def _read_sqlite_header(path: Path) -> bytes:
    """Read the first 100 bytes containing SQLite database header."""
    with path.open("rb") as handle:
        return handle.read(100)


def _read_sqlite_data_version(path: Path) -> int:
    """Read PRAGMA data_version from SQLite database."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA data_version").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _take_database_snapshot(
    path: Path, monitor_conn: sqlite3.Connection | None = None
) -> _DatabaseSnapshot:
    """Capture snapshot fingerprint and change counter for an SQLite database."""
    header = _read_sqlite_header(path)
    change_counter = (
        int.from_bytes(header[24:28], byteorder="big")
        if len(header) >= _SQLITE_HEADER_MIN_SIZE
        else 0
    )
    if monitor_conn is not None:
        try:
            row = monitor_conn.execute("PRAGMA data_version").fetchone()
            data_version = int(row[0]) if row else 0
        except sqlite3.Error:
            data_version = 0
    else:
        data_version = _read_sqlite_data_version(path)

    return _DatabaseSnapshot(
        sha256=_sha256_file(path),
        size=path.stat().st_size,
        header=header,
        change_counter=change_counter,
        data_version=data_version,
    )


def _verify_source_database_unchanged(
    path: Path,
    expected: _DatabaseSnapshot,
    monitor_conn: sqlite3.Connection | None = None,
) -> None:
    """Verify that source database has not been mutated or acquired active WAL/SHM."""
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    if wal_path.exists():
        raise TransferError(
            f"database {path.name} active session appeared during capture: "
            f"{wal_path.name}"
        )
    if shm_path.exists():
        raise TransferError(
            f"database {path.name} active session appeared during capture: "
            f"{shm_path.name}"
        )
    if monitor_conn is not None:
        try:
            row = monitor_conn.execute("PRAGMA data_version").fetchone()
            current_dv = int(row[0]) if row else 0
            if current_dv != expected.data_version:
                msg = (
                    f"source database {path.name} was modified during capture "
                    f"(data_version changed from {expected.data_version} to "
                    f"{current_dv}): paired snapshot is inconsistent"
                )
                raise TransferError(msg)
        except sqlite3.Error as err:
            raise TransferError(
                f"failed to read data_version for {path.name}: {err}"
            ) from err

    current = _take_database_snapshot(path, monitor_conn=monitor_conn)
    if (
        current.sha256 != expected.sha256
        or current.change_counter != expected.change_counter
        or current.header != expected.header
        or current.size != expected.size
    ):
        raise TransferError(
            f"source database {path.name} was modified during capture: "
            "paired snapshot is inconsistent"
        )


class DatabaseExclusionFence:
    """Hold Win32 exclusion locks and persistent SQLite change monitors."""

    def __init__(self, database_paths: Sequence[Path]) -> None:
        """Initialize exclusion fence for the provided database paths."""
        self.database_paths: tuple[Path, ...] = tuple(database_paths)
        self._kernel32 = (
            ctypes.windll.kernel32 if os.name == "nt" else None  # type: ignore[attr-defined]
        )
        self._handles: list[int] = []
        self._monitors: dict[Path, sqlite3.Connection] = {}
        self.initial_snapshots: dict[Path, _DatabaseSnapshot] = {}

    def __enter__(self) -> dict[Path, _DatabaseSnapshot]:
        """Acquire Win32 locks, open monitors, and record initial snapshots."""
        try:
            for db_path in self.database_paths:
                wal_path = db_path.with_name(db_path.name + "-wal")
                shm_path = db_path.with_name(db_path.name + "-shm")
                if wal_path.exists():
                    msg = (
                        f"database {db_path.name} is active (WAL present: "
                        f"{wal_path.name})"
                    )
                    raise TransferError(msg)
                if shm_path.exists():
                    msg = (
                        f"database {db_path.name} is active (SHM present: "
                        f"{shm_path.name})"
                    )
                    raise TransferError(msg)
                if os.name == "nt" and self._kernel32 is not None:
                    handle = self._kernel32.CreateFileW(
                        str(db_path),
                        _GENERIC_READ,
                        _FILE_SHARE_READ,
                        None,
                        _OPEN_EXISTING,
                        _FILE_ATTRIBUTE_NORMAL,
                        None,
                    )
                    if handle == -1 or handle == ctypes.c_void_p(-1).value:
                        error_code = ctypes.GetLastError()
                        if error_code in (
                            _ERROR_LOCK_VIOLATION,
                            _ERROR_SHARING_VIOLATION,
                        ):
                            raise TransferError(
                                f"database {db_path.name} is locked by an "
                                "active process or writer"
                            )
                        raise TransferError(
                            f"database {db_path.name} lock failed with WinError "
                            f"{error_code}"
                        )
                    self._handles.append(handle)

                monitor = sqlite3.connect(
                    f"{db_path.resolve().as_uri()}?mode=ro", uri=True
                )
                self._monitors[db_path] = monitor
                self.initial_snapshots[db_path] = _take_database_snapshot(
                    db_path, monitor_conn=monitor
                )

            return self.initial_snapshots
        except Exception:
            self.close()
            raise

    def verify_database_unchanged(self, path: Path) -> None:
        """Verify single database against baseline snapshot using active monitor."""
        expected = self.initial_snapshots.get(path)
        if expected is None:
            raise TransferError(f"no baseline snapshot recorded for {path.name}")
        monitor = self._monitors.get(path)
        _verify_source_database_unchanged(path, expected, monitor_conn=monitor)

    def verify_all_unchanged(self) -> None:
        """Verify all guarded databases against baseline snapshots."""
        for db_path in self.database_paths:
            self.verify_database_unchanged(db_path)

    def close(self) -> None:
        """Release persistent monitor connections and Win32 handles."""
        for monitor in self._monitors.values():
            with suppress(Exception):
                monitor.close()
        self._monitors.clear()
        if os.name == "nt" and self._kernel32 is not None:
            for h in self._handles:
                with suppress(Exception):
                    self._kernel32.CloseHandle(h)
            self._handles.clear()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Verify state on clean exit and ensure all handles are closed."""
        try:
            if exc_type is None:
                self.verify_all_unchanged()
        finally:
            self.close()


def win32_database_exclusion_fence(
    database_paths: Sequence[Path],
) -> DatabaseExclusionFence:
    """Hold Win32 shared-read locks and persistent monitors throughout capture."""
    return DatabaseExclusionFence(database_paths)


def export_windows_profile(
    *,
    profile_root: Path,
    destination: Path,
    repositories: Sequence[Path] | None = None,
    model_config: Path | None = None,
) -> Path:
    """Capture and publication pipeline for Windows development profiles."""
    profile_root = profile_root.expanduser().absolute()
    if not profile_root.is_dir():
        raise TransferError(
            f"profile root must be an existing directory: {profile_root}"
        )

    target = destination.expanduser().absolute()
    if target.name in {"", ".", ".."}:
        raise TransferError("transfer destination must name a child directory")
    if target.exists():
        raise TransferError(f"transfer destination already exists: {target}")
    parent = target.parent
    if not parent.is_dir():
        raise TransferError(
            f"transfer destination parent directory must exist: {parent}"
        )

    with win32_runtime_fence(profile_root):
        business_db = profile_root / "business.sqlite3"
        if not business_db.is_file():
            raise TransferError(f"profile business database is missing: {business_db}")
        trace_db = profile_root / "adk-trace.sqlite3"
        artifacts = profile_root / "artifacts"
        if not artifacts.is_dir():
            raise TransferError(f"profile artifacts directory is missing: {artifacts}")
        profile_json = profile_root / "profile.json"
        if not profile_json.is_file():
            raise TransferError(f"profile metadata is missing: {profile_json}")

        try:
            profile_data = json.loads(profile_json.read_text(encoding="utf-8"))
            if not isinstance(profile_data, dict):
                raise ValueError("profile.json must be a JSON object")
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise TransferError(
                f"profile metadata is invalid: {profile_json}"
            ) from error

        if model_config is not None:
            resolved_model_config = model_config.expanduser().absolute()
        elif (
            "model_config_path" in profile_data
            and Path(profile_data["model_config_path"]).is_file()
        ):
            resolved_model_config = Path(profile_data["model_config_path"]).resolve()
        else:
            default_config = Path("config/models.yaml").expanduser().absolute()
            if not default_config.is_file():
                raise TransferError(
                    f"model configuration could not be resolved: {default_config}"
                )
            resolved_model_config = default_config

        if not resolved_model_config.is_file():
            raise TransferError(
                f"model configuration file is missing: {resolved_model_config}"
            )

        expected_model_hash = profile_data.get("model_config_sha256")
        if (
            isinstance(expected_model_hash, str)
            and _sha256_file(resolved_model_config) != expected_model_hash
        ):
            raise TransferError(
                "model configuration hash does not match profile metadata"
            )

        verify_database_quiescence(business_db)
        if trace_db.is_file():
            verify_database_quiescence(trace_db)

        initial_trace_exists = trace_db.is_file()
        source_databases: tuple[Path, ...] = (
            (business_db, trace_db) if initial_trace_exists else (business_db,)
        )

        fence = win32_database_exclusion_fence(source_databases)
        with fence as _initial_snapshots:
            if repositories is not None and len(repositories) > 0:
                repo_candidates = tuple(
                    Path(r).expanduser().absolute() for r in repositories
                )
            else:
                repo_candidates = discover_registered_repositories(business_db)
            expanded_repositories = _expand_repository_groups(repo_candidates)

            staging: Path | None = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent)
            )
            target_published = False
            try:
                _write_unpublished_marker(staging, target.name)
                _backup_database(business_db, staging / "business.sqlite3")
                if initial_trace_exists:
                    _backup_database(trace_db, staging / "trace.sqlite3")
                _copy_tree(artifacts, staging / "artifacts", allow_symlinks=False)
                _copy_regular(resolved_model_config, staging / "model-config")

                provenance_root = staging / "provenance"
                provenance_root.mkdir(mode=0o700)
                _copy_regular(profile_json, provenance_root / "profile.json")

                repository_manifest = _capture_repositories(
                    expanded_repositories, staging
                )
                databases, observed_links = _database_manifest(staging)
                manifest = TransferManifest(
                    format=_FORMAT,
                    created_at=datetime.now(tz=UTC).isoformat(),
                    files=_file_inventory(staging),
                    databases=databases,
                    repositories=repository_manifest,
                    model_config_sha256=_sha256_file(staging / "model-config"),
                    observed_links=observed_links,
                )
                _write_manifest(staging, manifest)
                _verify_payload(staging, manifest)

                fence.verify_all_unchanged()
                if (
                    not initial_trace_exists
                    and (profile_root / "adk-trace.sqlite3").is_file()
                ):
                    raise TransferError(
                        "database adk-trace.sqlite3 was created during capture: "
                        "paired snapshot is inconsistent"
                    )

                win32_fsync_directory(staging)
                staging.rename(target)
                staging = None
                win32_fsync_directory(parent)
                marker = target / _UNPUBLISHED_NAME
                if marker.read_text(encoding="utf-8") != target.name:
                    raise TransferError(
                        "transfer publication marker does not match target"
                    )
                marker.unlink()
                win32_fsync_directory(target)
                win32_fsync_directory(parent)
                verify_backup(target)
                target_published = True
            finally:
                if staging is not None:
                    _safe_rmtree(staging)
                if not target_published:
                    _safe_rmtree(target)

    return target


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser for Windows profile migration."""
    parser = argparse.ArgumentParser(
        description="One-time Windows profile migration utility"
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        required=True,
        help="Path to Windows dev profile root",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Path for migration backup bundle",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        action="append",
        help="Repository path(s) to include",
    )
    parser.add_argument(
        "--model-config",
        type=Path,
        help="Optional path to models.yaml",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON result",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint for Windows profile migration export."""
    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(args.json)
    try:
        target = export_windows_profile(
            profile_root=args.profile_root,
            destination=args.destination,
            repositories=args.repository,
            model_config=args.model_config,
        )
    except (FenceError, TransferError, OSError, ValueError) as error:
        if json_output:
            sys.stdout.write(json.dumps({"ok": False, "error": str(error)}, indent=2))
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"Error: {error}\n")
        return 2
    else:
        if json_output:
            sys.stdout.write(json.dumps({"ok": True, "backup": str(target)}, indent=2))
            sys.stdout.write("\n")
        else:
            sys.stdout.write(f"Verified migration bundle created at: {target}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
