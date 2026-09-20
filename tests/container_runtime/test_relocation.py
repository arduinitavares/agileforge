# tests/container_runtime/test_relocation.py
"""Relocate synthetic durable state and Git worktrees without losing history."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import stat
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from git import Repo
from google.adk.sessions import DatabaseSessionService
from sqlmodel import Session, col, create_engine, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from cli.repository_transfer import (
    _RELOCATION_FORMAT,
    _RELOCATION_NAME,
    RepositoryTransferError,
    _read_relocation_record,
    _write_atomic,
    finalize_restored_repositories,
    pending_relocations,
    validate_relocation_targets,
    write_relocation_record,
)
from cli.state_transfer import (
    StateLayout,
    TransferManifest,
    backup_state,
    restore_payload,
    verify_backup,
    verify_current_trace_schema,
)
from models.core import Project, ProjectTeam, Sprint, Team
from models.db import ensure_business_db_ready
from models.enums import SprintStatus
from models.repository import RepositoryBinding
from models.workflow import WorkflowNodeAttempt, WorkflowTransitionReceipt
from repositories.workflow import WorkflowFactRepository
from tests.workflow.test_product_discovery_transitions import (
    NOW,
    _accept_request,
    _domain,
    _payload,
    _ready_project,
    _structure,
)
from tests.workflow.test_specification_rebinding import _attach
from utils.runtime_config import ADK_EXECUTION_TRACE_IDENTITY
from workflow.definitions.product_discovery import accepted_current_spec

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult


pytestmark = pytest.mark.skipif(
    __import__("os").name != "posix",
    reason="Git relocation and runtime fences are POSIX-only before cutover.",
)

_IMMUTABLE_SPEC_TABLES = {
    "spec_registry",
    "specification_candidates",
    "specification_decisions",
    "specification_sources",
}
_ATTACH_CHANGED_TABLES = {
    "projects",
    "repository_bindings",
    "workflow_transition_receipts",
}


def _layout(
    root: Path,
    *,
    repositories: tuple[Path, ...] | None,
    maintenance_roots: tuple[Path, ...],
) -> StateLayout:
    return StateLayout(
        root=root,
        business_database=root / "business.sqlite3",
        trace_database=root / "trace.sqlite3",
        artifacts=root / "artifacts",
        model_config=root / "model-config",
        repositories=repositories,
        maintenance_roots=maintenance_roots,
    )


def _seed_business_state(engine: Engine, tmp_path: Path) -> tuple[int, Path, str]:
    project_id, _, _, _, _, repository, _ = _ready_project(
        engine,
        tmp_path,
        name="relocation-project",
    )
    domain = _domain(engine)
    structured = _structure(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="relocation-structure",
    )
    assert structured.ok
    domain = _domain(engine, at=NOW + timedelta(seconds=1))
    accepted = domain.transition(
        _accept_request(domain, project_id=project_id, key="relocation-accept")
    )
    assert accepted.ok

    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt).where(
                col(WorkflowNodeAttempt.node_id) == "specification.structure"
            )
        ).one()
        team = Team(name="Relocation team")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        session.add(ProjectTeam(project_id=project_id, team_id=team.team_id))
        session.add_all(
            [
                Sprint(
                    project_id=project_id,
                    team_id=team.team_id,
                    goal="Completed durable history",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 14),
                    status=SprintStatus.COMPLETED,
                    started_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    completed_at=datetime(2026, 8, 14, 17, tzinfo=UTC),
                    close_snapshot_json=json.dumps(
                        {"accepted": True, "stories": ["STORY-1"]},
                        sort_keys=True,
                    ),
                ),
                Sprint(
                    project_id=project_id,
                    team_id=team.team_id,
                    goal="Active durable history",
                    start_date=date(2026, 8, 15),
                    end_date=date(2026, 8, 28),
                    status=SprintStatus.ACTIVE,
                    started_at=datetime(2026, 8, 15, 9, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
        return project_id, repository, attempt.attempt_fingerprint


async def _persist_trace(trace_database: Path, attempt_fingerprint: str) -> None:
    service = DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{trace_database.as_posix()}"
    )
    try:
        session = await service.create_session(
            app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
            user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
            session_id="relocation-trace",
            state={"attempt_fingerprint": attempt_fingerprint},
        )
        assert session.id == "relocation-trace"
    finally:
        await service.close()


def _dirty_linked_worktrees(main: Path, linked: Path) -> None:
    with Repo(main) as repository:
        (main / "tracked.txt").write_text("committed\n", encoding="utf-8")
        repository.index.add(["tracked.txt"])
        repository.index.commit("add relocation fixture")
        repository.create_remote("origin", "https://example.test/agileforge.git")
        repository.git.worktree("add", "-b", "relocation-linked", str(linked))
    (main / "tracked.txt").write_text("dirty main\n", encoding="utf-8")
    (main / "main-untracked.txt").write_text("main untracked\n", encoding="utf-8")
    (linked / "tracked.txt").write_text("dirty linked\n", encoding="utf-8")
    executable = linked / "linked-untracked.sh"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)


def _assert_same_git_identity(
    before: RepositoryProbeResult,
    after: RepositoryProbeResult,
) -> None:
    assert after.head_sha == before.head_sha
    assert after.branch_name == before.branch_name
    assert after.detached_head == before.detached_head
    assert after.dirty == before.dirty
    assert after.status_entries == before.status_entries
    assert after.status_fingerprint == before.status_fingerprint
    assert after.remotes == before.remotes


def _business_tables(manifest: TransferManifest) -> dict[str, object]:
    database = manifest.databases["business"]
    tables = database["tables"]
    assert isinstance(tables, dict)
    return cast("dict[str, object]", tables)


def _assert_accepted_specification(engine: Engine, project_id: int) -> None:
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert accepted_current_spec(snapshot) is not None


def test_relocation_preserves_state_then_guarded_attach_has_exact_delta(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Repair moved Git metadata before one guarded, append-only reattachment."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "artifacts").mkdir()
    (state_root / "artifacts" / "accepted.bin").write_bytes(b"accepted\x00artifact")
    (state_root / "model-config").write_text(
        '{"model": "local-test"}\n', encoding="utf-8"
    )
    source_workspace = tmp_path / "source-workspace"
    source_workspace.mkdir()

    business_database = state_root / "business.sqlite3"
    source_engine = create_engine(
        f"sqlite:///{business_database}",
        connect_args={"check_same_thread": False},
    )
    ensure_business_db_ready(source_engine)
    project_id, main, attempt_fingerprint = _seed_business_state(
        source_engine,
        source_workspace,
    )
    _assert_accepted_specification(source_engine, project_id)
    source_engine.dispose()
    asyncio.run(_persist_trace(state_root / "trace.sqlite3", attempt_fingerprint))
    verify_current_trace_schema(state_root / "trace.sqlite3")

    linked = source_workspace / "linked"
    _dirty_linked_worktrees(main, linked)
    probe = GitPythonRepositoryProbe()
    binding_engine = create_engine(
        f"sqlite:///{business_database}",
        connect_args={"check_same_thread": False},
    )
    _attach(
        binding_engine,
        _domain(binding_engine, at=NOW + timedelta(seconds=2)),
        project_id,
        linked,
        probe,
    )
    binding_engine.dispose()
    source_identities = (probe.inspect(main), probe.inspect(linked))
    accepted_source_bytes = (main / "SPECIFICATION.md").read_bytes()
    linked_mode = stat.S_IMODE((linked / "linked-untracked.sh").stat().st_mode)

    source_layout = _layout(
        state_root,
        repositories=None,
        maintenance_roots=(state_root, source_workspace),
    )
    original_manifest = backup_state(source_layout, tmp_path / "bundle")
    original = restore_payload(original_manifest, tmp_path / "relocated-state")
    assert original.observed_links == (
        {
            "attempt_fingerprint": attempt_fingerprint,
            "session_id": "relocation-trace",
        },
    )
    shutil.rmtree(source_workspace)

    relocated_root = tmp_path / "relocated-state"
    relocated_main = relocated_root / "repositories" / "0000"
    relocated_linked = relocated_root / "repositories" / "0001"
    finalize_restored_repositories(
        original,
        relocated_root,
    )
    relocation_record = write_relocation_record(
        original,
        relocated_root,
        relocated_root,
    )
    assert relocation_record.name == "repository-relocations.json"
    assert [
        item.project_id
        for item in pending_relocations(
            relocated_root / "business.sqlite3", relocated_root
        )
    ] == [project_id]

    relocated_identities = (
        probe.inspect(relocated_main),
        probe.inspect(relocated_linked),
    )
    for source, relocated in zip(source_identities, relocated_identities, strict=True):
        _assert_same_git_identity(source, relocated)
    assert (relocated_main / "SPECIFICATION.md").read_bytes() == accepted_source_bytes
    assert (
        stat.S_IMODE((relocated_linked / "linked-untracked.sh").stat().st_mode)
        == linked_mode
    )

    relocated_layout = _layout(
        relocated_root,
        repositories=(),
        maintenance_roots=(relocated_root,),
    )
    relocated_layout = StateLayout(
        root=relocated_layout.root,
        business_database=relocated_layout.business_database,
        trace_database=relocated_layout.trace_database,
        artifacts=relocated_layout.artifacts,
        model_config=relocated_layout.model_config,
        repositories=None,
        include_registered_repositories=False,
        maintenance_roots=relocated_layout.maintenance_roots,
    )
    before_attach = verify_backup(
        backup_state(relocated_layout, tmp_path / "before-attach")
    )
    assert before_attach.databases == original.databases
    assert before_attach.observed_links == original.observed_links
    assert (relocated_root / "artifacts" / "accepted.bin").read_bytes() == (
        b"accepted\x00artifact"
    )

    relocated_engine = create_engine(
        f"sqlite:///{relocated_root / 'business.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    _assert_accepted_specification(relocated_engine, project_id)
    with Session(relocated_engine) as session:
        prior_project = session.get(Project, project_id)
        assert prior_project is not None
        prior_binding_id = prior_project.active_repository_binding_id
        prior_binding_count = len(session.exec(select(RepositoryBinding)).all())
        prior_receipt_count = len(session.exec(select(WorkflowTransitionReceipt)).all())

    _attach(
        relocated_engine,
        _domain(relocated_engine, at=NOW + timedelta(seconds=3)),
        project_id,
        relocated_linked,
        probe,
    )
    _assert_accepted_specification(relocated_engine, project_id)
    relocated_engine.dispose()
    assert (
        pending_relocations(relocated_root / "business.sqlite3", relocated_root) == ()
    )

    after_attach = verify_backup(
        backup_state(relocated_layout, tmp_path / "after-attach")
    )
    before_tables = _business_tables(before_attach)
    after_tables = _business_tables(after_attach)
    changed_tables = {
        name for name in before_tables if before_tables[name] != after_tables[name]
    }
    assert changed_tables == _ATTACH_CHANGED_TABLES
    for table_name in _IMMUTABLE_SPEC_TABLES:
        assert after_tables[table_name] == before_tables[table_name]
    assert after_attach.databases["trace"] == before_attach.databases["trace"]
    assert after_attach.observed_links == before_attach.observed_links

    verification_engine = create_engine(
        f"sqlite:///{relocated_root / 'business.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    with Session(verification_engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.active_repository_binding_id != prior_binding_id
        active = session.get(RepositoryBinding, project.active_repository_binding_id)
        assert active is not None
        assert active.supersedes_repository_binding_id == prior_binding_id
        assert active.worktree_path == str(relocated_linked.resolve())
        assert len(session.exec(select(RepositoryBinding)).all()) == (
            prior_binding_count + 1
        )
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert len(receipts) == prior_receipt_count + 1
        assert receipts[-1].request_kind == "record_repository_binding"
    verification_engine.dispose()


def test_relocation_record_accepts_windows_source_path(tmp_path: Path) -> None:
    """Relocation records accept Windows absolute source paths without failing."""
    doc = {
        "format": _RELOCATION_FORMAT,
        "repositories": [
            {
                "project_id": 1,
                "source_path": r"C:\Users\atavares\Projects\backend",
                "restored_path": str((tmp_path / "restored").resolve()),
            }
        ],
    }
    content = json.dumps(doc).encode("utf-8") + b"\n"
    target = tmp_path / _RELOCATION_NAME
    _write_atomic(target, content)

    relocations = _read_relocation_record(tmp_path)
    assert len(relocations) == 1
    assert relocations[0].project_id == 1
    assert relocations[0].source_path == r"C:\Users\atavares\Projects\backend"
    assert relocations[0].restored_path == str((tmp_path / "restored").resolve())


def test_write_relocation_record_empty_manifest_repositories(tmp_path: Path) -> None:
    """Empty manifest repositories leaves relocations empty without raising error."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    profile_root = tmp_path / "profile"
    profile_root.mkdir()

    conn = sqlite3.connect(payload_root / "business.sqlite3")
    conn.execute(
        "CREATE TABLE projects ("
        "project_id INTEGER PRIMARY KEY, "
        "active_repository_binding_id INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE repository_bindings ("
        "repository_binding_id INTEGER PRIMARY KEY, "
        "worktree_path TEXT"
        ")"
    )
    conn.execute("INSERT INTO projects VALUES (1, 10)")
    conn.execute("INSERT INTO repository_bindings VALUES (10, '/path/to/source')")
    conn.commit()
    conn.close()

    manifest = TransferManifest(
        format="agileforge.transfer-manifest.v1",
        created_at=datetime.now(tz=UTC).isoformat(),
        files=(),
        databases={},
        repositories=(),
        model_config_sha256="",
        observed_links=(),
    )

    record_path = write_relocation_record(manifest, payload_root, profile_root)
    assert record_path.is_file()
    relocations = _read_relocation_record(profile_root)
    assert relocations == ()


def test_write_relocation_record_unmapped_with_repositories(
    tmp_path: Path,
) -> None:
    """Non-empty manifest repositories raises error when active binding is unmapped."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    profile_root = tmp_path / "profile"
    profile_root.mkdir()

    conn = sqlite3.connect(payload_root / "business.sqlite3")
    conn.execute(
        "CREATE TABLE projects ("
        "project_id INTEGER PRIMARY KEY, "
        "active_repository_binding_id INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE repository_bindings ("
        "repository_binding_id INTEGER PRIMARY KEY, "
        "worktree_path TEXT"
        ")"
    )
    conn.execute("INSERT INTO projects VALUES (1, 10)")
    conn.execute("INSERT INTO repository_bindings VALUES (10, '/path/to/unmapped')")
    conn.commit()
    conn.close()

    repo_dir = payload_root / "repositories" / "0000"
    repo_dir.mkdir(parents=True)
    manifest = TransferManifest(
        format="agileforge.transfer-manifest.v1",
        created_at=datetime.now(tz=UTC).isoformat(),
        files=(),
        databases={},
        repositories=(
            {
                "index": 0,
                "source_path": "/path/to/other",
                "components": [{"kind": "worktree", "payload": "repositories/0000"}],
                "identity": {"common_git_dir": "/path/to/other/.git"},
            },
        ),
        model_config_sha256="",
        observed_links=(),
    )

    with pytest.raises(
        RepositoryTransferError,
        match="active repository has no restored payload mapping",
    ):
        write_relocation_record(manifest, payload_root, profile_root)


def _state_only_payload(
    tmp_path: Path,
    bindings: dict[int, str],
) -> tuple[Path, Path, TransferManifest]:
    """Write a repository-less payload whose database has the given bindings."""
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    connection = sqlite3.connect(payload_root / "business.sqlite3")
    connection.execute(
        "CREATE TABLE projects ("
        "project_id INTEGER PRIMARY KEY, "
        "active_repository_binding_id INTEGER"
        ")"
    )
    connection.execute(
        "CREATE TABLE repository_bindings ("
        "repository_binding_id INTEGER PRIMARY KEY, "
        "worktree_path TEXT"
        ")"
    )
    for offset, (project_id, worktree_path) in enumerate(sorted(bindings.items())):
        binding_id = 10 + offset
        connection.execute(
            "INSERT INTO projects VALUES (?, ?)", (project_id, binding_id)
        )
        connection.execute(
            "INSERT INTO repository_bindings VALUES (?, ?)",
            (binding_id, worktree_path),
        )
    connection.commit()
    connection.close()
    manifest = TransferManifest(
        format="agileforge.transfer-manifest.v1",
        created_at=datetime.now(tz=UTC).isoformat(),
        files=(),
        databases={},
        repositories=[],
        model_config_sha256="",
        observed_links=(),
    )
    return payload_root, profile_root, manifest


def test_relocation_targets_map_every_active_binding(tmp_path: Path) -> None:
    """Operator-supplied targets become guarded relocations for state-only payloads."""
    windows_source = r"C:\Users\atavares\.codex\worktrees\054b\backend"
    payload_root, profile_root, manifest = _state_only_payload(
        tmp_path, {1: windows_source}
    )
    targets = {1: "/workspace/repos/targets/backend"}

    validate_relocation_targets(payload_root / "business.sqlite3", manifest, targets)
    write_relocation_record(
        manifest, payload_root, profile_root, relocation_targets=targets
    )

    relocations = _read_relocation_record(profile_root)
    assert len(relocations) == 1
    assert relocations[0].project_id == 1
    assert relocations[0].source_path == windows_source
    assert relocations[0].restored_path == "/workspace/repos/targets/backend"
    pending = pending_relocations(payload_root / "business.sqlite3", profile_root)
    assert pending == relocations


def test_relocation_targets_require_every_active_binding(tmp_path: Path) -> None:
    """Strict targets refuse to leave any active binding unmapped."""
    payload_root, profile_root, manifest = _state_only_payload(
        tmp_path, {1: "/old/first", 2: "/old/second"}
    )
    targets = {1: "/workspace/repos/targets/first"}

    with pytest.raises(
        RepositoryTransferError, match="active repository has no relocation target"
    ):
        validate_relocation_targets(
            payload_root / "business.sqlite3", manifest, targets
        )
    with pytest.raises(
        RepositoryTransferError, match="active repository has no relocation target"
    ):
        write_relocation_record(
            manifest, payload_root, profile_root, relocation_targets=targets
        )
    assert not (profile_root / _RELOCATION_NAME).exists()


def test_relocation_targets_reject_unknown_project(tmp_path: Path) -> None:
    """A target for a project without an active binding is an operator error."""
    payload_root, _profile_root, manifest = _state_only_payload(
        tmp_path, {1: "/old/first"}
    )

    with pytest.raises(
        RepositoryTransferError,
        match="relocation target names no active repository",
    ):
        validate_relocation_targets(
            payload_root / "business.sqlite3",
            manifest,
            {1: "/workspace/repos/targets/first", 7: "/workspace/repos/targets/x"},
        )


def test_relocation_targets_reject_relative_path(tmp_path: Path) -> None:
    """Relocation targets must be absolute container paths."""
    payload_root, _profile_root, manifest = _state_only_payload(
        tmp_path, {1: "/old/first"}
    )

    with pytest.raises(
        RepositoryTransferError, match="relocation target path must be absolute"
    ):
        validate_relocation_targets(
            payload_root / "business.sqlite3", manifest, {1: "repos/targets/first"}
        )


def test_relocation_targets_reject_conflict_with_bundled_repository(
    tmp_path: Path,
) -> None:
    """A bundled repository keeps its restored mapping; a target may not override it."""
    payload_root, _profile_root, manifest = _state_only_payload(
        tmp_path, {1: "/old/first"}
    )
    bundled = TransferManifest(
        format=manifest.format,
        created_at=manifest.created_at,
        files=(),
        databases={},
        repositories=[
            {
                "index": 0,
                "source_path": "/old/first",
                "components": [{"kind": "worktree", "payload": "repositories/0000"}],
                "identity": {"common_git_dir": "/old/first/.git"},
            }
        ],
        model_config_sha256="",
        observed_links=(),
    )

    with pytest.raises(
        RepositoryTransferError,
        match="relocation target conflicts with restored payload mapping",
    ):
        validate_relocation_targets(
            payload_root / "business.sqlite3",
            bundled,
            {1: "/workspace/repos/targets/first"},
        )


def test_relocation_targets_none_keeps_lenient_state_only_record(
    tmp_path: Path,
) -> None:
    """Without targets a repository-less payload still writes an empty record."""
    payload_root, profile_root, manifest = _state_only_payload(
        tmp_path, {1: "/old/first"}
    )

    write_relocation_record(manifest, payload_root, profile_root)

    assert _read_relocation_record(profile_root) == ()
