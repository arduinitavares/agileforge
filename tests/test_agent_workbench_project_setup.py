"""Tests for CLI project setup mutation runner."""

# ruff: noqa: ANN401, ARG005, D102, D103, D107, E501, PLC0415, PLR0911, PLR0913, TC002, TC003

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from db.migrations import ensure_schema_current
from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.mutation_ledger import (
    MUTATION_RESUME_CONFLICT,
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
    _row_payload,
)
from services.agent_workbench.project_setup import (
    ProjectCreateRequest,
    ProjectSetupMutationRunner,
    ProjectSetupRetryRequest,
)
from services.agent_workbench.project_setup_fingerprints import (
    setup_retry_context_fingerprint,
)


class FakeWorkflowPort:
    """In-memory workflow port for project setup tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.created_sessions: list[str] = []
        self.status_writes: list[tuple[str, dict[str, Any]]] = []
        self.fail_after_session_create = False
        self.fail_after_status_write = False

    def initialize_session(self, session_id: str | None = None) -> str:
        if session_id is None:
            session_id = f"session-{len(self.created_sessions) + 1}"
        self.created_sessions.append(session_id)
        self.sessions.setdefault(session_id, {"fsm_state": "SETUP_REQUIRED"})
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        current = self.sessions.setdefault(session_id, {"fsm_state": "SETUP_REQUIRED"})
        current.update(partial_update)
        self.status_writes.append((session_id, dict(partial_update)))

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        return dict(self.sessions.get(session_id, {}))

    def ensure_setup_state(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        lease_guard: Any,
        record_progress: Any,
    ) -> dict[str, Any]:
        session_id = str(project_id)
        current = self.get_session_status(session_id)
        if current == {}:
            if not lease_guard("workflow_session_created"):
                return {"ok": False, "error_code": "MUTATION_IN_PROGRESS"}
            self.initialize_session(session_id=session_id)
            if self.fail_after_session_create:
                self.fail_after_session_create = False
                return {"ok": False, "error_code": "WORKFLOW_SESSION_FAILED"}
            current = self.get_session_status(session_id)
        if not record_progress("workflow_session_created"):
            return {"ok": False, "error_code": "MUTATION_RECOVERY_REQUIRED"}

        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_pending_review",
            "setup_error": None,
            "setup_spec_file_path": str(resolved_spec_path),
        }
        merged = {**current, **required_state}
        if current != merged:
            if not lease_guard("workflow_session_status_written"):
                return {"ok": False, "error_code": "MUTATION_IN_PROGRESS"}
            self.update_session_status(session_id, required_state)
            if self.fail_after_status_write:
                self.fail_after_status_write = False
                return {"ok": False, "error_code": "WORKFLOW_SESSION_FAILED"}
        if not record_progress("workflow_session_status_written"):
            return {"ok": False, "error_code": "MUTATION_RECOVERY_REQUIRED"}
        return {"ok": True, "session_id": session_id, "state": self.get_session_status(session_id)}


def _structured_spec_payload(
    *,
    title: str = "App Spec",
    requirement_statement: str = "The system MUST record audit evidence.",
) -> dict[str, Any]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.app",
        "title": title,
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Create a project from a structured authority spec.",
        "problem_statement": "Operators need project setup to persist authority evidence.",
        "items": [
            {
                "id": "REQ.app.audit",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Audit evidence",
                "statement": requirement_statement,
                "verification": "system-test",
                "acceptance": ["Audit evidence is stored for each setup operation."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def _write_spec(
    tmp_path: Path,
    *,
    title: str = "App Spec",
    requirement_statement: str = "The system MUST record audit evidence.",
) -> Path:
    spec_file = tmp_path / "specs" / "spec.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(
        json.dumps(
            _structured_spec_payload(
                title=title,
                requirement_statement=requirement_statement,
            )
        ),
        encoding="utf-8",
    )
    return spec_file


def _install_fast_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.agent_workbench import project_setup

    def compile_fast(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: Any | None = None,
        record_progress: Any | None = None,
    ) -> dict[str, Any]:
        del force_recompile, tool_context
        if lease_guard is not None and not lease_guard("compiled_authority_persisted"):
            return {
                "success": False,
                "error_code": "MUTATION_IN_PROGRESS",
                "boundary": "compiled_authority_persisted",
            }
        with Session(engine) as session:
            authority = session.exec(
                select(CompiledSpecAuthority).where(
                    CompiledSpecAuthority.spec_version_id == spec_version_id
                )
            ).first()
            if authority is None:
                authority = CompiledSpecAuthority(
                    spec_version_id=spec_version_id,
                    compiler_version="test-compiler",
                    prompt_hash="sha256:test",
                    compiled_artifact_json='{"ok":true}',
                    scope_themes="[]",
                    invariants="[]",
                    eligible_feature_ids="[]",
                    rejected_features="[]",
                    spec_gaps="[]",
                )
                session.add(authority)
                session.commit()
                session.refresh(authority)
            authority_id = authority.authority_id
        if record_progress is not None:
            assert record_progress("compiled_authority_persisted")
            assert record_progress("product_authority_cache_persisted")
        return {
            "success": True,
            "authority_id": authority_id,
            "spec_version_id": spec_version_id,
            "compiler_version": "test-compiler",
            "prompt_hash": "sha256:test",
        }

    monkeypatch.setattr(
        project_setup,
        "compile_spec_authority_for_version_with_engine",
        compile_fast,
    )


def _install_failing_compiler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_code: str = "SPEC_COMPILE_FAILED",
    failure_artifact_id: str | None = None,
    blocking_gaps: list[str] | None = None,
) -> None:
    from services.agent_workbench import project_setup

    def compile_failing(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: Any | None = None,
        record_progress: Any | None = None,
    ) -> dict[str, Any]:
        del engine, spec_version_id, force_recompile, tool_context
        del lease_guard, record_progress
        result = {
            "success": False,
            "error_code": error_code,
            "error": "Injected compile failure.",
            "reason": "SOURCE_MAP_INVARIANT_MISMATCH",
            "blocking_gaps": blocking_gaps or [],
        }
        if failure_artifact_id is not None:
            result.update(
                {
                    "failure_artifact_id": failure_artifact_id,
                    "failure_stage": "output_validation",
                    "failure_summary": (
                        "SPEC_COMPILATION_FAILED: SOURCE_MAP_INVARIANT_MISMATCH"
                    ),
                    "has_full_artifact": True,
                    "raw_output_preview": '{"result":',
                }
            )
        return result

    monkeypatch.setattr(
        project_setup,
        "compile_spec_authority_for_version_with_engine",
        compile_failing,
    )


def _error_code(result: dict[str, Any]) -> str:
    return result["errors"][0]["code"]


def _retry_fingerprint(
    *,
    project_id: int,
    spec_file: Path,
    workflow_state: dict[str, Any],
) -> str:
    return setup_retry_context_fingerprint(
        project_id=project_id,
        resolved_spec_path=spec_file.resolve(),
        workflow_state=workflow_state,
    )


def _create_recovery_row(
    *,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow: FakeWorkflowPort | None = None,
    idempotency_key: str = "create-project-001",
    name: str = "CLI Project",
) -> tuple[dict[str, Any], Path, FakeWorkflowPort]:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = workflow or FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)
    runner.fail_after_step_for_test = "product_created"

    with pytest.raises(RuntimeError, match="Injected project setup failure"):
        runner.create_project(
            ProjectCreateRequest(
                name=name,
                spec_file=str(spec_file),
                idempotency_key=idempotency_key,
                changed_by="agent",
            )
        )

    result = runner.create_project(
        ProjectCreateRequest(
            name=name,
            spec_file=str(spec_file),
            idempotency_key=idempotency_key,
            changed_by="agent",
        )
    )
    assert _error_code(result) == "MUTATION_RECOVERY_REQUIRED"
    return result, spec_file, fake_workflow


def _seed_expired_pending_create_after_spec_approval(
    *,
    engine: Engine,
    spec_file: Path,
    project_name: str = "Interrupted Project",
) -> tuple[int, int]:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    repo = MutationLedgerRepository(engine=engine)
    row = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-interrupted-001",
        request_hash="sha256:create-interrupted",
        project_id=None,
        correlation_id="corr-create",
        changed_by="agent",
        lease_owner="create-owner",
        now=now,
        lease_seconds=300,
    ).ledger
    assert row.mutation_event_id is not None
    with Session(engine) as session:
        product = Product(name=project_name)
        session.add(product)
        session.flush()
        project_id = product.product_id
        assert project_id is not None
        assert MutationLedgerRepository.set_project_id_in_session(
            session,
            mutation_event_id=row.mutation_event_id,
            lease_owner="create-owner",
            project_id=project_id,
            now=now,
        )
        product.spec_file_path = str(spec_file.resolve())
        product.spec_loaded_at = now
        session.add(product)
        spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:seeded-spec",
            content=spec_file.read_text(encoding="utf-8"),
            content_ref=str(spec_file.resolve()),
            status="approved",
            approved_at=now,
            approved_by="cli-project-create",
            approval_notes="Seeded setup state before pending authority.",
        )
        session.add(spec)
        for step in (
            "product_created",
            "product_spec_linked",
            "spec_registry_written",
            "spec_marked_approved",
        ):
            assert MutationLedgerRepository.mark_step_complete_in_session(
                session,
                mutation_event_id=row.mutation_event_id,
                lease_owner="create-owner",
                step=step,
                next_step=step,
                now=now,
            )
        ledger = session.get(CliMutationLedger, row.mutation_event_id)
        assert ledger is not None
        ledger.lease_expires_at = now
        session.add(ledger)
        session.commit()
    return project_id, row.mutation_event_id


def test_project_create_dry_run_resolves_spec_from_caller_cwd_without_writes(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    caller = tmp_path / "caller"
    caller.mkdir()
    spec_file = _write_spec(caller)

    monkeypatch.chdir(caller)
    runner = ProjectSetupMutationRunner(engine=engine)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file="specs/spec.json",
            dry_run=True,
            dry_run_id="dry-run-project-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    assert result["data"]["preview_available"] is True
    assert result["data"]["resolved_spec_path"] == str(spec_file.resolve())

    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []
        assert session.exec(select(CliMutationLedger)).all() == []


def test_project_create_dry_run_missing_spec_returns_structured_error_without_writes(
    engine: Engine,
    tmp_path: Path,
) -> None:
    ensure_schema_current(engine)
    missing_spec = tmp_path / "specs" / "missing.md"
    runner = ProjectSetupMutationRunner(engine=engine)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(missing_spec),
            dry_run=True,
            dry_run_id="dry-run-project-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is False
    assert _error_code(result) == "SPEC_FILE_NOT_FOUND"
    assert result["errors"][0]["details"] == {
        "spec_file": str(missing_spec.resolve())
    }

    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []
        assert session.exec(select(CliMutationLedger)).all() == []


def test_project_create_missing_spec_returns_structured_error_before_ledger(
    engine: Engine,
    tmp_path: Path,
) -> None:
    ensure_schema_current(engine)
    missing_spec = tmp_path / "specs" / "missing.md"
    runner = ProjectSetupMutationRunner(engine=engine)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(missing_spec),
            idempotency_key="create-project-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is False
    assert _error_code(result) == "SPEC_FILE_NOT_FOUND"
    assert result["errors"][0]["details"] == {
        "spec_file": str(missing_spec.resolve())
    }

    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []
        assert session.exec(select(CliMutationLedger)).all() == []


def test_project_create_request_validation_rules() -> None:
    with pytest.raises(ValidationError, match="idempotency_key is required"):
        ProjectCreateRequest(name="CLI Project", spec_file="specs/app.md")

    with pytest.raises(ValidationError, match="idempotency_key is not allowed with dry_run"):
        ProjectCreateRequest(
            name="CLI Project",
            spec_file="specs/app.md",
            dry_run=True,
            dry_run_id="preview-001",
            idempotency_key="create-project-001",
        )

    with pytest.raises(ValidationError, match="dry_run_id is required"):
        ProjectCreateRequest(
            name="CLI Project",
            spec_file="specs/app.md",
            dry_run=True,
        )

    with pytest.raises(ValidationError, match="idempotency_key must be ASCII"):
        ProjectCreateRequest(
            name="CLI Project",
            spec_file="specs/app.md",
            idempotency_key="create-é-001",
        )

    with pytest.raises(ValidationError, match="dry_run_id must match"):
        ProjectCreateRequest(
            name="CLI Project",
            spec_file="specs/app.md",
            dry_run=True,
            dry_run_id="bad key 001",
        )


def test_project_create_success_creates_authority_without_acceptance(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["name"] == "CLI Project"
    assert data["resolved_spec_path"] == str(spec_file.resolve())
    assert data["setup_status"] == "authority_pending_review"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert data["authority_id"] is None
    assert isinstance(data["pending_authority_id"], int)
    assert data["pending_authority_id"] == data["compiled_authority_id"]
    assert data["next_actions"] == [
        {
            "command": "agileforge authority status",
            "args": {"project_id": data["project_id"]},
            "reason": "Review pending compiled authority before acceptance.",
        }
    ]

    with Session(engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(SpecRegistry)).all()) == 1
        assert len(session.exec(select(CompiledSpecAuthority)).all()) == 1
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        ledger = session.get(CliMutationLedger, data["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.SUCCEEDED.value
        assert ledger.project_id == data["project_id"]

    assert fake_workflow.created_sessions == [str(data["project_id"])]
    assert fake_workflow.sessions[str(data["project_id"])]["setup_status"] == (
        "authority_pending_review"
    )


def test_project_create_compile_failure_records_failed_setup_not_recovery(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(
        monkeypatch,
        failure_artifact_id="spec-authority-failure-1",
        blocking_gaps=["source_map excerpt does not mention required field 'selected squad'"],
    )
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    failed = runner.create_project(
        ProjectCreateRequest(
            name="Compiler Failure Project",
            spec_file=str(spec_file),
            idempotency_key="create-compile-fails-001",
            changed_by="agent",
        )
    )

    assert failed["ok"] is False
    assert _error_code(failed) == "SPEC_COMPILE_FAILED"
    data = failed["data"]
    project_id = data["project_id"]
    assert data["setup_status"] == "failed"
    assert data["setup_failure_stage"] == "authority_compile"
    assert data["setup_failure_summary"] == (
        "SPEC_COMPILATION_FAILED: SOURCE_MAP_INVARIANT_MISMATCH"
    )
    assert data["setup_failure_artifact_id"] == "spec-authority-failure-1"
    assert data["setup_failure_first_error"] == (
        "source_map excerpt does not mention required field 'selected squad'"
    )
    assert data["has_full_artifact"] is True
    assert data["next_actions"][0]["command"] == "agileforge project setup retry"
    assert "recovery_mutation_event_id" not in data["next_actions"][0]["args"]
    assert workflow.sessions[str(project_id)]["setup_status"] == "failed"
    assert (
        workflow.sessions[str(project_id)]["setup_failure_artifact_id"]
        == "spec-authority-failure-1"
    )

    replay = runner.create_project(
        ProjectCreateRequest(
            name="Compiler Failure Project",
            spec_file=str(spec_file),
            idempotency_key="create-compile-fails-001",
            changed_by="agent",
        )
    )
    assert replay == failed

    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, data["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.VALIDATION_FAILED.value
        assert len(session.exec(select(Product)).all()) == 1
        assert session.exec(select(CompiledSpecAuthority)).all() == []

    listed = MutationLedgerRepository(engine=engine).list_events(status="recovery_required")
    assert listed["data"]["items"] == []


def test_project_create_compiler_timeout_is_failed_setup_not_recovery(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(
        monkeypatch,
        error_code="SPEC_COMPILE_FAILED",
        failure_artifact_id="spec-timeout-1",
        blocking_gaps=["Spec authority compiler exceeded 1800 seconds."],
    )
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    failed = runner.create_project(
        ProjectCreateRequest(
            name="Compiler Timeout Project",
            spec_file=str(spec_file),
            idempotency_key="create-timeout-001",
            changed_by="agent",
        )
    )

    assert failed["ok"] is False
    assert _error_code(failed) == "SPEC_COMPILE_FAILED"
    assert failed["data"]["setup_status"] == "failed"
    assert failed["data"]["next_actions"][0]["command"] == "agileforge project setup retry"
    assert "recovery_mutation_event_id" not in failed["data"]["next_actions"][0]["args"]
    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, failed["data"]["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.VALIDATION_FAILED.value
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_create_recovery_mark_failure_does_not_claim_recovery_required(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(monkeypatch, error_code="MUTATION_IN_PROGRESS")
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    monkeypatch.setattr(
        MutationLedgerRepository,
        "mark_recovery_required",
        lambda *args, **kwargs: False,
    )

    result = runner.create_project(
        ProjectCreateRequest(
            name="Recovery Mark Failure Project",
            spec_file=str(spec_file),
            idempotency_key="create-recovery-mark-fails-001",
            changed_by="agent",
        )
    )

    assert result["ok"] is False
    assert _error_code(result) == "MUTATION_IN_PROGRESS"
    assert result["data"]["status"] == MutationStatus.PENDING.value
    with Session(engine) as session:
        ledger = session.get(CliMutationLedger, result["data"]["mutation_event_id"])
        assert ledger is not None
        assert ledger.status == MutationStatus.PENDING.value


def test_project_setup_retry_without_recovery_link_recovers_failed_setup(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_failing_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    failed = runner.create_project(
        ProjectCreateRequest(
            name="Recover Failed Setup Project",
            spec_file=str(spec_file),
            idempotency_key="create-compile-fails-001",
            changed_by="agent",
        )
    )
    project_id = failed["data"]["project_id"]

    _install_fast_compiler(monkeypatch)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions[str(project_id)],
    )
    retried = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            idempotency_key="retry-failed-setup-001",
            changed_by="agent",
        )
    )

    assert retried["ok"] is True
    assert retried["data"]["setup_status"] == "authority_pending_review"
    assert retried["data"]["recovery_mutation_event_id"] is None
    assert workflow.sessions[str(project_id)]["setup_status"] == "authority_pending_review"
    with Session(engine) as session:
        create_row = session.get(CliMutationLedger, failed["data"]["mutation_event_id"])
        retry_row = session.get(CliMutationLedger, retried["data"]["mutation_event_id"])
        assert create_row is not None
        assert retry_row is not None
        assert create_row.status == MutationStatus.VALIDATION_FAILED.value
        assert retry_row.status == MutationStatus.SUCCEEDED.value
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(CompiledSpecAuthority)).all()) == 1


def test_project_create_duplicate_replay_key_reuse_and_recovery_required(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    first = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )
    assert first["ok"] is True
    duplicate = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-002",
        )
    )
    assert _error_code(duplicate) == "PROJECT_ALREADY_EXISTS"

    replay = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )
    assert replay == first

    reused_name = runner.create_project(
        ProjectCreateRequest(
            name="Changed Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )
    assert _error_code(reused_name) == "IDEMPOTENCY_KEY_REUSED"

    changed_spec = _write_spec(
        tmp_path,
        title="Changed App Spec",
        requirement_statement="The changed system MUST record updated audit evidence.",
    )
    reused_spec = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(changed_spec),
            idempotency_key="create-project-001",
        )
    )
    assert _error_code(reused_spec) == "IDEMPOTENCY_KEY_REUSED"

    with Session(engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(CliMutationLedger)).all()) == 1

    recovery, _, _ = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path / "recovery",
        monkeypatch=monkeypatch,
        idempotency_key="create-recovery-001",
        name="Recovery Project",
    )
    assert recovery["data"]["mutation_event_id"]
    assert recovery["data"]["project_id"]
    assert recovery["data"]["next_actions"][0]["command"] == (
        "agileforge project setup retry"
    )


def test_setup_retry_request_validation_and_guard_hash_inputs() -> None:
    missing_state: dict[str, object] = {
        "project_id": 1,
        "spec_file": "specs/app.md",
        "expected_context_fingerprint": "sha256:" + "a" * 64,
        "idempotency_key": "retry-project-001",
    }
    with pytest.raises(ValidationError, match="expected_state"):
        ProjectSetupRetryRequest.model_validate(missing_state)

    missing_fingerprint: dict[str, object] = {
        "project_id": 1,
        "spec_file": "specs/app.md",
        "expected_state": "SETUP_REQUIRED",
        "idempotency_key": "retry-project-001",
    }
    with pytest.raises(ValidationError, match="expected_context_fingerprint"):
        ProjectSetupRetryRequest.model_validate(missing_fingerprint)

    first = ProjectSetupRetryRequest(
        project_id=1,
        spec_file="specs/app.md",
        expected_state="SETUP_REQUIRED",
        expected_context_fingerprint="sha256:" + "a" * 64,
        idempotency_key="retry-project-001",
    )
    second = ProjectSetupRetryRequest(
        project_id=1,
        spec_file="specs/app.md",
        expected_state="SETUP_REQUIRED",
        expected_context_fingerprint="sha256:" + "b" * 64,
        idempotency_key="retry-project-001",
    )
    assert first.normalized_request_hash() != second.normalized_request_hash()


def test_setup_retry_rejects_stale_state_and_context(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SPRINT_PLANNING",
        "setup_status": "authority_pending_review",
        "setup_error": None,
        "setup_spec_file_path": str(spec_file.resolve()),
    }
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    stale_state = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint="sha256:" + "a" * 64,
            recovery_mutation_event_id=recovery["data"]["mutation_event_id"],
            idempotency_key="retry-project-001",
        )
    )
    assert _error_code(stale_state) == "STALE_STATE"

    stale_context = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SPRINT_PLANNING",
            expected_context_fingerprint="sha256:" + "b" * 64,
            recovery_mutation_event_id=recovery["data"]["mutation_event_id"],
            idempotency_key="retry-project-002",
        )
    )
    assert _error_code(stale_context) == "STALE_CONTEXT_FINGERPRINT"


def test_project_setup_retry_repairs_expired_pending_create_recovery_event(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    project_id, original_event_id = _seed_expired_pending_create_after_spec_approval(
        engine=engine,
        spec_file=spec_file,
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state={},
    )

    retried = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-interrupted-001",
            changed_by="agent",
        )
    )

    assert retried["ok"] is True
    assert retried["data"]["project_id"] == project_id
    assert retried["data"]["recovery_mutation_event_id"] == original_event_id
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retried["data"]["mutation_event_id"])
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.SUPERSEDED.value
        assert retry.status == MutationStatus.SUCCEEDED.value
        assert session.exec(select(CompiledSpecAuthority)).first() is not None


def test_project_setup_retry_dry_run_repairs_only_expired_pending_create(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    project_id, original_event_id = _seed_expired_pending_create_after_spec_approval(
        engine=engine,
        spec_file=spec_file,
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state={},
    )

    preview = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            dry_run=True,
            dry_run_id="retry-preview-interrupted-001",
            changed_by="agent",
        )
    )

    assert preview["ok"] is True
    assert preview["data"]["preview_available"] is True
    assert preview["data"]["project_id"] == project_id
    assert preview["data"]["recovery_mutation_event_id"] == original_event_id
    assert preview["data"]["recovery_status"] == MutationStatus.RECOVERY_REQUIRED.value
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        assert original is not None
        assert original.status == MutationStatus.RECOVERY_REQUIRED.value
        rows = session.exec(select(CliMutationLedger)).all()
        assert len(rows) == 1
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert workflow.sessions == {}


def test_project_setup_retry_active_pending_create_recovery_event_in_progress(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    workflow = FakeWorkflowPort()
    project_id, original_event_id = _seed_expired_pending_create_after_spec_approval(
        engine=engine,
        spec_file=spec_file,
    )
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        assert original is not None
        original.lease_expires_at = datetime(2099, 1, 1, tzinfo=UTC)
        session.add(original)
        session.commit()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state={},
    )

    result = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-active-pending-001",
            changed_by="agent",
        )
    )

    assert _error_code(result) == "MUTATION_IN_PROGRESS"
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        assert original is not None
        assert original.status == MutationStatus.PENDING.value
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_linked_setup_retry_success_supersedes_original_and_replays(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    monkeypatch.setattr(
        MutationLedgerRepository,
        "supersede_recovered_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy helper called")),
    )
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions.get(str(project_id), {}),
    )

    result = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )

    assert result["ok"] is True
    retry_event_id = result["data"]["mutation_event_id"]
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retry_event_id)
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.SUPERSEDED.value
        assert original.superseded_by_mutation_event_id == retry_event_id
        assert retry.status == MutationStatus.SUCCEEDED.value
        assert retry.recovers_mutation_event_id == original_event_id

    replay = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
            changed_by="agent",
        )
    )
    assert replay["ok"] is True
    assert replay["data"]["mutation_event_id"] == retry_event_id


def test_linked_setup_retry_dry_run_leaves_original_recovery_row_unchanged(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    original_event_id = recovery["data"]["mutation_event_id"]
    project_id = recovery["data"]["project_id"]
    with Session(engine) as session:
        original_row = session.get(CliMutationLedger, original_event_id)
        assert original_row is not None
        original_before = _row_payload(original_row)
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions.get(str(project_id), {}),
    )

    result = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            dry_run=True,
            dry_run_id="retry-preview-001",
        )
    )

    assert result["ok"] is True
    with Session(engine) as session:
        original_row = session.get(CliMutationLedger, original_event_id)
        assert original_row is not None
        original_after = _row_payload(original_row)
        rows = session.exec(select(CliMutationLedger)).all()
    assert original_after == original_before
    assert len(rows) == 1


def test_linked_retry_pre_side_effect_failure_preserves_original_and_replays_retry(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    runner.fail_retry_before_side_effects_for_test = True
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions.get(str(project_id), {}),
    )

    failed = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )

    assert failed["ok"] is False
    assert failed["data"]["side_effects_started"] is False
    retry_event_id = failed["data"]["mutation_event_id"]
    replay = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )
    assert replay["data"] == failed["data"]
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retry_event_id)
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.RECOVERY_REQUIRED.value
        assert retry.status == MutationStatus.DOMAIN_FAILED_NO_SIDE_EFFECTS.value


def test_linked_retry_post_side_effect_failure_transfers_recovery(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    monkeypatch.setattr(
        MutationLedgerRepository,
        "supersede_recovered_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy helper called")),
    )
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    runner.fail_retry_after_side_effects_for_test = True
    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=workflow.sessions.get(str(project_id), {}),
    )

    failed = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )

    assert _error_code(failed) == "MUTATION_RECOVERY_REQUIRED"
    retry_event_id = failed["data"]["mutation_event_id"]
    assert failed["data"]["recovery_mutation_event_id"] == original_event_id
    with Session(engine) as session:
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retry_event_id)
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.SUPERSEDED.value
        assert original.superseded_by_mutation_event_id == retry_event_id
        assert retry.status == MutationStatus.RECOVERY_REQUIRED.value
    listed = MutationLedgerRepository(engine=engine).list_events(status="recovery_required")
    assert [item["mutation_event_id"] for item in listed["data"]["items"]] == [retry_event_id]

    original_replay = ProjectSetupMutationRunner(engine=engine, workflow=workflow).create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
            changed_by="agent",
        )
    )
    assert original_replay["data"]["recovered_by_mutation_event_id"] == retry_event_id
    assert original_replay["data"]["retry_status"] == "recovery_required"


def test_retry_rejects_invalid_original_recovery_link(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)

    wrong_project = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id + 1,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint="sha256:" + "0" * 64,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )
    assert _error_code(wrong_project) == "MUTATION_RECOVERY_INVALID"

    with Session(engine) as session:
        row = session.get(CliMutationLedger, original_event_id)
        assert row is not None
        row.status = MutationStatus.SUCCEEDED.value
        session.add(row)
        session.commit()
    not_recovery = runner.retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint="sha256:" + "0" * 64,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-002",
        )
    )
    assert _error_code(not_recovery) == "MUTATION_RECOVERY_INVALID"


def test_retry_without_recovery_link_rejects_when_unresolved_create_exists(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery, spec_file, workflow = _create_recovery_row(
        engine=engine,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project_id = recovery["data"]["project_id"]
    result = ProjectSetupMutationRunner(engine=engine, workflow=workflow).retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint="sha256:" + "0" * 64,
            idempotency_key="retry-project-001",
        )
    )
    assert _error_code(result) == "MUTATION_RECOVERY_INVALID"
    assert "--recovery-mutation-event-id" in result["errors"][0]["remediation"][0]


def test_workflow_setup_reconciles_partial_or_existing_session_state(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.sessions["1"] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_error": "stale",
        "setup_spec_file_path": "/old/spec.md",
        "unrelated_state": "preserved",
    }
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)

    result = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )

    assert result["ok"] is True
    assert fake_workflow.created_sessions == []
    assert fake_workflow.sessions["1"]["setup_status"] == "authority_pending_review"
    assert fake_workflow.sessions["1"]["setup_error"] is None
    assert fake_workflow.sessions["1"]["setup_spec_file_path"] == str(spec_file.resolve())
    assert fake_workflow.sessions["1"]["unrelated_state"] == "preserved"


def test_workflow_setup_retry_recovers_session_created_without_status(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.fail_after_session_create = True
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)

    recovery = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )

    assert _error_code(recovery) == "MUTATION_RECOVERY_REQUIRED"
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    assert fake_workflow.created_sessions == [str(project_id)]
    assert "setup_status" not in fake_workflow.sessions[str(project_id)]

    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=fake_workflow.sessions[str(project_id)],
    )
    retry = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow).retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )

    assert retry["ok"] is True
    assert fake_workflow.created_sessions == [str(project_id)]
    assert fake_workflow.sessions[str(project_id)]["setup_status"] == (
        "authority_pending_review"
    )
    assert fake_workflow.sessions[str(project_id)]["setup_error"] is None


def test_workflow_setup_retry_recovers_status_written_without_ledger_progress(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_schema_current(engine)
    spec_file = _write_spec(tmp_path)
    _install_fast_compiler(monkeypatch)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.fail_after_status_write = True
    runner = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow)

    recovery = runner.create_project(
        ProjectCreateRequest(
            name="CLI Project",
            spec_file=str(spec_file),
            idempotency_key="create-project-001",
        )
    )

    assert _error_code(recovery) == "MUTATION_RECOVERY_REQUIRED"
    project_id = recovery["data"]["project_id"]
    original_event_id = recovery["data"]["mutation_event_id"]
    assert fake_workflow.created_sessions == [str(project_id)]
    assert len(fake_workflow.status_writes) == 1
    assert fake_workflow.sessions[str(project_id)]["setup_status"] == (
        "authority_pending_review"
    )

    expected_fingerprint = _retry_fingerprint(
        project_id=project_id,
        spec_file=spec_file,
        workflow_state=fake_workflow.sessions[str(project_id)],
    )
    retry = ProjectSetupMutationRunner(engine=engine, workflow=fake_workflow).retry_setup(
        ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=str(spec_file),
            expected_state="SETUP_REQUIRED",
            expected_context_fingerprint=expected_fingerprint,
            recovery_mutation_event_id=original_event_id,
            idempotency_key="retry-project-001",
        )
    )

    assert retry["ok"] is True
    assert fake_workflow.created_sessions == [str(project_id)]
    assert len(fake_workflow.status_writes) == 1
    with Session(engine) as session:
        retry_row = session.get(CliMutationLedger, retry["data"]["mutation_event_id"])
        assert retry_row is not None
        assert "workflow_session_created" in _row_payload(retry_row)["completed_steps"]
        assert "workflow_session_status_written" in _row_payload(retry_row)["completed_steps"]


def test_linked_retry_repository_helpers_roll_back_and_report_conflict(
    engine: Engine,
) -> None:
    ensure_schema_current(engine)
    repo = MutationLedgerRepository(engine=engine)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    original = repo.create_or_load(
        command="agileforge project create",
        idempotency_key="create-project-001",
        request_hash="sha256:original",
        project_id=7,
        correlation_id="corr",
        changed_by="agent",
        lease_owner="owner-original",
        now=now,
    ).ledger
    retry = repo.create_or_load(
        command="agileforge project setup retry",
        idempotency_key="retry-project-001",
        request_hash="sha256:retry",
        project_id=7,
        correlation_id="corr",
        changed_by="agent",
        lease_owner="owner-retry",
        now=now,
    ).ledger
    assert original.mutation_event_id is not None
    assert retry.mutation_event_id is not None
    assert repo.mark_recovery_required(
        mutation_event_id=original.mutation_event_id,
        lease_owner="owner-original",
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=False,
        last_error={"code": "WORKFLOW_SESSION_FAILED"},
        now=now,
    )
    assert repo.acquire_recovery_lease(
        mutation_event_id=original.mutation_event_id,
        expected_project_id=7,
        recovery_lease_owner="owner-original-recovery",
        now=now,
    )
    before = _ledger_rows(engine)

    repo.fail_after_retry_update_for_test = True
    success_conflict = repo.finalize_linked_retry_success(
        retry_mutation_event_id=retry.mutation_event_id,
        retry_lease_owner="owner-retry",
        original_mutation_event_id=original.mutation_event_id,
        original_recovery_lease_owner="owner-original-recovery",
        after={"done": True},
        retry_response={"ok": True, "data": {"done": True}},
        original_replay_response={"ok": True, "data": {"done": True}},
        now=now,
    )
    assert success_conflict.error_code == MUTATION_RESUME_CONFLICT
    assert _ledger_rows(engine) == before

    recovery_conflict = repo.transfer_linked_retry_recovery(
        retry_mutation_event_id=retry.mutation_event_id,
        retry_lease_owner="owner-retry",
        original_mutation_event_id=original.mutation_event_id,
        original_recovery_lease_owner="wrong-owner",
        recovery_action=RecoveryAction.RESUME_FROM_STEP,
        safe_to_auto_resume=True,
        last_error={"code": "WORKFLOW_SESSION_FAILED"},
        retry_response={"ok": False, "data": {"retry": "recovery"}},
        original_replay_response={"ok": False, "data": {"retry": "recovery"}},
        now=now,
    )
    assert recovery_conflict.error_code == MUTATION_RESUME_CONFLICT
    assert _ledger_rows(engine) == before


def _ledger_rows(engine: Engine) -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.exec(
            select(CliMutationLedger).order_by(
                cast("Any", CliMutationLedger.mutation_event_id)
            )
        ).all()
        return [copy.deepcopy(_row_payload(row)) for row in rows]
