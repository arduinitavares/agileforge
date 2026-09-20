"""Portable, synthetic-only backup and restore tests."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess  # nosec B404
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from git import Repo
from google.adk.sessions import DatabaseSessionService
from sqlmodel import create_engine

from cli.state_transfer import (
    StateLayout,
    TransferError,
    backup_state,
    discover_registered_repositories,
    restore_payload,
    restored_repositories,
    verify_backup,
    verify_current_business_schema,
    verify_current_trace_schema,
)
from models.db import ensure_business_db_ready
from utils.runtime_fence import FenceError, runtime_fence

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="portable state transfer requires POSIX maintenance fencing",
)

_EXPECTED_PAYLOAD_FSYNC_COUNT = 7


@pytest.mark.parametrize("component", ["artifacts", "repository"])
def test_backup_refuses_a_destination_inside_a_captured_tree_before_staging(
    layout: StateLayout,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    """A nested destination must not recursively copy its own staging tree."""
    from cli import state_transfer  # noqa: PLC0415

    repository = layout.root / "repository"
    _init_repository(repository)
    layout = replace(layout, repositories=(repository,))
    source = layout.artifacts if component == "artifacts" else repository
    destination = source / "backup"

    def forbidden_staging(**_kwargs: object) -> str:
        message = "unsafe backup began staging inside its source"
        raise AssertionError(message)

    monkeypatch.setattr(state_transfer.tempfile, "mkdtemp", forbidden_staging)
    with pytest.raises(TransferError, match="overlaps"):
        backup_state(layout, destination)
    assert not destination.exists()


def _write_database(database: Path, *, trace: bool = False) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if trace:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    attempt_fingerprint TEXT,
                    payload BLOB NOT NULL
                );
                INSERT INTO sessions VALUES ('linked', 'attempt-one', X'00FF10');
                INSERT INTO sessions VALUES ('unlinked', NULL, X'00FF10');
                """
            )
            return
        connection.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );
            CREATE TABLE spec_registry (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                body BLOB NOT NULL
            );
            CREATE TABLE workflow_events (
                id INTEGER PRIMARY KEY,
                attempt_fingerprint TEXT NOT NULL,
                value REAL,
                note TEXT
            );
            CREATE TABLE duplicate_values (value TEXT NOT NULL);
            INSERT INTO projects(name) VALUES ('duplicate'), ('duplicate');
            INSERT INTO spec_registry VALUES (1, 1, X'00FF10');
            INSERT INTO workflow_events
                VALUES (1, 'attempt-one', 1.25, NULL);
            INSERT INTO duplicate_values VALUES ('same'), ('same');
            DELETE FROM projects WHERE id = 2;
            INSERT INTO projects(name) VALUES ('sequence-preserved');
            """
        )


def _init_repository(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    with Repo.init(path) as repository:
        with repository.config_writer() as config:
            config.set_value("user", "name", "Transfer Test")
            config.set_value("user", "email", "transfer@example.test")
        repository.index.add(["tracked.txt"])
        repository.index.commit("initialize transfer fixture")


@pytest.fixture
def layout(tmp_path: Path) -> StateLayout:
    """Create complete synthetic durable state with no real profile access."""
    root = tmp_path / "state"
    root.mkdir()
    business = root / "business.sqlite3"
    trace = root / "trace.sqlite3"
    _write_database(business)
    _write_database(trace, trace=True)
    artifacts = root / "artifacts"
    artifacts.mkdir()
    (artifacts / "accepted.md").write_bytes(b"accepted\x00evidence\n")
    nested = artifacts / "nested"
    nested.mkdir()
    (nested / "receipt.json").write_bytes(b'{"accepted":true}\n')
    model_config = root / "models.yaml"
    model_config.write_bytes(b"model: synthetic-only\n")
    return StateLayout(
        root=root,
        business_database=business,
        trace_database=trace,
        artifacts=artifacts,
        model_config=model_config,
    )


def test_backup_restore_preserves_typed_rows_sequences_and_unlinked_trace(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Dropping rows, blobs, sequence state, or unlinked sessions changes evidence."""
    bundle = backup_state(layout, tmp_path / "backup")
    manifest = verify_backup(bundle)

    assert manifest.format == "agileforge.transfer.v1"
    assert manifest.observed_links == (
        {"attempt_fingerprint": "attempt-one", "session_id": "linked"},
    )
    restored = tmp_path / "restored"
    restored_manifest = restore_payload(bundle, restored)
    assert restored_manifest.to_dict() == manifest.to_dict()

    with sqlite3.connect(restored / "business.sqlite3") as connection:
        assert connection.execute(
            "SELECT id, name FROM projects ORDER BY id"
        ).fetchall() == [(1, "duplicate"), (3, "sequence-preserved")]
        assert connection.execute("SELECT seq FROM sqlite_sequence").fetchone() == (3,)
        assert connection.execute("SELECT body FROM spec_registry").fetchone() == (
            b"\x00\xff\x10",
        )
    with sqlite3.connect(restored / "trace.sqlite3") as connection:
        assert connection.execute(
            "SELECT id, attempt_fingerprint, payload FROM sessions ORDER BY id"
        ).fetchall() == [
            ("linked", "attempt-one", b"\x00\xff\x10"),
            ("unlinked", None, b"\x00\xff\x10"),
        ]
    assert (restored / "artifacts" / "accepted.md").read_bytes() == (
        b"accepted\x00evidence\n"
    )
    assert (restored / "model-config").read_bytes() == b"model: synthetic-only\n"


def test_absent_unused_trace_database_remains_absent(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Creating an empty trace store would invent durable state during transfer."""
    layout.trace_database.unlink()
    bundle = backup_state(layout, tmp_path / "backup")
    manifest = verify_backup(bundle)
    restored = tmp_path / "restored"

    restore_payload(bundle, restored)

    assert manifest.databases["trace"]["present"] is False
    assert not (restored / "trace.sqlite3").exists()


def test_runtime_provenance_preserves_durable_identity(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Omitting runtime provenance would regenerate durable state identity."""
    runtime_manifest = layout.root / "runtime.json"
    runtime_manifest.write_bytes(
        b'{"state_id":"synthetic-state","created_at":"2026-01-01T00:00:00Z"}\n'
    )
    with_provenance = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        provenance_files=(runtime_manifest,),
    )

    bundle = backup_state(with_provenance, tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_payload(bundle, restored)

    assert (restored / "provenance" / "runtime.json").read_bytes() == (
        runtime_manifest.read_bytes()
    )


def test_backup_uses_all_explicit_maintenance_roots(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Locking only the profile root would miss workspace-wide runtime holders."""
    first_root = tmp_path / "state-volume"
    second_root = tmp_path / "workspace"
    first_root.mkdir()
    second_root.mkdir()
    fenced = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        maintenance_roots=(first_root, second_root),
    )

    with runtime_fence(second_root), pytest.raises(FenceError, match="busy"):
        backup_state(fenced, tmp_path / "backup")

    assert not (tmp_path / "backup").exists()


def test_caller_held_maintenance_fence_avoids_nested_lock(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """A wrapper may load ownership only after taking the exact exclusive fence."""
    with runtime_fence(layout.root, exclusive=True):
        bundle = backup_state(
            layout,
            tmp_path / "backup",
            maintenance_fences_held=True,
        )

    assert verify_backup(bundle).databases["business"]["present"] is True


def test_registered_repositories_are_auto_discovered_and_cannot_be_omitted(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Default capture must include every active durable repository binding."""
    repository = tmp_path / "workspace" / "registered"
    _init_repository(repository)
    with sqlite3.connect(layout.business_database) as connection:
        connection.execute(
            "ALTER TABLE projects ADD COLUMN active_repository_binding_id INTEGER"
        )
        connection.execute(
            "CREATE TABLE repository_bindings ("
            "repository_binding_id INTEGER PRIMARY KEY, worktree_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO repository_bindings VALUES (1, ?)",
            (str(repository),),
        )
        connection.execute(
            "UPDATE projects SET active_repository_binding_id = 1 WHERE id = 1"
        )

    assert discover_registered_repositories(layout.business_database) == (
        repository.resolve(),
    )
    auto_layout = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        maintenance_roots=(tmp_path,),
    )
    bundle = backup_state(auto_layout, tmp_path / "auto-backup")
    manifest = verify_backup(bundle)
    assert [item["source_path"] for item in manifest.repositories] == [
        str(repository.resolve())
    ]

    omitted = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        repositories=(),
        maintenance_roots=(tmp_path,),
    )
    with pytest.raises(TransferError, match="omits an active"):
        backup_state(omitted, tmp_path / "omitted-backup")


def test_interrupted_publication_residue_never_verifies(
    layout: StateLayout,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash-visible staged copy must remain distinguishable from a publication."""
    original_rename = Path.rename

    def interrupt(staging: Path, target: Path) -> Path:
        if staging.name.startswith(".backup.staging-"):
            original_rename(staging, target)
            message = "synthetic publication interruption"
            raise OSError(message)
        return original_rename(staging, target)

    monkeypatch.setattr(Path, "rename", interrupt)
    with pytest.raises(OSError, match="publication interruption"):
        backup_state(layout, tmp_path / "backup")

    interrupted = tmp_path / "backup"
    assert interrupted.exists()
    with pytest.raises(TransferError, match="publication is incomplete"):
        verify_backup(interrupted)


def test_payload_files_are_fsynced_before_publication(
    layout: StateLayout,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing before copied files are durable could expose a torn bundle."""
    original_fsync = os.fsync
    original_rename = Path.rename
    regular_fsyncs = 0

    def observe_fsync(descriptor: int) -> None:
        nonlocal regular_fsyncs
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            regular_fsyncs += 1
        original_fsync(descriptor)

    def observe_publication(staging: Path, target: Path) -> Path:
        if staging.name.startswith(".backup.staging-"):
            assert regular_fsyncs >= _EXPECTED_PAYLOAD_FSYNC_COUNT
        return original_rename(staging, target)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(Path, "rename", observe_publication)

    backup_state(layout, tmp_path / "backup")


def test_corrupt_artifact_prevents_restore(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Skipping byte verification would publish altered accepted evidence."""
    bundle = backup_state(layout, tmp_path / "backup")
    (bundle / "artifacts" / "accepted.md").write_bytes(b"corrupt")

    with pytest.raises(TransferError, match="inventory"):
        restore_payload(bundle, tmp_path / "restored")

    assert not (tmp_path / "restored").exists()


def test_deleted_manifest_entry_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Trusting an incomplete manifest would silently omit durable payload."""
    bundle = backup_state(layout, tmp_path / "backup")
    manifest_path = bundle / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"] = [
        entry for entry in payload["files"] if entry["path"] != "artifacts/accepted.md"
    ]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TransferError, match="unlisted"):
        verify_backup(bundle)


def test_unlisted_payload_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Ignoring extra bundle files would permit unverified payload injection."""
    bundle = backup_state(layout, tmp_path / "backup")
    (bundle / "injected").write_bytes(b"not inventoried")

    with pytest.raises(TransferError, match="unlisted"):
        verify_backup(bundle)


def test_corrupt_database_is_rejected_before_destination_creation(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Hash-only omission or late verification could publish a broken database."""
    bundle = backup_state(layout, tmp_path / "backup")
    database = bundle / "business.sqlite3"
    content = bytearray(database.read_bytes())
    content[len(content) // 2] ^= 0xFF
    database.write_bytes(content)

    with pytest.raises(TransferError):
        restore_payload(bundle, tmp_path / "restored")

    assert not (tmp_path / "restored").exists()


def test_restore_rejects_existing_destination(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Allowing any existing destination risks overwriting reserved profile state."""
    bundle = backup_state(layout, tmp_path / "backup")
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(TransferError, match="already exists"):
        restore_payload(bundle, destination)


def test_layout_rejects_database_alias(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Aliased business and trace paths cannot represent two durable stores."""
    alias = layout.root / "trace-alias.sqlite3"
    os.link(layout.business_database, alias)
    aliased = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=alias,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
    )

    with pytest.raises(TransferError, match="alias"):
        backup_state(aliased, tmp_path / "backup")


def test_layout_rejects_symlinked_state_file(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Following a state symlink could export an unowned external database."""
    alias = layout.root / "trace-alias.sqlite3"
    alias.symlink_to(layout.trace_database)
    unsafe = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=alias,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
    )

    with pytest.raises(TransferError, match="symlink"):
        backup_state(unsafe, tmp_path / "backup")


def test_repository_inventory_preserves_modes_and_safe_symlinks(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Flattening repo metadata would lose dirty bytes and executable/symlink state."""
    repository = layout.root / "repositories" / "target"
    _init_repository(repository)
    script = repository / "tool.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o750)
    (repository / "dirty.txt").write_bytes(b"untracked synthetic bytes")
    (repository / "tool-link").symlink_to("tool.sh")
    (repository / ".env").write_bytes(b"SYNTHETIC_SECRET=excluded")
    (repository / ".npmrc").write_bytes(b"//registry/:_authToken=excluded")
    (repository / ".aws").mkdir()
    (repository / ".aws" / "credentials").write_bytes(b"[test]\nsecret=excluded")
    (repository / ".git" / "config.worktree").write_bytes(b"[credential]\n")
    with_repositories = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        repositories=(repository,),
    )

    bundle = backup_state(with_repositories, tmp_path / "backup")
    manifest = verify_backup(bundle)
    restored = tmp_path / "restored"
    restore_payload(bundle, restored)

    restored_repository = restored / "repositories" / "0000"
    assert (restored_repository / "dirty.txt").read_bytes() == (
        b"untracked synthetic bytes"
    )
    executable_mode = 0o750
    assert (
        stat.S_IMODE((restored_repository / "tool.sh").stat().st_mode)
        == executable_mode
    )
    assert (restored_repository / "tool-link").readlink() == Path("tool.sh")
    assert not (restored_repository / ".env").exists()
    assert manifest.repositories[0]["excluded"] == [
        ".aws/credentials",
        ".env",
        ".git/config",
        ".git/config.worktree",
        ".npmrc",
    ]
    mapped = restored_repositories(manifest, restored)
    assert mapped[0].source_path == str(repository.resolve())
    assert mapped[0].worktree == restored_repository.resolve()
    assert mapped[0].identity["head_sha"] == Repo(repository).head.commit.hexsha
    assert mapped[0].to_dict()["worktree"] == str(restored_repository.resolve())


def test_repository_symlink_escape_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Dereferencing or preserving an escaping link could expose host data."""
    repository = layout.root / "repository"
    _init_repository(repository)
    (repository / "escape").symlink_to("../../outside")
    unsafe = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        repositories=(repository,),
    )

    with pytest.raises(TransferError, match="escapes"):
        backup_state(unsafe, tmp_path / "backup")

    assert not (tmp_path / "backup").exists()


def test_repository_payload_requires_repository_manifest_entry(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Unbound repository bytes would evade explicit component accounting."""
    repository = layout.root / "repository"
    _init_repository(repository)
    (repository / "dirty.txt").write_bytes(b"synthetic")
    with_repository = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        repositories=(repository,),
    )
    bundle = backup_state(with_repository, tmp_path / "backup")
    manifest_path = bundle / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["repositories"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TransferError, match="repository inventory"):
        verify_backup(bundle)


def test_setuid_repository_file_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Restoring privileged mode bits from an archive would cross a trust boundary."""
    repository = layout.root / "repository"
    _init_repository(repository)
    privileged = repository / "tool"
    privileged.write_bytes(b"synthetic")
    privileged.chmod(0o4755)
    unsafe = StateLayout(
        root=layout.root,
        business_database=layout.business_database,
        trace_database=layout.trace_database,
        artifacts=layout.artifacts,
        model_config=layout.model_config,
        repositories=(repository,),
    )

    with pytest.raises(TransferError, match="privileged mode"):
        backup_state(unsafe, tmp_path / "backup")


def test_special_artifact_file_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Copying a FIFO/device/socket could block or cross the archive trust boundary."""
    fifo = layout.artifacts / "blocked.fifo"
    os.mkfifo(fifo)  # ty: ignore[possibly-missing-attribute]

    with pytest.raises(TransferError, match="special"):
        backup_state(layout, tmp_path / "backup")

    assert not (tmp_path / "backup").exists()


def test_unknown_manifest_format_is_rejected(
    layout: StateLayout,
    tmp_path: Path,
) -> None:
    """Best-effort parsing of a future format could misinterpret state."""
    bundle = backup_state(layout, tmp_path / "backup")
    manifest_path = bundle / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["format"] = "agileforge.transfer.v999"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TransferError, match="unsupported transfer format"):
        verify_backup(bundle)


def test_current_business_schema_is_accepted_read_only(tmp_path: Path) -> None:
    """Rejecting the exact reviewed schema would block a valid offline restore."""
    database = tmp_path / "business.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    try:
        ensure_business_db_ready(engine)
    finally:
        engine.dispose()

    before = database.read_bytes()
    verify_current_business_schema(database)

    assert database.read_bytes() == before


def test_schema_gate_works_without_ambient_database_environment(
    tmp_path: Path,
) -> None:
    """Depending on ambient DB configuration would block clean installed startup."""
    database = tmp_path / "business.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    try:
        ensure_business_db_ready(engine)
    finally:
        engine.dispose()
    environment = os.environ.copy()
    environment.pop("AGILEFORGE_DB_URL", None)
    environment.pop("AGILEFORGE_CONFIG_ROOT", None)
    environment.pop("AGILEFORGE_LAUNCHER_CHILD", None)
    script = (
        "import sys; "
        "from pathlib import Path; "
        "from cli.state_transfer import verify_current_business_schema; "
        "verify_current_business_schema(Path(sys.argv[1]))"
    )

    result = subprocess.run(  # noqa: S603 # nosec B603
        [sys.executable, "-c", script, str(database)],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_partial_business_schema_is_rejected_by_runtime_gate(
    layout: StateLayout,
) -> None:
    """Generic transfer validity must not imply runtime schema compatibility."""
    with pytest.raises(TransferError, match="unsupported current business schema"):
        verify_current_business_schema(layout.business_database)


def test_handmade_trace_schema_is_rejected_by_runtime_gate(
    layout: StateLayout,
) -> None:
    """A generic SQLite trace table is not a supported current ADK store."""
    with pytest.raises(TransferError, match="unsupported current trace schema"):
        verify_current_trace_schema(layout.trace_database)


async def _create_adk_trace_database(database: Path) -> None:
    service = DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    try:
        await service.create_session(
            app_name="agileforge-test",
            user_id="running-loop-probe",
            session_id="running-loop-probe",
            state={},
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_trace_schema_gate_accepts_current_store_inside_running_loop(
    tmp_path: Path,
) -> None:
    """The dashboard lifespan validates a live trace store under its event loop."""
    from cli import state_transfer  # noqa: PLC0415

    database = tmp_path / "adk-trace.sqlite3"
    await _create_adk_trace_database(database)
    state_transfer._current_trace_schema_shape.cache_clear()

    verify_current_trace_schema(database)
